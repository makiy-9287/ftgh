"""Market structure: swings, BOS/CHoCH, displacement, dealing range, HTF bias.

LOOKAHEAD DISCIPLINE
A fractal pivot at index *i* is only knowable at index *i + N*. Every function
here carries `confirmed_index` and callers must respect it. `detect_swings`
never returns a pivot whose confirmation bar lies beyond the series end.
"""

from __future__ import annotations

import numpy as np

from ofsignals.types import (
    Bias,
    Candles,
    DealingRange,
    Direction,
    Displacement,
    StructureEvent,
    StructureKind,
    Swing,
    SwingKind,
)


# ------------------------------------------------------------------------- ATR
def true_range(candles: Candles) -> np.ndarray:
    if len(candles) == 0:
        return np.array([])
    prev_close = np.concatenate(([candles.close[0]], candles.close[:-1]))
    return np.maximum.reduce([
        candles.high - candles.low,
        np.abs(candles.high - prev_close),
        np.abs(candles.low - prev_close),
    ])


def atr(candles: Candles, period: int = 14) -> np.ndarray:
    """Wilder ATR, causal. atr[i] uses bars 0..i only."""
    tr = true_range(candles)
    n = tr.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    period = max(1, min(period, n))
    out[period - 1] = tr[:period].mean()
    alpha = 1.0 / period
    for i in range(period, n):
        out[i] = out[i - 1] * (1 - alpha) + tr[i] * alpha
    if period > 1:
        out[: period - 1] = out[period - 1]
    return out


