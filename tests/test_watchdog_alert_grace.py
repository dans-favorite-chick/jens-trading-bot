"""Watchdog disconnect alert grace — Sprint D F3 regression tests.

Pre-fix: every bot restart (operator deployment, code update, network
blip) fired a paired DISCONNECTED + RECONNECTED telegram pair within
seconds. Yesterday's 23:24 + this morning's 06:54 deploys each fired
4 telegrams of pure ops-noise.

Post-fix:
  - DISCONNECT_TG_GRACE_S = 60: only fire DISCONNECTED telegram if
    downtime > 60s. Restarts complete in 5-15s and stay silent.
  - RECONNECTED telegram only if we previously alerted on the
    matching disconnect.
  - Restart telegram only when restart_count >= RESTART_ALERT_THRESHOLD
    (3) — tells operator the restart loop is struggling, not just that
    a routine retry happened.

Tests use real BotTracker + a captured _send_telegram replacement.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


@pytest.fixture
def captured_tg(monkeypatch):
    """Replace _send_telegram with a MagicMock so we can count calls
    and inspect message content."""
    sent = MagicMock()
    import tools.watchdog as wd
    monkeypatch.setattr(wd, "_send_telegram", sent)
    return sent


@pytest.fixture
def tracker():
    from tools.watchdog import BotTracker
    return BotTracker("sim")


# ─── short downtime: zero telegrams ───────────────────────────────────

def test_30s_downtime_no_telegram(captured_tg, tracker):
    """Disconnect for 30s then reconnect → no telegrams."""
    tracker.mark_connected()  # bot is initially up
    captured_tg.reset_mock()
    tracker.mark_disconnected("operator_restart")
    # Simulate 30s passing — short of the 60s grace window
    tracker.last_disconnect_ts = time.time() - 30
    tracker.check_disconnect_grace()
    assert captured_tg.call_count == 0, "no DISCONNECT alert within grace"
    # Reconnect comes back — also silent (we never alerted)
    tracker.mark_connected()
    assert captured_tg.call_count == 0, "no RECONNECT alert without prior DISCONNECT"


# ─── long downtime: paired pages ──────────────────────────────────────

def test_90s_downtime_one_disconnect_telegram(captured_tg, tracker):
    """Disconnect, age past 60s without reconnect → one DISCONNECTED page."""
    tracker.mark_connected()
    captured_tg.reset_mock()
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - 90
    tracker.check_disconnect_grace()
    assert captured_tg.call_count == 1
    msg = captured_tg.call_args.args[0]
    assert "DISCONNECTED" in msg
    assert "nt8_disconnected" in msg
    assert tracker.disconnect_alert_sent is True


def test_90s_downtime_then_reconnect_two_telegrams(captured_tg, tracker):
    """90s downtime → 1 DISCONNECTED + 1 RECONNECTED on recovery."""
    tracker.mark_connected()
    captured_tg.reset_mock()
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - 90
    tracker.check_disconnect_grace()
    assert captured_tg.call_count == 1
    # Now reconnect
    tracker.mark_connected()
    assert captured_tg.call_count == 2
    last_msg = captured_tg.call_args.args[0]
    assert "RECONNECTED" in last_msg


# ─── grace check is idempotent: 50 polls, still 1 telegram ────────────

def test_grace_check_does_not_spam_on_repeated_polls(captured_tg, tracker):
    """check_disconnect_grace runs every poll cycle. Once the grace
    page has fired, repeat polls must NOT re-fire."""
    tracker.mark_connected()
    captured_tg.reset_mock()
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - 90
    for _ in range(50):
        tracker.check_disconnect_grace()
    assert captured_tg.call_count == 1


# ─── boundary: exactly 60s ────────────────────────────────────────────

def test_grace_at_exact_threshold_fires(captured_tg, tracker):
    """At exactly DISCONNECT_TG_GRACE_S, alert fires (>= boundary)."""
    from tools.watchdog import DISCONNECT_TG_GRACE_S
    tracker.mark_connected()
    captured_tg.reset_mock()
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - DISCONNECT_TG_GRACE_S
    tracker.check_disconnect_grace()
    assert captured_tg.call_count == 1


def test_grace_just_under_threshold_silent(captured_tg, tracker):
    """At GRACE - 0.5s, alert does NOT fire."""
    from tools.watchdog import DISCONNECT_TG_GRACE_S
    tracker.mark_connected()
    captured_tg.reset_mock()
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - (DISCONNECT_TG_GRACE_S - 0.5)
    tracker.check_disconnect_grace()
    assert captured_tg.call_count == 0


# ─── reconnect after grace fired must clear the flag ──────────────────

def test_reconnect_clears_alert_sent_flag(captured_tg, tracker):
    """After RECONNECTED telegram, disconnect_alert_sent goes False so
    a future disconnect/reconnect cycle behaves correctly."""
    tracker.mark_connected()
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - 90
    tracker.check_disconnect_grace()
    assert tracker.disconnect_alert_sent is True
    tracker.mark_connected()
    assert tracker.disconnect_alert_sent is False


def test_two_disconnect_cycles_two_pairs(captured_tg, tracker):
    """Two genuine outages (each >60s) → two DISCONNECT + two RECONNECT
    telegrams. Operator-restart blips between them stay silent."""
    tracker.mark_connected()
    captured_tg.reset_mock()
    # Outage 1 (90s, alerted)
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - 90
    tracker.check_disconnect_grace()
    tracker.mark_connected()
    assert captured_tg.call_count == 2
    # Operator restart (10s, silent)
    captured_tg.reset_mock()
    tracker.mark_disconnected("operator_restart")
    tracker.last_disconnect_ts = time.time() - 10
    tracker.check_disconnect_grace()
    tracker.mark_connected()
    assert captured_tg.call_count == 0
    # Outage 2 (120s, alerted)
    captured_tg.reset_mock()
    tracker.mark_disconnected("nt8_disconnected")
    tracker.last_disconnect_ts = time.time() - 120
    tracker.check_disconnect_grace()
    tracker.mark_connected()
    assert captured_tg.call_count == 2


# ─── restart-attempt telegram threshold ───────────────────────────────

def test_restart_alert_threshold_constant_exists():
    from tools.watchdog import RESTART_ALERT_THRESHOLD
    assert RESTART_ALERT_THRESHOLD >= 2


def test_restart_telegram_source_check():
    """Source-grep: the 'bot restarted' telegram must be wrapped in a
    `>= RESTART_ALERT_THRESHOLD` check."""
    import pathlib
    src = (pathlib.Path(ROOT) / "tools" / "watchdog.py").read_text(encoding="utf-8")
    # The "bot restarted" telegram payload should now sit after the
    # threshold guard. Find both lines and verify proximity.
    msg_idx = src.find("bot restarted (attempt")
    assert msg_idx > 0, "couldn't find 'bot restarted' telegram payload"
    pre = src[max(0, msg_idx - 400):msg_idx]
    assert "RESTART_ALERT_THRESHOLD" in pre, (
        "'bot restarted' telegram is not gated by RESTART_ALERT_THRESHOLD — "
        "every retry will page the operator (Sprint D F3 regression)"
    )
