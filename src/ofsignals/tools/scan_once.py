"""One-shot live scan with full diagnostics — no Telegram, no database writes.

    python -m ofsignals.tools.scan_once --symbols BTC/USDT:USDT,ETH/USDT:USDT
    python -m ofsignals.tools.scan_once --mode intraday --top 15 --warmup 120

This is the tool you actually use to understand the engine. It prints WHERE each
symbol died in the cascade, which is the difference between "the bot doesn't
work" and "nothing has swept a pool in the last hour, which is correct".
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections import Counter

import ccxt.async_support as ccxt

from ofsignals.config import load_settings
from ofsignals.exchange.rest_client import CandleStore
from ofsignals.exchange.universe import build_universe
from ofsignals.exchange.ws_streams import MarketDataHub
from ofsignals.logging_setup import configure_logging
from ofsignals.notify.formatter import format_signal
from ofsignals.strategy.intraday import IntradayEngine
from ofsignals.strategy.scalp import ScalpEngine
from ofsignals.strategy.swing import SwingEngine

ENGINES = {"scalp": ScalpEngine, "intraday": IntradayEngine, "swing": SwingEngine}
STRIP = str.maketrans({"<": "", ">": ""})


def plain(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run one cascade pass with diagnostics")
    parser.add_argument("--mode", default="scalp", choices=list(ENGINES))
    parser.add_argument("--symbols", default="", help="comma-separated, else use the universe")
    parser.add_argument("--top", type=int, default=10, help="universe symbols to scan")
    parser.add_argument("--warmup", type=int, default=90,
                        help="seconds to fill books/tape before evaluating")
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir)

    ex_cfg = settings.section("exchange")
    credentials = {
        "apiKey": settings.secrets.binance_key or None,
        "secret": settings.secrets.binance_secret or None,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }
    rest = getattr(ccxt, ex_cfg["id"])(credentials)

    import ccxt.pro as ccxtpro
    ws = getattr(ccxtpro, ex_cfg["id"])(credentials)

    hub = MarketDataHub(ws, rest, settings.strategy)
    store = CandleStore(rest)
    engine = ENGINES[args.mode](settings, store, hub)

    try:
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        else:
            universe = await build_universe(rest, settings.section("universe"))
            symbols = [info.symbol for info in universe[: args.top]]

        print(f"\n  mode={args.mode}  symbols={len(symbols)}")
        print(f"  filling order books and trade tape for {args.warmup}s "
              f"(footprint needs history — this is not optional)…\n")

        await hub.sync(symbols)
        await asyncio.sleep(args.warmup)

        stages: Counter[str] = Counter()
        published = 0

        for symbol in symbols:
            fresh = "fresh" if hub.is_fresh(symbol) else "STALE"
            tape = hub.tape(symbol)
            prints = len(tape) if tape else 0
            result = await engine.evaluate(symbol)
            stages[result.stage] += 1

            flag = "✓" if result.passed else " "
            print(f"{flag} {symbol:<22} book={fresh:<6} prints={prints:<6} "
                  f"{result.stage:<10} {result.reason[:78]}")

            if result.passed:
                published += 1
                print("\n" + plain(format_signal(result.signal)) + "\n")

        print("\n  stage histogram:")
        for stage, count in stages.most_common():
            print(f"    {count:>4}  {stage}")
        print(f"\n  {published} signal(s) would have been published "
              f"(portfolio filters not applied here)\n")
        return 0
    finally:
        await hub.close()
        for client in (ws, rest):
            with contextlib.suppress(Exception):
                await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
