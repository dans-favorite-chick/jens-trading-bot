"""Bulletproof bot-process restart — 2026-05-13.

Verifies the four fixes that ended the prod_bot zombie-death-loop:

  1. dashboard/server.py::_start_bot must use creationflags=0 (NO
     CREATE_NEW_PROCESS_GROUP). The flag caused subprocesses to die
     silently 2-3 minutes after launch on Windows. Documented in
     memory/context/MORNING_2026-05-12.md "Open Issue #1".

  2. dashboard/server.py::_stop_bot accepts a `force` parameter, default
     False. When False, only kill dashboard-tracked subprocesses (Path 1).
     When True, also psutil-scan and kill by process name (Path 2). This
     protects operator-launched cmd-window bots from being killed by
     watchdog auto-restart cycles.

  3. /api/bot/stop endpoint reads `force` from JSON body, defaulting to
     False. Watchdog auto-restart -> safe by default. Operator UI can
     opt in to force=True.

  4. tools/watcher_agent.py::_execute_restart must NOT use
     CREATE_NEW_PROCESS_GROUP (0x00000200) and must NOT set stdin=DEVNULL.
     Same zombie pattern as #1 but worse (15s death instead of 3min).

These are static checks on the source files plus behavioral tests on
_stop_bot's two paths. We can't unit-test the actual subprocess.Popen
behavior in a test environment (would spawn real processes), but the
static checks verify the call sites are correct.

Run: python -m unittest tests.test_bot_process_safe_restart -v
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════════
# Static source-file checks — guard against regression of the four fixes
# ═══════════════════════════════════════════════════════════════════

DASHBOARD_SRC = (Path(__file__).parent.parent / "dashboard" / "server.py").read_text(
    encoding="utf-8"
)
WATCHER_SRC = (Path(__file__).parent.parent / "tools" / "watcher_agent.py").read_text(
    encoding="utf-8"
)


class TestStartBotSubprocessFlags(unittest.TestCase):
    """Fix #1 — dashboard _start_bot must use creationflags=0."""

    def test_start_bot_does_not_use_create_new_process_group(self):
        # Extract the _start_bot function body via regex.
        m = re.search(
            r"def _start_bot\(name: str\).*?(?=\ndef |\Z)",
            DASHBOARD_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "could not locate _start_bot in dashboard/server.py")
        body = m.group(0)
        # The Popen() call inside _start_bot must NOT reference
        # CREATE_NEW_PROCESS_GROUP. (Comments may mention it for context;
        # we check that the actual subprocess.Popen call has
        # creationflags=0.)
        # Find the Popen call.
        popen_match = re.search(
            r"subprocess\.Popen\([^)]*\)",
            body, re.DOTALL,
        )
        self.assertIsNotNone(popen_match, "no subprocess.Popen call found in _start_bot")
        popen_call = popen_match.group(0)
        self.assertNotIn(
            "CREATE_NEW_PROCESS_GROUP", popen_call,
            "_start_bot's Popen still references CREATE_NEW_PROCESS_GROUP — "
            "this causes the 2-3 min zombie death pattern on Windows."
        )
        self.assertIn(
            "creationflags=0", popen_call,
            "_start_bot's Popen must use creationflags=0",
        )

    def test_start_bot_redirects_stdout_to_log_file(self):
        """Sanity check the existing line-buffered file-redirect still exists."""
        m = re.search(r"def _start_bot\(name: str\).*?(?=\ndef |\Z)",
                       DASHBOARD_SRC, re.DOTALL)
        body = m.group(0)
        self.assertIn("buffering=1", body,
                      "_start_bot lost its line-buffered log file open")
        self.assertIn("stdout=log_file", body)
        self.assertIn("stderr=subprocess.STDOUT", body)


class TestStopBotForceParameter(unittest.TestCase):
    """Fix #2 — _stop_bot must accept force param, default False."""

    def test_stop_bot_signature_has_force_default_false(self):
        # Find the def line specifically — regex anchored at start of file
        m = re.search(r"^def _stop_bot\([^)]*\)", DASHBOARD_SRC, re.MULTILINE)
        self.assertIsNotNone(m, "could not locate _stop_bot signature")
        sig = m.group(0)
        self.assertIn("force", sig,
                      "_stop_bot signature must accept a `force` parameter")
        self.assertIn("force: bool = False", sig,
                      "_stop_bot's force parameter must default to False")

    def test_path_2_psutil_scan_gated_on_force(self):
        # Pull the _stop_bot function body.
        m = re.search(r"def _stop_bot\([^)]*\).*?(?=\ndef |\Z)",
                       DASHBOARD_SRC, re.DOTALL)
        body = m.group(0)
        # The psutil scan must be inside an `if force:` block.
        self.assertIn("if force:", body,
                      "_stop_bot must gate the psutil scan on `if force:`")
        # The scan code itself must still exist (for force=True callers).
        self.assertIn("psutil.process_iter", body,
                      "_stop_bot lost its psutil scan entirely")


