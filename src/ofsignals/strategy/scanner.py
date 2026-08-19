"""The scanner: turns evaluations into published signals, or refuses to.

Every filter here is a PORTFOLIO property rather than a setup property, which is
why they live outside the cascade. The correlation guard is the one that saves
accounts: ten alts long during a BTC bid is one trade with ten sets of fees, and
the cascade cannot see that because it only ever looks at one symbol.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import timedelta
from typing import Any

import numpy as np

from ofsignals.exchange.rest_client import CandleStore
from ofsignals.logging_setup import get_logger
from ofsignals.notify.formatter import format_signal
from ofsignals.store.db import SignalStore
from ofsignals.strategy.base import Signal
from ofsignals.strategy.intraday import IntradayEngine
from ofsignals.strategy.mtf import Evaluation
from ofsignals.strategy.scalp import ScalpEngine
from ofsignals.strategy.swing import SwingEngine
from ofsignals.types import Direction, utcnow

log = get_logger(__name__)

ENGINES = {"scalp": ScalpEngine, "intraday": IntradayEngine, "swing": SwingEngine}


class Scanner:
    def __init__(self, settings: Any, candles: CandleStore, hub: Any,
                 store: SignalStore, telegram: Any) -> None:
        self.settings = settings
        self.candles = candles
        self.hub = hub
        self.store = store
        self.telegram = telegram
        self.filters = settings.section("filters")

        self.engines = {
            mode: ENGINES[mode](settings, candles, hub)
            for mode in settings.enabled_modes if mode in ENGINES
        }
        self.universe: list = []
        self.rejections: Counter[str] = Counter()
        self.evaluations = 0
        self.published = 0
        self._correlation_cache: dict[str, tuple[float, float]] = {}

        log.info("scanner_ready", modes=list(self.engines))

    # ------------------------------------------------------------- scanning
    async def scan(self) -> list[Signal]:
        if not self.universe:
            return []

        eligible = [s for s in self.universe if self.hub.is_fresh(s.symbol)]
        if not eligible:
            log.info("scan_skipped", reason="no symbols with a fresh depth stream")
            return []

        cycle_rejections: Counter[str] = Counter()
        published: list[Signal] = []
        semaphore = asyncio.Semaphore(6)

        async def evaluate(symbol_info, mode: str) -> Evaluation | None:
            async with semaphore:
                try:
                    return await self.engines[mode].evaluate(symbol_info.symbol)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one symbol must not stop the cycle
                    log.error("evaluation_failed", symbol=symbol_info.symbol,
                              mode=mode, error=str(exc)[:200])
                    return None

        tasks = [evaluate(info, mode) for info in eligible for mode in self.engines]
        started = time.perf_counter()

        for coroutine in asyncio.as_completed(tasks):
            evaluation = await coroutine
            if evaluation is None:
                continue
            self.evaluations += 1

            if not evaluation.passed:
                cycle_rejections[f"{evaluation.stage}: {evaluation.reason[:60]}"] += 1
                continue

            signal = evaluation.signal
            blocked = await self._portfolio_block(signal)
            if blocked:
                cycle_rejections[f"filter: {blocked}"] += 1
                log.info("signal_blocked", symbol=signal.symbol, mode=signal.mode,
                         reason=blocked, score=signal.confluence_score)
                continue

            await self._publish(signal)
            published.append(signal)

        self.rejections = cycle_rejections
        log.info("scan_complete", symbols=len(eligible), evaluations=len(tasks),
                 published=len(published), elapsed_s=round(time.perf_counter() - started, 1))
        return published

    # ------------------------------------------------------ portfolio filters
    async def _portfolio_block(self, signal: Signal) -> str:
        cooldown = int(self.settings.mode(signal.mode).get("cooldown_minutes", 60))
        if await self.store.in_cooldown(signal.symbol, signal.mode, cooldown):
            return f"cooldown active ({cooldown}m)"

        open_signals = await self.store.open_directions()
        if len(open_signals) >= int(self.filters.get("max_concurrent_signals", 6)):
            return "max concurrent signals reached"

        if await self.store.count_since(1440) >= int(self.filters.get("max_signals_per_day", 12)):
            return "daily signal cap reached"

        last_at = await self.store.last_signal_at()
        gap = int(self.filters.get("min_minutes_between_any_signal", 5))
        if last_at and utcnow() - last_at < timedelta(minutes=gap):
            return f"minimum {gap}m spacing between publishes"

        correlated = await self._correlated_same_direction(signal, open_signals)
        if correlated >= int(self.filters.get("max_same_direction_correlated", 2)):
            return f"{correlated} correlated {signal.direction.value} positions already open"

        spread = self._current_spread(signal.symbol)
        max_spread = float(self.settings.section("universe").get("max_spread_bps", 5.0))
        if spread > max_spread:
            return f"spread widened to {spread:.1f}bps at publish time"

        return ""

    async def _correlated_same_direction(self, signal: Signal,
                                         open_signals: list[tuple[str, str]]) -> int:
        same = [sym for sym, direction in open_signals
                if direction == signal.direction.value and sym != signal.symbol]
        if not same:
            return 0

        threshold = float(self.filters.get("btc_correlation_threshold", 0.75))
        window = int(self.filters.get("btc_correlation_window_bars", 96))
        candidate_beta = await self._btc_correlation(signal.symbol, window)
        if candidate_beta < threshold:
            return 0

        count = 0
        for symbol in same:
            if await self._btc_correlation(symbol, window) >= threshold:
                count += 1
        return count

    async def _btc_correlation(self, symbol: str, window: int) -> float:
        cached = self._correlation_cache.get(symbol)
        if cached and time.time() - cached[0] < 1800:
            return cached[1]

        try:
            btc = await self.candles.get("BTC/USDT:USDT", "15m", limit=window + 5)
            other = await self.candles.get(symbol, "15m", limit=window + 5)
        except Exception:  # noqa: BLE001 - correlation is advisory, never fatal
            return 0.0

        size = min(len(btc), len(other), window)
        if size < 30:
            return 0.0

        btc_returns = np.diff(np.log(btc.close[-size:]))
        other_returns = np.diff(np.log(other.close[-size:]))
        if btc_returns.std() == 0 or other_returns.std() == 0:
            return 0.0

        correlation = float(np.corrcoef(btc_returns, other_returns)[0, 1])
        correlation = 0.0 if not np.isfinite(correlation) else correlation
        self._correlation_cache[symbol] = (time.time(), correlation)
        return correlation

    def _current_spread(self, symbol: str) -> float:
        state = self.hub.book_state(symbol)
        return state.spread_bps if state else float("inf")

    # ------------------------------------------------------------ publishing
    async def _publish(self, signal: Signal) -> None:
        await self.store.insert(signal)
        await self.telegram.send(format_signal(signal))
        self.published += 1
        log.info("signal_published", symbol=signal.symbol, mode=signal.mode,
                 direction=signal.direction.value, score=signal.confluence_score,
                 rr2=signal.rr["tp2"], leverage=signal.leverage["recommended"],
                 sl_pct=signal.sl_distance_pct)

    # ------------------------------------------------------------ diagnostics
    def rejection_summary(self) -> dict[str, int]:
        return dict(self.rejections)

    def stage_histogram(self) -> dict[str, int]:
        histogram: Counter[str] = Counter()
        for key, count in self.rejections.items():
            histogram[key.split(":", 1)[0]] += count
        return dict(histogram)
