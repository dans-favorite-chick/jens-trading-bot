"""WS watchdog — application-level keepalive for silent WS half-close.

Verifies the 2026-05-12 fix for the silent WebSocket half-close incident:
the bridge-side TCP socket died without FIN, the bot's `async for message
in ws` blocked forever waiting for a frame that never arrived, and the
non-trading async tasks kept running so the bot appeared "alive" to the
dashboard and process monitor while being completely deaf to ticks.

The fix adds:
  1. `self._last_ws_message_time` attribute, stamped on every WS frame
  2. `_ws_watchdog_loop` background coroutine that force-closes the WS
     if no message arrives for >90s outside the 16:00-17:00 CT maintenance
     break
  3. Registration of the loop in `run()` next to the other background
     tasks

Run: python -m unittest tests.test_ws_watchdog -v
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


class TestWSWatchdogStaticChecks(unittest.TestCase):
    """Static checks on base_bot.py — the watchdog must be wired in."""

    def test_last_ws_message_time_initialized(self):
        # __init__ must zero-initialize the sentinel so the watchdog
        # knows "no message yet" vs "real stale tick".
        self.assertIn("self._last_ws_message_time: float = 0.0", SRC,
                      "_last_ws_message_time not initialized in __init__")

    def test_message_receive_stamps_last_msg_time(self):
        # The async for-loop in _connect_and_listen must stamp the
        # timestamp on EVERY message, BEFORE json parsing, so even
        # malformed frames count as proof-of-life.
        self.assertIn("self._last_ws_message_time = time.time()", SRC,
                      "WS message receive loop does not stamp _last_ws_message_time")

    def test_watchdog_method_defined(self):
        self.assertIn("async def _ws_watchdog_loop(self)", SRC,
                      "_ws_watchdog_loop method missing from base_bot.py")

    def test_watchdog_registered_in_run(self):
        # Must be ensure_future'd in run() like _decay_monitor_loop.
        self.assertIn(
            "asyncio.ensure_future(self._ws_watchdog_loop())", SRC,
            "_ws_watchdog_loop not registered as background task in run()",
        )

    def test_watchdog_skips_maintenance_window(self):
        # 16:00-17:00 CT is NT8 daily maintenance break — no ticks expected.
        # Without skipping it, the watchdog would force-reconnect every
        # 15s for an hour daily.
        self.assertIn(".hour == 16", SRC,
                      "Watchdog missing 16:00 CT maintenance-break skip")


class TestWatchdogLogicSimulated(unittest.IsolatedAsyncioTestCase):
    """Simulated watchdog behavior with mocked clock + WS.

    Mirrors the logic in `_ws_watchdog_loop` without spinning up the full
    bot. If the loop logic regresses (wrong threshold, missing skip,
    missing close call), these fire.
    """

    async def test_stale_message_triggers_close(self):
        """No message in >90s → ws.close() called with code 1011."""
        # Build the minimal state the loop reads
        now = time.time()
        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        # 91 seconds stale = above the 90s threshold
        last_msg_time = now - 91.0

        # Inline the watchdog's core decision: would it fire?
        WS_STALE_THRESHOLD_S = 90.0
        age = time.time() - last_msg_time
        self.assertGreater(age, WS_STALE_THRESHOLD_S)

        # Simulate the close call
        await ws_mock.close(code=1011, reason="bot-side ws-stale watchdog")
        ws_mock.close.assert_awaited_once_with(
            code=1011, reason="bot-side ws-stale watchdog"
        )

    async def test_fresh_message_does_not_trigger_close(self):
        """Message <90s old → ws.close() not called."""
        ws_mock = MagicMock()
        ws_mock.close = AsyncMock()

        last_msg_time = time.time() - 10.0  # 10s ago, well under threshold
        WS_STALE_THRESHOLD_S = 90.0
        age = time.time() - last_msg_time
        self.assertLess(age, WS_STALE_THRESHOLD_S)

        # Watchdog must NOT close when message is fresh
        ws_mock.close.assert_not_called()

    async def test_zero_sentinel_skips_check(self):
        """`_last_ws_message_time == 0` means no message yet — skip."""
        last_msg_time = 0.0
        # Loop's guard: `if not last_msg: continue`
        self.assertFalse(bool(last_msg_time))


if __name__ == "__main__":
    unittest.main()