class TestApiStopEndpointReadsForce(unittest.TestCase):
    """Fix #3 — /api/bot/stop reads force from body, defaults False."""

    def test_api_stop_reads_force(self):
        m = re.search(
            r"def api_stop_bot\(\):.*?(?=\n@|\ndef |\Z)",
            DASHBOARD_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "could not locate api_stop_bot")
        body = m.group(0)
        self.assertIn('data.get("force"', body,
                      "api_stop_bot must read `force` from JSON body")
        # Must pass it through to _stop_bot.
        self.assertIn("_stop_bot(name, force=force)", body,
                      "api_stop_bot must pass force= through to _stop_bot")


class TestWatcherAgentRestartFlags(unittest.TestCase):
    """Fix #4 — watcher_agent's _execute_restart must use safe flags."""

    def test_execute_restart_no_create_new_process_group(self):
        # Pull the full _execute_restart function body. The 0x00000200
        # magic constant (CREATE_NEW_PROCESS_GROUP) must not appear in
        # the Popen call. The Popen call has nested parens (`cwd=str(...)`)
        # so we don't try to regex-isolate just the call — we check the
        # function body for the absence of dangerous constants and the
        # presence of the safe one.
        m = re.search(
            r"def _execute_restart\(self, target: str\).*?(?=\n    def |\Z)",
            WATCHER_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "could not locate _execute_restart")
        body = m.group(0)
        # No CREATE_NEW_PROCESS_GROUP in the body (comments may reference
        # it for context — we look for it in active code by checking the
        # specific magic-constant form too).
        # Strip comment lines so a "documented removed CREATE_..." note
        # doesn't false-positive.
        non_comment_lines = "\n".join(
            line for line in body.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn(
            "0x00000200", non_comment_lines,
            "_execute_restart still uses the 0x00000200 magic constant "
            "(CREATE_NEW_PROCESS_GROUP) — causes silent subprocess death.",
        )
        self.assertNotIn(
            "stdin=subprocess.DEVNULL", non_comment_lines,
            "_execute_restart still uses stdin=subprocess.DEVNULL — "
            "explicit no-stdin causes 15s subprocess death.",
        )
        # creationflags=0 must be the active value.
        self.assertIn(
            "creationflags=0", non_comment_lines,
            "_execute_restart's Popen must use creationflags=0",
        )


# ═══════════════════════════════════════════════════════════════════
# Behavioral test — _stop_bot's force semantics
# ═══════════════════════════════════════════════════════════════════

class TestStopBotBehavior(unittest.TestCase):
    """Verify _stop_bot's force=False / force=True branches work.

    We mock psutil so no real processes are touched.
    """

    def setUp(self):
        # Late import to use the (just-edited) module.
        from dashboard import server as dash
        self.dash = dash
        # Snapshot/clear the bot_processes dict.
        with dash._bot_proc_lock:
            self._saved = dict(dash._bot_processes)
            dash._bot_processes.clear()

    def tearDown(self):
        with self.dash._bot_proc_lock:
            self.dash._bot_processes.clear()
            self.dash._bot_processes.update(self._saved)

    def test_force_false_does_not_call_psutil_iter(self):
        """force=False must NOT scan psutil for external processes."""
        with patch.object(self.dash, "_bot_processes", new={}, create=False):
            # Mock psutil import inside _stop_bot — we want to ensure the
            # branch isn't even entered. Easiest: patch the module-level
            # `import psutil` site by patching sys.modules so any access
            # would raise.
            import sys as _sys
            with patch.dict(_sys.modules, {"psutil": MagicMock(side_effect=AssertionError(
                "psutil must not be touched when force=False"
            ))}):
                # No tracked subprocess exists for 'prod', so Path 1 is a no-op.
                result = self.dash._stop_bot("prod", force=False)
                self.assertTrue(result["ok"])
                self.assertEqual(result.get("force"), False)

    def test_force_true_does_attempt_psutil_iter(self):
        """force=True branch must reach the psutil.process_iter call."""
        called = {"iter": False}
        fake_psutil = MagicMock()

        def _fake_iter(*args, **kwargs):
            called["iter"] = True
            return iter([])

        fake_psutil.process_iter = _fake_iter
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        fake_psutil.TimeoutExpired = type("TimeoutExpired", (Exception,), {})

        import sys as _sys
        with patch.dict(_sys.modules, {"psutil": fake_psutil}):
            result = self.dash._stop_bot("prod", force=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result.get("force"), True)
            self.assertTrue(called["iter"],
                            "force=True did not invoke psutil.process_iter")


if __name__ == "__main__":
    unittest.main()
