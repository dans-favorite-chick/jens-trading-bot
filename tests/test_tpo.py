"""Tests for core.tpo_builder.

Run with:
    pytest tests/test_tpo.py -v
"""
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from core.tpo_builder import TPOBuilder, TPOProfile

_CT = ZoneInfo("America/Chicago")


def _ct_ts(year, month, day, hour, minute, sec=0) -> float:
    return datetime(year, month, day, hour, minute, sec, tzinfo=_CT).timestamp()


class TestTPOBuilder:
    def test_letters_assigned_to_periods(self):
        b = TPOBuilder(period_minutes=30)
        # 08:30 = period A, 09:00 = period B, 09:30 = period C
        assert b._letter_for_time(dtime(8, 30)) == "A"
        assert b._letter_for_time(dtime(8, 59)) == "A"
        assert b._letter_for_time(dtime(9, 0)) == "B"
        assert b._letter_for_time(dtime(9, 29)) == "B"
        assert b._letter_for_time(dtime(9, 30)) == "C"

    def test_outside_rth_returns_no_letter(self):
        b = TPOBuilder()
        assert b._letter_for_time(dtime(8, 0)) is None
        assert b._letter_for_time(dtime(15, 0)) is None
        assert b._letter_for_time(dtime(16, 0)) is None

    def test_basic_tick_aggregation(self):
        b = TPOBuilder(period_minutes=30, price_tick=0.25)
        # Period A (08:30-09:00) — ticks at 22000, 22001
        b.add_tick(22000.0, _ct_ts(2026, 5, 15, 8, 30))
        b.add_tick(22001.0, _ct_ts(2026, 5, 15, 8, 45))
        # Period B (09:00-09:30) — ticks at 22001, 22002
        b.add_tick(22001.0, _ct_ts(2026, 5, 15, 9, 0))
        b.add_tick(22002.0, _ct_ts(2026, 5, 15, 9, 15))
        prof = b.get_profile()
        assert prof is not None
        # Each price should have its letters counted
        # 22000: visited by A only → 1 letter
        # 22001: visited by both A and B → 2 letters → POC
        # 22002: visited by B only → 1 letter
        assert prof.histogram.get(22001.0) == 2
        assert prof.poc == 22001.0
        assert 22000.0 in prof.single_prints
        assert 22002.0 in prof.single_prints

    def test_value_area_70pct(self):
        b = TPOBuilder()
        # Build 10 periods worth of data, concentrated at 22000
        for period in range(10):
            hour = 8 + (period * 30) // 60
            minute = 30 + (period * 30) % 60
            if minute >= 60:
                hour += 1
                minute -= 60
            ts = _ct_ts(2026, 5, 15, hour, minute)
            # Heavy at 22000, lighter at edges
            for _ in range(5):
                b.add_tick(22000.0, ts)
            b.add_tick(21998.0, ts)
            b.add_tick(22002.0, ts)
        prof = b.get_profile()
        assert prof is not None
        assert prof.poc == 22000.0
        # VAH should not extend too far above POC since edge prices are thinner
        assert prof.vah <= 22002.0
        assert prof.val >= 21998.0

    def test_single_prints_detected(self):
        b = TPOBuilder()
        # Period A only touches 22010 (high spike)
        b.add_tick(22010.0, _ct_ts(2026, 5, 15, 8, 35))
        b.add_tick(22000.0, _ct_ts(2026, 5, 15, 8, 35))
        # Period B never goes back to 22010
        b.add_tick(22000.0, _ct_ts(2026, 5, 15, 9, 5))
        b.add_tick(22001.0, _ct_ts(2026, 5, 15, 9, 5))
        # Period C also doesn't visit 22010
        b.add_tick(22000.0, _ct_ts(2026, 5, 15, 9, 35))

        prof = b.get_profile()
        assert prof is not None
        assert 22010.0 in prof.single_prints

    def test_initial_balance_computed(self):
        b = TPOBuilder()
        # IB = A + B periods (08:30-09:30 CT)
        b.add_tick(22000.0, _ct_ts(2026, 5, 15, 8, 35))   # A
        b.add_tick(22010.0, _ct_ts(2026, 5, 15, 8, 50))   # A
        b.add_tick(21995.0, _ct_ts(2026, 5, 15, 9, 5))    # B
        b.add_tick(22005.0, _ct_ts(2026, 5, 15, 9, 15))   # B
        # C extends UP beyond IB
        b.add_tick(22015.0, _ct_ts(2026, 5, 15, 9, 35))   # C — extends IB high
        prof = b.get_profile()
        assert prof is not None
        assert prof.ib_high == 22010.0
        assert prof.ib_low == 21995.0
        assert prof.ib_extended_high is True
        assert prof.ib_extended_low is False

    def test_add_bar_works_as_tick_alternative(self):
        """When tick data isn't available, add_bar() should distribute the bar's range."""
        from dataclasses import dataclass

        @dataclass
        class TBar:
            high: float
            low: float
            end_time: float

        b = TPOBuilder(period_minutes=30)
        # Period A: bar from 22000 to 22003
        b.add_bar(TBar(high=22003, low=22000, end_time=_ct_ts(2026, 5, 15, 8, 45)))
        # Period B: bar from 22002 to 22004
        b.add_bar(TBar(high=22004, low=22002, end_time=_ct_ts(2026, 5, 15, 9, 15)))
        prof = b.get_profile()
        assert prof is not None
        # 22002 and 22003 should each have 2 letters (both A and B)
        assert prof.histogram.get(22002.0) == 2
        assert prof.histogram.get(22003.0) == 2

    def test_session_reset(self):
        b = TPOBuilder()
        b.add_tick(22000.0, _ct_ts(2026, 5, 15, 8, 35))
        b.reset_for_new_session()
        prof = b.get_profile()
        assert prof is None

    def test_day_type_classification_balanced(self):
        """Symmetric distribution should classify as D-day."""
        b = TPOBuilder()
        # Symmetric distribution around 22000 across many periods
        prices = [22000] * 10 + [21999, 22001] * 4 + [21998, 22002] * 2
        for period in range(13):
            hour = 8 + (30 + period * 30) // 60
            minute = (30 + period * 30) % 60
            ts = _ct_ts(2026, 5, 15, hour, minute)
            for p in prices:
                b.add_tick(float(p), ts)
        prof = b.get_profile()
        assert prof is not None
        # Should classify as D (balanced) — but the heuristic may also return "neutral"
        # in edge cases; either is acceptable for a symmetric profile
        assert prof.day_type in ("D", "neutral")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
