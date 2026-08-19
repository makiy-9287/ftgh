"""Liquidity: pool mapping (EQH/EQL, PDH/PDL, PWH/PWL, sessions) and sweep detection.

Doctrine: an unswept pool is a TARGET, a swept pool is a TRIGGER. This module
produces both sides of that statement.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from ofsignals.analytics.structure import atr, detect_swings, sma
from ofsignals.types import Candles, Direction, Pool, PoolSide, Sweep, SwingKind

_MS_DAY = 86_400_000

# UTC session windows (start_hour, end_hour)
SESSIONS: dict[str, tuple[int, int]] = {
    "asia": (0, 8),
    "london": (7, 16),
    "newyork": (13, 22),
}


# --------------------------------------------------------------------- helpers
def _hour_of(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour


def _day_key(ts_ms: int) -> int:
    return int(ts_ms // _MS_DAY)


def _week_key(ts_ms: int) -> int:
    # Epoch day 0 was a Thursday; shift so weeks start Monday.
    return int((ts_ms // _MS_DAY + 3) // 7)


def _cluster(values: list[tuple[int, float]], tolerance: float) -> list[tuple[float, int, int]]:
    """Group near-equal levels. Returns (level, touch_count, last_index)."""
    if not values:
        return []
    ordered = sorted(values, key=lambda item: item[1])
    clusters: list[list[tuple[int, float]]] = [[ordered[0]]]
    for item in ordered[1:]:
        if abs(item[1] - clusters[-1][-1][1]) <= tolerance:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    out = []
    for group in clusters:
        prices = [p for _, p in group]
        out.append((float(np.mean(prices)), len(group), max(i for i, _ in group)))
    return out


# ----------------------------------------------------------------- pool building
def build_pools(candles: Candles, cfg: dict, structure_cfg: dict) -> list[Pool]:
    """Full liquidity map for one timeframe, ranked by touches x recency."""
    n = len(candles)
    if n < 10:
        return []

    price = candles.last_price
    atr_value = float(atr(candles, int(structure_cfg.get("atr_period", 14)))[-1])
    if not np.isfinite(atr_value) or atr_value <= 0:
        return []

    tolerance = atr_value * float(cfg.get("equal_level_tolerance_atr", 0.10))
    min_touches = int(cfg.get("min_touches_for_pool", 2))
    lookback = int(structure_cfg.get("swing_fractal_lookback", 2))
    pools: list[Pool] = []

    # --- equal highs / lows from confirmed pivots -------------------------
    swings = detect_swings(candles, lookback)
    highs = [(s.index, s.price) for s in swings if s.kind is SwingKind.HIGH]
    lows = [(s.index, s.price) for s in swings if s.kind is SwingKind.LOW]

    for level, touches, last_index in _cluster(highs, tolerance):
        if touches >= min_touches and level > price:
            pools.append(Pool(level, PoolSide.BUYSIDE, touches, "eqh", last_index))
    for level, touches, last_index in _cluster(lows, tolerance):
        if touches >= min_touches and level < price:
            pools.append(Pool(level, PoolSide.SELLSIDE, touches, "eql", last_index))

    # --- previous day / week extremes ------------------------------------
    if cfg.get("include_prev_day_high_low", True):
        pools += _period_extremes(candles, _day_key, "pd", price)
    if cfg.get("include_prev_week_high_low", True):
        pools += _period_extremes(candles, _week_key, "pw", price)

    # --- previous session extremes ---------------------------------------
    for name in cfg.get("session_levels", []):
        window = SESSIONS.get(str(name).lower())
        if window:
            pools += _session_extremes(candles, name, window, price)

    # --- rank -------------------------------------------------------------
    for pool in pools:
        recency = 1.0 - min((n - 1 - pool.last_index) / max(n, 1), 0.95)
        proximity = 1.0 / (1.0 + abs(pool.price - price) / atr_value)
        source_weight = {"eqh": 1.0, "eql": 1.0, "pdh": 0.9, "pdl": 0.9,
                         "pwh": 1.1, "pwl": 1.1}.get(pool.source, 0.8)
        pool.rank = round(pool.touches * recency * proximity * source_weight, 4)

    pools.sort(key=lambda p: p.rank, reverse=True)
    return _dedupe(pools, tolerance)


def _dedupe(pools: list[Pool], tolerance: float) -> list[Pool]:
    kept: list[Pool] = []
    for pool in pools:
        if any(p.side is pool.side and abs(p.price - pool.price) <= tolerance for p in kept):
            continue
        kept.append(pool)
    return kept


def _period_extremes(candles: Candles, key_fn, prefix: str, price: float) -> list[Pool]:
    keys = np.array([key_fn(int(t)) for t in candles.ts])
    unique = np.unique(keys)
    if unique.size < 2:
        return []
    previous = unique[-2]
    mask = keys == previous
    if not mask.any():
        return []
    high = float(candles.high[mask].max())
    low = float(candles.low[mask].min())
    last_index = int(np.nonzero(mask)[0][-1])
    out = []
    if high > price:
        out.append(Pool(high, PoolSide.BUYSIDE, 1, f"{prefix}h", last_index))
    if low < price:
        out.append(Pool(low, PoolSide.SELLSIDE, 1, f"{prefix}l", last_index))
    return out


def _session_extremes(candles: Candles, name: str, window: tuple[int, int],
                      price: float) -> list[Pool]:
    start, end = window
    hours = np.array([_hour_of(int(t)) for t in candles.ts])
    days = np.array([_day_key(int(t)) for t in candles.ts])
    in_session = (hours >= start) & (hours < end)
    if not in_session.any():
        return []

    unique_days = np.unique(days[in_session])
    if unique_days.size == 0:
        return []
    # Use the most recent COMPLETED session where possible.
    target_day = unique_days[-2] if unique_days.size >= 2 else unique_days[-1]
    mask = in_session & (days == target_day)
    if not mask.any():
        return []

    high = float(candles.high[mask].max())
    low = float(candles.low[mask].min())
    last_index = int(np.nonzero(mask)[0][-1])
    out = []
    if high > price:
        out.append(Pool(high, PoolSide.BUYSIDE, 1, f"session_{name}_high", last_index))
    if low < price:
        out.append(Pool(low, PoolSide.SELLSIDE, 1, f"session_{name}_low", last_index))
    return out


# ------------------------------------------------------------------- targeting
def unswept_pools(pools: list[Pool], side: PoolSide) -> list[Pool]:
    return [p for p in pools if p.side is side and not p.swept]


def nearest_pool(pools: list[Pool], price: float, side: PoolSide) -> Pool | None:
    candidates = unswept_pools(pools, side)
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs(p.price - price))


def furthest_pool(pools: list[Pool], price: float, side: PoolSide) -> Pool | None:
    candidates = unswept_pools(pools, side)
    if not candidates:
        return None
    return max(candidates, key=lambda p: abs(p.price - price))


# ---------------------------------------------------------------------- sweeps
def detect_sweep(candles: Candles, pools: list[Pool], cfg: dict,
                 structure_cfg: dict, scan_bars: int | None = None) -> Sweep | None:
    """Find the most recent VALID sweep of a pool.

    All four conditions must hold:
      1. wick pierces the pool by >= sweep_penetration_atr
      2. sweep-bar volume >= sweep_volume_mult x SMA(20) volume
      3. price closes back inside within sweep_reclaim_bars
      4. (checked by the caller via `displacement_after`) an impulse follows

    A pierce that never reclaims is a breakout, not a sweep — it returns None.
    """
    n = len(candles)
    if n < 25 or not pools:
        return None

    if scan_bars is None:
        scan_bars = int(cfg.get("sweep_scan_bars", 15))
    period = int(structure_cfg.get("atr_period", 14))
    atr_values = atr(candles, period)
    volume_ma = sma(candles.volume, 20)

    penetration_mult = float(cfg.get("sweep_penetration_atr", 0.15))
    volume_mult_min = float(cfg.get("sweep_volume_mult", 1.8))
    reclaim_bars = int(cfg.get("sweep_reclaim_bars", 3))

    best: Sweep | None = None
    start = max(1, n - scan_bars - reclaim_bars)

    for i in range(start, n):
        atr_value = float(atr_values[i])
        if not np.isfinite(atr_value) or atr_value <= 0:
            continue
        required = atr_value * penetration_mult
        ma = float(volume_ma[i])
        volume_multiple = float(candles.volume[i]) / ma if ma > 0 else 0.0
        if volume_multiple < volume_mult_min:
            continue

        for pool in pools:
            if pool.swept:
                continue

            if pool.side is PoolSide.SELLSIDE:
                penetration = pool.price - float(candles.low[i])
                if penetration < required:
                    continue
                reclaim = _find_reclaim(candles, i, pool.price, reclaim_bars, above=True)
                if reclaim is None:
                    continue
                sweep = Sweep(pool, i, reclaim, penetration / atr_value, volume_multiple,
                              float(candles.low[i]), Direction.LONG)
            else:
                penetration = float(candles.high[i]) - pool.price
                if penetration < required:
                    continue
                reclaim = _find_reclaim(candles, i, pool.price, reclaim_bars, above=False)
                if reclaim is None:
                    continue
                sweep = Sweep(pool, i, reclaim, penetration / atr_value, volume_multiple,
                              float(candles.high[i]), Direction.SHORT)

            if best is None or sweep.reclaim_index >= best.reclaim_index:
                best = sweep

    return best


def _find_reclaim(candles: Candles, sweep_index: int, level: float,
                  max_bars: int, above: bool) -> int | None:
    """First bar within the window that closes back on the correct side."""
    end = min(len(candles), sweep_index + max_bars + 1)
    for j in range(sweep_index, end):
        close = float(candles.close[j])
        if (above and close > level) or (not above and close < level):
            return j
    return None


def mark_swept(pools: list[Pool], candles: Candles, lookback: int = 200,
               skip_recent: int = 0) -> None:
    """Flag pools already taken out, so they are never used as TARGETS.

    MUST run AFTER `detect_sweep`. Running it first flags the very pool that was
    just raided, and `detect_sweep` skips flagged pools — so the fresher the
    sweep, the more certainly it was discarded. That inversion silently blocked
    exactly the setups the engine exists to find.

    `skip_recent` excludes the newest bars from the "already taken" test, so a
    raid inside the detection window never disqualifies its own pool.
    """
    if len(candles) == 0:
        return
    usable = candles.head(len(candles) - skip_recent) if skip_recent else candles
    if len(usable) == 0:
        return
    window = usable.tail(lookback)
    high = float(window.high.max())
    low = float(window.low.min())
    for pool in pools:
        if pool.side is PoolSide.BUYSIDE and high > pool.price:
            pool.swept = True
        elif pool.side is PoolSide.SELLSIDE and low < pool.price:
            pool.swept = True
