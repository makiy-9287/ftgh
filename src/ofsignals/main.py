"""Service entrypoint: wires ingestion, analytics, strategy, dispatch, storage.

Task topology
    universe-refresh   rebuild the >10M USDT list, resync WebSocket subscriptions
    ws-watchdog        restart streams that go silent without erroring
    scan               run the cascade across universe x enabled modes
    tracker            resolve open signals against live price, emit TP/SL updates
    heartbeat          liveness + rejection-stage digest to the admin chat

Every loop is independently supervised: one failing loop logs, backs off and
retries rather than taking the process down. SIGTERM drains cleanly so systemd
restarts never truncate the Telegram queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal as signal_module
import sys
import time
from typing import Any

import ccxt.async_support as ccxt

from ofsignals.config import Settings, load_settings
from ofsignals.exchange.rest_client import CandleStore
from ofsignals.exchange.universe import build_universe
from ofsignals.exchange.ws_streams import MarketDataHub
from ofsignals.logging_setup import configure_logging, get_logger
from ofsignals.notify.formatter import (
    format_heartbeat,
    format_rejection_digest,
    format_update,
)
from ofsignals.notify.commands import CommandServer
from ofsignals.notify.telegram_bot import TelegramDispatcher
from ofsignals.store.db import (
    SignalStore,
    evaluate_progress,
    excursion_r,
    realised_delta,
)
from ofsignals.strategy.scanner import Scanner

log = get_logger("main")

VERSION = "1.0.0"


class StartupError(RuntimeError):
    """A misconfiguration the user can fix — reported without a stack trace."""


class Engine:
    """Owns every long-lived resource and guarantees release on shutdown."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rest: Any | None = None
        self.ws: Any | None = None
        self.telegram: TelegramDispatcher | None = None
        self.candles: CandleStore | None = None
        self.hub: MarketDataHub | None = None
        self.store: SignalStore | None = None
        self.scanner: Scanner | None = None
        self.commands: CommandServer | None = None
        self.version = VERSION
        self.started_at = time.time()
        self.scan_interval = float(
            settings.strategy.get("schedule", {}).get("scan_interval_seconds", 20))
        # Last known price per symbol, so signals on rotated-out pairs still resolve.
        self.last_prices: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._scan_now = asyncio.Event()

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        ex_cfg = self.settings.section("exchange")
        secrets = self.settings.secrets
        credentials = {
            "apiKey": secrets.binance_key or None,
            "secret": secrets.binance_secret or None,
            "enableRateLimit": True,
            "rateLimit": ex_cfg.get("rest_rate_limit_ms", 120),
            "options": {"defaultType": "swap",
                        "recvWindow": ex_cfg.get("recv_window_ms", 5000),
                        "adjustForTimeDifference": True},
        }

        self.rest = getattr(ccxt, ex_cfg["id"])(credentials)
        self.ws = self._build_ws_client(ex_cfg["id"], credentials)
        if ex_cfg.get("sandbox"):
            self.rest.set_sandbox_mode(True)

        tg_cfg = self.settings.section("telegram")
        self.telegram = TelegramDispatcher(
            token=secrets.telegram_token,
            chat_id=secrets.telegram_chat_id,
            admin_chat_id=secrets.telegram_admin_chat_id,
            max_per_minute=tg_cfg.get("max_messages_per_minute", 18),
            parse_mode=tg_cfg.get("parse_mode", "HTML"),
            retry_attempts=tg_cfg.get("retry_attempts", 4),
        )
        try:
            await self.telegram.start()
        except Exception as exc:
            raise StartupError(
                "Telegram connection failed. Check TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env, and that this machine can reach "
                "api.telegram.org."
            ) from exc

        self.store = SignalStore(self.settings.data_dir / "signals.db")
        await self.store.open()

        self.candles = CandleStore(self.rest)
        self.hub = MarketDataHub(self.ws, self.rest, self.settings.strategy)
        self.scanner = Scanner(self.settings, self.candles, self.hub,
                               self.store, self.telegram)

        await self._refresh_universe()

        if tg_cfg.get("send_startup_message", True):
            await self.telegram.send(
                f"<b>ofsignals {VERSION} online</b>\n"
                f"Universe <code>{len(self.scanner.universe)}</code> pairs · "
                f"modes <code>{', '.join(self.settings.enabled_modes)}</code>\n"
                f"Scanning every <code>{self.scan_interval:.0f}s</code> · "
                f"/help for commands\n"
                f"<i>Footprint warms up over ~12-60 min depending on mode; "
                f"signals stay suppressed until the tape is deep enough.</i>",
                admin=True,
            )

        if tg_cfg.get("commands_enabled", True):
            self.commands = CommandServer(
                token=secrets.telegram_token, engine=self,
                allowed_chat_ids={secrets.telegram_chat_id,
                                  secrets.telegram_admin_chat_id})
            try:
                await self.commands.start()
            except Exception as exc:  # noqa: BLE001 - commands are non-essential
                log.error("commands_unavailable", error=str(exc)[:160])
                self.commands = None

        self._tasks = [
            self._spawn(self._universe_loop, "universe"),
            self._spawn(self._scan_loop, "scan"),
            self._spawn(self._tracker_loop, "tracker"),
            self._spawn(self._heartbeat_loop, "heartbeat"),
            asyncio.create_task(self.hub.watchdog(), name="ws-watchdog"),
        ]

    def _build_ws_client(self, exchange_id: str, credentials: dict) -> Any:
        try:
            import ccxt.pro as ccxtpro  # noqa: PLC0415 - optional at import time
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ccxt.pro unavailable — install ccxt>=4.0 (WebSocket support is bundled)"
            ) from exc
        return getattr(ccxtpro, exchange_id)(credentials)

    def _spawn(self, coroutine_fn, name: str) -> asyncio.Task:
        async def supervised() -> None:
            attempt = 0
            while not self._stopping.is_set():
                try:
                    await coroutine_fn()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    attempt += 1
                    delay = min(300, 5 * 2 ** min(attempt, 6))
                    log.error("loop_crashed", loop=name, error=str(exc)[:200],
                              restart_in=delay, attempt=attempt)
                    await self._sleep_or_stop(delay)

        return asyncio.create_task(supervised(), name=name)

    # ------------------------------------------------------------------ loops
    async def _refresh_universe(self) -> None:
        universe = await build_universe(self.rest, self.settings.section("universe"))
        self.scanner.universe = universe
        await self.hub.sync([info.symbol for info in universe])
        log.info("universe_active", count=len(universe),
                 top=", ".join(i.display for i in universe[:8]))

    async def _universe_loop(self) -> None:
        minutes = self.settings.section("universe").get("refresh_minutes", 30)
        while not self._stopping.is_set():
            await self._sleep_or_stop(minutes * 60)
            if self._stopping.is_set():
                return
            await self._refresh_universe()

    def request_scan(self) -> None:
        """Trigger a pass immediately (used by /scan)."""
        self._scan_now.set()

    async def _scan_loop(self) -> None:
        schedule = self.settings.strategy.get("schedule", {})
        await self._sleep_or_stop(float(schedule.get("warmup_seconds", 25)))
        while not self._stopping.is_set():
            await self.scanner.scan()
            await self._wait_next_scan()

    async def _wait_next_scan(self) -> None:
        """Sleep until the next cycle, or until /scan asks for one."""
        waiters = [asyncio.create_task(self._stopping.wait()),
                   asyncio.create_task(self._scan_now.wait())]
        done, pending = await asyncio.wait(
            waiters, timeout=self.scan_interval, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        self._scan_now.clear()

    async def _tracker_loop(self) -> None:
        """Resolve open signals against live price and report transitions."""
        interval = float(
            self.settings.strategy.get("schedule", {}).get("tracker_interval_seconds", 20))
        while not self._stopping.is_set():
            await self._sleep_or_stop(interval)
            if self._stopping.is_set():
                return

            open_signals = await self.store.open_signals()
            allocation = self.settings.section("risk").get("tp_allocation")

            for open_signal in open_signals:
                price = await self._price_for(open_signal.symbol)
                if price is None:
                    continue

                mfe, mae = excursion_r(open_signal, price)
                new_status, outcome = evaluate_progress(open_signal, price)
                if new_status is None:
                    await self.store.update_status(open_signal.signal_id,
                                                   open_signal.status, mfe=mfe, mae=mae)
                    continue

                booked = realised_delta(open_signal, new_status, allocation)
                await self.store.update_status(open_signal.signal_id, new_status,
                                               outcome=outcome, mfe=mfe, mae=mae,
                                               realised_delta=booked)

                total_r = open_signal.realised_r + booked
                await self.telegram.send(
                    format_update(open_signal, new_status, price,
                                  total_r if new_status != "filled" else None))
                log.info("signal_transition", symbol=open_signal.symbol,
                         signal_id=open_signal.signal_id[:8], status=new_status,
                         price=price, realised_r=round(total_r, 2))

    async def _heartbeat_loop(self) -> None:
        minutes = self.settings.section("telegram").get("send_heartbeat_every_minutes", 60)
        while not self._stopping.is_set():
            await self._sleep_or_stop(minutes * 60)
            if self._stopping.is_set():
                return

            hub_stats = self.hub.stats()
            total = self.candles.fetches + self.candles.cache_hits
            stats = {
                "universe": len(self.scanner.universe),
                "fresh_books": hub_stats["fresh_books"],
                "evaluations": self.scanner.evaluations,
                "published": self.scanner.published,
                "published_24h": await self.store.count_since(1440),
                "open_signals": len(await self.store.open_signals()),
                "cache_hit_rate": f"{(self.candles.cache_hits / total * 100) if total else 0:.0f}%",
            }
            await self.telegram.send(format_heartbeat(stats), admin=True, silent=True)
            await self.telegram.send(
                format_rejection_digest(self.scanner.stage_histogram()),
                admin=True, silent=True)
            log.info("heartbeat", **stats)

    async def _price_for(self, symbol: str) -> float | None:
        """Live mid from the book; REST ticker if the pair left the universe."""
        state = self.hub.book_state(symbol) if self.hub else None
        if state and state.mid > 0:
            self.last_prices[symbol] = state.mid
            return state.mid
        try:
            ticker = await self.rest.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0.0)
        except Exception as exc:  # noqa: BLE001 - never let tracking die on one pair
            log.debug("price_lookup_failed", symbol=symbol, error=str(exc)[:120])
            return self.last_prices.get(symbol)
        if price > 0:
            self.last_prices[symbol] = price
            return price
        return self.last_prices.get(symbol)

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # ------------------------------------------------------------- shutdown
    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        if self.commands:
            await self.commands.stop()
        if self.hub:
            await self.hub.close()
        if self.telegram:
            uptime = (time.time() - self.started_at) / 3600
            await self.telegram.send(
                f"<i>ofsignals {VERSION} shutting down after {uptime:.1f}h "
                f"({self.scanner.published if self.scanner else 0} signals published).</i>",
                admin=True)
            await self.telegram.stop()
        if self.store:
            await self.store.close()
        for client in (self.ws, self.rest):
            if client:
                with contextlib.suppress(Exception):
                    await client.close()
        log.info("engine_stopped")


async def _run() -> int:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir)
    log.info("boot", version=VERSION, env=settings.env,
             modes=settings.enabled_modes, credentials=settings.secrets.masked())

    engine = Engine(settings)
    loop = asyncio.get_running_loop()
    stop_signal = asyncio.Event()
    for sig in (signal_module.SIGTERM, signal_module.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_signal.set)

    try:
        await engine.start()
        await stop_signal.wait()
        return 0
    except StartupError as exc:
        # Configuration problems get a readable line, not a wall of traceback.
        print(f"\n  ERROR: {exc}\n", file=sys.stderr)
        log.error("startup_failed", error=str(exc))
        return 1
    except Exception:
        log.exception("fatal_error")
        return 1
    finally:
        await engine.stop()


def main() -> None:
    try:
        import uvloop  # noqa: PLC0415 - optional accelerator
        uvloop.install()
    except ImportError:
        pass
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
