"""Risk: structural stops, liquidity-anchored targets, derived leverage.

THREE RULES, ENFORCED HERE AND NOWHERE ELSE

1. The stop is structural. It sits beyond the sweep wick that created the setup,
   plus a buffer. It is never a round percentage chosen to make the maths work.
2. Targets are liquidity-anchored. TP1/2/3 are pools, POIs, value-area edges and
   naked POCs. The R multiples are an OUTPUT of where liquidity sits, not an input.
3. Leverage is derived from stop distance. `leverage_max = safety / sl_pct` keeps
   the stop well inside the liquidation boundary. A wide stop mechanically
   produces low leverage; there is no path around it.

If the resulting R:R misses the mode's floor the plan is REJECTED. It is never
resized, and the targets are never pulled closer to manufacture a passing ratio.
"""

from __future__ import annotations

from dataclasses import dataclass

from ofsignals.types import (
    Direction,
    Pool,
    PoolSide,
    Sweep,
    VolumeProfile,
    Zone,
    clamp,
)


@dataclass(slots=True, frozen=True)
class TradePlan:
    direction: Direction
    entry_high: float
    entry_low: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr1: float
    rr2: float
    rr3: float
    sl_distance_pct: float
    leverage: int
    leverage_max_safe: int
    risk_unit: float
    rejected_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.rejected_reason

    @property
    def entry_mid(self) -> float:
        return (self.entry_high + self.entry_low) / 2.0


def _rejection(reason: str, direction: Direction) -> TradePlan:
    return TradePlan(direction, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, reason)


# ------------------------------------------------------------------- stop loss
def structural_stop(direction: Direction, sweep: Sweep, zone: Zone, atr_value: float,
                    spread_bps: float, cfg: dict) -> float:
    """Beyond the sweep extreme (or the zone edge, whichever is further out)."""
    buffer_atr = float(cfg.get("sl_buffer_atr", 0.15)) * atr_value
    reference = sweep.extreme

    if direction is Direction.LONG:
        anchor = min(reference, zone.bottom)
        min_buffer = anchor * float(cfg.get("sl_min_buffer_bps", 8)) / 10_000.0
        spread_buffer = anchor * (spread_bps * 3) / 10_000.0
        return anchor - max(buffer_atr, min_buffer, spread_buffer)

    anchor = max(reference, zone.top)
    min_buffer = anchor * float(cfg.get("sl_min_buffer_bps", 8)) / 10_000.0
    spread_buffer = anchor * (spread_bps * 3) / 10_000.0
    return anchor + max(buffer_atr, min_buffer, spread_buffer)


# --------------------------------------------------------------------- targets
def build_targets(direction: Direction, entry: float, stop: float,
                  pools: list[Pool], htf_pools: list[Pool],
                  profile: VolumeProfile | None, naked_poc_levels: list[float],
                  mid_poi: Zone | None) -> tuple[float, float, float]:
    """Walk outward through real liquidity; fall back to R multiples only if bare."""
    risk = abs(entry - stop)
    if risk <= 0:
        return entry, entry, entry

    target_side = PoolSide.BUYSIDE if direction is Direction.LONG else PoolSide.SELLSIDE
    sign = direction.sign

    candidates: list[float] = []

    for pool in pools + htf_pools:
        if pool.side is not target_side or pool.swept:
            continue
        if (pool.price - entry) * sign > 0:
            candidates.append(pool.price)

    if profile is not None:
        for level in (profile.vah, profile.val, profile.poc):
            if (level - entry) * sign > 0:
                candidates.append(level)

    for level in naked_poc_levels:
        if (level - entry) * sign > 0:
            candidates.append(level)

    if mid_poi is not None:
        edge = mid_poi.top if direction is Direction.LONG else mid_poi.bottom
        if (edge - entry) * sign > 0:
            candidates.append(edge)

    # Order by distance, drop anything closer than 0.6R (noise, not a target).
    candidates = sorted({round(c, 10) for c in candidates},
                        key=lambda level: abs(level - entry))
    candidates = [c for c in candidates if abs(c - entry) >= risk * 0.6]

    picked: list[float] = []
    for level in candidates:
        if len(picked) == 3:
            break
        # Enforce meaningful separation between rungs of the ladder.
        if picked and abs(level - picked[-1]) < risk * 0.5:
            continue
        picked.append(level)

    # Fallback rungs always extend BEYOND the last real target. Anchoring them
    # to fixed R multiples off entry can place TP2 behind TP1 whenever the only
    # nearby liquidity sits further out than 2R.
    while len(picked) < 3:
        anchor = picked[-1] if picked else entry
        picked.append(anchor + sign * risk)

    return picked[0], picked[1], picked[2]


