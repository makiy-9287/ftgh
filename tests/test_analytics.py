"""Unit tests for the analytics core.

Every test builds a hand-constructed price series with a known answer. That is
the only way to catch off-by-one and lookahead errors — a test that asserts
"returns a list" proves nothing about a strategy.
"""

from __future__ import annotations

import numpy as np
import pytest

from ofsignals.analytics.footprint import TradeTape, analyse_flow, cumulative_delta
from ofsignals.analytics.liquidity import build_pools, detect_sweep
from ofsignals.analytics.structure import (
    atr,
    compute_bias,
    detect_displacements,
    detect_structure_events,
    detect_swings,
)
from ofsignals.analytics.volume_profile import build_profile
from ofsignals.analytics.zones import find_fvgs, find_order_blocks
from ofsignals.risk.sizing import derive_leverage
from ofsignals.types import Candles, Direction, StructureKind, SwingKind

STRUCTURE_CFG = {
    "swing_fractal_lookback": 2,
    "displacement_atr_mult": 1.5,
    "displacement_body_ratio": 0.55,
    "atr_period": 14,
    "equilibrium_pct": 0.5,
}
LIQUIDITY_CFG = {
    "equal_level_tolerance_atr": 0.10,
    "min_touches_for_pool": 2,
    "sweep_penetration_atr": 0.15,
    "sweep_reclaim_bars": 3,
    "sweep_volume_mult": 1.8,
    "session_levels": [],
    "include_prev_day_high_low": False,
    "include_prev_week_high_low": False,
}


def make_candles(bars: list[tuple[float, float, float, float, float]],
                 start_ms: int = 1_700_000_000_000, step_ms: int = 900_000) -> Candles:
    rows = [[start_ms + i * step_ms, o, h, l, c, v]
            for i, (o, h, l, c, v) in enumerate(bars)]
    return Candles.from_ohlcv(rows, "TEST/USDT:USDT", "15m")


def flat_series(n: int, price: float = 100.0, volume: float = 1000.0):
    return [(price, price + 0.5, price - 0.5, price, volume) for _ in range(n)]


# --------------------------------------------------------------------- degenerate
class TestDegenerate:
    def test_empty_series(self):
        candles = Candles.from_ohlcv([])
        assert len(candles) == 0
        assert detect_swings(candles) == []
        assert detect_structure_events(candles, []) == []
        assert atr(candles).size == 0
        assert build_profile(candles) is None

    def test_single_bar(self):
        candles = make_candles([(100, 101, 99, 100.5, 10)])
        assert detect_swings(candles) == []
        assert atr(candles).size == 1

    def test_all_equal_prices(self):
        candles = make_candles(flat_series(60))
        assert detect_displacements(candles) == []
        bias = compute_bias(candles, STRUCTURE_CFG)
        assert bias.direction is Direction.NONE

    def test_zero_volume_does_not_crash_profile(self):
        candles = make_candles([(100, 101, 99, 100, 0.0) for _ in range(30)])
        assert build_profile(candles) is None

    def test_series_shorter_than_lookback(self):
        candles = make_candles(flat_series(3))
        assert detect_swings(candles, lookback=2) == []


# ---------------------------------------------------------------------- swings
class TestSwings:
    def test_finds_exact_pivot_index(self):
        bars = flat_series(3)
        bars.append((100, 110, 99.5, 100, 1000))       # index 3: the swing high
        bars += flat_series(3)
        candles = make_candles(bars)

        highs = [s for s in detect_swings(candles, 2) if s.kind is SwingKind.HIGH]
        assert len(highs) == 1
        assert highs[0].index == 3
        assert highs[0].price == pytest.approx(110.0)

    def test_confirmation_index_prevents_lookahead(self):
        """A pivot at i must not be usable before i + lookback."""
        bars = flat_series(3) + [(100, 110, 99.5, 100, 1000)] + flat_series(3)
        swings = detect_swings(make_candles(bars), lookback=2)
        for swing in swings:
            assert swing.confirmed_index == swing.index + 2

    def test_no_pivot_within_lookback_of_the_edge(self):
        bars = flat_series(5) + [(100, 120, 99, 100, 1000)]
        swings = detect_swings(make_candles(bars), lookback=2)
        assert all(s.index <= len(bars) - 3 for s in swings)


