"""P1-4 / F-10 — NT8 silent-stall auto-recovery daemon tests.

Covers the trigger state machine + interlocks of
tools/nt8_silent_stall_recovery.py. Every test drives
StallRecoveryDaemon.process_health() with synthetic HealthSnapshot
objects and an in-memory RecoveryActions double, so no NT8 process,
no PowerShell, and no HTTP I/O are touched.

Cases:
  1. live + 0 ticks sustained > threshold  → recovery triggers
  2. fresh ticks (>0)                       → no trigger
  3. env flag unset                         → logs intent, no kill
  4. backoff: 2nd stall within 5 min        → no 2nd recovery
  5. /health unreachable                    → refuse to act
  6. transient stall that clears            → timer resets, no trigger
  7. nt8_status != live                     → no trigger
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.nt8_silent_stall_recovery import (  # noqa: E402
    HealthSnapshot,
    RecoveryActions,
    StallRecoveryDaemon,
)


# ─── Test doubles ───────────────────────────────────────────────────

class RecordingActions(RecoveryActions):
    """Captures every action without executing it. `sleep` is a no-op
    so tests stay fast.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.kill_result = True
        self.relaunch_result = True
        self.halt_result = True

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def kill_nt8(self) -> bool:
        self._record("kill_nt8")
        return self.kill_result

    def relaunch_nt8(self) -> bool:
        self._record("relaunch_nt8")
        return self.relaunch_result

    def post_halt_new_entries(self, duration_s: int = 60) -> bool:
        self._record("post_halt_new_entries", duration_s=duration_s)
        return self.halt_result

    def send_telegram(self, text: str) -> bool:
        self._record("send_telegram", text=text)
        return True

    def sleep(self, seconds: float) -> None:
        self._record("sleep", seconds=seconds)

    # Convenience helpers for assertions.
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


def _live_zero() -> HealthSnapshot:
    return HealthSnapshot(
        reachable=True, nt8_status="live", tick_rate_10s=0.0,
        raw={"nt8_status": "live", "tick_rate_10s": 0.0},
    )


def _live_ticks(rate: float = 5.0) -> HealthSnapshot:
    return HealthSnapshot(
        reachable=True, nt8_status="live", tick_rate_10s=rate,
        raw={"nt8_status": "live", "tick_rate_10s": rate},
    )


def _unreachable() -> HealthSnapshot:
    return HealthSnapshot(reachable=False)


def _disconnected() -> HealthSnapshot:
    return HealthSnapshot(
        reachable=True, nt8_status="disconnected", tick_rate_10s=0.0,
        raw={"nt8_status": "disconnected", "tick_rate_10s": 0.0},
    )


def _make(actions=None, *, env=True, dry_run=False,
          threshold=180, backoff=300) -> tuple[
              StallRecoveryDaemon, RecordingActions]:
    actions = actions or RecordingActions()
    daemon = StallRecoveryDaemon(
        actions=actions,
        env_flag_set=env,
        dry_run=dry_run,
        stall_threshold_s=threshold,
        backoff_s=backoff,
    )
    return daemon, actions


# ─── Trigger on sustained stall ─────────────────────────────────────

def test_recovery_triggers_after_sustained_stall():
    daemon, actions = _make()
    t0 = 1_000_000.0
    # First observation seeds the stall timer; should NOT fire yet.
    assert daemon.process_health(_live_zero(), now=t0) is False
    assert "kill_nt8" not in actions.names()

    # Still within window (179 s elapsed).
    assert daemon.process_health(_live_zero(), now=t0 + 179) is False
    assert "kill_nt8" not in actions.names()

    # Cross the threshold (181 s).
    assert daemon.process_health(_live_zero(), now=t0 + 181) is True
    # Full sequence executed in order.
    expected_actions = ["send_telegram", "kill_nt8", "sleep",
                        "relaunch_nt8", "sleep",
                        "post_halt_new_entries", "send_telegram"]
    # Filter just the action names (telegrams + sleeps may interleave).
    names = actions.names()
    assert "kill_nt8" in names
    assert "relaunch_nt8" in names
    assert "post_halt_new_entries" in names
    # kill → sleep → relaunch → sleep → halt ordering preserved.
    k = names.index("kill_nt8")
    r = names.index("relaunch_nt8")
    h = names.index("post_halt_new_entries")
    assert k < r < h
    # Two sleeps separate the three actions.
    assert names.count("sleep") >= 2


# ─── Fresh ticks → never trigger ────────────────────────────────────

def test_no_trigger_when_ticks_are_fresh():
    daemon, actions = _make()
    t0 = 1_000_000.0
    for off in (0, 60, 120, 180, 240, 300, 1000):
        assert daemon.process_health(_live_ticks(7.0),
                                     now=t0 + off) is False
    assert actions.count("kill_nt8") == 0
    assert actions.count("relaunch_nt8") == 0


# ─── Env flag interlock ─────────────────────────────────────────────

def test_env_flag_unset_logs_intent_only():
    daemon, actions = _make(env=False)
    t0 = 1_000_000.0
    daemon.process_health(_live_zero(), now=t0)
    triggered = daemon.process_health(_live_zero(), now=t0 + 200)
    # process_health() returns True (the trigger fired logically),
    # but no NT8 actions executed — only a telegram alert.
    assert triggered is True
    assert actions.count("kill_nt8") == 0
    assert actions.count("relaunch_nt8") == 0
    assert actions.count("post_halt_new_entries") == 0
    # One telegram describing the would-be recovery.
    assert actions.count("send_telegram") == 1
    tg_text = actions.calls[
        [c[0] for c in actions.calls].index("send_telegram")
    ][2]["text"]
    assert "DISABLED" in tg_text or "Auto-recovery" in tg_text


