"""
G-B26 / S1 phase-eh — MenthorQ feed parser robustness.

Covers:
  * _coerce_float: None, empty string, whitespace, NaN, +/-Inf, -0.0,
    bool, numeric strings, lists.
  * load_bridge_levels: round-trip of realistic JSON and a hostile
    JSON (NaN / empty / None / missing keys) with no exceptions and
    all-finite float outputs.
  * load(): CRITICAL log fires when data/menthorq_daily.json is
    > 24h old (phase-eh staleness check).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCoerceFloat(unittest.TestCase):
    """_coerce_float is the G-B26 robustness primitive."""

    def test_none_returns_default(self):
        from core.menthorq_feed import _coerce_float
        self.assertEqual(_coerce_float(None), 0.0)
        self.assertEqual(_coerce_float(None, default=1.0), 1.0)

    def test_empty_string_returns_default(self):
        from core.menthorq_feed import _coerce_float
        self.assertEqual(_coerce_float(""), 0.0)
        self.assertEqual(_coerce_float("   "), 0.0)
        self.assertEqual(_coerce_float("\t\n"), 0.0)

    def test_nan_and_inf_return_default(self):
        from core.menthorq_feed import _coerce_float
        self.assertEqual(_coerce_float(float("nan")), 0.0)
        self.assertEqual(_coerce_float(float("inf")), 0.0)
        self.assertEqual(_coerce_float(float("-inf")), 0.0)
        self.assertEqual(_coerce_float("nan"), 0.0)
        self.assertEqual(_coerce_float("inf"), 0.0)

    def test_negative_zero_normalizes_to_zero(self):
        from core.menthorq_feed import _coerce_float
        out = _coerce_float(-0.0)
        self.assertEqual(out, 0.0)
        # The important part: no "-" sign in the representation
        self.assertEqual(repr(out), "0.0")

    def test_numeric_string_parses(self):
        from core.menthorq_feed import _coerce_float
        self.assertEqual(_coerce_float("25210.5"), 25210.5)
        self.assertEqual(_coerce_float(" 42 "), 42.0)
        self.assertEqual(_coerce_float("-2.1"), -2.1)

    def test_garbage_string_returns_default(self):
        from core.menthorq_feed import _coerce_float
        self.assertEqual(_coerce_float("abc"), 0.0)
        self.assertEqual(_coerce_float("25210.x"), 0.0)

    def test_bool_returns_default(self):
        # bool is int subclass — explicitly rejected so True != 1.0.
        from core.menthorq_feed import _coerce_float
        self.assertEqual(_coerce_float(True), 0.0)
        self.assertEqual(_coerce_float(False), 0.0)

    def test_list_and_dict_return_default(self):
        from core.menthorq_feed import _coerce_float
        self.assertEqual(_coerce_float([1.0]), 0.0)
        self.assertEqual(_coerce_float({"v": 1.0}), 0.0)

    def test_int_returns_float(self):
        from core.menthorq_feed import _coerce_float
        out = _coerce_float(42)
        self.assertIsInstance(out, float)
        self.assertEqual(out, 42.0)


class TestLoadBridgeLevelsRobustness(unittest.TestCase):
    """load_bridge_levels must survive hostile JSON without exceptions."""

    def _write_bridge(self, payload: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def test_round_trip_clean_payload(self):
        from core import menthorq_feed
        payload = {
            "hvl": 25210.0,
            "call_resistance": 25300.0,
            "put_support": 25100.0,
            "day_min": 25050.0,
            "day_max": 25350.0,
            "gex_1": 25220.0,
            "qqq_to_nq_ratio": 41.2,
            "ts": "2026-04-21T08:00:00",
            "source": "MQBridge",
        }
        path = self._write_bridge(payload)
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                levels = menthorq_feed.load_bridge_levels()
            self.assertEqual(levels["hvl"], 25210.0)
            self.assertEqual(levels["call_resistance_all"], 25300.0)
            self.assertEqual(levels["put_support_all"], 25100.0)
            self.assertEqual(levels["day_min"], 25050.0)
            self.assertEqual(levels["gex_level_1"], 25220.0)
            self.assertEqual(levels["_bridge_ratio"], 41.2)
        finally:
            os.unlink(path)

    def test_hostile_payload_returns_zeros_no_nan(self):
        """Missing keys / None / empty strings / 'NaN' strings must all
        degrade to 0.0 and never produce a NaN or exception."""
        from core import menthorq_feed
        payload = {
            "hvl": None,
            "call_resistance": "",
            "put_support": "   ",
            "day_min": "NaN",
            "day_max": "not a number",
            "gex_1": -0.0,
            # gex_2..gex_10 intentionally missing
            "qqq_to_nq_ratio": None,
        }
        path = self._write_bridge(payload)
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                levels = menthorq_feed.load_bridge_levels()
            # Must return a dict (not {}).
            self.assertIsInstance(levels, dict)
            # All numeric values must be finite floats.
            for k, v in levels.items():
                if k.startswith("_"):
                    continue
                self.assertIsInstance(v, float, f"{k} was {type(v).__name__}")
                self.assertTrue(math.isfinite(v), f"{k}={v} not finite")
                # negative-zero must have been normalized out
                self.assertFalse(
                    v == 0.0 and math.copysign(1.0, v) < 0,
                    f"{k} is -0.0 (should be +0.0)"
                )
        finally:
            os.unlink(path)

    def test_empty_json_object(self):
        from core import menthorq_feed
        path = self._write_bridge({})
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                levels = menthorq_feed.load_bridge_levels()
            self.assertEqual(levels["hvl"], 0.0)
            self.assertEqual(levels["gex_level_10"], 0.0)
        finally:
            os.unlink(path)


class TestDailyJsonStalenessCritical(unittest.TestCase):
    """phase-eh: load() must emit CRITICAL when daily JSON is > 24h old."""

    def _write_daily(self, age_hours: float, date_str: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": date_str}, f)
        mtime = time.time() - (age_hours * 3600.0)
        os.utime(path, (mtime, mtime))
        return path

    def test_fresh_file_no_critical(self):
        from core import menthorq_feed
        path = self._write_daily(age_hours=1.0, date_str=str(menthorq_feed.date.today()))
        try:
            with patch.object(menthorq_feed, "DATA_FILE", path), \
                 patch.object(menthorq_feed, "BRIDGE_FILE", "/nonexistent/x.json"):
                with self.assertLogs("MenthorQ", level="CRITICAL") as cm:
                    menthorq_feed.load()
                    # We need at least one log of any level — but no CRITICAL
                    # should be present. assertLogs requires one record though,
                    # so force a sentinel log.
                    logging.getLogger("MenthorQ").critical("__sentinel__")
            critical_lines = [r for r in cm.records if r.levelname == "CRITICAL"
                              and "__sentinel__" not in r.getMessage()]
            self.assertEqual(critical_lines, [], "No CRITICAL expected for fresh file")
        finally:
            os.unlink(path)

    def test_stale_file_emits_critical(self):
        from core import menthorq_feed
        # 30 hours old — past the 24h threshold.
        path = self._write_daily(age_hours=30.0, date_str="2026-04-19")
        try:
            with patch.object(menthorq_feed, "DATA_FILE", path), \
                 patch.object(menthorq_feed, "BRIDGE_FILE", "/nonexistent/x.json"):
                with self.assertLogs("MenthorQ", level="CRITICAL") as cm:
                    menthorq_feed.load()
            msgs = [r.getMessage() for r in cm.records if r.levelname == "CRITICAL"]
            self.assertTrue(any("24h" in m or "h old" in m for m in msgs),
                            f"Expected CRITICAL about staleness, got: {msgs}")
            # and the number of hours should appear
            self.assertTrue(any("30." in m or "29." in m or "h old" in m
                                for m in msgs),
                            f"Expected age hours in message: {msgs}")
        finally:
            os.unlink(path)

    def test_load_does_not_raise_when_stale(self):
        """Non-blocking: stale file must not take down startup."""
        from core import menthorq_feed
        path = self._write_daily(age_hours=72.0, date_str="2026-04-17")
        try:
            with patch.object(menthorq_feed, "DATA_FILE", path), \
                 patch.object(menthorq_feed, "BRIDGE_FILE", "/nonexistent/x.json"):
                snap = menthorq_feed.load()  # must not raise
            self.assertIsNotNone(snap)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
