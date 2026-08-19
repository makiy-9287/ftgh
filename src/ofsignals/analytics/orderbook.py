"""Order book: depth imbalance and wall persistence.

ANTI-SPOOF
Raw depth is adversarial data. A large resting order that vanishes before price
touches it was never liquidity — it was an advertisement. `BookState` timestamps
every price level on first sight and a wall only counts once it has survived
`wall_min_resting_s`. Levels that disappear are forgotten immediately, so a
flickering order can never accumulate age.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ofsignals.types import BookSnapshot, Direction


@dataclass(slots=True)
class _Level:
    notional: float
    first_seen: float


@dataclass(slots=True)
class BookState:
    """Rolling view of one symbol's book, updated on every depth event."""

    symbol: str
    updated_at: float = 0.0
    mid: float = 0.0
    spread_bps: float = float("inf")
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    _tracked: dict[tuple[str, float], _Level] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- ingest
    def update(self, book: dict[str, Any], depth_levels: int = 20) -> None:
        bids = [(float(p), float(q)) for p, q in (book.get("bids") or [])[:depth_levels]]
        asks = [(float(p), float(q)) for p, q in (book.get("asks") or [])[:depth_levels]]
        if not bids or not asks:
            return

        self.bids, self.asks = bids, asks
        best_bid, best_ask = bids[0][0], asks[0][0]
        self.mid = (best_bid + best_ask) / 2.0
        self.spread_bps = ((best_ask - best_bid) / self.mid * 10_000.0) if self.mid > 0 else float("inf")
        self.updated_at = time.monotonic()
        self._track_levels(bids, asks)

    def _track_levels(self, bids: list[tuple[float, float]],
                      asks: list[tuple[float, float]]) -> None:
        now = time.monotonic()
        seen: set[tuple[str, float]] = set()

        for side, levels in (("bid", bids), ("ask", asks)):
            for price, quantity in levels:
                key = (side, round(price, 10))
                seen.add(key)
                notional = price * quantity
                existing = self._tracked.get(key)
                if existing is None:
                    self._tracked[key] = _Level(notional, now)
                else:
                    # A level that shrinks by more than half is a different order.
                    if notional < existing.notional * 0.5:
                        existing.first_seen = now
                    existing.notional = notional

        # Forget anything that left the book — pulled orders earn no age credit.
        for key in [k for k in self._tracked if k not in seen]:
            del self._tracked[key]

    # -------------------------------------------------------------- analysis
    def snapshot(self, cfg: dict, direction: Direction | None = None) -> BookSnapshot:
        age = time.monotonic() - self.updated_at if self.updated_at else 1e9
        if not self.bids or not self.asks or self.mid <= 0:
            return BookSnapshot(False, 0.0, float("inf"), 0.0, 0.0, 1.0,
                                Direction.NONE, None, 0.0, age)

        band_pct = float(cfg.get("band_pct", 0.30)) / 100.0
        low, high = self.mid * (1 - band_pct), self.mid * (1 + band_pct)

        bid_notional = sum(p * q for p, q in self.bids if p >= low)
        ask_notional = sum(p * q for p, q in self.asks if p <= high)
        ratio = bid_notional / ask_notional if ask_notional > 0 else float("inf")

        if ratio >= 1.0:
            dominant = Direction.LONG
        else:
            dominant = Direction.SHORT
            ratio = (ask_notional / bid_notional) if bid_notional > 0 else float("inf")

        wall_price, wall_age = self._dominant_wall(cfg, dominant)

        return BookSnapshot(
            ready=True,
            mid=self.mid,
            spread_bps=self.spread_bps,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            imbalance_ratio=float(ratio),
            dominant=dominant,
            wall_price=wall_price,
            wall_resting_s=wall_age,
            age_s=age,
        )

    def _dominant_wall(self, cfg: dict, dominant: Direction) -> tuple[float | None, float]:
        side = "bid" if dominant is Direction.LONG else "ask"
        levels = self.bids if dominant is Direction.LONG else self.asks
        if not levels:
            return None, 0.0

        notionals = np.array([p * q for p, q in levels])
        median = float(np.median(notionals)) if notionals.size else 0.0
        if median <= 0:
            return None, 0.0

        threshold = median * float(cfg.get("wall_notional_mult", 4.0))
        now = time.monotonic()
        best_price: float | None = None
        best_age = 0.0

        for price, quantity in levels:
            if price * quantity < threshold:
                continue
            tracked = self._tracked.get((side, round(price, 10)))
            age = (now - tracked.first_seen) if tracked else 0.0
            if age > best_age:
                best_price, best_age = price, age

        return best_price, best_age


def dom_confirms(snapshot: BookSnapshot, direction: Direction, cfg: dict) -> tuple[bool, int]:
    """Returns (passes_L3_check, score_points_out_of_10)."""
    if snapshot.stale or snapshot.dominant is not direction:
        return False, 0

    required_ratio = float(cfg.get("imbalance_ratio", 3.0))
    min_resting = float(cfg.get("book", {}).get("wall_min_resting_s", 8.0))
    persistent = snapshot.wall_resting_s >= min_resting

    if snapshot.imbalance_ratio >= required_ratio and persistent:
        return True, 10
    if snapshot.imbalance_ratio >= max(2.0, required_ratio * 0.66) and persistent:
        return False, 5
    return False, 0