def test_dry_run_logs_intent_only():
    daemon, actions = _make(env=True, dry_run=True)
    t0 = 1_000_000.0
    daemon.process_health(_live_zero(), now=t0)
    daemon.process_health(_live_zero(), now=t0 + 200)
    assert actions.count("kill_nt8") == 0
    assert actions.count("relaunch_nt8") == 0


# ─── Backoff ────────────────────────────────────────────────────────

def test_backoff_suppresses_second_recovery_within_window():
    daemon, actions = _make(threshold=180, backoff=300)
    t0 = 1_000_000.0
    # First recovery
    daemon.process_health(_live_zero(), now=t0)
    assert daemon.process_health(_live_zero(), now=t0 + 200) is True
    first_kill_count = actions.count("kill_nt8")
    assert first_kill_count == 1

    # Stall comes back almost immediately
    daemon.process_health(_live_zero(), now=t0 + 250)
    # 250 + 200 = 450 since t0; backoff started at t0+200, so
    # backoff window runs t0+200 .. t0+500. Re-trigger at t0+450
    # MUST be suppressed.
    assert daemon.process_health(_live_zero(), now=t0 + 450) is False
    assert actions.count("kill_nt8") == first_kill_count, (
        "second recovery should NOT have fired inside backoff"
    )


def test_backoff_lifts_after_window():
    daemon, actions = _make(threshold=180, backoff=300)
    t0 = 1_000_000.0
    # First recovery at t0+200
    daemon.process_health(_live_zero(), now=t0)
    daemon.process_health(_live_zero(), now=t0 + 200)
    assert actions.count("kill_nt8") == 1

    # Long quiet gap so the backoff fully expires
    daemon.process_health(_live_ticks(5.0), now=t0 + 700)
    # New stall observed at t0+800, threshold crossed at t0+1000
    daemon.process_health(_live_zero(), now=t0 + 800)
    triggered = daemon.process_health(_live_zero(), now=t0 + 1000)
    assert triggered is True
    assert actions.count("kill_nt8") == 2


# ─── Health unreachable → refuse to act ─────────────────────────────

def test_unreachable_health_refuses_to_act_even_when_stall_was_pending():
    daemon, actions = _make()
    t0 = 1_000_000.0
    daemon.process_health(_live_zero(), now=t0)  # seed stall timer
    # Bridge goes down before threshold crosses.
    triggered = daemon.process_health(_unreachable(), now=t0 + 250)
    assert triggered is False
    assert actions.count("kill_nt8") == 0
    # Stall timer was reset by the unreachable observation, so a fresh
    # live+0 observation must restart the timer — no instant trigger.
    triggered = daemon.process_health(_live_zero(), now=t0 + 260)
    assert triggered is False
    assert actions.count("kill_nt8") == 0


def test_unreachable_never_triggers_recovery():
    """Even an indefinite outage must not trigger NT8 actions —
    bridge down ≠ NT8 down. Operator-managed."""
    daemon, actions = _make()
    t = 1_000_000.0
    for off in range(0, 10_000, 30):
        daemon.process_health(_unreachable(), now=t + off)
    assert actions.count("kill_nt8") == 0
    assert actions.count("relaunch_nt8") == 0


# ─── Transient stall that clears ────────────────────────────────────

def test_transient_stall_clears_timer():
    daemon, actions = _make()
    t0 = 1_000_000.0
    # Stall observed, then ticks resume well before threshold.
    daemon.process_health(_live_zero(), now=t0)
    daemon.process_health(_live_zero(), now=t0 + 60)
    daemon.process_health(_live_ticks(3.0), now=t0 + 120)
    # New stall later, but only 100 s old when re-observed.
    daemon.process_health(_live_zero(), now=t0 + 500)
    triggered = daemon.process_health(_live_zero(), now=t0 + 600)
    assert triggered is False
    assert actions.count("kill_nt8") == 0


# ─── Non-live status doesn't trigger ────────────────────────────────

def test_disconnected_status_does_not_trigger():
    daemon, actions = _make()
    t0 = 1_000_000.0
    for off in range(0, 1000, 30):
        daemon.process_health(_disconnected(), now=t0 + off)
    assert actions.count("kill_nt8") == 0


# ─── fetch_health() uses test-seam opener ───────────────────────────

def test_fetch_health_uses_injected_opener():
    """The `_opener` seam lets tests bypass real HTTP I/O."""
    from tools.nt8_silent_stall_recovery import fetch_health

    def fake(url, timeout):
        assert url.endswith("/health")
        return {"nt8_status": "live", "tick_rate_10s": 4.2}

    snap = fetch_health(_opener=fake)
    assert snap.reachable is True
    assert snap.nt8_status == "live"
    assert snap.tick_rate_10s == 4.2


def test_fetch_health_handles_io_error():
    from tools.nt8_silent_stall_recovery import fetch_health

    def boom(url, timeout):
        raise OSError("connection refused")

    snap = fetch_health(_opener=boom)
    assert snap.reachable is False
    assert snap.tick_rate_10s == 0.0
