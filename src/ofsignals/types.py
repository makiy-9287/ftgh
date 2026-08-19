"""Core domain types.

Everything downstream speaks these structures. Candle data is held as numpy
arrays rather than DataFrames: the analytics are index-arithmetic heavy and the
array form makes lookahead bugs easier to see and to test for.

INVARIANT: a `Candles` object contains CLOSED bars only. The forming bar is
stripped at ingestion. Every function here may therefore treat index -1 as
"the most recent fully known bar".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Sequence

import numpy as np


# --------------------------------------------------------------------------- enums
class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1 if self is Direction.SHORT else 0

    @property
    def opposite(self) -> "Direction":
        return {Direction.LONG: Direction.SHORT,
                Direction.SHORT: Direction.LONG}.get(self, Direction.NONE)


class StructureKind(str, Enum):
    BOS = "BOS"       # continuation
    CHOCH = "CHoCH"   # regime flip


class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


class PoolSide(str, Enum):
    BUYSIDE = "buyside"    # resting stops ABOVE price (shorts' stops)
    SELLSIDE = "sellside"  # resting stops BELOW price (longs' stops)


class ZoneKind(str, Enum):
    ORDER_BLOCK = "order_block"
    FVG = "fvg"
    BREAKER = "breaker"


# --------------------------------------------------------------------------- candles
@dataclass(slots=True)
class Candles:
    """Closed-bar OHLCV series for one symbol/timeframe."""

    ts: np.ndarray      # epoch ms, int64, ascending
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    symbol: str = ""
    timeframe: str = ""

    # ------------------------------------------------------------ construction
    @classmethod
    def from_ohlcv(cls, rows: Sequence[Sequence[float]], symbol: str = "",
                   timeframe: str = "") -> "Candles":
        if not rows:
            return cls(*(np.array([], dtype=np.float64) for _ in range(6)),
                       symbol=symbol, timeframe=timeframe)
        arr = np.asarray(rows, dtype=np.float64)
        return cls(
            ts=arr[:, 0].astype(np.int64),
            open=arr[:, 1].copy(),
            high=arr[:, 2].copy(),
            low=arr[:, 3].copy(),
            close=arr[:, 4].copy(),
            volume=arr[:, 5].copy(),
            symbol=symbol,
            timeframe=timeframe,
        )

    # -------------------------------------------------------------- accessors
    def __len__(self) -> int:
        return int(self.close.size)

    def tail(self, n: int) -> "Candles":
        if n >= len(self):
            return self
        return Candles(self.ts[-n:], self.open[-n:], self.high[-n:], self.low[-n:],
                       self.close[-n:], self.volume[-n:], self.symbol, self.timeframe)

    def head(self, n: int) -> "Candles":
        return Candles(self.ts[:n], self.open[:n], self.high[:n], self.low[:n],
                       self.close[:n], self.volume[:n], self.symbol, self.timeframe)

    @property
    def range(self) -> np.ndarray:
        return self.high - self.low

    @property
    def body(self) -> np.ndarray:
        return np.abs(self.close - self.open)

    @property
    def bullish(self) -> np.ndarray:
        return self.close > self.open

    @property
    def last_price(self) -> float:
        return float(self.close[-1]) if len(self) else float("nan")

    @property
    def last_ts(self) -> int:
        return int(self.ts[-1]) if len(self) else 0

    def dt(self, index: int) -> datetime:
        return datetime.fromtimestamp(int(self.ts[index]) / 1000, tz=timezone.utc)


# --------------------------------------------------------------------- structure
@dataclass(slots=True, frozen=True)
class Swing:
    index: int
    price: float
    kind: SwingKind
    confirmed_index: int   # bar at which this pivot became knowable (index + N)


@dataclass(slots=True, frozen=True)
class StructureEvent:
    index: int
    kind: StructureKind
    direction: Direction   # direction of the break
    level: float           # the swing price that was broken


@dataclass(slots=True, frozen=True)
class DealingRange:
    low: float
    high: float
    direction: Direction   # direction of the displacement leg that formed it

    @property
    def equilibrium(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def size(self) -> float:
        return max(self.high - self.low, 1e-12)

    def position_of(self, price: float) -> float:
        """0.0 = range low, 1.0 = range high."""
        return (price - self.low) / self.size

    def is_discount(self, price: float, eq: float = 0.5) -> bool:
        return self.position_of(price) < eq

    def is_premium(self, price: float, eq: float = 0.5) -> bool:
        return self.position_of(price) > eq


@dataclass(slots=True, frozen=True)
class Displacement:
    index: int
    direction: Direction
    origin_index: int
    low: float
    high: float
    atr_multiple: float


@dataclass(slots=True, frozen=True)
class Bias:
    direction: Direction
    dealing_range: DealingRange | None
    last_event: StructureEvent | None
    reason: str

    @property
    def tradeable(self) -> bool:
        return self.direction is not Direction.NONE and self.dealing_range is not None


# --------------------------------------------------------------------- liquidity
@dataclass(slots=True)
class Pool:
    price: float
    side: PoolSide
    touches: int
    source: str                 # "eqh" | "eql" | "pdh" | "pdl" | "pwh" | "pwl" | "session"
    last_index: int = -1
    swept: bool = False
    swept_index: int = -1
    rank: float = 0.0


@dataclass(slots=True, frozen=True)
class Sweep:
    pool: Pool
    index: int                  # bar that pierced the pool
    reclaim_index: int          # bar that closed back inside
    penetration_atr: float
    volume_multiple: float
    extreme: float              # wick extreme of the sweep -> stop anchor
    direction: Direction        # direction of the resulting setup


# -------------------------------------------------------------------------- zones
@dataclass(slots=True)
class Zone:
    kind: ZoneKind
    direction: Direction        # direction it is expected to support
    top: float
    bottom: float
    index: int
    mitigated: bool = False
    invalidated: bool = False
    fresh: bool = True

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return max(self.top - self.bottom, 1e-12)

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def entry_at(self, depth: float) -> float:
        """depth 0.0 = far edge (conservative), 1.0 = near edge (aggressive)."""
        if self.direction is Direction.LONG:
            return self.bottom + self.height * depth
        return self.top - self.height * depth


# ---------------------------------------------------------------- volume profile
@dataclass(slots=True, frozen=True)
class VolumeProfile:
    poc: float
    vah: float
    val: float
    lvn_prices: tuple[float, ...]
    hvn_prices: tuple[float, ...]
    bin_edges: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    bin_volume: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))

    def in_value_area(self, price: float) -> bool:
        return self.val <= price <= self.vah

    def nearest_lvn(self, price: float, direction: Direction) -> float | None:
        if not self.lvn_prices:
            return None
        if direction is Direction.LONG:
            below = [p for p in self.lvn_prices if p < price]
            return max(below) if below else None
        above = [p for p in self.lvn_prices if p > price]
        return min(above) if above else None


# ------------------------------------------------------------------- order flow
@dataclass(slots=True, frozen=True)
class FlowSnapshot:
    """Verdicts from the footprint layer. `ready` gates all of them."""

    ready: bool
    cvd: float
    bar_delta: float
    cvd_divergence: bool
    absorption: bool
    delta_flip: bool
    delta_extreme: bool
    note: str = ""

    @property
    def confirmations(self) -> int:
        return sum((self.cvd_divergence, self.absorption,
                    self.delta_flip and self.delta_extreme))


@dataclass(slots=True, frozen=True)
class BookSnapshot:
    ready: bool
    mid: float
    spread_bps: float
    bid_notional: float
    ask_notional: float
    imbalance_ratio: float      # >1 favours bids
    dominant: Direction
    wall_price: float | None
    wall_resting_s: float
    age_s: float

    @property
    def stale(self) -> bool:
        return not self.ready or self.age_s > 15.0


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def first(iterable: Iterable, default=None):
    for item in iterable:
        return item
    return default
