"""OHLCV fetching with bar-close-aware caching.

Two rules keep this correct and cheap:

1. THE FORMING BAR IS ALWAYS DROPPED. ccxt returns the in-progress candle as the
   final element. Analysing it is lookahead bias in its purest form, so it is
   stripped at ingestion and no downstream module ever sees it.
2. A cache entry is valid until the next bar closes. Re-fetching a 4H series
   every 30 seconds is wasted weight; the data cannot have changed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

from ofsignals.logging_setup import get_logger
from ofsignals.types import Candles

log = get_logger(__name__)

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000, "1w": 604_800_000,
}


def timeframe_ms(timeframe: str) -> int:
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe!r}") from exc


@dataclass(slots=True)
class _Entry:
    candles: Candles
    expires_at: float


class CandleStore:
    """Async OHLCV cache shared by every strategy engine."""

    def __init__(self, exchange: Any, default_limit: int = 300,
                 max_concurrency: int = 8) -> None:
        self._exchange = exchange
        self._limit = default_limit
        self._cache: dict[tuple[str, str], _Entry] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self.fetches = 0
        self.cache_hits = 0

    # ------------------------------------------------------------------ api
    async def get(self, symbol: str, timeframe: str, limit: int | None = None,
                  force: bool = False) -> Candles:
        key = (symbol, timeframe)
        limit = limit or self._limit
        now = time.time()

        entry = self._cache.get(key)
        if entry and not force and now < entry.expires_at and len(entry.candles) >= limit * 0.9:
            self.cache_hits += 1
            return entry.candles

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._cache.get(key)               # another task may have filled it
            if entry and not force and time.time() < entry.expires_at:
                self.cache_hits += 1
                return entry.candles

            candles = await self._fetch(symbol, timeframe, limit)
            self._cache[key] = _Entry(candles, self._expiry(timeframe))
            return candles

    async def get_many(self, symbol: str, timeframes: dict[str, str],
                       limit: int | None = None) -> dict[str, Candles]:
        """Fetch a mode's whole timeframe ladder concurrently.

        `timeframes` maps role -> timeframe, e.g. {"bias": "1h", "entry": "3m"}.
        """
        roles = list(timeframes)
        results = await asyncio.gather(
            *(self.get(symbol, timeframes[role], limit) for role in roles),
            return_exceptions=True,
        )
        out: dict[str, Candles] = {}
        for role, result in zip(roles, results):
            if isinstance(result, Exception):
                log.warning("ohlcv_role_failed", symbol=symbol, role=role,
                            timeframe=timeframes[role], error=str(result))
                out[role] = Candles.from_ohlcv([], symbol, timeframes[role])
            else:
                out[role] = result
        return out

    def invalidate(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k[0] == symbol]:
            del self._cache[key]

    # -------------------------------------------------------------- internals
    def _expiry(self, timeframe: str) -> float:
        """Valid until the next bar closes (+2s slack for exchange lag)."""
        step_ms = timeframe_ms(timeframe)
        now_ms = int(time.time() * 1000)
        next_close_ms = ((now_ms // step_ms) + 1) * step_ms
        return next_close_ms / 1000 + 2.0

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
           reraise=True)
    async def _fetch_raw(self, symbol: str, timeframe: str, limit: int) -> list:
        async with self._semaphore:
            self.fetches += 1
            return await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def _fetch(self, symbol: str, timeframe: str, limit: int) -> Candles:
        rows = await self._fetch_raw(symbol, timeframe, limit + 1)
        rows = _drop_forming_bar(rows, timeframe)
        rows = _repair_gaps(rows, timeframe)
        candles = Candles.from_ohlcv(rows, symbol, timeframe)
        if len(candles) < limit * 0.5:
            log.debug("thin_series", symbol=symbol, timeframe=timeframe, bars=len(candles))
        return candles


# ------------------------------------------------------------------- helpers
def _drop_forming_bar(rows: list, timeframe: str) -> list:
    """Remove the in-progress candle. This is the lookahead firewall."""
    if not rows:
        return rows
    step_ms = timeframe_ms(timeframe)
    now_ms = int(time.time() * 1000)
    current_open = (now_ms // step_ms) * step_ms
    return [row for row in rows if int(row[0]) < current_open]


def _repair_gaps(rows: list, timeframe: str, max_fill: int = 5) -> list:
    """Forward-fill short holes so index arithmetic stays sane.

    Filled bars carry zero volume, which keeps them out of volume-based tests.
    Long outages are left as gaps rather than fabricating a session of data.
    """
    if len(rows) < 2:
        return rows
    step_ms = timeframe_ms(timeframe)
    out = [rows[0]]
    filled = 0

    for row in rows[1:]:
        previous_ts = int(out[-1][0])
        gap = int(row[0]) - previous_ts
        missing = gap // step_ms - 1
        if 0 < missing <= max_fill:
            close = float(out[-1][4])
            for k in range(1, missing + 1):
                out.append([previous_ts + step_ms * k, close, close, close, close, 0.0])
                filled += 1
        out.append(row)

    if filled:
        log.debug("gaps_filled", timeframe=timeframe, bars=filled)
    return out


def resample(candles: Candles, factor: int) -> Candles:
    """Aggregate N bars into one. Used for coarse profiles without extra fetches."""
    n = len(candles)
    if factor <= 1 or n < factor:
        return candles
    usable = (n // factor) * factor
    start = n - usable

    def block(array: np.ndarray) -> np.ndarray:
        return array[start:].reshape(-1, factor)

    return Candles(
        ts=block(candles.ts.astype(np.int64))[:, 0],
        open=block(candles.open)[:, 0],
        high=block(candles.high).max(axis=1),
        low=block(candles.low).min(axis=1),
        close=block(candles.close)[:, -1],
        volume=block(candles.volume).sum(axis=1),
        symbol=candles.symbol,
        timeframe=f"{candles.timeframe}x{factor}",
    )
