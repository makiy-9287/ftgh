"""End-to-end environment validation. Run this before enabling the systemd unit.

    python -m ofsignals.tools.preflight
"""

from __future__ import annotations

import asyncio
import time

import ccxt.async_support as ccxt

from ofsignals.config import load_settings
from ofsignals.exchange.universe import build_universe
from ofsignals.logging_setup import configure_logging, get_logger
from ofsignals.notify.telegram_bot import TelegramDispatcher

log = get_logger("preflight")

OK, FAIL = "\033[92m PASS \033[0m", "\033[91m FAIL \033[0m"


async def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir)
    failures = 0

    print(f"\n  environment : {settings.env}")
    print(f"  credentials : {settings.secrets.masked()}\n")

    # 1) Clock drift ------------------------------------------------------
    exchange = getattr(ccxt, settings.section("exchange")["id"])({
        "apiKey": settings.secrets.binance_key or None,
        "secret": settings.secrets.binance_secret or None,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })

    try:
        t0 = time.time() * 1000
        server_ms = await exchange.fetch_time()
        drift = abs(server_ms - t0)
        status = OK if drift < 1000 else FAIL
        failures += status is FAIL
        print(f"[{status}] clock drift vs exchange: {drift:.0f} ms")

        # 2) Market data --------------------------------------------------
        ticker = await exchange.fetch_ticker("BTC/USDT:USDT")
        print(f"[{OK}] REST market data: BTCUSDT last = {ticker['last']}")

        # 3) Universe filter ----------------------------------------------
        started = time.perf_counter()
        universe = await build_universe(exchange, settings.section("universe"))
        elapsed = time.perf_counter() - started
        status = OK if universe else FAIL
        failures += status is FAIL
        print(f"[{status}] universe scan: {len(universe)} pairs in {elapsed:.1f}s")
        for info in universe[:10]:
            print(f"         {info.display:<14} vol={info.quote_volume_24h/1e6:>8.1f}M "
                  f"spread={info.spread_bps:>4.1f}bps score={info.liquidity_score:.2f}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[{FAIL}] exchange checks: {exc}")
    finally:
        await exchange.close()

    # 4) Telegram ---------------------------------------------------------
    try:
        tg_cfg = settings.section("telegram")
        dispatcher = TelegramDispatcher(
            token=settings.secrets.telegram_token,
            chat_id=settings.secrets.telegram_chat_id,
            admin_chat_id=settings.secrets.telegram_admin_chat_id,
            max_per_minute=tg_cfg.get("max_messages_per_minute", 18),
        )
        await dispatcher.start()
        await dispatcher.send("<b>preflight</b> — pipeline reachable ✅", admin=True)
        await dispatcher.stop()
        print(f"[{OK}] telegram delivery")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[{FAIL}] telegram delivery: {exc}")

    print(f"\n  {'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
