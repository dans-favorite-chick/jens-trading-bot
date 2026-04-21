"""
Tests for core/session_levels_aggregator.py — prior-day + live opening levels.

Run: pytest tests/test_session_levels_aggregator.py -v
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.session_levels_aggregator import SessionLevelsAggregator


# ─── Helpers ────────────────────────────────────────────────────────
@dataclass
class FakeBar:
    open: float
    high: float
    low: float
    close: float
    volume: int = 100


def ct(hh: int, mm: int, ss: int = 0, d: date | None = None) -> datetime:
    the_date = d or date(2026, 4, 20)
    return datetime(the_date.year, the_date.month, the_date.day, hh, mm, ss)


def write_jsonl(tmp_path, trade_date: date, bot_name: str, lines: list[dict]):
    """Write a JSONL history file and return its path."""
    path = tmp_path / f"{trade_date.isoformat()}_{bot_name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return path


def _bar_event(timeframe: str, o: float, h: float, l: float, c: float,
               volume: int = 1000, ts: str = "2026-04-18T09:00:00") -> dict:
    return {
        "event": "bar",
        "ts": ts,
        "bot": "lab",
        "timeframe": timeframe,
        "open": o, "high": h, "low": l, "close": c,
        "volume": volume,
    }


@pytest.fixture
def agg(tmp_path):
    """Aggregator pointed at an empty temp history dir."""
    return SessionLevelsAggregator("lab", history_dir=tmp_path)


# ═══════════════════════════════════════════════════════════════════
# Prior-day computation (7 tests)
# ═══════════════════════════════════════════════════════════════════
class TestPriorDayComputation:
    def test_loads_prior_day_high_low_from_jsonl(self, tmp_path):
        pd = date(2026, 4, 17)
        write_jsonl(tmp_path, pd, "lab", [
            _bar_event("5m", 100, 105, 99, 102),
            _bar_event("5m", 102, 110, 100, 108),
            _bar_event("5m", 108, 112, 106, 110),
            _bar_event("1m", 0, 999, 0, 0),  # should be ignored (not 5m)
        ])
        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        a.load_prior_day(target_date=date(2026, 4, 18))
        assert a.prior_day_open == 100
        assert a.prior_day_close == 110
        assert a.prior_day_high == 112
        assert a.prior_day_low == 99

    def test_computes_volume_profile_poc_correctly(self, tmp_path):
        # Build 12 bars; most volume concentrated in the 100-102 range.
        pd = date(2026, 4, 17)
        lines = []
        for _ in range(10):
            lines.append(_bar_event("5m", 101, 102, 100, 101, volume=5000))
        for _ in range(2):
            lines.append(_bar_event("5m", 110, 112, 108, 110, volume=500))
        write_jsonl(tmp_path, pd, "lab", lines)

        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        a.load_prior_day(target_date=date(2026, 4, 18))
        # POC must fall within the heavy 100-102 cluster.
        assert a.prior_day_poc is not None
        assert 100 <= a.prior_day_poc <= 102

    def test_computes_value_area_70pct(self, tmp_path):
        pd = date(2026, 4, 17)
        lines = []
        # Heavy cluster 100-102 → value area should encompass this range.
        for _ in range(10):
            lines.append(_bar_event("5m", 101, 102, 100, 101, volume=5000))
        # Light wings far away.
        for _ in range(2):
            lines.append(_bar_event("5m", 110, 111, 109, 110, volume=500))
        write_jsonl(tmp_path, pd, "lab", lines)

        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        a.load_prior_day(target_date=date(2026, 4, 18))
        assert a.prior_day_vah is not None
        assert a.prior_day_val is not None
        # VA brackets the POC.
        assert a.prior_day_val <= a.prior_day_poc <= a.prior_day_vah
        # VA does not stretch across the whole range — the light wing at 110
        # adds only 1000 of a ~52000 total, so < 70% threshold without it.
        assert a.prior_day_vah < 109

    def test_computes_pivot_points_from_prior_ohlc(self, tmp_path):
        pd = date(2026, 4, 17)
        # Single "bar" with H=110, L=90, C=100 → PP=100, R1=110, S1=90, R2=120, S2=80.
        write_jsonl(tmp_path, pd, "lab", [
            _bar_event("5m", 95, 110, 90, 100),
        ])
        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        a.load_prior_day(target_date=date(2026, 4, 18))
        assert a.pivot_pp == pytest.approx(100.0)
        assert a.pivot_r1 == pytest.approx(110.0)
        assert a.pivot_s1 == pytest.approx(90.0)
        assert a.pivot_r2 == pytest.approx(120.0)
        assert a.pivot_s2 == pytest.approx(80.0)

    def test_handles_missing_jsonl_file_gracefully(self, tmp_path, caplog):
        import logging
        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        with caplog.at_level(logging.WARNING, logger="SessionLevelsAggregator"):
            a.load_prior_day(target_date=date(2026, 4, 18))
        # All prior-day fields stay None; warning emitted.
        assert a.prior_day_high is None
        assert a.prior_day_low is None
        assert a.pivot_pp is None
        assert any("no prior-day JSONL" in r.getMessage() for r in caplog.records)

    def test_handles_corrupt_jsonl_lines(self, tmp_path):
        pd = date(2026, 4, 17)
        path = tmp_path / f"{pd.isoformat()}_lab.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_bar_event("5m", 100, 105, 95, 102)) + "\n")
            f.write("{not valid json}\n")
            f.write(json.dumps(_bar_event("5m", 102, 110, 101, 108)) + "\n")

        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        a.load_prior_day(target_date=date(2026, 4, 18))
        # Valid bars still parsed.
        assert a.prior_day_high == 110
        assert a.prior_day_low == 95

    def test_skips_value_area_when_insufficient_bars(self, tmp_path):
        pd = date(2026, 4, 17)
        # Only 5 bars — below the 10-bar minimum for volume profile.
        write_jsonl(tmp_path, pd, "lab", [
            _bar_event("5m", 100, 102, 99, 101) for _ in range(5)
        ])
        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        a.load_prior_day(target_date=date(2026, 4, 18))
        # OHLC still computed …
        assert a.prior_day_high is not None
        # … but POC/VAH/VAL stay None.
        assert a.prior_day_poc is None
        assert a.prior_day_vah is None
        assert a.prior_day_val is None


# ═══════════════════════════════════════════════════════════════════
# Premarket tracking (4 tests)
# ═══════════════════════════════════════════════════════════════════
class TestPremarketTracking:
    def test_tracks_pmh_during_premarket_window(self, agg):
        agg.update(ct(7, 30), bar_1m=FakeBar(100, 105, 99, 102))
        agg.update(ct(7, 45), bar_1m=FakeBar(102, 108, 101, 107))
        agg.update(ct(8, 15), bar_1m=FakeBar(107, 106, 103, 104))  # lower high OK
        assert agg.pmh == 108

    def test_tracks_pml_during_premarket_window(self, agg):
        agg.update(ct(7, 30), bar_1m=FakeBar(100, 105, 99, 102))
        agg.update(ct(7, 45), bar_1m=FakeBar(102, 106, 95, 103))
        agg.update(ct(8, 15), bar_1m=FakeBar(103, 104, 97, 100))
        assert agg.pml == 95

    def test_premarket_frozen_at_830_ct(self, agg):
        # Accumulate in-window high.
        agg.update(ct(7, 30), bar_1m=FakeBar(100, 105, 99, 102))
        assert agg.pmh == 105
        # Bar at 08:30+ must NOT extend PMH.
        agg.update(ct(8, 31), bar_1m=FakeBar(105, 200, 104, 150))
        assert agg.pmh == 105  # unchanged
        assert agg.pml == 99   # unchanged

    def test_premarket_reset_at_day_change(self, agg):
        # Day 1 PM
        agg.update(ct(7, 30, d=date(2026, 4, 20)), bar_1m=FakeBar(100, 105, 99, 102))
        assert agg.pmh == 105
        # Day 2 — new date triggers live-state reset.
        agg.update(ct(7, 10, d=date(2026, 4, 21)), bar_1m=FakeBar(200, 205, 199, 203))
        assert agg.pmh == 205
        assert agg.pml == 199


# ═══════════════════════════════════════════════════════════════════
# RTH opening tracking (5 tests)
# ═══════════════════════════════════════════════════════════════════
class TestRTHOpeningTracking:
    def test_captures_rth_open_at_830(self, agg):
        agg.update(ct(8, 30, 5), bar_1m=FakeBar(500, 502, 499, 501))
        assert agg.rth_open_price == 500

    def test_captures_5min_high_low_at_835(self, agg):
        agg.update(ct(8, 35), bar_5m=FakeBar(500, 510, 498, 508, volume=2000))
        assert agg.rth_5min_high == 510
        assert agg.rth_5min_low == 498
        assert agg.rth_5min_close == 508

    def test_captures_15min_high_low_at_845(self, agg):
        agg.update(ct(8, 31), bar_1m=FakeBar(500, 510, 499, 508))
        agg.update(ct(8, 38), bar_1m=FakeBar(508, 515, 506, 512))
        agg.update(ct(8, 43), bar_1m=FakeBar(512, 513, 507, 510))
        # After 08:45 boundary — no further updates to 15m window.
        agg.update(ct(8, 46), bar_1m=FakeBar(510, 999, 400, 500))
        assert agg.rth_15min_high == 515
        assert agg.rth_15min_low == 499

    def test_captures_60min_high_low_at_930(self, agg):
        agg.update(ct(8, 31), bar_1m=FakeBar(500, 520, 499, 518))
        agg.update(ct(9, 10), bar_1m=FakeBar(518, 530, 515, 525))
        agg.update(ct(9, 29), bar_1m=FakeBar(525, 527, 516, 520))
        # Post-09:30 bar must not extend the IB.
        agg.update(ct(9, 31), bar_1m=FakeBar(520, 700, 100, 300))
        assert agg.rth_60min_high == 530
        assert agg.rth_60min_low == 499

    def test_rth_fields_none_before_capture_time(self, agg):
        # At 08:00 CT, nothing RTH-related should be set.
        agg.update(ct(8, 0), bar_1m=FakeBar(500, 505, 499, 502))
        assert agg.rth_open_price is None
        assert agg.rth_5min_high is None
        assert agg.rth_15min_high is None
        assert agg.rth_60min_high is None


# ═══════════════════════════════════════════════════════════════════
# ORB break tracking (3 tests)
# ═══════════════════════════════════════════════════════════════════
class TestORBBreakTracking:
    def _prime_15min(self, agg):
        """Populate a 500-515 15-min OR."""
        agg.update(ct(8, 31), bar_1m=FakeBar(500, 510, 499, 508))
        agg.update(ct(8, 43), bar_1m=FakeBar(508, 515, 505, 510))

    def test_orb_first_break_long_set_on_first_close_above_15min_high(self, agg):
        self._prime_15min(agg)
        agg.update(ct(8, 50), bar_5m=FakeBar(510, 520, 509, 517))  # close > 515
        assert agg.orb_first_break_direction == "LONG"

    def test_orb_first_break_short_set_on_first_close_below_15min_low(self, agg):
        self._prime_15min(agg)
        agg.update(ct(8, 50), bar_5m=FakeBar(510, 511, 490, 495))  # close < 499
        assert agg.orb_first_break_direction == "SHORT"

    def test_orb_first_break_persists_for_session(self, agg):
        self._prime_15min(agg)
        agg.update(ct(8, 50), bar_5m=FakeBar(510, 520, 509, 517))  # LONG set
        # Later SHORT break must NOT overwrite.
        agg.update(ct(10, 0), bar_5m=FakeBar(515, 516, 490, 495))
        assert agg.orb_first_break_direction == "LONG"


# ═══════════════════════════════════════════════════════════════════
# Opening type + auction-out check (3 tests)
# ═══════════════════════════════════════════════════════════════════
class TestOpeningTypeAndAuctionOut:
    def _primed(self, tmp_path) -> SessionLevelsAggregator:
        """Aggregator with a prior day loaded so classifier has VAH/VAL/etc."""
        pd = date(2026, 4, 17)
        # 12 uniform bars so POC/VAH/VAL + pivots + avg_5min_volume all compute.
        write_jsonl(tmp_path, pd, "lab", [
            _bar_event("5m", 25000, 25010, 24990, 25000, volume=1000)
            for _ in range(12)
        ])
        a = SessionLevelsAggregator("lab", history_dir=tmp_path)
        a.load_prior_day(target_date=date(2026, 4, 20))
        return a

    def test_opening_type_classified_at_835(self, tmp_path):
        a = self._primed(tmp_path)
        # Drive a clean OPEN_AUCTION_IN: open=25000 inside VAH/VAL, small displacement.
        a.update(ct(8, 30, 5), bar_1m=FakeBar(25000, 25001, 24999, 25000))
        a.update(ct(8, 35), bar_5m=FakeBar(25000, 25005, 24995, 25001, volume=1000))
        assert a.opening_type is not None
        assert a.opening_type in (
            "OPEN_DRIVE", "OPEN_TEST_DRIVE",
            "OPEN_AUCTION_IN", "OPEN_AUCTION_OUT", "INDETERMINATE",
        )

    def test_auction_out_holds_outside_set_at_845_true_when_outside(self, tmp_path):
        a = self._primed(tmp_path)
        # Prior day range was 24990-25010. Price at 08:45 bar close=25050 is outside.
        a.update(ct(8, 45), bar_1m=FakeBar(25050, 25052, 25048, 25050))
        assert a.opening_holds_outside_at_845 is True

    def test_auction_out_holds_outside_set_at_845_false_when_returned_inside(self, tmp_path):
        a = self._primed(tmp_path)
        # Close at 08:45 is 25000 — inside [24990, 25010].
        a.update(ct(8, 45), bar_1m=FakeBar(25005, 25010, 24998, 25000))
        assert a.opening_holds_outside_at_845 is False


# ═══════════════════════════════════════════════════════════════════
# get_levels_dict (3 tests)
# ═══════════════════════════════════════════════════════════════════
class TestGetLevelsDict:
    _EXPECTED_KEYS = {
        "now_ct",
        "prior_day_open", "prior_day_high", "prior_day_low", "prior_day_close",
        "prior_day_poc", "prior_day_vah", "prior_day_val",
        "pivot_pp", "pivot_r1", "pivot_r2", "pivot_s1", "pivot_s2",
        "pmh", "pml",
        "rth_open_price", "rth_5min_high", "rth_5min_low", "rth_5min_close",
        "rth_15min_high", "rth_15min_low", "rth_60min_high", "rth_60min_low",
        "opening_type", "opening_holds_outside_at_845",
        "orb_first_break_direction",
        "avg_5min_volume", "rth_5min_volume",
    }

    def test_get_levels_dict_returns_all_fields(self, agg):
        d = agg.get_levels_dict()
        assert self._EXPECTED_KEYS.issubset(d.keys())

    def test_get_levels_dict_none_for_uncomputed_fields(self, agg):
        # No prior day loaded, no updates fed → all level fields None.
        d = agg.get_levels_dict()
        for k in self._EXPECTED_KEYS - {"now_ct"}:
            assert d[k] is None, f"{k} should be None when uncomputed"

    def test_now_ct_always_present_in_dict(self, agg):
        # Before any update — now_ct falls back to datetime.now().
        d_before = agg.get_levels_dict()
        assert isinstance(d_before["now_ct"], datetime)

        # After update — now_ct reflects what the caller passed.
        passed = ct(10, 30)
        agg.update(passed, bar_1m=FakeBar(100, 101, 99, 100))
        d_after = agg.get_levels_dict()
        assert d_after["now_ct"] == passed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
