"""
Tests for core/session_levels.py — pure helpers for the Opening Session strategy.

Run:  pytest tests/test_session_levels.py -v
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.session_levels import (
    calc_pivot_points,
    classify_opening_type,
    is_in_window,
    get_premarket_range,
    is_news_blackout,
)


# ─── calc_pivot_points ──────────────────────────────────────────────
class TestCalcPivotPoints:
    def test_pivot_basic(self):
        p = calc_pivot_points(100, 90, 95)
        assert p["pp"] == pytest.approx(95.0)
        assert p["r1"] == pytest.approx(100.0)
        assert p["s1"] == pytest.approx(90.0)
        assert p["r2"] == pytest.approx(105.0)
        assert p["s2"] == pytest.approx(85.0)

    def test_pivot_real_nq(self):
        # Realistic prior NQ session
        p = calc_pivot_points(26800, 26500, 26700)
        assert p["pp"] == pytest.approx(26666.6667, rel=1e-4)
        assert p["r1"] == pytest.approx(26833.3333, rel=1e-4)
        assert p["s1"] == pytest.approx(26533.3333, rel=1e-4)
        assert p["r2"] == pytest.approx(26966.6667, rel=1e-4)
        assert p["s2"] == pytest.approx(26366.6667, rel=1e-4)

    def test_pivot_returns_dict_with_all_5_keys(self):
        p = calc_pivot_points(26800, 26500, 26700)
        assert set(p.keys()) == {"pp", "r1", "r2", "s1", "s2"}
        for v in p.values():
            assert isinstance(v, float)


# ─── classify_opening_type ──────────────────────────────────────────
def _base_snapshot(**overrides):
    """Neutral snapshot — will classify as OPEN_AUCTION_IN unless overridden."""
    base = {
        "rth_open_price": 25000.0,
        "rth_5min_high": 25005.0,
        "rth_5min_low": 24995.0,
        "rth_5min_close": 25002.0,
        "rth_5min_volume": 100.0,
        "avg_5min_volume": 100.0,
        "prior_day_vah": 25050.0,
        "prior_day_val": 24950.0,
        "prior_day_high": 25100.0,
        "prior_day_low": 24900.0,
    }
    base.update(overrides)
    return base


class TestClassifyOpeningType:
    def test_open_drive_long_detected(self):
        # Bullish 20-point displacement, pullback <30%, volume >1.4x, close at high
        snap = _base_snapshot(
            rth_open_price=25000.0,
            rth_5min_high=25020.0,
            rth_5min_low=24998.0,         # 2-pt pullback, within 30% of 20 = 6
            rth_5min_close=25020.0,       # close at high
            rth_5min_volume=150.0,        # 1.5x avg
            avg_5min_volume=100.0,
        )
        assert classify_opening_type(snap) == "OPEN_DRIVE"

    def test_open_drive_short_detected(self):
        # Bearish 20-point displacement, pullback <30%, volume >1.4x, close at low
        snap = _base_snapshot(
            rth_open_price=25000.0,
            rth_5min_high=25002.0,        # 2-pt pullback, within 6
            rth_5min_low=24980.0,
            rth_5min_close=24980.0,       # close at low
            rth_5min_volume=150.0,
            avg_5min_volume=100.0,
        )
        assert classify_opening_type(snap) == "OPEN_DRIVE"

    def test_open_drive_blocked_by_pullback(self):
        # 20-pt displacement bullish, but low is 50% pullback (10 pts) — > 30% max
        snap = _base_snapshot(
            rth_open_price=25000.0,
            rth_5min_high=25020.0,
            rth_5min_low=24990.0,         # 10-pt pullback, >6 → blocked
            rth_5min_close=25020.0,
            rth_5min_volume=150.0,
            avg_5min_volume=100.0,
        )
        assert classify_opening_type(snap) != "OPEN_DRIVE"

    def test_open_drive_blocked_by_volume(self):
        # Displacement and pullback OK, but volume only 1.2x avg (< 1.4x)
        snap = _base_snapshot(
            rth_open_price=25000.0,
            rth_5min_high=25020.0,
            rth_5min_low=24998.0,
            rth_5min_close=25020.0,
            rth_5min_volume=120.0,
            avg_5min_volume=100.0,
        )
        assert classify_opening_type(snap) != "OPEN_DRIVE"

    def test_open_test_drive_detected(self):
        # Wicks above prior_day_high, closes back inside prior-day range,
        # close < open (opposite side of open from the tested high)
        snap = _base_snapshot(
            rth_open_price=25000.0,
            rth_5min_high=25120.0,        # > prior_day_high (25100)
            rth_5min_low=24990.0,
            rth_5min_close=24995.0,       # back inside [24900, 25100], below open
            rth_5min_volume=100.0,
            avg_5min_volume=100.0,
            prior_day_high=25100.0,
            prior_day_low=24900.0,
        )
        assert classify_opening_type(snap) == "OPEN_TEST_DRIVE"

    def test_open_auction_in_detected(self):
        # Open sits between VAL and VAH, no drive/test-drive trigger
        snap = _base_snapshot(
            rth_open_price=25000.0,       # inside [24950, 25050]
            rth_5min_high=25005.0,
            rth_5min_low=24995.0,
            rth_5min_close=25001.0,
            prior_day_vah=25050.0,
            prior_day_val=24950.0,
            prior_day_high=25100.0,
            prior_day_low=24900.0,
        )
        assert classify_opening_type(snap) == "OPEN_AUCTION_IN"

    def test_open_auction_out_detected(self):
        # Open above prior_day_high, stays above (no test-drive), small displacement
        snap = _base_snapshot(
            rth_open_price=25150.0,       # above prior_day_high=25100
            rth_5min_high=25155.0,
            rth_5min_low=25145.0,
            rth_5min_close=25152.0,
            prior_day_high=25100.0,
            prior_day_low=24900.0,
            prior_day_vah=25050.0,
            prior_day_val=24950.0,
        )
        assert classify_opening_type(snap) == "OPEN_AUCTION_OUT"

    def test_indeterminate_returns_when_no_match(self):
        # Open above VAH but below prior_day_high, small displacement,
        # no wick above prior_day_high → nothing triggers.
        snap = _base_snapshot(
            rth_open_price=25075.0,       # > VAH(25050), < pd_high(25100)
            rth_5min_high=25080.0,
            rth_5min_low=25070.0,
            rth_5min_close=25078.0,
            prior_day_vah=25050.0,
            prior_day_val=24950.0,
            prior_day_high=25100.0,
            prior_day_low=24900.0,
        )
        assert classify_opening_type(snap) == "INDETERMINATE"


# ─── is_in_window ───────────────────────────────────────────────────
class TestIsInWindow:
    def test_in_window_true(self):
        now = datetime(2026, 4, 20, 10, 0)   # 10:00 AM
        assert is_in_window(now, "08:30", "14:30") is True

    def test_in_window_false_before(self):
        now = datetime(2026, 4, 20, 8, 0)    # 8:00 AM, before 8:30 start
        assert is_in_window(now, "08:30", "14:30") is False

    def test_in_window_false_after(self):
        now = datetime(2026, 4, 20, 15, 0)   # 3:00 PM, after 14:30 end
        assert is_in_window(now, "08:30", "14:30") is False


# ─── get_premarket_range ────────────────────────────────────────────
class TestGetPremarketRange:
    def test_returns_pmh_pml_from_snapshot(self):
        snap = {"pmh": 26800.25, "pml": 26700.50}
        pmh, pml = get_premarket_range(snap)
        assert pmh == pytest.approx(26800.25)
        assert pml == pytest.approx(26700.50)

    def test_returns_none_when_missing(self):
        assert get_premarket_range({}) == (None, None)
        assert get_premarket_range({"pmh": 26800.0}) == (None, None)
        assert get_premarket_range({"pml": 26700.0}) == (None, None)

    def test_returns_none_when_both_present_but_null(self):
        snap = {"pmh": None, "pml": None}
        assert get_premarket_range(snap) == (None, None)


# ─── is_news_blackout ───────────────────────────────────────────────
class TestIsNewsBlackout:
    def test_no_blackout_when_no_news(self):
        now = datetime(2026, 4, 20, 9, 30)
        assert is_news_blackout(now, []) is False
        assert is_news_blackout(now, None) is False

    def test_blackout_5min_before_news(self):
        # Event at 9:30, now 9:25 → exactly 5 min before, within window
        now = datetime(2026, 4, 20, 9, 25)
        cal = [{"time_ct": datetime(2026, 4, 20, 9, 30), "impact": "high"}]
        assert is_news_blackout(now, cal) is True

    def test_blackout_5min_after_news(self):
        # Event at 9:30, now 9:34 → 4 min after, within window
        now = datetime(2026, 4, 20, 9, 34)
        cal = [{"time_ct": datetime(2026, 4, 20, 9, 30), "impact": "high"}]
        assert is_news_blackout(now, cal) is True

    def test_no_blackout_outside_window(self):
        # Event at 9:30, now 9:20 → 10 min before, outside window
        now = datetime(2026, 4, 20, 9, 20)
        cal = [{"time_ct": datetime(2026, 4, 20, 9, 30), "impact": "high"}]
        assert is_news_blackout(now, cal) is False

    def test_low_impact_ignored(self):
        # Medium/low impact events don't trigger blackout
        now = datetime(2026, 4, 20, 9, 30)
        cal = [{"time_ct": datetime(2026, 4, 20, 9, 30), "impact": "low"}]
        assert is_news_blackout(now, cal) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
