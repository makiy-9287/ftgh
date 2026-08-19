"""WebSocket ingestion hub: depth + aggTrade per symbol.

ccxt.pro owns snapshot/diff synchronisation for `watch_order_book` — it fetches
the REST snapshot, buffers diffs, validates `pu`/`U`/`u` continuity and resyncs
on a sequence gap. We do not reimplement that; we supervise it. What this module
adds is the operational layer ccxt.pro does not provide:

  * one restartable task per stream, with bounded exponential backoff
  * staleness detection — a stream that stops delivering is worse than one that
    errors, because it fails silently while the book quietly rots
  * a REST seed of the trade tape so footprint warmup does not start from zero
  * clean teardown so a universe rotation does not leak tasks or sockets
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from ofsignals.analytics.footprint import TradeTape
from ofsignals.analytics.orderbook import BookState
from ofsignals.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class SymbolStreams:
    symbol: str
    book: BookState
    tape: TradeTape
    tasks: list[asyncio.Task] = field(default_factory=list)
    last_book_ms: float = 0.0
    last_trade_ms: float = 0.0
    book_errors: int = 0
    trade_errors: int = 0


class MarketDataHub:
    """Owns every live stream. Subscribe/unsubscribe as the universe rotates."""

    def __init__(self, exchange_pro: Any, rest_exchange: Any, cfg: dict) -> None:
        self._ws = exchange_pro
        self._rest = rest_exchange
        self._cfg = cfg
        self._ws_cfg = cfg.get("ws", {}) or {}
        self._depth_levels = int(
            (cfg.get("orderflow", {}) or {}).get("book", {}).get("depth_levels", 20)
        )
        self._backoff: list[float] = list(self._ws_cfg.get("reconnect_backoff_s",
                                                           [1, 2, 5, 10, 30, 60]))
        self._stale_after = float(self._ws_cfg.get("stale_book_timeout_s", 15))
        self._streams: dict[str, SymbolStreams] = {}
        self._stopping = False

    # ------------------------------------------------------------ properties
    @property
    def symbols(self) -> list[str]:
        return list(self._streams)

    def book_state(self, symbol: str) -> BookState | None:
        stream = self._streams.get(symbol)
        return stream.book if stream else None

    def tape(self, symbol: str) -> TradeTape | None:
        stream = self._streams.get(symbol)
        return stream.tape if stream else None

    def is_fresh(self, symbol: str) -> bool:
        stream = self._streams.get(symbol)
        if not stream or not stream.last_book_ms:
            return False
        return (time.monotonic() - stream.last_book_ms) <= self._stale_after

    # --------------------------------------------------------- subscriptions
    async def sync(self, symbols: list[str]) -> None:
        """Reconcile live streams with the desired symbol set."""
        wanted = set(symbols)
        current = set(self._streams)

        for symbol in current - wanted:
            await self.unsubscribe(symbol)
        for symbol in wanted - current:
            await self.subscribe(symbol)

        log.info("streams_synced", live=len(self._streams),
                 added=len(wanted - current), removed=len(current - wanted))

    async def subscribe(self, symbol: str) -> None:
        if symbol in self._streams:
            return
        stream = SymbolStreams(symbol, BookState(symbol), TradeTape(symbol))
        self._streams[symbol] = stream
        stream.tasks = [
            asyncio.create_task(self._watch_book(stream), name=f"book:{symbol}"),
            asyncio.create_task(self._watch_trades(stream), name=f"trades:{symbol}"),
        ]
        asyncio.create_task(self._seed_tape(stream), name=f"seed:{symbol}")

    async def unsubscribe(self, symbol: str) -> None:
        stream = self._streams.pop(symbol, None)
        if not stream:
            return
        for task in stream.tasks:
            task.cancel()
        for task in stream.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def close(self) -> None:
        self._stopping = True
        for symbol in list(self._streams):
            await self.unsubscribe(symbol)

    # ---------------------------------------------------------------- loops
    async def _watch_book(self, stream: SymbolStreams) -> None:
        attempt = 0
        while not self._stopping:
            try:
                book = await self._ws.watch_order_book(stream.symbol, limit=self._depth_levels)
                stream.book.update(book, self._depth_levels)
                stream.last_book_ms = time.monotonic()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one symbol must not kill the hub
                stream.book_errors += 1
                delay = self._delay(attempt)
                attempt += 1
                log.warning("book_stream_error", symbol=stream.symbol,
                            error=str(exc)[:200], retry_in=delay,
                            errors=stream.book_errors)
                await asyncio.sleep(delay)

    async def _watch_trades(self, stream: SymbolStreams) -> None:
        attempt = 0
        while not self._stopping:
            try:
                trades = await self._ws.watch_trades(stream.symbol)
                if trades:
                    stream.tape.add_many(trades)
                    stream.last_trade_ms = time.monotonic()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                stream.trade_errors += 1
                delay = self._delay(attempt)
                attempt += 1
                log.warning("trade_stream_error", symbol=stream.symbol,
                            error=str(exc)[:200], retry_in=delay)
                await asyncio.sleep(delay)

    async def _seed_tape(self, stream: SymbolStreams, pages: int = 3) -> None:
        """Backfill recent prints so footprint warmup starts partway home."""
        try:
            since = self._rest.milliseconds() - 90 * 60_000
            total = 0
            for _ in range(pages):
                trades = await self._rest.fetch_trades(stream.symbol, since=since, limit=1000)
                if not trades:
                    break
                total += stream.tape.add_many(trades)
                since = int(trades[-1]["timestamp"]) + 1
                if len(trades) < 1000:
                    break
            log.debug("tape_seeded", symbol=stream.symbol, trades=total)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - seeding is best-effort
            log.debug("tape_seed_failed", symbol=stream.symbol, error=str(exc)[:160])

    def _delay(self, attempt: int) -> float:
        return float(self._backoff[min(attempt, len(self._backoff) - 1)])

    # ------------------------------------------------------------- watchdog
    async def watchdog(self, interval_s: float = 20.0) -> None:
        """Restart streams that have gone quiet without raising."""
        while not self._stopping:
            await asyncio.sleep(interval_s)
            now = time.monotonic()
            for symbol, stream in list(self._streams.items()):
                if not stream.last_book_ms:
                    continue
                silence = now - stream.last_book_ms
                if silence > self._stale_after * 3:
                    log.warning("stream_stale_resubscribe", symbol=symbol,
                                silent_for_s=round(silence, 1))
                    await self.unsubscribe(symbol)
                    await self.subscribe(symbol)

    def stats(self) -> dict[str, Any]:
        fresh = sum(1 for s in self._streams if self.is_fresh(s))
        return {
            "symbols": len(self._streams),
            "fresh_books": fresh,
            "tape_sizes": {s.symbol: len(s.tape) for s in list(self._streams.values())[:5]},
        }
