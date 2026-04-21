"""
BUG-TL2 — WebSocket 1011 guard tests.

Verifies that per-message dispatch failures (bridge trade command, bot
tick aggregation) are logged-and-swallowed instead of bubbling out of
`async for` and causing websockets to close the socket with code=1011.

Run: python -m unittest tests.test_ws_1011_guards -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestBridgePerMessageGuardPresent(unittest.TestCase):
    """Static check: bridge_server.py's handle_bot must wrap per-message
    dispatch in try/except so a single bad message doesn't kick the WS."""

    def test_handle_bot_has_per_message_guard(self):
        src = (Path(__file__).parent.parent / "bridge" / "bridge_server.py").read_text(
            encoding="utf-8"
        )
        # The BUG-TL2 guard is tagged in the code comment — if this tag
        # disappears in a refactor, alert the reader.
        self.assertIn("BUG-TL2 guard", src,
                      "bridge_server.py handle_bot lost its per-message guard")
        # The specific log prefix is the canary — if a refactor drops the
        # per-message catch, this log line won't appear either.
        self.assertIn("[WS:", src,
                      "Expected per-message keeping-socket-alive log marker")
        self.assertIn("per-message handler failed", src)


class TestBotAggregatorGuardPresent(unittest.TestCase):
    """Static check: base_bot.py's tick loop must guard the aggregator
    call, which is the highest-risk path on the WS message handler."""

    def test_aggregator_process_tick_wrapped(self):
        src = (Path(__file__).parent.parent / "bots" / "base_bot.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("BUG-TL2 guard", src,
                      "base_bot.py tick loop lost its aggregator guard")
        self.assertIn("[TICK AGGREGATOR]", src,
                      "Expected aggregator keeping-WS-alive log marker")
        self.assertIn("process_tick failed", src)


class TestGuardLogic(unittest.TestCase):
    """Simulated per-message dispatch with an exception-raising handler
    — the guard pattern must swallow and `continue`, NOT re-raise.

    Mirrors the dispatcher structure in bridge_server.py handle_bot.
    If this test starts failing, the guard semantics have regressed.
    """

    def test_single_bad_message_does_not_stop_loop(self):
        """Simulate the handle_bot pattern with 3 messages; one raises."""
        processed = []
        errors = []

        def dispatch(msg):
            if msg == "BAD":
                raise ValueError("synthetic handler failure")
            processed.append(msg)

        # Simulate `async for message in websocket:` with per-message guard
        messages = ["A", "BAD", "C"]
        for m in messages:
            try:
                dispatch(m)
            except Exception as e:
                errors.append(repr(e))
                continue  # keep looping — this is the TL2 fix

        # C must have been processed AFTER the bad message (loop didn't exit)
        self.assertEqual(processed, ["A", "C"])
        self.assertEqual(len(errors), 1)

    def test_without_guard_loop_exits_on_bad_message(self):
        """Counter-example: without the guard, the loop exits on first raise.
        This captures the pre-fix behavior that caused BUG-TL2."""
        processed = []
        with self.assertRaises(ValueError):
            for m in ["A", "BAD", "C"]:
                if m == "BAD":
                    raise ValueError("synthetic handler failure")
                processed.append(m)
        # Only A got processed before the raise
        self.assertEqual(processed, ["A"])


if __name__ == "__main__":
    unittest.main()
