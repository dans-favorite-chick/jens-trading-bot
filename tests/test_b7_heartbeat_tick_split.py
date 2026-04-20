"""
B7 — Heartbeat vs tick timestamp split in bridge_server.py.

The 2026-04-16 silent-stall incident exposed a blindspot: bridge conflated
heartbeats and ticks into a single nt8_last_tick_time, so when NT8's feed
froze (heartbeats still arriving, zero ticks), the bridge couldn't detect
the stall at all. B7 splits these into two timestamps and gives
stale_watcher distinct "SOCKET_DEAD" vs "SILENT_STALL" signals.

Run: python -m unittest tests.test_b7_heartbeat_tick_split -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _read_bridge_src() -> str:
    return (Path(__file__).parent.parent / "bridge" / "bridge_server.py").read_text(
        encoding="utf-8"
    )


class TestTimestampsExistAndSeparate(unittest.TestCase):
    def test_heartbeat_time_attribute_added(self):
        src = _read_bridge_src()
        self.assertIn("self.nt8_last_heartbeat_time", src,
                      "B7 fix: nt8_last_heartbeat_time attribute missing")

    def test_heartbeat_handler_bumps_only_heartbeat_time(self):
        """heartbeat handler must NOT bump nt8_last_tick_time (that's the
        exact blindspot B7 fixes — a frozen NT8 with fresh heartbeats kept
        nt8_last_tick_time fresh and stalled invisibly)."""
        src = _read_bridge_src()
        # Find the heartbeat handler block
        idx = src.index('elif msg_type == "heartbeat":')
        # Get the next ~5 lines — the handler body
        tick_idx = src.index('elif msg_type == "tick":', idx)
        block = src[idx:tick_idx]
        self.assertIn("self.nt8_last_heartbeat_time = time.time()", block)
        # Must NOT bump tick_time in heartbeat handler
        self.assertNotIn("self.nt8_last_tick_time = time.time()", block,
                         "heartbeat handler still bumps nt8_last_tick_time — B7 regressed")

    def test_tick_handler_bumps_both(self):
        """tick handler should bump BOTH (ticks imply liveness)."""
        src = _read_bridge_src()
        idx = src.index('elif msg_type == "tick":')
        # Get the next ~10 lines
        tail = src[idx:idx + 600]
        self.assertIn("self.nt8_last_tick_time = time.time()", tail)
        self.assertIn("self.nt8_last_heartbeat_time = time.time()", tail)


class TestStaleWatcherDistinctSignals(unittest.TestCase):
    def test_stale_watcher_emits_socket_dead(self):
        src = _read_bridge_src()
        self.assertIn("SOCKET_DEAD", src,
                      "stale_watcher must emit SOCKET_DEAD signal (B7)")

    def test_stale_watcher_emits_silent_stall(self):
        src = _read_bridge_src()
        self.assertIn("SILENT_STALL", src,
                      "stale_watcher must emit SILENT_STALL signal (B7)")

    def test_stale_watcher_has_recovery_transitions(self):
        """Both failure modes should log 'resumed' / 'cleared' transitions."""
        src = _read_bridge_src()
        watcher_start = src.index("async def stale_watcher")
        watcher_end = src.index("async def", watcher_start + 1)
        body = src[watcher_start:watcher_end]
        self.assertIn("SOCKET RESUMED", body)
        self.assertIn("SILENT_STALL cleared", body)


class TestHealthEndpointExposesBoth(unittest.TestCase):
    def test_health_reports_heartbeat_age(self):
        src = _read_bridge_src()
        self.assertIn("nt8_last_heartbeat_age_s", src,
                      "health endpoint should expose heartbeat age alongside tick age (B7)")

    def test_nt8_status_includes_silent_stall_tier(self):
        src = _read_bridge_src()
        self.assertIn('"silent_stall"', src,
                      "nt8_status must have silent_stall tier")


class TestSilentStallDecisionLogic(unittest.TestCase):
    """Pure-logic simulation of the SILENT_STALL detection rule.
    Mirrors stale_watcher's decision — if this logic regresses, the
    2026-04-16 incident class becomes invisible again."""

    def _detect_silent_stall(self, hb_age: float, tick_age: float,
                              hb_max: float = 10, tick_min: float = 60):
        return hb_age < hb_max and tick_age > tick_min

    def test_normal_flow_no_stall(self):
        # Both fresh
        self.assertFalse(self._detect_silent_stall(hb_age=1, tick_age=1))

    def test_socket_dead_not_silent_stall(self):
        # Heartbeat stale too → it's socket dead, not silent stall
        self.assertFalse(self._detect_silent_stall(hb_age=100, tick_age=200))

    def test_silent_stall_hits(self):
        # Heartbeat fresh, ticks stale → silent stall
        self.assertTrue(self._detect_silent_stall(hb_age=3, tick_age=90))

    def test_borderline_tick_age(self):
        # Exactly at tick threshold — should NOT fire (>, not >=)
        self.assertFalse(self._detect_silent_stall(hb_age=3, tick_age=60))

    def test_recovery_when_tick_arrives(self):
        # Fresh tick clears the stall even if heartbeats were still aging
        self.assertFalse(self._detect_silent_stall(hb_age=5, tick_age=1))


if __name__ == "__main__":
    unittest.main()
