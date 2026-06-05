"""Regression test for FINDING-2026-06-04-SUPERVISOR-DRIFT.

Phoenix's dashboard supervisor (`dashboard.server._bot_processes`) only
contains subprocesses spawned via `/api/bot/start`. When the operator
launches the stack from a shell (the round-2 PowerShell pattern, the
`launch_all.bat` script, or a manual `python bots/prod_bot.py`), the
dashboard never registers those bots. Pre-fix, `/api/bot/stop` returned
`{"ok":True,"message":"... was not running","force":False}` for both
externally-launched bots and genuinely-stopped ones — visibly identical
responses for two very different states.

The 2026-05-13 fix at commit `8b471af` made the watchdog auto-restart
loop safe by defaulting `force=False`, gating the psutil scan behind
`force=True`. The fix here PRESERVES that invariant (watchdog stops
are still no-ops against externally-launched bots) while adding a
**graceful-shutdown-via-command-queue** path for operator-explicit
`force=True` stops:

  Path 1   — registry pop (already existed; for dashboard-spawned bots)
  Path 1.5 — NEW: queue shutdown command + poll _bot_status for graceful
             exit. Runs only when force=True AND _bot_status detects an
             externally-running bot. The bot's existing /api/commands
             poll loop consumes the shutdown and exits cleanly with
             state-saved.
  Path 2   — psutil terminate fallback (already existed; gated on
             force=True). Now only reached when Path 1.5 times out.

The UI Stop button is also flipped to send `force: true` so operator
clicks engage Paths 1.5 + 2; watchdog stops continue to default false.

Test coverage:
  T1 — graceful shutdown path: force=True, registry empty, bot
       externally running, command queue picks up shutdown → graceful exit.
  T2 — psutil fallback: force=True, registry empty, command queue doesn't
       take (bot ignores shutdown) → falls through to Path 2.
  T3 — force=False preserved: registry empty + bot externally running →
       returns "not running" with NO command queued (watchdog-safety
       invariant).
  T4 — _bot_status enrichment: /api/bot/status returns the new fields
       (interpreter, parent_pid, external, shim_warn) for monitoring.
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

import pytest


@pytest.fixture
def srv(monkeypatch):
    """Import the dashboard server module fresh per test, reset its
    module-level dicts so tests don't bleed."""
    from dashboard import server as s
    # Clean slate
    with s._bot_proc_lock:
        s._bot_processes.clear()
    with s._state_lock:
        for k in list(s._state.keys()):
            if k.startswith("_commands_") or k in ("prod", "sim"):
                if k.startswith("_commands_"):
                    s._state.pop(k, None)
                else:
                    s._state[k] = {}
        s._state["bridge_health"] = {}
    # Tighten the graceful timeout so tests run fast
    monkeypatch.setattr(s, "_GRACEFUL_SHUTDOWN_TIMEOUT_S", 1.0)
    return s


def test_t1_graceful_shutdown_via_command_queue(srv, monkeypatch):
    """force=True + externally-running bot → graceful command-queue
    shutdown succeeds before psutil scan is needed."""
    name = "prod"

    # Simulate: bot is externally running (in bots_connected) then
    # exits cleanly after one poll cycle (drops out of bots_connected).
    poll_state = {"calls": 0}

    def fake_bot_status(n):
        # First call: running; subsequent calls: stopped (bot exited)
        poll_state["calls"] += 1
        return "running" if poll_state["calls"] <= 1 else "stopped"

    monkeypatch.setattr(srv, "_bot_status", fake_bot_status)

    # psutil scan must NOT be needed — fail loudly if it is.
    def fake_process_iter(*args, **kwargs):
        raise AssertionError(
            "psutil.process_iter should not be called when graceful "
            "shutdown succeeds"
        )
    import psutil
    monkeypatch.setattr(psutil, "process_iter", fake_process_iter)

    result = srv._stop_bot(name, force=True)

    assert result["ok"] is True, f"unexpected: {result}"
    # The fix should record that graceful shutdown was used
    assert result.get("path") == "graceful", (
        f"expected path=graceful, got {result}"
    )
    # Confirm the shutdown command was queued for the bot to consume
    with srv._state_lock:
        cmds = srv._state.get(f"_commands_{name}", [])
    assert any(c.get("type") == "shutdown" for c in cmds), (
        f"shutdown command not queued: {cmds}"
    )


def test_t2_psutil_fallback_when_graceful_times_out(srv, monkeypatch):
    """force=True + externally-running bot that ignores shutdown
    command → falls through to Path 2 (psutil terminate)."""
    name = "sim"

    # Simulate: bot is externally running and STAYS running (ignores shutdown)
    monkeypatch.setattr(srv, "_bot_status", lambda n: "running")

    # Track that psutil.process_iter WAS called as fallback
    psutil_called = {"yes": False}
    killed_pids = []

    class FakeProc:
        def __init__(self, pid, name_, cmdline):
            self.info = {"pid": pid, "name": name_, "cmdline": cmdline}
            self.pid = pid

        def terminate(self):
            killed_pids.append(self.pid)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            killed_pids.append(self.pid)

    def fake_process_iter(attrs=None):
        psutil_called["yes"] = True
        # One fake process matching "sim_bot.py"
        yield FakeProc(99999, "python.exe",
                       ["python", "C:\\fake\\sim_bot.py"])

    import psutil
    monkeypatch.setattr(psutil, "process_iter", fake_process_iter)

    result = srv._stop_bot(name, force=True)

    assert result["ok"] is True
    assert psutil_called["yes"], "psutil fallback should have been called"
    assert 99999 in killed_pids, (
        f"expected fake PID 99999 in killed_pids; got {killed_pids}, "
        f"result={result}"
    )
    assert result.get("pids") == [99999], f"unexpected: {result}"