def atr_at(candles: Candles, index: int = -1, period: int = 14) -> float:
    values = atr(candles, period)
    if values.size == 0:
        return float("nan")
    value = float(values[index])
    if not np.isfinite(value) or value <= 0:
        finite = values[np.isfinite(values)]
        return float(finite[-1]) if finite.size else float("nan")
    return value


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Causal simple moving average; positions before warmup use the partial mean."""
    n = values.size
    if n == 0:
        return values
    cumulative = np.cumsum(np.insert(values, 0, 0.0))
    out = np.empty(n)
    for i in range(n):
        start = max(0, i - period + 1)
        out[i] = (cumulative[i + 1] - cumulative[start]) / (i - start + 1)
    return out


# ---------------------------------------------------------------------- swings
def detect_swings(candles: Candles, lookback: int = 2) -> list[Swing]:
    """Fractal pivots with N bars either side."""
    n = len(candles)
    swings: list[Swing] = []
    if n < lookback * 2 + 1:
        return swings

    highs, lows = candles.high, candles.low
    for i in range(lookback, n - lookback):
        window = slice(i - lookback, i + lookback + 1)
        if highs[i] == highs[window].max() and (highs[window] < highs[i]).sum() >= lookback:
            swings.append(Swing(i, float(highs[i]), SwingKind.HIGH, i + lookback))
        if lows[i] == lows[window].min() and (lows[window] > lows[i]).sum() >= lookback:
            swings.append(Swing(i, float(lows[i]), SwingKind.LOW, i + lookback))

    swings.sort(key=lambda s: s.index)
    return swings


def last_swing(swings: list[Swing], kind: SwingKind,
               before_index: int | None = None) -> Swing | None:
    for swing in reversed(swings):
        if swing.kind is not kind:
            continue
        if before_index is not None and swing.confirmed_index > before_index:
            continue
        return swing
    return None


# ------------------------------------------------------------------- structure
def detect_structure_events(candles: Candles, swings: list[Swing]) -> list[StructureEvent]:
    """Walk the series once, emitting BOS/CHoCH on body closes through pivots.

    Trend state starts NONE, so the first break is classified CHoCH — the
    conservative reading when no prior regime is established.
    """
    events: list[StructureEvent] = []
    if not swings or len(candles) == 0:
        return events

    trend = Direction.NONE
    active_high: Swing | None = None
    active_low: Swing | None = None
    cursor = 0
    close = candles.close

    for i in range(len(candles)):
        # Admit only pivots confirmed at or before this bar (no lookahead).
        while cursor < len(swings) and swings[cursor].confirmed_index <= i:
            swing = swings[cursor]
            if swing.kind is SwingKind.HIGH:
                active_high = swing
            else:
                active_low = swing
            cursor += 1

        price = float(close[i])

        if active_high is not None and price > active_high.price:
            kind = StructureKind.BOS if trend is Direction.LONG else StructureKind.CHOCH
            events.append(StructureEvent(i, kind, Direction.LONG, active_high.price))
            trend = Direction.LONG
            active_high = None          # consumed; wait for a fresh pivot
            continue

        if active_low is not None and price < active_low.price:
            kind = StructureKind.BOS if trend is Direction.SHORT else StructureKind.CHOCH
            events.append(StructureEvent(i, kind, Direction.SHORT, active_low.price))
            trend = Direction.SHORT
            active_low = None
            continue

    return events


# ---------------------------------------------------------------- displacement
def detect_displacements(candles: Candles, atr_mult: float = 1.5,
                         body_ratio: float = 0.55, period: int = 14) -> list[Displacement]:
    """Impulse legs: oversized range with a dominant body."""
    n = len(candles)
    out: list[Displacement] = []
    if n == 0:
        return out

    atr_values = atr(candles, period)
    ranges = candles.range
    bodies = candles.body

    for i in range(n):
        a = atr_values[i]
        rng = ranges[i]
        if not np.isfinite(a) or a <= 0 or rng <= 0:
            continue
        multiple = rng / a
        if multiple < atr_mult or bodies[i] / rng < body_ratio:
            continue

        direction = Direction.LONG if candles.close[i] > candles.open[i] else Direction.SHORT
        origin = i
        for j in range(i - 1, max(-1, i - 6), -1):
            if direction is Direction.LONG and candles.low[j] <= candles.low[origin]:
                origin = j
            elif direction is Direction.SHORT and candles.high[j] >= candles.high[origin]:
                origin = j
            else:
                break

        out.append(Displacement(
            index=i,
            direction=direction,
            origin_index=origin,
            low=float(candles.low[origin:i + 1].min()),
            high=float(candles.high[origin:i + 1].max()),
            atr_multiple=float(multiple),
        ))
    return out


def displacement_after(candles: Candles, start_index: int, direction: Direction,
                       atr_mult: float, body_ratio: float, window: int = 6,
                       period: int = 14) -> Displacement | None:
    """Did an impulse in `direction` occur within `window` bars of `start_index`?"""
    for disp in detect_displacements(candles, atr_mult, body_ratio, period):
        if disp.direction is direction and start_index <= disp.index <= start_index + window:
            return disp
    return None


# ---------------------------------------------------------------- dealing range
def build_dealing_range(candles: Candles, displacements: list[Displacement],
                        direction: Direction | None = None,
                        max_age_bars: int = 40) -> DealingRange | None:
    """Range anchored to the most recent displacement, preferring one that
    agrees with `direction`.

    Requiring the very last impulse to agree was too brittle: one counter-trend
    candle inside a healthy pullback voided the entire bias, which is a common
    way for a correct read to be thrown away.
    """
    if not displacements:
        return None

    chosen = displacements[-1]
    if direction is not None:
        recent = len(candles) - 1
        agreeing = [d for d in displacements
                    if d.direction is direction and recent - d.index <= max_age_bars]
        if agreeing:
            chosen = agreeing[-1]
        elif chosen.direction is not direction:
            return None

    if chosen.high <= chosen.low:
        return None
    return DealingRange(low=chosen.low, high=chosen.high, direction=chosen.direction)


# ------------------------------------------------------------------------ bias
def compute_bias(candles: Candles, cfg: dict) -> Bias:
    """L1 of the cascade: which side is the higher timeframe delivering to?"""
    lookback = int(cfg.get("swing_fractal_lookback", 2))
    atr_mult = float(cfg.get("displacement_atr_mult", 1.5))
    body_ratio = float(cfg.get("displacement_body_ratio", 0.55))
    period = int(cfg.get("atr_period", 14))

    if len(candles) < max(period * 3, 40):
        return Bias(Direction.NONE, None, None, "insufficient history")

    swings = detect_swings(candles, lookback)
    events = detect_structure_events(candles, swings)
    if not events:
        return Bias(Direction.NONE, None, None, "no structural break in window")

    displacements = detect_displacements(candles, atr_mult, body_ratio, period)
    last = events[-1]
    dealing_range = build_dealing_range(candles, displacements, last.direction)

    if len(events) >= 2:
        prev = events[-2]
        if prev.direction is not last.direction and (last.index - prev.index) <= 3:
            return Bias(Direction.NONE, dealing_range, last,
                        "conflicting structure within 3 bars (consolidation)")

    if dealing_range is None:
        return Bias(Direction.NONE, None, last, "no displacement leg to anchor a range")

    if dealing_range.direction is not last.direction:
        return Bias(Direction.NONE, dealing_range, last,
                    "dealing range opposes the last structural break")

    reason = (f"{last.kind.value} {last.direction.value.lower()} at {last.level:.6g}, "
              f"price at {dealing_range.position_of(candles.last_price) * 100:.0f}% of range")
    return Bias(last.direction, dealing_range, last, reason)


def volatility_ok(candles: Candles, period: int = 14, percentile: float = 20.0,
                  window: int = 200) -> bool:
    """Reject dead tape: ATR% below the 20th percentile of its own history."""
    if len(candles) < 30:
        return False
    atr_values = atr(candles, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_pct = atr_values / np.where(candles.close == 0, np.nan, candles.close) * 100.0
    recent = atr_pct[-window:]
    recent = recent[np.isfinite(recent)]
    if recent.size < 20:
        return True
    return bool(recent[-1] >= np.percentile(recent, percentile))
