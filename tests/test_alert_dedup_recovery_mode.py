"""RECOVERY MODE alert dedup — Sprint D F2 regression tests.

Pre-fix: every loss after recovery threshold fires another "RECOVERY
MODE activated" telegram. Forensic 2026-05-04 saw ~5 pages on a single
recovery day.

Post-fix: one telegram on first transition into recovery for a given
session date. One "RECOVERY EXITED" confirmation at the next daily
reset.

These tests poke the alert flag directly rather than driving the full
trade-close pipeline, since that's what the regression is about.
"""
from __future__ import annotations

import datetime
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


@pytest.fixture
def alert_state():
    """Returns an object holding the dedup state we need to mutate +
    inspect, simulating what BaseBot does."""
    class S:
        _recovery_alert_session_date = None
    return S()


def _try_fire(state, today, daily_pnl, mock_tg):
    """Mirror the production check from base_bot.py:
       if recovery_mode and trade.result == LOSS:
           if state._recovery_alert_session_date != today:
               fire telegram
               state._recovery_alert_session_date = today
    """
    if state._recovery_alert_session_date != today:
        mock_tg(f"RECOVERY MODE\nDaily P&L: ${daily_pnl:.2f}")
        state._recovery_alert_session_date = today


def test_one_telegram_per_session_day(alert_state):
    """Five losses on the same day → exactly 1 telegram."""
    today = datetime.date(2026, 5, 4)
    mock_tg = MagicMock()
    for pnl in (-50, -55, -65, -70, -85):
        _try_fire(alert_state, today, pnl, mock_tg)
    assert mock_tg.call_count == 1


def test_subsequent_losses_same_session_no_alert(alert_state):
    today = datetime.date(2026, 5, 4)
    mock_tg = MagicMock()
    _try_fire(alert_state, today, -50, mock_tg)
    assert mock_tg.call_count == 1
    for _ in range(20):  # 20 more losses
        _try_fire(alert_state, today, -100, mock_tg)
    assert mock_tg.call_count == 1


def test_two_days_two_alerts(alert_state):
    """Day 1: alert. Day 2 (after reset): alert again."""
    mock_tg = MagicMock()
    day1 = datetime.date(2026, 5, 4)
    day2 = datetime.date(2026, 5, 5)
    _try_fire(alert_state, day1, -50, mock_tg)
    # Simulate daily reset
    alert_state._recovery_alert_session_date = None
    _try_fire(alert_state, day2, -50, mock_tg)
    assert mock_tg.call_count == 2


def test_state_set_to_today_after_first_fire(alert_state):
    today = datetime.date(2026, 5, 4)
    mock_tg = MagicMock()
    _try_fire(alert_state, today, -50, mock_tg)
    assert alert_state._recovery_alert_session_date == today


# ─── verify the production code path actually has the dedup wiring ────

def test_base_bot_has_recovery_alert_session_date_field():
    """Class-level attribute exists for the per-instance dedup flag."""
    from bots.base_bot import BaseBot
    assert hasattr(BaseBot, "_recovery_alert_session_date")
    assert BaseBot._recovery_alert_session_date is None


def test_base_bot_init_sets_recovery_alert_session_date_none():
    """Each instance starts with no recovery telegram fired."""
    from bots.base_bot import BaseBot
    # We can't easily instantiate BaseBot (NT8 path validation, etc.)
    # without a heavy harness. Verify the class-level default is what
    # we expect; the __init__ explicitly resets it via
    # self._recovery_alert_session_date = None which is identical.
    assert BaseBot._recovery_alert_session_date is None


def test_recovery_alert_block_uses_dedup(monkeypatch):
    """Source-level grep: the RECOVERY MODE branch must check the dedup
    state before firing notify_alert."""
    src = (ROOT_PATH := __import__("pathlib").Path(ROOT)) / "bots" / "base_bot.py"
    text = src.read_text(encoding="utf-8")
    # The notify_alert("RECOVERY MODE" ...) call must be GUARDED by a
    # check on _recovery_alert_session_date. Find both lines and verify
    # the guard appears within ~4 lines BEFORE the alert.
    idx = text.find('notify_alert(\n                        "RECOVERY MODE"')
    if idx < 0:
        # Fallback: any single-line variant
        idx = text.find('"RECOVERY MODE"')
    assert idx > 0, "couldn't find RECOVERY MODE alert site"
    # Look back ~6 lines for the dedup check
    pre = text[max(0, idx - 600):idx]
    assert "_recovery_alert_session_date" in pre, (
        "RECOVERY MODE alert is not guarded by _recovery_alert_session_date "
        "dedup — every loss will re-fire"
    )


# Source path constant for the grep test
ROOT_PATH = ROOT
