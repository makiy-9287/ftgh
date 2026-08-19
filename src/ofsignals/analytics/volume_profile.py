"""Volume profile: POC, value area, low/high volume nodes, naked POCs.

Volume is distributed uniformly across each bar's high-low range. That is an
approximation — true distribution needs tick data — but it is the standard
market-profile treatment and is stable across timeframes.
"""

from __future__ import annotations

import numpy as np

from ofsignals.types import Candles, VolumeProfile


def build_profile(candles: Candles, bins: int = 120, value_area_pct: float = 0.70,
                  lvn_percentile: float = 0.20) -> VolumeProfile | None:
    n = len(candles)
    if n < 5:
        return None

    low = float(candles.low.min())
    high = float(candles.high.max())
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        return None

    bins = max(10, int(bins))
    edges = np.linspace(low, high, bins + 1)
    centres = (edges[:-1] + edges[1:]) / 2.0
    width = edges[1] - edges[0]
    histogram = np.zeros(bins)

    # Spread each bar's volume evenly over the bins its range covers.
    for i in range(n):
        bar_low, bar_high = float(candles.low[i]), float(candles.high[i])
        volume = float(candles.volume[i])
        if volume <= 0:
            continue
        if bar_high <= bar_low:
            index = int(np.clip((bar_low - low) / width, 0, bins - 1))
            histogram[index] += volume
            continue
        start = int(np.clip((bar_low - low) / width, 0, bins - 1))
        end = int(np.clip((bar_high - low) / width, 0, bins - 1))
        span = end - start + 1
        histogram[start:end + 1] += volume / span

    total = histogram.sum()
    if total <= 0:
        return None

    poc_index = int(histogram.argmax())
    poc = float(centres[poc_index])

    # Expand outward from the POC until the target share of volume is captured.
    target = total * float(value_area_pct)
    captured = histogram[poc_index]
    lower, upper = poc_index, poc_index
    while captured < target and (lower > 0 or upper < bins - 1):
        below = histogram[lower - 1] if lower > 0 else -1.0
        above = histogram[upper + 1] if upper < bins - 1 else -1.0
        if above >= below:
            upper += 1
            captured += histogram[upper]
        else:
            lower -= 1
            captured += histogram[lower]

    val, vah = float(centres[lower]), float(centres[upper])

    nonzero = histogram[histogram > 0]
    if nonzero.size:
        lvn_threshold = float(np.quantile(nonzero, float(lvn_percentile)))
        hvn_threshold = float(np.quantile(nonzero, 0.85))
    else:
        lvn_threshold = hvn_threshold = 0.0

    lvn = tuple(float(c) for c, v in zip(centres, histogram) if 0 < v <= lvn_threshold)
    hvn = tuple(float(c) for c, v in zip(centres, histogram) if v >= hvn_threshold)

    return VolumeProfile(poc=poc, vah=vah, val=val, lvn_prices=lvn, hvn_prices=hvn,
                         bin_edges=edges, bin_volume=histogram)


def session_pocs(candles: Candles, period_bars: int, bins: int = 60,
                 max_sessions: int = 20) -> list[tuple[int, float]]:
    """POC per fixed-size block. Returns (end_index, poc_price)."""
    out: list[tuple[int, float]] = []
    n = len(candles)
    if n < period_bars * 2:
        return out
    start = max(0, n - period_bars * max_sessions)
    for begin in range(start, n - period_bars + 1, period_bars):
        block = Candles(
            candles.ts[begin:begin + period_bars],
            candles.open[begin:begin + period_bars],
            candles.high[begin:begin + period_bars],
            candles.low[begin:begin + period_bars],
            candles.close[begin:begin + period_bars],
            candles.volume[begin:begin + period_bars],
            candles.symbol, candles.timeframe,
        )
        profile = build_profile(block, bins=bins)
        if profile:
            out.append((begin + period_bars - 1, profile.poc))
    return out


def naked_pocs(candles: Candles, period_bars: int, bins: int = 60) -> list[float]:
    """POCs that price has not traded back through since they formed.

    These are the highest-quality TP3 anchors: unfinished business.
    """
    result: list[float] = []
    for end_index, poc in session_pocs(candles, period_bars, bins):
        after_high = candles.high[end_index + 1:]
        after_low = candles.low[end_index + 1:]
        if after_high.size == 0:
            continue
        touched = bool(((after_low <= poc) & (after_high >= poc)).any())
        if not touched:
            result.append(float(poc))
    return sorted(set(result))


def nearest_above(levels: list[float] | tuple[float, ...], price: float) -> float | None:
    candidates = [level for level in levels if level > price]
    return min(candidates) if candidates else None


def nearest_below(levels: list[float] | tuple[float, ...], price: float) -> float | None:
    candidates = [level for level in levels if level < price]
    return max(candidates) if candidates else None
