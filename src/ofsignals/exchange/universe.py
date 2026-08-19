"""Tradable-universe construction.

Agent 1's mandate: USDT-M perpetuals only, 24h quote volume above 10M USDT,
tight spreads, real book depth, no freshly listed chaos. Everything downstream
consumes the ranked list this module produces.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ofsignals.logging_setup import get_logger

log = get_logger(__name__)

_DAY_MS = 86_400_000


@dataclass(slots=True)
class SymbolInfo:
    """A single vetted instrument."""

    symbol: str                 # ccxt unified, e.g. "BTC/USDT:USDT"
    ws_symbol: str              # exchange-native lowercase, e.g. "btcusdt"
    quote_volume_24h: float
    last_price: float
    spread_bps: float
    change_pct_24h: float
    liquidity_score: float = 0.0
    depth_usd_0p5pct: float | None = None

    @property
    def display(self) -> str:
        return self.ws_symbol.upper()


def _spread_bps(bid: float | None, ask: float | None) -> float:
    if not bid or not ask or bid <= 0 or ask <= 0:
        return float("inf")
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 10_000.0


async def _measure_depth(exchange: Any, symbol: str, band_pct: float,
                         limit: int = 50) -> float:
    """Resting notional within +/- band_pct of mid. Cheap proxy for real depth."""
    try:
        book = await exchange.fetch_order_book(symbol, limit=limit)
    except Exception as exc:  # noqa: BLE001 - one bad book must not kill the scan
        log.debug("depth_probe_failed", symbol=symbol, error=str(exc))
        return 0.0

    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return 0.0

    mid = (bids[0][0] + asks[0][0]) / 2.0
    lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    notional = sum(p * q for p, q in bids if p >= lo)
    notional += sum(p * q for p, q in asks if p <= hi)
    return notional


async def build_universe(exchange: Any, cfg: dict[str, Any],
                         probe_depth: bool = True) -> list[SymbolInfo]:
    """Return the ranked, filtered instrument list."""
    started = time.perf_counter()

    markets = await exchange.load_markets(reload=True)
    tickers = await exchange.fetch_tickers()

    min_volume = float(cfg["min_quote_volume_24h"])
    max_spread = float(cfg["max_spread_bps"])
    min_age_ms = int(cfg["min_listing_age_days"]) * _DAY_MS
    blacklist = {s.upper() for s in cfg.get("blacklist", [])}
    always = {s.upper() for s in cfg.get("always_include", [])}
    now_ms = exchange.milliseconds()

    candidates: list[SymbolInfo] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for symbol, market in markets.items():
        if not (market.get("swap") and market.get("linear") and market.get("active")):
            continue
        if market.get("quote") != cfg.get("quote", "USDT"):
            continue

        native = str(market.get("id", "")).upper()
        if native in blacklist:
            reject("blacklist")
            continue

        ticker = tickers.get(symbol)
        if not ticker:
            reject("no_ticker")
            continue

        quote_volume = float(ticker.get("quoteVolume") or 0.0)
        forced = native in always

        if quote_volume < min_volume and not forced:
            reject("volume")
            continue

        listed_ms = market.get("info", {}).get("onboardDate")
        if listed_ms and not forced:
            try:
                if now_ms - int(listed_ms) < min_age_ms:
                    reject("too_new")
                    continue
            except (TypeError, ValueError):
                pass

        spread = _spread_bps(ticker.get("bid"), ticker.get("ask"))
        if spread > max_spread and not forced:
            reject("spread")
            continue

        candidates.append(
            SymbolInfo(
                symbol=symbol,
                ws_symbol=native.lower(),
                quote_volume_24h=quote_volume,
                last_price=float(ticker.get("last") or 0.0),
                spread_bps=spread,
                change_pct_24h=float(ticker.get("percentage") or 0.0),
            )
        )

    # Depth probe is REST-heavy; only run it on the volume-sorted shortlist.
    candidates.sort(key=lambda s: s.quote_volume_24h, reverse=True)
    shortlist = candidates[: int(cfg["max_tracked_symbols"]) * 2]

    if probe_depth:
        band = float(cfg.get("depth_band_pct", 0.5))
        min_depth = float(cfg["min_depth_usd_0p5pct"])
        sem = asyncio.Semaphore(8)

        async def probe(info: SymbolInfo) -> None:
            async with sem:
                info.depth_usd_0p5pct = await _measure_depth(exchange, info.symbol, band)

        await asyncio.gather(*(probe(i) for i in shortlist))
        kept = []
        for info in shortlist:
            if info.ws_symbol.upper() in always or (info.depth_usd_0p5pct or 0) >= min_depth:
                kept.append(info)
            else:
                reject("depth")
        shortlist = kept

    # Liquidity score blends turnover with book quality and tradeable range.
    for info in shortlist:
        depth_term = (info.depth_usd_0p5pct or 0.0) / max(float(cfg["min_depth_usd_0p5pct"]), 1)
        volume_term = info.quote_volume_24h / min_volume
        spread_term = max(0.1, 1.0 - info.spread_bps / max(max_spread, 0.1))
        info.liquidity_score = round(volume_term * 0.6 + depth_term * 0.25 + spread_term * 0.15, 4)

    shortlist.sort(key=lambda s: s.liquidity_score, reverse=True)
    universe = shortlist[: int(cfg["max_tracked_symbols"])]

    log.info(
        "universe_built",
        kept=len(universe),
        scanned=len(markets),
        rejected=rejected,
        elapsed_s=round(time.perf_counter() - started, 2),
    )
    return universe
