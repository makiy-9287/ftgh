"""Zones: order blocks, fair value gaps, breakers.

An order block is only valid if a displacement leg follows it — an "OB" without
displacement is just a candle. That requirement is enforced here, not optional.
"""

from __future__ import annotations

import numpy as np

from ofsignals.analytics.structure import atr, detect_displacements
from ofsignals.types import Candles, Direction, Zone, ZoneKind


# ------------------------------------------------------------------ order blocks
def find_order_blocks(candles: Candles, cfg: dict, structure_cfg: dict,
                      max_zones: int = 12) -> list[Zone]:
    """Last opposing-colour candle immediately preceding a displacement leg."""
    n = len(candles)
    if n < 10:
        return []

    atr_mult = float(structure_cfg.get("displacement_atr_mult", 1.5))
    body_ratio = float(structure_cfg.get("displacement_body_ratio", 0.55))
    period = int(structure_cfg.get("atr_period", 14))
    max_age = int(cfg.get("max_age_bars", 60))

    displacements = detect_displacements(candles, atr_mult, body_ratio, period)
    zones: list[Zone] = []

    for disp in displacements:
        if n - 1 - disp.index > max_age:
            continue
        origin = disp.origin_index
        ob_index: int | None = None
        for j in range(origin, max(-1, origin - 8), -1):
            is_bearish_candle = candles.close[j] < candles.open[j]
            if disp.direction is Direction.LONG and is_bearish_candle:
                ob_index = j
                break
            if disp.direction is Direction.SHORT and not is_bearish_candle:
                ob_index = j
                break
        if ob_index is None:
            continue

        zones.append(Zone(
            kind=ZoneKind.ORDER_BLOCK,
            direction=disp.direction,
            top=float(candles.high[ob_index]),
            bottom=float(candles.low[ob_index]),
            index=ob_index,
        ))

    _apply_mitigation(candles, zones, bool(cfg.get("invalidate_on_body_close", True)))
    zones = [z for z in zones if not z.invalidated]
    zones.sort(key=lambda z: z.index, reverse=True)
    return zones[:max_zones]


# --------------------------------------------------------------------- FVG
def find_fvgs(candles: Candles, cfg: dict, structure_cfg: dict,
              max_zones: int = 15) -> list[Zone]:
    """Three-bar imbalance. Bullish: low[i] > high[i-2]. Bearish: high[i] < low[i-2]."""
    n = len(candles)
    if n < 5:
        return []

    period = int(structure_cfg.get("atr_period", 14))
    atr_values = atr(candles, period)
    min_gap = float(cfg.get("min_gap_atr", 0.20))
    zones: list[Zone] = []

    for i in range(2, n):
        atr_value = float(atr_values[i])
        if not np.isfinite(atr_value) or atr_value <= 0:
            continue
        threshold = atr_value * min_gap

        gap_up = float(candles.low[i] - candles.high[i - 2])
        if gap_up >= threshold:
            zones.append(Zone(ZoneKind.FVG, Direction.LONG,
                              top=float(candles.low[i]),
                              bottom=float(candles.high[i - 2]), index=i - 1))
            continue

        gap_down = float(candles.low[i - 2] - candles.high[i])
        if gap_down >= threshold:
            zones.append(Zone(ZoneKind.FVG, Direction.SHORT,
                              top=float(candles.low[i - 2]),
                              bottom=float(candles.high[i]), index=i - 1))

    _apply_mitigation(candles, zones, invalidate_on_body_close=True)
    zones = [z for z in zones if not z.invalidated]
    zones.sort(key=lambda z: z.index, reverse=True)
    return zones[:max_zones]


