"""Tests for core.liquidity_levels.

Run with:
    pytest tests/test_liquidity_levels.py -v
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.liquidity_levels import (
    LiquidityLevel,
    LiquidityLevelTracker,
    SweepEvent,
    detect_sweep,
    filter_bars_rth,
    filter_bars_premarket,
    filter_bars_or,
    filter_bars_yesterday_rth,
    TICK_SIZE,
)

_CT = ZoneInfo("America/Chicago")


@dataclass
class Bar:
    """Test Bar — minimal attribute set."""
    open: float
    high: float
    low: float
    close: float
    volume: float
    start_time: float
    end_time: float


def _ct_ts(year, month, day, hour, minute) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=_CT).timestamp()


# ────────────────────────────────────────────────────────────────────
# detect_sweep tests
# ────────────────────────────────────────────────────────────────────
class TestDetectSweep:
    def test_long_sweep_happy_path(self):
        """Wick pierces level below, closes back above, ≥50% rejection."""
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        # Bar: open 22002, low 21997 (4 ticks below), close 22002.5, high 22003
        bar = Bar(open=22002.0, high=22003.0, low=21997.0, close=22002.5,
                  volume=1000, start_time=0, end_time=100)
        ev = detect_sweep(level, bar)
        assert ev is not None
        assert ev.direction == "LONG"
        assert ev.wick_depth_ticks == int(round((22000.00 - 21997.0) / TICK_SIZE))  # 12 ticks
        assert ev.wick_pct_of_range >= 0.5

    def test_short_sweep_happy_path(self):
        level = LiquidityLevel("PDH", 22000.00, "HIGH", 0)
        # Bar: open 21999, high 22003 (12 ticks above), close 21998.5, low 21998
        bar = Bar(open=21999.0, high=22003.0, low=21998.0, close=21998.5,
                  volume=1000, start_time=0, end_time=100)
        ev = detect_sweep(level, bar)
        assert ev is not None
        assert ev.direction == "SHORT"
        assert ev.wick_depth_ticks == int(round((22003.0 - 22000.00) / TICK_SIZE))

    def test_long_sweep_close_below_level_fails(self):
        """Wick pierced but close stayed below — not a rejection."""
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        bar = Bar(open=22001, high=22001.5, low=21997, close=21998,
                  volume=1000, start_time=0, end_time=100)
        ev = detect_sweep(level, bar)
        assert ev is None

    def test_insufficient_penetration_fails(self):
        """Wick only touched the level — not a real sweep."""
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        # bar.low = 21999.75 = level - 0.25 = only 1 tick below
        bar = Bar(open=22001, high=22002, low=21999.75, close=22001,
                  volume=1000, start_time=0, end_time=100)
        ev = detect_sweep(level, bar, min_penetration_ticks=2)
        assert ev is None

    def test_wick_too_small_fails(self):
        """Bar pierced but the wick is <50% of bar range — weak rejection."""
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        # Bar: open 22001, low 21997 (4 below), close just barely above — short wick relative to range
        bar = Bar(open=22001, high=22010, low=21997, close=22000.75,
                  volume=1000, start_time=0, end_time=100)
        # Range = 13 points. Lower wick = close - low = 22000.75 - 21997 = 3.75 pts. 3.75/13 = ~0.29
        ev = detect_sweep(level, bar, min_wick_pct=0.5)
        assert ev is None

    def test_zero_range_bar_returns_none(self):
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        bar = Bar(open=22000, high=22000, low=22000, close=22000,
                  volume=100, start_time=0, end_time=100)
        ev = detect_sweep(level, bar)
        assert ev is None

    def test_structural_stop_distance_is_reasonable(self):
        """The proposed stop should be a small number of ticks (8-30 range)."""
        level = LiquidityLevel("PDL", 22000.00, "LOW", 0)
        # Wick goes 3 points below, close goes 0.5 points above
        bar = Bar(open=22001, high=22002, low=21997, close=22000.5,
                  volume=1000, start_time=0, end_time=100)
        ev = detect_sweep(level, bar)
        assert ev is not None
        # Stop distance = close - low + 2 ticks buffer = 3.5pt + 0.5pt = 4pt = 16 ticks
        assert 10 <= ev.structural_stop_ticks <= 25


# ────────────────────────────────────────────────────────────────────
# LiquidityLevelTracker tests
# ────────────────────────────────────────────────────────────────────
class TestLiquidityLevelTracker:
    def test_pdh_pdl_set_from_rth_bars(self):
        t = LiquidityLevelTracker()
        bars = [
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 9, 0)),
            Bar(0, 22075, 22010, 22030, 100, 0, _ct_ts(2026, 5, 15, 10, 0)),
            Bar(0, 22080, 22005, 22020, 100, 0, _ct_ts(2026, 5, 15, 11, 0)),
        ]
        t.update_pdh_pdl(bars)
        assert t.get("PDH").price == 22080
        assert t.get("PDL").price == 21990

    def test_active_levels_excludes_consumed(self):
        t = LiquidityLevelTracker(level_cooloff_minutes=60)
        bars = [
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 9, 0)),
        ]
        t.update_pdh_pdl(bars)
        assert len(t.active_levels()) == 2  # PDH + PDL
        t.mark_level_consumed("PDH")
        actives = t.active_levels()
        names = [lv.name for lv in actives]
        assert "PDH" not in names
        assert "PDL" in names

    def test_swing_levels_detected(self):
        """A clear swing high in the middle of a bar series should be detected."""
        t = LiquidityLevelTracker(swing_lookback_bars=3, swing_peak_window=2)
        # 15 bars: ascending then descending — peak in the middle
        bars = []
        for i in range(15):
            ts = _ct_ts(2026, 5, 15, 10, i)
            high = 22000 + abs(7 - i) * -5 + 100  # parabolic peak at i=7
            high = 22000 + (10 - abs(7 - i)) * 5
            low = high - 5
            bars.append(Bar(high - 1, high, low, high - 0.5, 100, ts - 60, ts))
        t.refresh_swing_levels(bars, current_price=21990)
        swings = [lv for lv in t.all_levels().values() if lv.name.startswith("Swing")]
        # Should detect at least one swing high
        assert any(lv.side == "HIGH" for lv in swings)

    def test_serialization_roundtrip(self):
        t = LiquidityLevelTracker()
        bars = [Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 9, 0))]
        t.update_pdh_pdl(bars)
        d = t.to_dict()
        t2 = LiquidityLevelTracker.from_dict(d)
        assert t2.get("PDH").price == 22050
        assert t2.get("PDL").price == 21990


# ────────────────────────────────────────────────────────────────────
# Bar filter tests
# ────────────────────────────────────────────────────────────────────
class TestBarFilters:
    def test_filter_rth_only(self):
        bars = [
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 7, 0)),   # premarket
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 9, 0)),   # RTH
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 14, 0)),  # RTH
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 15, 30)), # post-close
        ]
        date = datetime(2026, 5, 15, 12, 0, tzinfo=_CT)
        rth = filter_bars_rth(bars, date_ct=date)
        assert len(rth) == 2  # the two during 08:30-15:00

    def test_filter_premarket(self):
        bars = [
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 4, 0)),
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 7, 0)),
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 9, 0)),
        ]
        date = datetime(2026, 5, 15, 12, 0, tzinfo=_CT)
        pm = filter_bars_premarket(bars, date_ct=date)
        assert len(pm) == 2

    def test_filter_or_window(self):
        bars = [
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 8, 30)),
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 8, 35)),
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 8, 44)),
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 8, 45)),
            Bar(0, 22050, 21990, 22000, 100, 0, _ct_ts(2026, 5, 15, 8, 50)),
        ]
        date = datetime(2026, 5, 15, 12, 0, tzinfo=_CT)
        ors = filter_bars_or(bars, date_ct=date)
        # 08:30, 08:35, 08:44 — three bars in [08:30, 08:45)
        assert len(ors) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
