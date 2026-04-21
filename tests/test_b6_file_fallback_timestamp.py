"""
B6 — file_fallback_poller must bump nt8_last_tick_time after each successful
fallback tick broadcast. Without this, stale_watcher falsely logs "NT8 stale"
forever while fallback is healthy (and blocks its own "resumed" transition).

Run: python -m unittest tests.test_b6_file_fallback_timestamp -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestFileFallbackTimestampBump(unittest.TestCase):
    """Static verification that the fix landed — a runtime test would
    require spinning up the whole bridge + a synthesized stale TCP."""

    def _src(self) -> str:
        return (Path(__file__).parent.parent / "bridge" / "bridge_server.py").read_text(
            encoding="utf-8"
        )

    def test_fallback_poller_bumps_last_tick_time(self):
        """The file_fallback_poller coroutine must contain an assignment
        to self.nt8_last_tick_time after the fallback broadcast."""
        src = self._src()
        # The file_fallback_poller function contains a `self.ticks_received += 1`
        # line; immediately after that (or before the next except), there must
        # be an assignment bumping nt8_last_tick_time.
        marker = "self.ticks_received += 1"
        self.assertIn(marker, src)
        # Check that nt8_last_tick_time is assigned in the same function body.
        # The poller's body is ~40 lines; grep for both markers in proximity.
        poller_start = src.index("async def file_fallback_poller")
        poller_end = src.index("async def", poller_start + 1) if src.find("async def", poller_start + 1) > 0 else len(src)
        poller_body = src[poller_start:poller_end]
        self.assertIn("self.nt8_last_tick_time = time.time()", poller_body,
                      "file_fallback_poller must bump nt8_last_tick_time (B6 fix)")

    def test_b6_marker_present(self):
        src = self._src()
        self.assertIn("B6 fix", src,
                      "B6 fix marker missing from bridge_server.py")

    def test_bump_is_after_broadcast(self):
        """The bump must come AFTER self._broadcast_to_bots(), not before
        (before = we'd mark fresh even on broadcast failure)."""
        src = self._src()
        poller_start = src.index("async def file_fallback_poller")
        poller_end = src.index("async def", poller_start + 1)
        body = src[poller_start:poller_end]
        broadcast_idx = body.index("self._broadcast_to_bots(")
        bump_idx = body.index("self.nt8_last_tick_time = time.time()")
        self.assertGreater(bump_idx, broadcast_idx,
                           "nt8_last_tick_time bump must come AFTER broadcast")


if __name__ == "__main__":
    unittest.main()
