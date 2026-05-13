"""Graceful shutdown via dashboard command queue — 2026-05-13.

Background
----------
Commit 8b471af removed `creationflags=CREATE_NEW_PROCESS_GROUP` from
dashboard `_start_bot` to end the zombie-subprocess pattern on Windows.
A side effect: the CTRL_BREAK_EVENT graceful-shutdown path (which
required CREATE_NEW_PROCESS_GROUP) was lost — `_stop_bot` would hard-
terminate() the bot on every stop. State persistence on every bar made
this acceptable in the short term, but a clean shutdown path is still
desirable for the routine "watchdog restarting after disconnect" case.

This test suite covers the replacement: a graceful-shutdown command
queued via the existing `_state["_commands_<bot>"]` queue that the bot
polls every 2s via its `_dashboard_loop`. When `_stop_bot` is called,
it queues `{"type": "shutdown"}`, waits up to
`_GRACEFUL_SHUTDOWN_TIMEOUT_S` for the bot to self-exit, and only falls
back to `terminate()` if the bot doesn't honor the command.

Tests:
  1. base_bot.py source contains the "shutdown" branch in
     _handle_dashboard_command and a _shutdown_requested-aware run loop.
  2. _stop_bot queues a shutdown command before any terminate() call.
  3. _stop_bot does NOT call terminate() when the bot self-exits in time.
  4. _stop_bot DOES call terminate() when the graceful timeout elapses.
"""

from __future__ import annotations

import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


BASE_BOT_SRC = (Path(__file__).parent.parent / "bots" / "base_bot.py").read_text(
    encoding="utf-8"
)
DASHBOARD_SRC = (Path(__file__).parent.parent / "dashboard" / "server.py").read_text(
    encoding="utf-8"
)


class TestBaseBotShutdownHandler(unittest.TestCase):
    """The bot must recognize {"type": "shutdown"} and exit cleanly."""

    def test_handle_dashboard_command_has_shutdown_branch(self):
        m = re.search(
            r"def _handle_dashboard_command\(self, cmd: dict\).*?(?=\n    (?:async )?def |\Z)",
            BASE_BOT_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "could not locate _handle_dashboard_command")
        body = m.group(0)
        self.assertIn(
            'elif cmd_type == "shutdown":', body,
            "_handle_dashboard_command missing 'shutdown' branch",
        )
        self.assertIn(
            "self._shutdown_requested = True", body,
            "shutdown branch must set _shutdown_requested = True",
        )
        # WS close keeps the inner async-for from blocking forever.
        self.assertIn(
            "self._ws.close()", body,
            "shutdown branch must close the WS so async-for unblocks",
        )

    def test_run_loop_honors_shutdown_flag(self):
        """run()'s outer while loop must be 'while not self._shutdown_requested:'."""
        m = re.search(
            r"async def run\(self\).*?(?=\n    (?:async )?def |\Z)",
            BASE_BOT_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "could not locate run()")
        body = m.group(0)
        self.assertIn(
            "while not self._shutdown_requested:", body,
            "run() outer loop must check self._shutdown_requested — "
            "otherwise a queued shutdown command can't terminate the loop",
        )

    def test_shutdown_requested_flag_initialized(self):
        """__init__ must set self._shutdown_requested = False so the flag exists."""
        m = re.search(
            r"def __init__\(self\).*?(?=\n    (?:async )?def |\Z)",
            BASE_BOT_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "could not locate __init__")
        body = m.group(0)
        self.assertIn(
            "self._shutdown_requested = False", body,
            "__init__ must initialize self._shutdown_requested = False — "
            "otherwise the first poll of the flag raises AttributeError",
        )


