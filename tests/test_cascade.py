"""End-to-end cascade test.

Builds a synthetic market that satisfies every layer of the specification, then
asserts the engine actually publishes — and, just as importantly, asserts it
REFUSES when a single required condition is removed. A strategy engine that
only ever says no is indistinguishable from a broken one, so both directions
have to be pinned down.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ofsignals.analytics.footprint import TradeTape
from ofsignals.analytics.orderbook import BookState
from ofsignals.strategy.mtf import CascadeEngine
from ofsignals.types import Candles, Direction

REPO_ROOT = Path(__file__).resolve().parents[1]
STEP_MS = {"1h": 3_600_000, "15m": 900_000, "5m": 300_000, "3m": 180_000}


# ------------------------------------------------------------------ fixtures
class FakeSettings:
    """Loads the real config.yaml so the test exercises shipped parameters."""

    def __init__(self) -> None:
        with (REPO_ROOT / "config.yaml").open() as fh:
            self.strategy = yaml.safe_load(fh)

    def section(self, name):
        return self.strategy[name]

    def mode(self, name):
        return self.strategy["modes"][name]

    @property
    def enabled_modes(self):
        return [k for k, v in self.strategy["modes"].items() if v.get("enabled")]


class FakeCandleStore:
    def __init__(self, series: dict[str, Candles]) -> None:
        self.series = series

    async def get(self, symbol, timeframe, limit=None, force=False):
        return self.series[timeframe]

    async def get_many(self, symbol, timeframes, limit=None):
        return {role: self.series[tf] for role, tf in timeframes.items()}


class FakeHub:
    def __init__(self, book: BookState, tape: TradeTape) -> None:
        self._book, self._tape = book, tape

    def book_state(self, symbol):
        return self._book

    def tape(self, symbol):
        return self._tape

    def is_fresh(self, symbol):
        return True


# -------------------------------------------------------------- market builder
def build_bullish_market(timeframe: str) -> Candles:
    """A market that: leaves buy-side liquidity high, trends down into a base,
    sweeps equal lows, reclaims, and displaces up leaving a fresh order block.

    Geometry is deliberately tight (0.1-0.8% moves) so the structural stop lands
    inside the scalp mode's 1.2% ceiling — exactly like a real 15m setup.
    """
    step = STEP_MS[timeframe]
    start = 1_700_000_000_000
    rows: list[list[float]] = []

    def bar(o, h, l, c, v=900.0):
        rows.append([start + len(rows) * step, o, h, l, c, v])

    # 1. Two equal highs at 101.20 -> unswept buy-side pool (the TP anchor).
    for block in range(2):
        for _ in range(4):
            bar(101.00, 101.05, 100.95, 101.02)
        bar(101.02, 101.20, 100.98, 101.00, 1300.0)      # the pivot high
    for _ in range(4):
        bar(101.00, 101.05, 100.95, 101.02)

    # 2. Downtrend into the base: lower highs, lower lows.
    price = 101.00
    for i in range(150):
        price -= 0.0063
        wiggle = 0.02 if i % 2 else -0.02
        o, c = price + wiggle, price - wiggle
        bar(o, max(o, c) + 0.03, min(o, c) - 0.03, c)

    # 3. Base with two EQUAL LOWS at 99.90 -> the sell-side pool to be swept.
    for _ in range(2):
        bar(100.00, 100.06, 99.97, 100.02)
        bar(100.02, 100.03, 99.98, 100.00)
        bar(100.00, 100.02, 99.90, 100.00, 1400.0)       # equal low
        bar(100.00, 100.06, 99.97, 100.02)
    for _ in range(3):
        bar(100.00, 100.06, 99.97, 100.02)
        bar(100.02, 100.03, 99.98, 100.00)

    # 4. THE SWEEP: wick through 99.90 on 9x volume, close back above. Body is
    #    tiny, so this is a raid rather than a breakout.
    bar(100.00, 100.04, 99.86, 100.02, 9500.0)

    # 5. The order block, then displacement up.
    bar(100.02, 100.06, 99.99, 100.03, 1200.0)
    bar(100.03, 100.07, 99.94, 99.96, 1500.0)            # bearish OB
    bar(99.96, 100.72, 99.95, 100.65, 12000.0)           # displacement

    # 6. Gentle retrace back toward the block (each bar under the ATR threshold,
    #    so none of them registers as an opposing displacement).
    bar(100.65, 100.68, 100.50, 100.55, 4000.0)
    bar(100.55, 100.58, 100.40, 100.45, 2600.0)
    bar(100.45, 100.48, 100.30, 100.35, 2200.0)
    bar(100.35, 100.38, 100.20, 100.25, 1900.0)
    bar(100.25, 100.28, 100.10, 100.15, 1700.0)

    return Candles.from_ohlcv(rows, "TEST/USDT:USDT", timeframe)


def build_tape(bucket_ms: int) -> TradeTape:
    """Tape where price makes a LOWER low while CVD makes a HIGHER low, with an
    absorption print inside the order block."""
    tape = TradeTape("TEST/USDT:USDT")
    origin = 1_700_000_000_000
    trades: list[dict] = []

    def prints(index, price, buy, sell, spread=0.04):
        ts = origin + index * bucket_ms + 1000
        trades.append({"timestamp": ts, "price": price, "amount": buy,
                       "info": {"m": False}})            # m=False -> taker bought
        trades.append({"timestamp": ts + 10, "price": price - spread, "amount": sell,
                       "info": {"m": True}})             # m=True  -> taker sold

    for i in range(22):                                  # decline, sellers dominant
        prints(i, 100.30 - i * 0.009, 5.0, 15.0)
    for i in (22, 23):                                   # first low
        prints(i, 100.05, 5.0, 15.0)
    for i in (24, 25):                                   # weak bounce
        prints(i, 100.15, 25.0, 5.0)
    for i in (26, 27):                                   # LOWER low, absorbed
        prints(i, 100.02, 100.0, 8.0, spread=0.06)
    for i in (28, 29):                                   # delta flips and holds
        prints(i, 100.20, 60.0, 10.0)

    tape.add_many(trades)
    return tape


def build_book(bid_heavy: bool = True) -> BookState:
    state = BookState("TEST/USDT:USDT")
    mid = 103.0
    bids = [[mid - 0.01 * (i + 1), 900.0 if (bid_heavy and i == 1) else 40.0]
            for i in range(20)]
    asks = [[mid + 0.01 * (i + 1), 40.0] for i in range(20)]
    state.update({"bids": bids, "asks": asks})
    # Age the tracked levels past the anti-spoof persistence threshold.
    for level in state._tracked.values():      # noqa: SLF001 - deliberate test reach-in
        level.first_seen -= 30.0
    return state


def make_engine(series_overrides: dict | None = None, bid_heavy: bool = True) -> CascadeEngine:
    settings = FakeSettings()
    tf = settings.mode("scalp")["timeframes"]
    series = {timeframe: build_bullish_market(timeframe) for timeframe in tf.values()}
    if series_overrides:
        series.update(series_overrides)
    tape = build_tape(STEP_MS[tf["confirm"]])
    hub = FakeHub(build_book(bid_heavy), tape)
    return CascadeEngine("scalp", settings, FakeCandleStore(series), hub)


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    """Freeze time to a Tuesday inside the London-open window.

    Session timing and the weekend gate both read the wall clock, so without
    pinning, this suite would pass on a Tuesday and fail on a Sunday.
    """
    fixed = datetime(2026, 3, 10, 8, 15, tzinfo=timezone.utc)
    monkeypatch.setattr("ofsignals.strategy.base.utcnow", lambda: fixed)
    monkeypatch.setattr("ofsignals.strategy.mtf.utcnow", lambda: fixed)
    # The store stamps cooldowns and expiries from the same clock; leaving it on
    # real time would put every generated signal months in the past.
    monkeypatch.setattr("ofsignals.store.db.utcnow", lambda: fixed)
    return fixed


# ----------------------------------------------------------------------- tests
def test_cascade_publishes_on_a_textbook_setup():
    engine = make_engine()
    result = asyncio.run(engine.evaluate("TEST/USDT:USDT"))

    assert result.passed, f"expected a signal, rejected at {result.stage}: {result.reason}"
    signal = result.signal

    assert signal.direction is Direction.LONG
    assert signal.mode == "scalp"
    assert signal.stop_loss < min(signal.entry_range)
    assert signal.targets["tp1"] > max(signal.entry_range)
    assert signal.targets["tp3"] > signal.targets["tp2"] > signal.targets["tp1"]
    assert signal.rr["tp2"] >= 2.0
    assert 1 <= signal.leverage["recommended"] <= 20
    assert signal.confluence_score >= 72
    assert sum(signal.score_breakdown.values()) == signal.confluence_score
    assert signal.sl_distance_pct <= 1.2


SWEEP_WICK = 99.86      # the low printed by the raid bar in build_bullish_market


def test_stop_sits_below_the_sweep_wick():
    """The stop must be structural — beyond the wick that created the setup."""
    engine = make_engine()
    result = asyncio.run(engine.evaluate("TEST/USDT:USDT"))
    assert result.passed
    assert result.signal.stop_loss < SWEEP_WICK
    # ...and not absurdly far beyond it: the buffer is ATR/bps-scaled, not arbitrary.
    assert SWEEP_WICK - result.signal.stop_loss < 0.30


def test_leverage_respects_the_liquidation_boundary():
    engine = make_engine()
    result = asyncio.run(engine.evaluate("TEST/USDT:USDT"))
    assert result.passed
    signal = result.signal
    implied = 0.40 / (signal.sl_distance_pct / 100.0)
    assert signal.leverage["recommended"] <= implied + 1e-6
    assert signal.leverage["margin_mode"] == "isolated"


def test_rejects_when_the_book_contradicts_and_flow_is_thin():
    """Removing DOM support should cost the setup its second confirmation."""
    engine = make_engine(bid_heavy=False)
    engine.hub._book.update({                       # noqa: SLF001
        "bids": [[102.99 - 0.01 * i, 40.0] for i in range(20)],
        "asks": [[103.01 + 0.01 * i, 900.0 if i == 1 else 40.0] for i in range(20)],
    })
    result = asyncio.run(engine.evaluate("TEST/USDT:USDT"))
    # Either it fails L3 outright, or it survives on flow alone with a lower score.
    if result.passed:
        assert result.signal.score_breakdown["dom"] == 0
    else:
        assert result.stage in ("L3", "score")


def test_rejects_a_flat_market():
    flat_rows = [[1_700_000_000_000 + i * 900_000, 100, 100.4, 99.6, 100, 800]
                 for i in range(220)]
    flat = Candles.from_ohlcv(flat_rows, "TEST/USDT:USDT", "15m")
    engine = make_engine(series_overrides={tf: flat for tf in STEP_MS})
    result = asyncio.run(engine.evaluate("TEST/USDT:USDT"))
    assert not result.passed
    assert result.stage in ("L1", "L2")


def test_rejects_when_the_tape_is_cold():
    """No footprint data must mean no signal — never invented flow."""
    engine = make_engine()
    engine.hub._tape = TradeTape("TEST/USDT:USDT")   # noqa: SLF001
    result = asyncio.run(engine.evaluate("TEST/USDT:USDT"))
    assert not result.passed
    assert result.stage == "L3"


def test_rejects_when_the_depth_stream_is_stale():
    engine = make_engine()
    engine.hub._book.updated_at -= 120.0             # noqa: SLF001
    result = asyncio.run(engine.evaluate("TEST/USDT:USDT"))
    assert not result.passed
    assert result.stage == "L3"
    assert "stale" in result.reason


@pytest.mark.asyncio
async def test_signal_store_roundtrip_and_dedupe(tmp_path):
    from ofsignals.store.db import SignalStore, evaluate_progress

    engine = make_engine()
    result = await engine.evaluate("TEST/USDT:USDT")
    assert result.passed

    store = SignalStore(tmp_path / "signals.db")
    await store.open()
    try:
        await store.insert(result.signal)
        assert await store.in_cooldown("TEST/USDT:USDT", "scalp", 30) is True
        assert await store.in_cooldown("OTHER/USDT:USDT", "scalp", 30) is False
        assert await store.count_since(60) == 1

        open_signals = await store.open_signals()
        assert len(open_signals) == 1

        live = open_signals[0]
        status, _ = evaluate_progress(live, (live.entry_high + live.entry_low) / 2)
        assert status == "filled"

        live.status = "filled"
        status, outcome = evaluate_progress(live, live.stop_loss - 1)
        assert status == "stopped" and outcome == "stopped"
    finally:
        await store.close()


# --------------------------------------------------- regression: the blockers
def test_fresh_sweep_is_not_disqualified_by_mark_swept():
    """The bug that blocked live signals: flagging pools before detection made
    the engine blind to the freshest raids — the ones that matter most."""
    from ofsignals.analytics import liquidity as liq

    settings = FakeSettings()
    L, S = settings.section("liquidity"), settings.section("structure")
    candles = build_bullish_market("15m")
    truncated = candles.head(len(candles) - 7)      # raid sits near the end

    pools = liq.build_pools(truncated, L, S)
    sweep = liq.detect_sweep(truncated, pools, L, S)
    assert sweep is not None, "a fresh sweep must still be detectable"

    # Flagging afterwards, skipping the detection window, must not erase it.
    liq.mark_swept(pools, truncated, lookback=200,
                   skip_recent=int(L.get("sweep_scan_bars", 15)))
    assert liq.detect_sweep(truncated, pools, L, S) is not None


def test_tape_survives_high_trade_rates():
    """Old ring buffer held ~8 min on BTC; buckets must hold hours."""
    tape = TradeTape("BTC/USDT:USDT")
    base = 1_700_000_000_000
    trades = [{"timestamp": base + i * 100, "price": 100.0 + (i % 7) * 0.01,
               "amount": 0.5, "side": "buy" if i % 2 else "sell"}
              for i in range(120_000)]              # 200 min at 10 trades/s
    tape.add_many(trades)

    bars = tape.bars(60_000, count=300)
    assert len(bars) > 150, f"expected hours of history, got {len(bars)} buckets"
    assert len(tape) <= 720


def test_realised_r_ladder_books_partials():
    from ofsignals.store.db import OpenSignal, realised_delta

    sig = OpenSignal("id", "X/USDT:USDT", "scalp", Direction.LONG,
                     101.0, 100.0, 99.0, 102.0, 104.0, 108.0, "filled",
                     "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 0.0, 0.0,
                     rr1=1.0, rr2=3.0, rr3=7.0)
    alloc = {"tp1": 0.40, "tp2": 0.35, "tp3": 0.25}
    assert realised_delta(sig, "tp1", alloc) == pytest.approx(0.40)
    assert realised_delta(sig, "tp2", alloc) == pytest.approx(1.05)
    assert realised_delta(sig, "tp3", alloc) == pytest.approx(1.75)
    assert realised_delta(sig, "stopped", alloc) == pytest.approx(-1.0)
    assert realised_delta(sig, "closed_be", alloc) == 0.0