# ------------------------------------------------------------------- structure
class TestStructure:
    def test_first_break_is_choch_then_bos(self):
        bars = flat_series(3)
        bars.append((100, 105, 99.5, 100, 1000))     # swing high at index 3
        bars += flat_series(3)
        bars.append((100, 107, 100, 106, 3000))      # closes above 105 -> CHoCH long
        bars += flat_series(3, price=106)
        bars.append((106, 112, 105.5, 106, 1000))    # new swing high at index 11
        bars += flat_series(3, price=106)
        bars.append((106, 115, 106, 114, 3000))      # closes above 112 -> BOS long

        candles = make_candles(bars)
        events = detect_structure_events(candles, detect_swings(candles, 2))

        assert events, "expected at least one structural break"
        assert events[0].kind is StructureKind.CHOCH
        assert events[0].direction is Direction.LONG
        kinds = [e.kind for e in events]
        assert StructureKind.BOS in kinds

    def test_wick_through_pivot_is_not_a_break(self):
        """Body close is required — a wick raid must not register as structure."""
        bars = flat_series(3)
        bars.append((100, 105, 99.5, 100, 1000))
        bars += flat_series(3)
        bars.append((100, 108, 99, 100.2, 1000))     # pierces 105 but closes below
        bars += flat_series(3)

        candles = make_candles(bars)
        events = detect_structure_events(candles, detect_swings(candles, 2))
        assert not any(e.direction is Direction.LONG for e in events)


# ---------------------------------------------------------------- displacement
class TestDisplacement:
    def test_requires_both_range_and_body(self):
        base = flat_series(30)
        # Big range, tiny body -> not displacement.
        wick_bar = base + [(100, 112, 88, 100.2, 5000)]
        assert detect_displacements(make_candles(wick_bar)) == []

        # Big range AND dominant body -> displacement.
        body_bar = base + [(100, 112, 99, 111, 5000)]
        found = detect_displacements(make_candles(body_bar))
        assert found and found[-1].direction is Direction.LONG


# --------------------------------------------------------------------- zones
class TestZones:
    def test_bullish_fvg_indices(self):
        bars = flat_series(30)
        bars.append((100, 101, 99, 100.5, 1000))     # i-2
        bars.append((100.5, 108, 100, 107, 4000))    # i-1 impulse
        bars.append((107, 109, 103, 108, 2000))      # i: low 103 > high 101 -> gap
        candles = make_candles(bars)

        fvgs = find_fvgs(candles, {"min_gap_atr": 0.20}, STRUCTURE_CFG)
        bullish = [z for z in fvgs if z.direction is Direction.LONG]
        assert bullish, "expected a bullish FVG"
        assert bullish[0].bottom == pytest.approx(101.0)
        assert bullish[0].top == pytest.approx(103.0)

    def test_order_block_is_last_opposing_candle(self):
        bars = flat_series(30)
        bars.append((100, 100.5, 97, 97.5, 1500))    # bearish candle = the OB
        bars.append((97.5, 110, 97.4, 109, 6000))    # displacement up
        candles = make_candles(bars)

        blocks = find_order_blocks(candles, {"max_age_bars": 60,
                                             "invalidate_on_body_close": True},
                                   STRUCTURE_CFG)
        longs = [z for z in blocks if z.direction is Direction.LONG]
        assert longs
        assert longs[0].bottom == pytest.approx(97.0)
        assert longs[0].top == pytest.approx(100.5)


# ----------------------------------------------------------------- liquidity
class TestLiquidity:
    def _series_with_equal_lows(self):
        bars = flat_series(20, price=100)
        for _ in range(2):
            bars += flat_series(3, price=100)
            bars.append((100, 100.5, 95.0, 100, 1200))   # equal low at 95
            bars += flat_series(3, price=100)
        return bars

    def test_equal_lows_form_a_pool(self):
        candles = make_candles(self._series_with_equal_lows())
        pools = build_pools(candles, LIQUIDITY_CFG, STRUCTURE_CFG)
        sellside = [p for p in pools if p.source == "eql"]
        assert sellside, "expected an equal-lows pool"
        assert sellside[0].touches >= 2

    def test_sweep_requires_reclaim(self):
        bars = self._series_with_equal_lows()
        # Pierce the pool hard on volume but close BELOW it: a breakout, not a sweep.
        bars.append((100, 100.2, 90.0, 91.0, 9000))
        bars += flat_series(2, price=91)
        candles = make_candles(bars)
        pools = build_pools(candles, LIQUIDITY_CFG, STRUCTURE_CFG)
        assert detect_sweep(candles, pools, LIQUIDITY_CFG, STRUCTURE_CFG) is None

    def test_valid_sweep_is_detected(self):
        bars = self._series_with_equal_lows()
        bars.append((100, 100.5, 90.0, 100.2, 9000))    # pierce and reclaim same bar
        bars += flat_series(2, price=100.5)
        candles = make_candles(bars)

        pools = build_pools(candles, LIQUIDITY_CFG, STRUCTURE_CFG)
        sweep = detect_sweep(candles, pools, LIQUIDITY_CFG, STRUCTURE_CFG)
        assert sweep is not None
        assert sweep.direction is Direction.LONG
        assert sweep.volume_multiple >= LIQUIDITY_CFG["sweep_volume_mult"]
        assert sweep.extreme == pytest.approx(90.0)