def test_t3_force_false_preserves_watchdog_safety(srv, monkeypatch):
    """force=False (the watchdog-auto-restart default) with externally
    running bot → returns 'not running' and queues NO shutdown command.
    This preserves the 2026-05-13 8b471af invariant."""
    name = "prod"

    # Bot is externally running (would be visible via _bot_status)
    monkeypatch.setattr(srv, "_bot_status", lambda n: "running")

    # If psutil scan is called when force=False, that's a regression
    def fake_process_iter(*args, **kwargs):
        raise AssertionError(
            "psutil.process_iter must NOT be called when force=False"
        )
    import psutil
    monkeypatch.setattr(psutil, "process_iter", fake_process_iter)

    result = srv._stop_bot(name, force=False)

    assert result["ok"] is True
    assert "not running" in result.get("message", ""), (
        f"expected 'not running' message; got {result}"
    )
    assert result.get("force") is False

    # No shutdown command queued (the 2026-05-13 invariant)
    with srv._state_lock:
        cmds = srv._state.get(f"_commands_{name}", [])
    assert not any(c.get("type") == "shutdown" for c in cmds), (
        f"watchdog-safety regression: shutdown was queued under "
        f"force=False: {cmds}"
    )


def test_t4_bot_status_enrichment(srv, monkeypatch):
    """/api/bot/status surfaces interpreter, parent_pid, external, and
    shim_warn fields per process. Operator + watchdog rely on these to
    detect the WindowsApps-shim launch foot-gun."""
    monkeypatch.setattr(srv, "_bot_status", lambda n: "running")

    # Simulate psutil view of a prod_bot with safe interpreter
    safe_python = (
        r"C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe"
    )

    class FakeProc:
        def __init__(self, pid, exe, ppid, cmdline):
            self.pid = pid
            self.info = {
                "pid": pid,
                "name": "python.exe",
                "exe": exe,
                "ppid": ppid,
                "cmdline": cmdline,
            }

    def fake_process_iter(attrs=None):
        yield FakeProc(11111, safe_python, 42,
                       ["python", "bots/prod_bot.py"])
        yield FakeProc(22222, safe_python, 99,
                       ["python", "bots/sim_bot.py"])

    import psutil
    monkeypatch.setattr(psutil, "process_iter", fake_process_iter)

    app = srv.app
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/api/bot/status")
    data = resp.get_json()

    # Backward compat: top-level prod/sim are still bare-string statuses
    # so frontend's `proc[activeBot] === 'running'` keeps working.
    assert data.get("prod") == "running", (
        f"backward-compat broken — prod should be bare string: {data}"
    )
    assert data.get("sim") == "running", f"sim bare string broken: {data}"

    # Enriched fields under processes.<bot> (Phase 3 WATCHDOG-OPACITY)
    procs = data.get("processes")
    assert isinstance(procs, dict), (
        f"missing top-level 'processes' dict: {data}"
    )
    for bot_name in ("prod", "sim"):
        entry = procs.get(bot_name)
        assert isinstance(entry, dict), (
            f"processes.{bot_name} should be a dict; got {entry!r}"
        )
        for field in ("status", "interpreter", "parent_pid",
                       "external", "shim_warn"):
            assert field in entry, (
                f"missing '{field}' in processes.{bot_name}: {entry}"
            )

    assert procs["prod"]["shim_warn"] is False
    assert procs["prod"]["interpreter"] == safe_python


def test_t5_bot_status_flags_shim_interpreter(srv, monkeypatch):
    """When a bot is running under the WindowsApps shim path,
    /api/bot/status surfaces shim_warn=True. This is the
    WATCHDOG-OPACITY closure: silent shim usage becomes loud."""
    monkeypatch.setattr(srv, "_bot_status", lambda n: "running")

    shim_python = (
        r"C:\Users\Trading PC\AppData\Local\Microsoft\WindowsApps\python.exe"
    )

    class FakeProc:
        def __init__(self, pid, exe, ppid, cmdline):
            self.pid = pid
            self.info = {
                "pid": pid, "name": "python.exe",
                "exe": exe, "ppid": ppid, "cmdline": cmdline,
            }

    def fake_process_iter(attrs=None):
        yield FakeProc(33333, shim_python, 44,
                       ["python", "bots/prod_bot.py"])

    import psutil
    monkeypatch.setattr(psutil, "process_iter", fake_process_iter)

    client = srv.app.test_client()
    resp = client.get("/api/bot/status")
    data = resp.get_json()

    procs = data["processes"]
    assert procs["prod"]["shim_warn"] is True, (
        f"shim_warn should be True for WindowsApps interpreter: "
        f"{procs['prod']}"
    )
    assert procs["prod"]["external"] is True, (
        f"externally-launched bot should report external=True: "
        f"{procs['prod']}"
    )
