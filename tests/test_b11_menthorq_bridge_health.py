"""
B11 — MenthorQ bridge staleness diagnostic tests.

The 2026-04-20 00:00 CT prod_bot log showed:
    "[MenthorQ] Bridge file is 2183 min old — MQBridge.cs may
     not be running in NT8"

That's a 36-hour stale — meaning MQBridge.cs hadn't been writing to
C:\\temp\\menthorq_levels.json since mid-Friday. Root cause is
operator-side (NT8 configuration), not code — but we can improve the
diagnostic signal so the escalation is obvious.

B11 upgrades the staleness check to tiered logging (warning at 5min,
error at 30min) and adds a `bridge_health()` helper that returns
structured state for dashboards/watchdogs/pre-flight checks.

Run: python -m unittest tests.test_b11_menthorq_bridge_health -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestBridgeHealthStatus(unittest.TestCase):
    """bridge_health() tier boundaries: missing / healthy / warning / stale."""

    def _with_bridge_file(self, age_min):
        """Context manager-ish — returns a patched BRIDGE_FILE path."""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tmp.write(b"{}")
        tmp.close()
        if age_min > 0:
            target_mtime = time.time() - (age_min * 60)
            os.utime(tmp.name, (target_mtime, target_mtime))
        return tmp.name

    def test_missing_bridge_file_returns_missing_status(self):
        from core import menthorq_feed
        with patch.object(menthorq_feed, "BRIDGE_FILE",
                          "/nonexistent/path/menthorq_levels.json"):
            state = menthorq_feed.bridge_health()
        self.assertEqual(state["status"], "missing")
        self.assertFalse(state["exists"])
        self.assertIn("MQBridge", state["action"])

    def test_healthy_file_returns_healthy_status(self):
        from core import menthorq_feed
        path = self._with_bridge_file(age_min=1)  # 1 min old
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                state = menthorq_feed.bridge_health()
            self.assertEqual(state["status"], "healthy")
            self.assertLessEqual(state["age_min"], 5)
        finally:
            os.unlink(path)

    def test_warning_tier_at_10_min(self):
        from core import menthorq_feed
        path = self._with_bridge_file(age_min=10)
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                state = menthorq_feed.bridge_health()
            self.assertEqual(state["status"], "warning")
            self.assertIn("reload", state["action"].lower())
        finally:
            os.unlink(path)

    def test_stale_tier_at_60_min(self):
        from core import menthorq_feed
        path = self._with_bridge_file(age_min=60)
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                state = menthorq_feed.bridge_health()
            self.assertEqual(state["status"], "stale")
            self.assertIn("NOT writing", state["action"])
        finally:
            os.unlink(path)

    def test_boundary_just_under_5_min_is_healthy(self):
        """Use 4.5 min to avoid mtime-set-vs-stat-read timing jitter at
        the boundary (setting mtime=5min ago and reading age_min returns
        slightly > 5 due to elapsed test-code time)."""
        from core import menthorq_feed
        path = self._with_bridge_file(age_min=4.5)
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                state = menthorq_feed.bridge_health()
            self.assertEqual(state["status"], "healthy")
        finally:
            os.unlink(path)

    def test_boundary_just_under_30_min_is_warning(self):
        from core import menthorq_feed
        path = self._with_bridge_file(age_min=25)
        try:
            with patch.object(menthorq_feed, "BRIDGE_FILE", path):
                state = menthorq_feed.bridge_health()
            self.assertEqual(state["status"], "warning")
        finally:
            os.unlink(path)


class TestLoadBridgeLevelsTieredLogging(unittest.TestCase):
    """load_bridge_levels must escalate log level by staleness tier."""

    def _src(self) -> str:
        return (Path(__file__).parent.parent / "core" / "menthorq_feed.py").read_text(
            encoding="utf-8"
        )

    def test_b11_marker_present(self):
        src = self._src()
        self.assertIn("B11 fix", src,
                      "B11 marker missing from menthorq_feed.py")

    def test_tiered_thresholds(self):
        """Both the 5-min and 30-min boundaries must be in the source."""
        src = self._src()
        # 30-min tier uses logger.error
        self.assertIn("age_min > 30", src)
        # 5-min tier uses logger.warning
        self.assertIn("elif age_min > 5", src)

    def test_error_log_mentions_operator_action(self):
        """The 30-min-stale log must tell the operator what to DO,
        not just note that the file is stale."""
        src = self._src()
        # Verify the error branch contains action keywords
        err_idx = src.index("age_min > 30")
        # Grab a window around the error branch
        window = src[err_idx:err_idx + 800]
        self.assertIn("NOT RUNNING", window)
        self.assertTrue("Reload NinjaScript Output" in window or "MQBridge" in window,
                        "30-min error log should reference operator action")


if __name__ == "__main__":
    unittest.main()
