"""Tradable-universe construction.

Agent 1's mandate: USDT-M perpetuals only, 24h quote volume above 10M USDT,
tight spreads, real book depth, no freshly listed chaos. Everything downstream
consumes the ranked list this module produces.
"""

from __future__ import annotations

import asyncio
import math
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


async def _probe_book(exchange: Any, symbol: str, band_pct: float,
                      limit: int = 500) -> tuple[float, float]:
    """Return (spread_bps, resting notional within +/- band_pct of mid).

    A single fetch gives both numbers. The previous limit of 50 levels was the
    quiet killer here: on most futures pairs 50 levels span far less than 0.5%
    of price, so measured depth was a fraction of real depth and almost
    everything failed the threshold.
    """
    try:
        book = await exchange.fetch_order_book(symbol, limit=limit)
    except Exception as exc:  # noqa: BLE001 - one bad book must not kill the scan
        log.debug("book_probe_failed", symbol=symbol, error=str(exc)[:140])
        return float("inf"), 0.0

    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return float("inf"), 0.0

    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return float("inf"), 0.0

    spread_bps = (best_ask - best_bid) / mid * 10_000.0
    lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    notional = sum(float(p) * float(q) for p, q in bids if float(p) >= lo)
    notional += sum(float(p) * float(q) for p, q in asks if float(p) <= hi)
    return spread_bps, notional


async def build_universe(exchange: Any, cfg: dict[str, Any],
                         probe_depth: bool = True) -> list[SymbolInfo]:
    """Return the ranked, filtered instrument list.

    ORDER MATTERS. Cheap metadata filters (volume, age, blacklist) run first
    against a single bulk ticker call. Spread and depth need real order books,
    so they are measured once, together, on the volume-ranked shortlist.

    Spread is deliberately NOT taken from `fetch_tickers`: Binance's futures
    24h ticker endpoint carries no bid/ask, so every symbol reported an
    infinite spread and was rejected. Only force-included pairs survived, which
    is why a 200-pair universe collapsed to BTC and ETH.
    """
    started = time.perf_counter()

    markets = await exchange.load_markets(reload=True)
    tickers = await exchange.fetch_tickers()

    min_volume = float(cfg["min_quote_volume_24h"])
    max_spread = float(cfg["max_spread_bps"])
    min_age_ms = int(cfg["min_listing_age_days"]) * _DAY_MS
    blacklist = {s.upper() for s in cfg.get("blacklist", [])}
    always = {s.upper() for s in cfg.get("always_include", [])}
    max_tracked = int(cfg["max_tracked_symbols"])
    now_ms = exchange.milliseconds()

    candidates: list[SymbolInfo] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    # ---- stage 1: metadata only, no extra network calls -------------------
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

        candidates.append(
            SymbolInfo(
                symbol=symbol,
                ws_symbol=native.lower(),
                quote_volume_24h=quote_volume,
                last_price=float(ticker.get("last") or 0.0),
                spread_bps=float("nan"),      # measured from the book below
                change_pct_24h=float(ticker.get("percentage") or 0.0),
            )
        )

    candidates.sort(key=lambda s: s.quote_volume_24h, reverse=True)
    passed_metadata = len(candidates)
    shortlist = candidates[: max_tracked * 3]

    # ---- stage 2: one book probe per shortlisted symbol -------------------
    if probe_depth and shortlist:
        band = float(cfg.get("depth_band_pct", 0.5))
        limit = int(cfg.get("depth_probe_limit", 500))
        min_depth = float(cfg["min_depth_usd_0p5pct"])
        semaphore = asyncio.Semaphore(8)

        async def probe(info: SymbolInfo) -> None:
            async with semaphore:
                spread, depth = await _probe_book(exchange, info.symbol, band, limit)
                info.spread_bps = spread
                info.depth_usd_0p5pct = depth

        await asyncio.gather(*(probe(i) for i in shortlist))

        kept: list[SymbolInfo] = []
        for info in shortlist:
            if info.ws_symbol.upper() in always:
                kept.append(info)
                continue
            if not math.isfinite(info.spread_bps):
                reject("no_book")
                continue
            if info.spread_bps > max_spread:
                reject("spread")
                continue
            if (info.depth_usd_0p5pct or 0.0) < min_depth:
                reject("depth")
                continue
            kept.append(info)
        shortlist = kept
    else:
        for info in shortlist:
            info.spread_bps = 0.0

    # ---- stage 3: rank ----------------------------------------------------
    min_depth_ref = max(float(cfg["min_depth_usd_0p5pct"]), 1.0)
    for info in shortlist:
        if not math.isfinite(info.spread_bps):
            info.spread_bps = max_spread
        depth_term = (info.depth_usd_0p5pct or 0.0) / min_depth_ref
        volume_term = info.quote_volume_24h / min_volume
        spread_term = max(0.1, 1.0 - info.spread_bps / max(max_spread, 0.1))
        info.liquidity_score = round(
            volume_term * 0.6 + depth_term * 0.25 + spread_term * 0.15, 4)

    shortlist.sort(key=lambda s: s.liquidity_score, reverse=True)
    universe = shortlist[:max_tracked]

    stats = {
        "kept": len(universe),
        "passed_volume_filter": passed_metadata,
        "probed": min(passed_metadata, max_tracked * 3),
        "rejected": rejected,
    }
    log.info("universe_built", elapsed_s=round(time.perf_counter() - started, 2), **stats)
    build_universe.last_stats = stats        # read by /watchlist
    return universe


build_universe.last_stats = {}