# ----------------------------------------------------------------- breakers
def find_breakers(candles: Candles, order_blocks: list[Zone]) -> list[Zone]:
    """An OB that price closed through becomes a breaker for the opposite side."""
    breakers: list[Zone] = []
    n = len(candles)
    for zone in order_blocks:
        if not zone.invalidated:
            continue
        after = candles.close[zone.index + 1:]
        if after.size == 0:
            continue
        if zone.direction is Direction.LONG and float(after.min()) < zone.bottom:
            breakers.append(Zone(ZoneKind.BREAKER, Direction.SHORT, zone.top,
                                 zone.bottom, zone.index))
        elif zone.direction is Direction.SHORT and float(after.max()) > zone.top:
            breakers.append(Zone(ZoneKind.BREAKER, Direction.LONG, zone.top,
                                 zone.bottom, zone.index))
    breakers.sort(key=lambda z: z.index, reverse=True)
    return breakers[:6]


# ------------------------------------------------------------------- mitigation
def _departure_index(candles: Candles, zone: Zone) -> int | None:
    """First bar after the zone that trades entirely clear of it.

    A zone is created BY the displacement leg that leaves it, so the leg itself
    necessarily overlaps the zone. Counting that as mitigation would mark every
    order block stale the moment it forms. Freshness is only meaningful once
    price has actually departed.
    """
    for i in range(zone.index + 1, len(candles)):
        if zone.direction is Direction.LONG and candles.low[i] > zone.top:
            return i
        if zone.direction is Direction.SHORT and candles.high[i] < zone.bottom:
            return i
    return None


def _apply_mitigation(candles: Candles, zones: list[Zone],
                      invalidate_on_body_close: bool) -> None:
    """Mark zones price has returned to, and those it has closed through."""
    for zone in zones:
        if zone.index + 1 >= len(candles):
            continue

        departure = _departure_index(candles, zone)
        if departure is None:
            zone.mitigated = False       # still forming; price has not left yet
            zone.fresh = True
        else:
            highs = candles.high[departure + 1:]
            lows = candles.low[departure + 1:]
            touched = bool(((lows <= zone.top) & (highs >= zone.bottom)).any())
            zone.mitigated = touched
            zone.fresh = not touched

        closes = candles.close[zone.index + 1:]
        if invalidate_on_body_close and closes.size:
            if zone.direction is Direction.LONG:
                zone.invalidated = bool((closes < zone.bottom).any())
            else:
                zone.invalidated = bool((closes > zone.top).any())


# -------------------------------------------------------------------- selection
def select_poi(zones: list[Zone], direction: Direction, reference_price: float,
               prefer_fresh: bool = True) -> Zone | None:
    """Best point of interest below (long) / above (short) the reference price."""
    candidates = [
        z for z in zones
        if z.direction is direction and not z.invalidated
        and ((direction is Direction.LONG and z.top <= reference_price * 1.001)
             or (direction is Direction.SHORT and z.bottom >= reference_price * 0.999))
    ]
    if not candidates:
        return None
    if prefer_fresh and any(z.fresh for z in candidates):
        candidates = [z for z in candidates if z.fresh]

    def distance(zone: Zone) -> float:
        return abs(reference_price - zone.mid)

    return min(candidates, key=distance)


def overlapping(zone_a: Zone | None, zone_b: Zone | None) -> bool:
    if zone_a is None or zone_b is None:
        return False
    return zone_a.bottom <= zone_b.top and zone_b.bottom <= zone_a.top


MIN_REFINED_FRACTION = 0.20


def refine_zone(zone: Zone, other: Zone | None) -> Zone:
    """Tighten an OB with an overlapping FVG — narrower zone, tighter invalidation.

    Zones that merely touch at an edge produce a degenerate intersection with
    near-zero height, which would collapse the entry range and make the stop
    distance meaningless. Below `MIN_REFINED_FRACTION` of the original height the
    refinement is discarded and the unrefined zone is kept.
    """
    if other is None or not overlapping(zone, other):
        return zone

    top = min(zone.top, other.top)
    bottom = max(zone.bottom, other.bottom)
    if (top - bottom) < zone.height * MIN_REFINED_FRACTION:
        return zone

    return Zone(
        kind=zone.kind,
        direction=zone.direction,
        top=top,
        bottom=bottom,
        index=max(zone.index, other.index),
        mitigated=zone.mitigated,
        invalidated=zone.invalidated,
        fresh=zone.fresh and other.fresh,
    )