# -------------------------------------------------------------------- leverage
def derive_leverage(sl_distance_pct: float, mode_cap: int, cfg: dict) -> tuple[int, int]:
    """(recommended, max_safe).

    Liquidation sits roughly 1/leverage away from entry (before maintenance
    margin). Requiring the stop to fall within `safety` of that distance gives
    leverage_max = safety / sl_pct. Isolated margin is assumed throughout.
    """
    safety = float(cfg.get("liquidation_safety_factor", 0.40))
    ceiling = int(cfg.get("absolute_leverage_ceiling", 25))
    if sl_distance_pct <= 0:
        return 1, 1

    # The formula is unbounded as the stop tightens: a 0.1% stop implies 400x.
    # Printing that would be reckless — exchange tier limits, funding and
    # slippage all bite long before the liquidation maths does — so the
    # advertised figure is clamped to a configured ceiling.
    max_safe = int(max(1.0, min((safety * 100.0) / sl_distance_pct, ceiling)))
    recommended = int(clamp(min(max_safe, mode_cap), 1, mode_cap))
    return recommended, max_safe


# ------------------------------------------------------------------ full plan
def build_plan(direction: Direction, zone: Zone, sweep: Sweep, atr_value: float,
               spread_bps: float, pools: list[Pool], htf_pools: list[Pool],
               profile: VolumeProfile | None, naked_poc_levels: list[float],
               mid_poi: Zone | None, risk_cfg: dict, zone_cfg: dict,
               mode_cfg: dict, max_sl_pct: float) -> TradePlan:
    mitigation_depth = float(zone_cfg.get("mitigation_depth", 0.50))

    if direction is Direction.LONG:
        entry_high = zone.top
        entry_low = zone.bottom + zone.height * (1.0 - mitigation_depth)
        entry_low, entry_high = min(entry_low, entry_high), max(entry_low, entry_high)
        reference_entry = entry_low + (entry_high - entry_low) * 0.5
    else:
        entry_low = zone.bottom
        entry_high = zone.top - zone.height * (1.0 - mitigation_depth)
        entry_low, entry_high = min(entry_low, entry_high), max(entry_low, entry_high)
        reference_entry = entry_low + (entry_high - entry_low) * 0.5

    stop = structural_stop(direction, sweep, zone, atr_value, spread_bps, risk_cfg)
    risk = abs(reference_entry - stop)
    if risk <= 0 or reference_entry <= 0:
        return _rejection("degenerate entry/stop geometry", direction)

    sl_distance_pct = risk / reference_entry * 100.0
    if sl_distance_pct > max_sl_pct:
        return _rejection(
            f"stop too wide: {sl_distance_pct:.2f}% > {max_sl_pct:.2f}% mode limit", direction)
    if sl_distance_pct < 0.05:
        return _rejection("stop implausibly tight (< 5 bps) — likely bad tick data", direction)

    tp1, tp2, tp3 = build_targets(direction, reference_entry, stop, pools, htf_pools,
                                  profile, naked_poc_levels, mid_poi)

    rr1 = abs(tp1 - reference_entry) / risk
    rr2 = abs(tp2 - reference_entry) / risk
    rr3 = abs(tp3 - reference_entry) / risk

    min_rr = float(mode_cfg.get("min_rr_to_tp2", 2.0))
    if rr2 < min_rr:
        return _rejection(
            f"R:R to TP2 {rr2:.2f} below {min_rr:.2f} floor — discarded, not resized",
            direction)

    recommended, max_safe = derive_leverage(sl_distance_pct,
                                            int(mode_cfg.get("max_leverage", 5)), risk_cfg)

    return TradePlan(
        direction=direction,
        entry_high=round(entry_high, 10),
        entry_low=round(entry_low, 10),
        stop_loss=round(stop, 10),
        tp1=round(tp1, 10), tp2=round(tp2, 10), tp3=round(tp3, 10),
        rr1=round(rr1, 2), rr2=round(rr2, 2), rr3=round(rr3, 2),
        sl_distance_pct=round(sl_distance_pct, 3),
        leverage=recommended,
        leverage_max_safe=max_safe,
        risk_unit=round(risk, 10),
    )


MAX_SL_PCT: dict[str, float] = {"scalp": 1.2, "intraday": 2.5, "swing": 5.0}
