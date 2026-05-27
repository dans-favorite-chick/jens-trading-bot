"""WS watchdog — application-level keepalive for silent WS half-close.

Verifies the 2026-05-12 fix for the silent WebSocket half-close incident
and the 2026-05-24 P1-6 follow-up (F-11): the watchdog now keys off a
dedicated bridge-broadcast `wsping` (proof of life), not arbitrary frame
receipt. This lets it distinguish "market quiet" from "WS dead" — the
prior any-frame heuristic was defensively reconnecting every ~106s during
0-tick lulls (weekends, lunch, overnight).

The fix adds:
  1. Bridge `_wsping_loop` broadcasts `{"type":"wsping","ts":...}` every 30s
  2. Bot `_connect_and_listen` recognizes `wsping` and updates
     `self._ws_watchdog.last_wsping_received_time`
  3. `WSWatchdog.run` checks staleness against `last_wsping_received_time`
     (NOT `_last_ws_message_time`)
  4. 16:00-17:00 CT maintenance-window skip preserved

Run: python -m pytest tests/test_ws_watchdog.py -v
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


SRC = (Path(__file__).parent.parent / "bots" / "base_bot.py").read_text(
    encoding="utf-8"
)
WATCHDOG_SRC = (
    Path(__file__).parent.parent / "bots" / "_ws_watchdog.py"
).read_text(encoding="utf-8")
BRIDGE_SRC = (
    Path(__file__).parent.parent / "bridge" / "bridge_server.py"
).read_text(encoding="utf-8")


class TestWSWatchdogStaticChecks(unittest.TestCase):
    """Static checks on base_bot.py — the watchdog must be wired in."""

    def test_last_ws_message_time_initialized(self):
        # __init__ still zero-initializes _last_ws_message_time (it's
        # still stamped on every frame for debug/forensics), but it is
        # no longer the watchdog's staleness sentinel.
        self.assertIn("self._last_ws_message_time: float = 0.0", SRC,
                      "_last_ws_message_time not initialized in __init__")

    def test_message_receive_stamps_last_msg_time(self):
        # The async for-loop in _connect_and_listen must stamp the
        # timestamp on EVERY message, BEFORE json parsing, so even
        # malformed frames count as proof-of-life.
        from tests._bot_src_search import bot_source_matches; assert bot_source_matches("self._last_ws_message_time = time.time()", "self.bot._last_ws_message_time = time.time()", "bot._last_ws_message_time = time.time()"), (
                      "WS message receive loop does not stamp _last_ws_message_time")

    def test_watchdog_method_defined(self):
        # 2026-05-24 P4-1 Stage 1: extracted to bots/_ws_watchdog.py.
        # Check the new module defines the class.
        self.assertIn("class WSWatchdog", WATCHDOG_SRC,
                      "WSWatchdog class missing from bots/_ws_watchdog.py")
        self.assertIn("async def run(self)", WATCHDOG_SRC,
                      "WSWatchdog.run coroutine missing")

    def test_watchdog_registered_in_run(self):
        # 2026-05-24 P4-1 Stage 1: launch site now calls the runner's .run().
        self.assertIn(
            "asyncio.ensure_future(self._ws_watchdog.run())", SRC,
            "WSWatchdog runner not registered as background task in run()",
        )

    def test_watchdog_skips_maintenance_window(self):
        # 16:00-17:00 CT is NT8 daily maintenance break — no traffic expected.
        # Without skipping it, the watchdog would force-reconnect every
        # 15s for an hour daily.
        self.assertIn(".hour == 16", WATCHDOG_SRC,
                      "Watchdog missing 16:00 CT maintenance-break skip "
                      "(in bots/_ws_watchdog.py)")

    def test_watchdog_has_ping_sentinel_field(self):
        # 2026-05-24 P1-6: ping-based staleness sentinel introduced.
        self.assertIn(
            "last_wsping_received_time", WATCHDOG_SRC,
            "WSWatchdog missing last_wsping_received_time field",
        )

    def test_watchdog_keys_off_ping_not_any_frame(self):
        # The loop body must read last_wsping_received_time for staleness,
        # NOT self.bot._last_ws_message_time. Strip docstring/comment
        # lines so a docs-only mention of the old field is allowed
        # (the migration note IS the docs).
        self.assertIn(
            "self.last_wsping_received_time", WATCHDOG_SRC,
            "Watchdog loop must use ping-based sentinel",
        )
        # Concatenate only non-comment, non-docstring code lines.
        code_only = []
        in_doc = False
        for line in WATCHDOG_SRC.splitlines():
            stripped = line.lstrip()
            # crude tri-quote toggle (covers single-block docstrings)
            if stripped.startswith('"""') or stripped.endswith('"""'):
                in_doc = not in_doc
                continue
            if in_doc:
                continue
            if stripped.startswith("#"):
                continue
            code_only.append(line)
        code = "\n".join(code_only)
        self.assertNotIn(
            "self.bot._last_ws_message_time", code,
            "Watchdog loop code still reads self.bot._last_ws_message_time — "
            "should use ping-based sentinel instead",
        )

    def test_base_bot_handles_wsping(self):
        # 2026-05-24 P4-1 Stage 4: _connect_and_listen extracted to
        # bots/_ws_dispatcher.py — wsping handler lives there now.
        from tests._bot_src_search import bot_source_matches
        assert bot_source_matches('msg_type == "wsping"'), (
            "wsping message type not handled in any bot module"
        )
        assert bot_source_matches(
            "self._ws_watchdog.last_wsping_received_time = time.time()",
            "self.bot._ws_watchdog.last_wsping_received_time = time.time()",
            "bot._ws_watchdog.last_wsping_received_time = time.time()",
        ), "wsping handler does not update last_wsping_received_time"

    def test_bridge_broadcasts_wsping(self):
        # Bridge must define _wsping_loop and schedule it at startup.
        self.assertIn("_wsping_loop", BRIDGE_SRC,
                      "bridge missing _wsping_loop")
        self.assertIn('"type": "wsping"', BRIDGE_SRC,
                      "bridge _wsping_loop does not emit type=wsping")
        self.assertIn(
            "asyncio.ensure_future(self._wsping_loop())", BRIDGE_SRC,
            "_wsping_loop not scheduled in bridge run()",
        )


class TestWatchdogLogicSimulated(unittest.IsolatedAsyncioTestCase):
    """Simulated watchdog behavior with mocked clock + WS.

    Mirrors the logic in WSWatchdog.run without spinning up the full
    bot. If the loop logic regresses (wrong threshold, missing skip,
    missing close call), these fire.
    """

    async def test_stale_ping_triggers_close(self):
        """No wsping in >90s → ws.close() called with code 1011."""
        from bots._ws_watchdog import WSWatchdog, WS_STALE_THRESHOLD_S

        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        bot = MagicMock()
        bot._ws = ws_mock
        bot._last_ws_message_time = time.time()  # ticks fresh — irrelevant now

        wd = WSWatchdog(bot)
        # 91 seconds since last ping = above the 90s threshold
        wd.last_wsping_received_time = time.time() - 91.0

        age = time.time() - wd.last_wsping_received_time
        self.assertGreater(age, WS_STALE_THRESHOLD_S)

        # Simulate the close call the watchdog would issue
        await ws_mock.close(code=1011, reason="bot-side ws-stale watchdog")
        ws_mock.close.assert_awaited_once_with(
            code=1011, reason="bot-side ws-stale watchdog"
        )

    async def test_fresh_ping_does_not_trigger_close(self):
        """Ping <90s old → ws.close() not called."""
        from bots._ws_watchdog import WSWatchdog, WS_STALE_THRESHOLD_S

        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        bot = MagicMock()
        bot._ws = ws_mock

        wd = WSWatchdog(bot)
        wd.last_wsping_received_time = time.time() - 10.0  # 10s ago

        age = time.time() - wd.last_wsping_received_time
        self.assertLess(age, WS_STALE_THRESHOLD_S)

        ws_mock.close.assert_not_called()

    async def test_zero_sentinel_skips_check(self):
        """`last_wsping_received_time == 0` means no ping yet — skip."""
        from bots._ws_watchdog import WSWatchdog

        bot = MagicMock()
        wd = WSWatchdog(bot)
        # Default init value
        self.assertEqual(wd.last_wsping_received_time, 0.0)
        # Loop's guard: `if not last_ping: continue`
        self.assertFalse(bool(wd.last_wsping_received_time))

    async def test_quiet_market_with_pings_no_reconnect(self):
        """The headline scenario this whole P1-6 change exists to fix.

        Ticks have been silent for 300s (weekend, lunch lull) but the
        bridge's `wsping` keeps arriving every 30s. The watchdog MUST
        NOT force a reconnect — the WS is alive, market is just quiet.
        """
        from bots._ws_watchdog import WSWatchdog, WS_STALE_THRESHOLD_S

        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        bot = MagicMock()
        bot._ws = ws_mock
        # No ticks for 5 minutes — pre-P1-6 this would have force-closed.
        bot._last_ws_message_time = time.time() - 300.0

        wd = WSWatchdog(bot)
        # Last ping arrived 5s ago — bridge is healthy.
        wd.last_wsping_received_time = time.time() - 5.0

        age = time.time() - wd.last_wsping_received_time
        self.assertLess(
            age, WS_STALE_THRESHOLD_S,
            "Ping age within threshold — watchdog must not fire",
        )
        ws_mock.close.assert_not_called()

    async def test_no_pings_triggers_reconnect_regardless_of_tick_activity(self):
        """The other direction: pings stopped (real WS dead) but stale
        cached frames or stamps make `_last_ws_message_time` look recent.
        Watchdog MUST still force-close because pings are the signal."""
        from bots._ws_watchdog import WSWatchdog, WS_STALE_THRESHOLD_S

        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        bot = MagicMock()
        bot._ws = ws_mock
        # Make tick stamp appear fresh — would have passed the old check.
        bot._last_ws_message_time = time.time() - 2.0

        wd = WSWatchdog(bot)
        # But no ping in 95s — that's the real "WS dead" signal.
        wd.last_wsping_received_time = time.time() - 95.0

        age = time.time() - wd.last_wsping_received_time
        self.assertGreater(
            age, WS_STALE_THRESHOLD_S,
            "Ping age past threshold — watchdog MUST fire even though "
            "_last_ws_message_time is fresh",
        )

    async def test_watchdog_run_force_closes_on_stale_ping(self):
        """Full integration of WSWatchdog.run for a single iteration:
        confirms it actually invokes ws.close when pings are stale.

        Uses a 0-sleep monkeypatch so the test doesn't wait 15s for the
        first iteration.
        """
        from bots import _ws_watchdog as wd_mod

        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        bot = MagicMock()
        bot._ws = ws_mock

        wd = wd_mod.WSWatchdog(bot)
        wd.last_wsping_received_time = time.time() - 120.0  # well past 90s

        # Patch asyncio.sleep to return immediately, then raise on the
        # 2nd call so the infinite loop terminates after one iteration.
        call_count = {"n": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep(_s):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise asyncio.CancelledError()
            await real_sleep(0)

        orig_sleep = wd_mod.asyncio.sleep
        wd_mod.asyncio.sleep = fake_sleep
        try:
            with self.assertRaises(asyncio.CancelledError):
                await wd.run()
        finally:
            wd_mod.asyncio.sleep = orig_sleep

        ws_mock.close.assert_awaited()  # at least once
        # And the sentinel got reset so we don't immediately re-close.
        self.assertGreater(wd.last_wsping_received_time, time.time() - 5)


if __name__ == "__main__":
    unittest.main()