class TestDashboardStopBotGraceful(unittest.TestCase):
    """_stop_bot must queue a shutdown command before terminating."""

    def test_stop_bot_queues_shutdown_in_command_queue(self):
        """Static check: _stop_bot body must reference the per-bot command queue."""
        m = re.search(
            r"def _stop_bot\([^)]*\).*?(?=\ndef |\Z)",
            DASHBOARD_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "could not locate _stop_bot")
        body = m.group(0)
        self.assertIn(
            'f"_commands_{name}"', body,
            "_stop_bot must queue into the per-bot _commands_<name> queue",
        )
        self.assertIn(
            '"type": "shutdown"', body,
            "_stop_bot must queue a {'type': 'shutdown', ...} command",
        )
        # The graceful wait must happen before terminate().
        idx_queue = body.find('"type": "shutdown"')
        idx_terminate = body.find("proc.terminate()")
        self.assertNotEqual(idx_queue, -1)
        self.assertNotEqual(idx_terminate, -1)
        self.assertLess(
            idx_queue, idx_terminate,
            "_stop_bot must queue the shutdown command BEFORE proc.terminate()",
        )

    def test_graceful_timeout_constant_exposed(self):
        """The timeout must be a module-level constant for tests to patch."""
        self.assertIn(
            "_GRACEFUL_SHUTDOWN_TIMEOUT_S", DASHBOARD_SRC,
            "_GRACEFUL_SHUTDOWN_TIMEOUT_S must be a module-level constant",
        )


class TestStopBotBehavioralGraceful(unittest.TestCase):
    """End-to-end: graceful path uses the queue and skips terminate."""

    def setUp(self):
        from dashboard import server as dash
        self.dash = dash
        with dash._bot_proc_lock:
            self._saved_procs = dict(dash._bot_processes)
            dash._bot_processes.clear()
        with dash._state_lock:
            self._saved_cmds = dash._state.pop("_commands_prod", None)

    def tearDown(self):
        with self.dash._bot_proc_lock:
            self.dash._bot_processes.clear()
            self.dash._bot_processes.update(self._saved_procs)
        with self.dash._state_lock:
            self.dash._state.pop("_commands_prod", None)
            if self._saved_cmds is not None:
                self.dash._state["_commands_prod"] = self._saved_cmds

    def test_graceful_exit_skips_terminate(self):
        """When proc.poll() flips to non-None within the timeout,
        terminate() must NOT be called."""
        fake_proc = MagicMock()
        # poll() returns None on the initial alive check, then returns 0
        # on the next call inside the wait loop (bot honored shutdown).
        poll_results = [None, 0]
        def _poll():
            return poll_results.pop(0) if poll_results else 0
        fake_proc.poll.side_effect = _poll
        fake_proc.pid = 99001

        with self.dash._bot_proc_lock:
            self.dash._bot_processes["prod"] = fake_proc

        # Shrink the timeout so we don't actually sleep 7s if something
        # goes wrong — but the path under test should exit in ~0.1s.
        with patch.object(self.dash, "_GRACEFUL_SHUTDOWN_TIMEOUT_S", 2.0):
            t0 = time.time()
            result = self.dash._stop_bot("prod", force=False)
            elapsed = time.time() - t0

        self.assertTrue(result["ok"])
        self.assertIn(fake_proc.pid, result.get("pids", []))
        fake_proc.terminate.assert_not_called()
        fake_proc.kill.assert_not_called()
        # Should complete well under the timeout.
        self.assertLess(elapsed, 1.5,
                        "graceful exit should not consume the full timeout")

        # Shutdown command was queued.
        with self.dash._state_lock:
            cmds = self.dash._state.get("_commands_prod", [])
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["type"], "shutdown")
        self.assertIn("ts", cmds[0])

    def test_graceful_timeout_falls_back_to_terminate(self):
        """When the bot never exits, _stop_bot must terminate() after the timeout."""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # bot stays "alive" — ignores shutdown
        fake_proc.pid = 99002

        # Make terminate() flip poll to a non-None value so the
        # subsequent proc.wait() doesn't spin.
        def _terminate():
            fake_proc.poll.return_value = -15
        fake_proc.terminate.side_effect = _terminate

        with self.dash._bot_proc_lock:
            self.dash._bot_processes["prod"] = fake_proc

        # Tiny timeout so the test is fast.
        with patch.object(self.dash, "_GRACEFUL_SHUTDOWN_TIMEOUT_S", 0.3):
            t0 = time.time()
            result = self.dash._stop_bot("prod", force=False)
            elapsed = time.time() - t0

        self.assertTrue(result["ok"])
        self.assertIn(fake_proc.pid, result.get("pids", []))
        fake_proc.terminate.assert_called_once()
        # Must have waited at least the timeout before falling back.
        self.assertGreaterEqual(elapsed, 0.3,
                                "should wait the full timeout before terminating")
        # And not much longer than timeout + terminate.wait(3s).
        self.assertLess(elapsed, 4.0)


if __name__ == "__main__":
    unittest.main()