# ------------------------------------------------------------ volume profile
class TestVolumeProfile:
    def test_poc_lands_on_the_heaviest_price(self):
        bars = flat_series(40, price=100, volume=100)
        bars += [(105, 105.2, 104.8, 105, 20_000) for _ in range(10)]
        profile = build_profile(make_candles(bars), bins=60)
        assert profile is not None
        assert profile.poc == pytest.approx(105.0, abs=0.5)

    def test_value_area_brackets_the_poc(self):
        bars = flat_series(50, price=100, volume=500)
        profile = build_profile(make_candles(bars), bins=40)
        assert profile is not None
        assert profile.val <= profile.poc <= profile.vah


# ----------------------------------------------------------------- footprint
class TestFootprint:
    def test_taker_side_convention(self):
        """m=True means the buyer was maker, so it is SELL aggression."""
        tape = TradeTape("TEST")
        tape.add_many([
            {"timestamp": 1_000_000, "price": 100.0, "amount": 5.0, "info": {"m": True}},
            {"timestamp": 1_000_001, "price": 100.0, "amount": 3.0, "info": {"m": False}},
        ])
        bars = tape.bars(bucket_ms=60_000, now_ms=1_200_000)
        assert len(bars) == 1
        assert bars[0].sell_volume == pytest.approx(5.0)
        assert bars[0].buy_volume == pytest.approx(3.0)
        assert bars[0].delta == pytest.approx(-2.0)

    def test_forming_bucket_is_excluded(self):
        tape = TradeTape("TEST")
        tape.add_many([
            {"timestamp": 60_000, "price": 100.0, "amount": 1.0, "side": "buy"},
            {"timestamp": 125_000, "price": 101.0, "amount": 1.0, "side": "buy"},
        ])
        bars = tape.bars(bucket_ms=60_000, now_ms=125_000)
        assert [b.start_ms for b in bars] == [60_000]

    def test_cvd_accumulates(self):
        tape = TradeTape("TEST")
        trades = []
        for i in range(10):
            trades.append({"timestamp": i * 60_000 + 100, "price": 100.0,
                           "amount": 2.0, "side": "buy"})
        tape.add_many(trades)
        bars = tape.bars(bucket_ms=60_000, now_ms=10 * 60_000)
        assert cumulative_delta(bars)[-1] == pytest.approx(sum(b.delta for b in bars))

    def test_reports_not_ready_before_warmup(self):
        tape = TradeTape("TEST")
        tape.add_many([{"timestamp": 1000, "price": 1.0, "amount": 1.0, "side": "buy"}])
        snapshot = analyse_flow(tape, 60_000, Direction.LONG,
                                {"cvd_lookback_bars": 96, "divergence_min_bars": 3})
        assert snapshot.ready is False
        assert snapshot.confirmations == 0


# ---------------------------------------------------------------------- risk
class TestRisk:
    def test_leverage_is_inverse_to_stop_distance(self):
        cfg = {"liquidation_safety_factor": 0.40}
        tight, tight_max = derive_leverage(0.5, mode_cap=20, cfg=cfg)
        wide, wide_max = derive_leverage(4.0, mode_cap=20, cfg=cfg)
        assert tight_max > wide_max
        assert wide <= 10
        assert tight <= 20

    def test_mode_cap_binds(self):
        cfg = {"liquidation_safety_factor": 0.40}
        recommended, max_safe = derive_leverage(0.2, mode_cap=5, cfg=cfg)
        assert recommended == 5
        assert max_safe > 5

    def test_never_returns_zero_leverage(self):
        cfg = {"liquidation_safety_factor": 0.40}
        recommended, max_safe = derive_leverage(99.0, mode_cap=20, cfg=cfg)
        assert recommended >= 1 and max_safe >= 1


# ------------------------------------------------------------------ lookahead
class TestNoLookahead:
    def test_atr_is_causal(self):
        """Appending future bars must not change historical ATR values."""
        bars = flat_series(50, price=100)
        bars += [(100, 130, 70, 128, 9000)]        # violent future bar
        full = atr(make_candles(bars))
        truncated = atr(make_candles(bars[:-1]))
        assert np.allclose(full[:len(truncated)], truncated, equal_nan=True)

    def test_structure_events_are_stable_under_extension(self):
        bars = flat_series(3) + [(100, 105, 99.5, 100, 1000)] + flat_series(3)
        bars.append((100, 107, 100, 106, 3000))
        base = make_candles(bars)
        extended = make_candles(bars + flat_series(10, price=106))

        base_events = detect_structure_events(base, detect_swings(base, 2))
        extended_events = detect_structure_events(extended, detect_swings(extended, 2))
        assert [(e.index, e.kind) for e in base_events] == \
               [(e.index, e.kind) for e in extended_events[:len(base_events)]]
