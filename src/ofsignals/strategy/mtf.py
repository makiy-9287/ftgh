"""The cascade: L1 bias -> L2 liquidity -> L3 confirmation -> L4 entry.

A layer only evaluates if the layer above it PASSED. Every rejection carries the
stage and a human-readable reason, which is what makes the engine debuggable
against live markets instead of a black box that silently emits nothing.

This module is PURE EVALUATION. Cooldowns, concurrency caps, correlation guards
and de-duplication live in the scanner, because they are properties of the
portfolio rather than of the setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ofsignals.analytics import liquidity as liq
from ofsignals.analytics import volume_profile as vp
from ofsignals.analytics import zones as zn
from ofsignals.analytics.footprint import analyse_flow
from ofsignals.analytics.orderbook import dom_confirms
from ofsignals.analytics.structure import (
    atr_at,
    compute_bias,
    detect_displacements,
    detect_structure_events,
    detect_swings,
    displacement_after,
    volatility_ok,
)
from ofsignals.exchange.rest_client import CandleStore, timeframe_ms
from ofsignals.logging_setup import get_logger
from ofsignals.risk.sizing import MAX_SL_PCT, TradePlan, build_plan
from ofsignals.strategy.base import ScoreCard, Signal, build_signal, effective_gate, score_setup
from ofsignals.types import (
    BookSnapshot,
    Candles,
    Direction,
    FlowSnapshot,
    PoolSide,
    Zone,
    utcnow,
)

log = get_logger(__name__)


@dataclass(slots=True)
class Evaluation:
    symbol: str
    mode: str
    signal: Signal | None
    stage: str
    reason: str

    @property
    def passed(self) -> bool:
        return self.signal is not None


def _reject(symbol: str, mode: str, stage: str, reason: str) -> Evaluation:
    return Evaluation(symbol, mode, None, stage, reason)


class CascadeEngine:
    """One instance per mode. Stateless across symbols; safe to reuse."""

    mode_name: str = "base"

    def __init__(self, mode: str, settings: Any, candles: CandleStore, hub: Any) -> None:
        self.mode = mode
        self.mode_name = mode
        self.settings = settings
        self.candles = candles
        self.hub = hub

        self.mode_cfg = settings.mode(mode)
        self.tf: dict[str, str] = self.mode_cfg["timeframes"]
        self.structure_cfg = settings.section("structure")
        self.liquidity_cfg = settings.section("liquidity")
        self.zones_cfg = settings.section("zones")
        self.vp_cfg = settings.section("volume_profile")
        self.orderflow_cfg = settings.section("orderflow")
        self.risk_cfg = settings.section("risk")
        self.atr_period = int(self.structure_cfg.get("atr_period", 14))
        self.max_sl_pct = MAX_SL_PCT.get(mode, 2.5)

        # Footprint runs on its own bucket, NOT the confirm timeframe. Twelve
        # 1h buckets would demand twelve hours of tape before a swing setup
        # could ever score; 5-minute buckets need an hour.
        self.footprint_bucket_ms = int(
            self.mode_cfg.get("footprint_bucket_seconds", 60)) * 1000
        self.min_ready_bars = int(self.orderflow_cfg.get("min_ready_bars", 12))

    # ------------------------------------------------------------- entrypoint
    async def evaluate(self, symbol: str) -> Evaluation:
        series = await self.candles.get_many(symbol, self.tf, limit=300)
        for role, candles in series.items():
            if len(candles) < 60:
                return _reject(symbol, self.mode, "data",
                               f"{role} ({self.tf[role]}) has only {len(candles)} closed bars")

        bias_c, liq_c = series["bias"], series["liquidity"]
        confirm_c, entry_c = series["confirm"], series["entry"]
        price = float(entry_c.last_price)

        # ---------------------------------------------------------- L1 BIAS
        bias = compute_bias(bias_c, self.structure_cfg)
        if not bias.tradeable:
            return _reject(symbol, self.mode, "L1", bias.reason)
        if not volatility_ok(bias_c, self.atr_period):
            return _reject(symbol, self.mode, "L1", "bias-TF volatility in bottom quintile")

        direction = bias.direction
        dealing_range = bias.dealing_range
        eq = float(self.structure_cfg.get("equilibrium_pct", 0.50))
        price_in_half = (dealing_range.is_discount(price, eq) if direction is Direction.LONG
                         else dealing_range.is_premium(price, eq))

        # ----------------------------------------------------- L2 LIQUIDITY
        pools = liq.build_pools(liq_c, self.liquidity_cfg, self.structure_cfg)
        if not pools:
            return _reject(symbol, self.mode, "L2", "no liquidity pools mapped")

        # Detect FIRST, flag afterwards. Flagging first marks the pool that was
        # just raided as "already taken", and detection skips flagged pools.
        sweep = liq.detect_sweep(liq_c, pools, self.liquidity_cfg, self.structure_cfg)
        liq.mark_swept(pools, liq_c, lookback=200,
                       skip_recent=int(self.liquidity_cfg.get("sweep_scan_bars", 15)))
        if sweep is None:
            return _reject(symbol, self.mode, "L2", "no valid sweep with reclaim")
        if sweep.direction is not direction:
            return _reject(symbol, self.mode, "L2",
                           f"sweep implies {sweep.direction.value}, bias is {direction.value}")

        impulse = displacement_after(
            liq_c, sweep.reclaim_index, direction,
            float(self.structure_cfg.get("displacement_atr_mult", 1.5)),
            float(self.structure_cfg.get("displacement_body_ratio", 0.55)),
            window=6, period=self.atr_period,
        )
        if impulse is None:
            return _reject(symbol, self.mode, "L2",
                           "sweep reclaimed but no displacement followed (continuation risk)")

        mid_poi = self._zone_for_displacement(liq_c, impulse.origin_index, direction)
        if mid_poi is None:
            return _reject(symbol, self.mode, "L2", "no order block behind the impulse leg")

        # -------------------------------------------------- L3 CONFIRMATION
        book = self._book_snapshot(symbol, direction)
        if book.stale:
            return _reject(symbol, self.mode, "L3",
                           "depth stream stale or absent — symbol ineligible")

        tape = self.hub.tape(symbol) if self.hub else None
        if tape is None:
            return _reject(symbol, self.mode, "L3", "no trade tape for symbol")

        confirm_atr = atr_at(confirm_c, -1, self.atr_period)
        flow: FlowSnapshot = analyse_flow(
            tape, self.footprint_bucket_ms, direction, self.orderflow_cfg,
            poi_low=mid_poi.bottom, poi_high=mid_poi.top, atr_value=confirm_atr,
            min_ready_bars=self.min_ready_bars,
        )
        dom_pass, _ = dom_confirms(book, direction, self.orderflow_cfg)
        confirmations = flow.confirmations + (1 if dom_pass else 0)

        if not flow.ready and confirmations < 2:
            return _reject(symbol, self.mode, "L3", flow.note)
        if confirmations < 2:
            return _reject(symbol, self.mode, "L3",
                           f"only {confirmations}/2 order-flow confirmations")

        veto = self._opposing_displacement_veto(confirm_c, direction, flow)
        if veto:
            return _reject(symbol, self.mode, "L3", veto)

        # ---------------------------------------------------------- L4 ENTRY
        ltf_ok, ltf_note = self._ltf_choch(entry_c, direction)
        if not ltf_ok:
            return _reject(symbol, self.mode, "L4", ltf_note)

        entry_zone, fvg_overlap = self._entry_zone(entry_c, direction, price, mid_poi)
        if entry_zone is None:
            return _reject(symbol, self.mode, "L4",
                           "no fresh entry zone inside the mid-TF POI")

        zone_in_half = (dealing_range.is_discount(entry_zone.mid, eq)
                        if direction is Direction.LONG
                        else dealing_range.is_premium(entry_zone.mid, eq))
        if not zone_in_half:
            return _reject(symbol, self.mode, "L4",
                           f"entry zone sits in the wrong half of the dealing range "
                           f"({dealing_range.position_of(entry_zone.mid) * 100:.0f}%)")

        # ------------------------------------------------------------ PLAN
        entry_atr = atr_at(entry_c, -1, self.atr_period)
        htf_pools = liq.build_pools(bias_c, self.liquidity_cfg, self.structure_cfg)
        liq.mark_swept(htf_pools, bias_c, lookback=200, skip_recent=3)

        profile = vp.build_profile(
            bias_c.tail(int(self.vp_cfg.get("lookback_bars", {}).get(self.tf["bias"], 168))),
            bins=int(self.vp_cfg.get("bins", 120)),
            value_area_pct=float(self.vp_cfg.get("value_area_pct", 0.70)),
            lvn_percentile=float(self.vp_cfg.get("lvn_percentile", 0.20)),
        )
        naked = vp.naked_pocs(bias_c, period_bars=24) if self.vp_cfg.get("track_naked_poc") else []

        plan: TradePlan = build_plan(
            direction=direction, zone=entry_zone, sweep=sweep, atr_value=entry_atr,
            spread_bps=book.spread_bps, pools=pools, htf_pools=htf_pools,
            profile=profile, naked_poc_levels=naked, mid_poi=mid_poi,
            risk_cfg=self.risk_cfg, zone_cfg=self.zones_cfg.get("order_block", {}),
            mode_cfg=self.mode_cfg, max_sl_pct=self.max_sl_pct,
        )
        if not plan.ok:
            return _reject(symbol, self.mode, "plan", plan.rejected_reason)

        # ------------------------------------------------------------ SCORE
        entry_at_lvn = self._near_lvn(profile, plan.entry_mid, entry_atr)
        naked_target = any(abs(plan.tp3 - level) <= entry_atr for level in naked)
        weekend = utcnow().weekday() >= 5

        card: ScoreCard = score_setup(
            bias=bias, in_correct_half=price_in_half, sweep=sweep, zone=entry_zone,
            fvg_overlap=fvg_overlap, flow=flow, book=book, profile=profile,
            entry_at_lvn=entry_at_lvn, naked_poc_target=naked_target, plan=plan,
            min_rr=float(self.mode_cfg.get("min_rr_to_tp2", 2.0)),
            orderflow_cfg=self.orderflow_cfg,
            sweep_volume_floor=float(self.liquidity_cfg.get("sweep_volume_mult", 1.8)),
            weekend=weekend,
        )
        gate = effective_gate(int(self.mode_cfg.get("min_confluence_score", 75)), weekend)
        if card.total < gate:
            return _reject(symbol, self.mode, "score",
                           f"confluence {card.total} below gate {gate}")

        signal = build_signal(
            symbol=symbol, mode=self.mode, direction=direction, timeframes=self.tf,
            plan=plan, score=card,
            rationale=self._rationale(bias, sweep, impulse, flow, book, entry_zone,
                                      profile, plan, dealing_range, price),
            price=price,
            ttl_minutes=int(self.mode_cfg.get("signal_ttl_minutes", 60)),
            allocation=self.risk_cfg.get("tp_allocation", {"tp1": 0.4, "tp2": 0.35, "tp3": 0.25}),
            tags=self._tags(entry_at_lvn, naked_target, weekend, sweep.pool.source),
        )
        return Evaluation(symbol, self.mode, signal, "published", "ok")

    # ---------------------------------------------------------------- pieces
    def _book_snapshot(self, symbol: str, direction: Direction) -> BookSnapshot:
        state = self.hub.book_state(symbol) if self.hub else None
        if state is None:
            return BookSnapshot(False, 0.0, float("inf"), 0.0, 0.0, 1.0,
                                Direction.NONE, None, 0.0, 1e9)
        return state.snapshot(self.orderflow_cfg, direction)

    def _zone_for_displacement(self, candles: Candles, origin_index: int,
                               direction: Direction) -> Zone | None:
        blocks = zn.find_order_blocks(candles, self.zones_cfg.get("order_block", {}),
                                      self.structure_cfg, max_zones=20)
        candidates = [z for z in blocks
                      if z.direction is direction and z.index <= origin_index + 1]
        if not candidates:
            return None
        return max(candidates, key=lambda z: z.index)

    def _opposing_displacement_veto(self, candles: Candles, direction: Direction,
                                    flow: FlowSnapshot) -> str:
        """Hard veto: fresh impulse against us with delta agreeing against us."""
        displacements = detect_displacements(
            candles,
            float(self.structure_cfg.get("displacement_atr_mult", 1.5)),
            float(self.structure_cfg.get("displacement_body_ratio", 0.55)),
            self.atr_period,
        )
        if not displacements:
            return ""
        last = displacements[-1]
        if last.direction is direction:
            return ""
        if (len(candles) - 1 - last.index) > 3:
            return ""
        if flow.ready and np.sign(flow.bar_delta) == last.direction.sign:
            return (f"veto: opposing {last.direction.value} displacement "
                    f"{len(candles) - 1 - last.index} bars ago with agreeing delta")
        return ""

    def _ltf_choch(self, candles: Candles, direction: Direction,
                   window: int = 12) -> tuple[bool, str]:
        swings = detect_swings(candles, int(self.structure_cfg.get("swing_fractal_lookback", 2)))
        events = detect_structure_events(candles, swings)
        if not events:
            return False, "no entry-TF structural break"
        last = events[-1]
        if last.direction is not direction:
            return False, f"entry-TF structure last broke {last.direction.value}"
        age = len(candles) - 1 - last.index
        if age > window:
            return False, f"entry-TF break is stale ({age} bars old)"
        return True, ""

    def _entry_zone(self, candles: Candles, direction: Direction, price: float,
                    mid_poi: Zone) -> tuple[Zone | None, bool]:
        ob_cfg = self.zones_cfg.get("order_block", {})
        fvg_cfg = self.zones_cfg.get("fvg", {})
        blocks = zn.find_order_blocks(candles, ob_cfg, self.structure_cfg)
        fvgs = zn.find_fvgs(candles, fvg_cfg, self.structure_cfg)
        if self.zones_cfg.get("breaker_blocks", True):
            blocks = blocks + zn.find_breakers(candles, blocks)

        tolerance = atr_at(candles, -1, self.atr_period) * 0.5
        expanded = Zone(mid_poi.kind, mid_poi.direction, mid_poi.top + tolerance,
                        mid_poi.bottom - tolerance, mid_poi.index)

        inside = [z for z in blocks if zn.overlapping(z, expanded)]
        zone = zn.select_poi(inside or blocks, direction, price)
        if zone is None:
            return None, False

        overlapping_fvg = next((f for f in fvgs
                                if f.direction is direction and zn.overlapping(zone, f)), None)
        return zn.refine_zone(zone, overlapping_fvg), overlapping_fvg is not None

    def _near_lvn(self, profile, price: float, atr_value: float) -> bool:
        if profile is None or not np.isfinite(atr_value) or atr_value <= 0:
            return False
        return any(abs(price - level) <= atr_value * 0.35 for level in profile.lvn_prices)

    def _tags(self, entry_at_lvn: bool, naked_target: bool, weekend: bool,
              pool_source: str) -> list[str]:
        tags = [f"pool:{pool_source}"]
        if entry_at_lvn:
            tags.append("lvn-entry")
        if naked_target:
            tags.append("naked-poc-target")
        if weekend:
            tags.append("weekend-thin")
        return tags

    def _rationale(self, bias, sweep, impulse, flow, book, zone, profile, plan,
                   dealing_range, price) -> dict[str, str]:
        pool = sweep.pool
        vp_note = "no profile"
        if profile is not None:
            vp_note = (f"POC {profile.poc:.6g}, value area "
                       f"{profile.val:.6g}-{profile.vah:.6g}; entry "
                       f"{'inside' if profile.in_value_area(plan.entry_mid) else 'outside'} VA")
        return {
            "bias": f"{self.tf['bias']} {bias.reason}",
            "liquidity": (f"{self.tf['liquidity']} swept {pool.source} "
                          f"{pool.price:.6g} ({pool.touches} touches) by "
                          f"{sweep.penetration_atr:.2f} ATR on {sweep.volume_multiple:.1f}x "
                          f"volume, reclaimed in {sweep.reclaim_index - sweep.index + 1} bar(s), "
                          f"then {impulse.atr_multiple:.1f} ATR displacement"),
            "flow": (f"{self.tf['confirm']} CVD {flow.cvd:,.0f}; "
                     f"divergence={flow.cvd_divergence}, absorption={flow.absorption}, "
                     f"delta_flip={flow.delta_flip}, delta_extreme={flow.delta_extreme}"),
            "dom": (f"{book.imbalance_ratio:.1f}:1 toward {book.dominant.value.lower()} "
                    f"within {self.orderflow_cfg.get('book', {}).get('band_pct', 0.3)}% of mid; "
                    f"dominant wall resting {book.wall_resting_s:.0f}s"),
            "profile": vp_note,
            "entry": (f"{self.tf['entry']} {zone.kind.value} "
                      f"{zone.bottom:.6g}-{zone.top:.6g}, "
                      f"{dealing_range.position_of(zone.mid) * 100:.0f}% of dealing range"),
            "invalidation": (f"{self.tf['entry']} close beyond {plan.stop_loss:.6g} "
                             f"({plan.sl_distance_pct:.2f}%) voids the reclaim"),
        }
