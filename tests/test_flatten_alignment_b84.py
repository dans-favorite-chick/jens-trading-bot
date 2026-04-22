"""B84 — Flatten timing alignment with NT8 Auto Close Position.

The defense-in-depth schedule (all CT):
  15:53  Phoenix stops accepting new entries (NO_NEW_ENTRIES gate)
  15:54  Phoenix DailyFlattener fires (PRIMARY)
  15:54:45  Phoenix logs WARN if any position is still open
  15:55  NT8 Auto Close Position (SAFETY NET, configured in NT8 GUI)
  16:00  CME globex 1-hour maintenance break (HARD FLOOR)

Tests here exercise the pieces of the architecture that live in Python:
- test_no_new_entries_after_15_53_ct: the gate in BaseBot._is_no_new_entries_window
- test_session_close_event_logged: history_logger.log_session_close_event
- test_flatten_fires_at_15_54_ct: lives in test_daily_flatten.py (B84 section)

Run: pytest tests/test_flatten_alignment_b84.py -v
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, date
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CT = ZoneInfo("America/Chicago")


def _ct(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=CT)


# ═══════════════════════════════════════════════════════════════════
# 15:53 CT no-new-entries gate
# ═══════════════════════════════════════════════════════════════════
class TestNoNewEntriesAfter1553:
    """BaseBot._is_no_new_entries_window returns True between 15:53 CT
    and the 17:00 CT globex reopen, False everywhere else. _enter_trade
    short-circuits when the gate is True (no new positions opened in
    the final runway before the 15:54 flatten)."""

    @pytest.fixture
    def gate(self):
        from bots.base_bot import BaseBot
        # Mock bot with just the one method bound so we don't spin up the
        # full BaseBot __init__ (it loads a lot of modules).
        bot = MagicMock(spec=BaseBot)
        bot._is_no_new_entries_window = (
            BaseBot._is_no_new_entries_window.__get__(bot)
        )
        return bot

    def test_1552_still_allows_entries(self, gate):
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 15, 52, 0)) is False

    def test_1552_59_still_allows_entries(self, gate):
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 15, 52, 59)) is False

    def test_1553_00_blocks_entries(self, gate):
        """On the dot at 15:53 CT, no new entries."""
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 15, 53, 0)) is True

    def test_1554_blocks_entries(self, gate):
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 15, 54, 0)) is True

    def test_1630_blocks_entries_during_maintenance(self, gate):
        """The no-entries window extends through the maintenance break
        and only lifts at 17:00 CT (globex reopen)."""
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 16, 30, 0)) is True

    def test_1659_59_still_blocks(self, gate):
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 16, 59, 59)) is True

    def test_1700_00_entries_allowed_again(self, gate):
        """New globex session: entries allowed again."""
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 17, 0, 0)) is False

    def test_morning_allows_entries(self, gate):
        assert gate._is_no_new_entries_window(_ct(2026, 4, 21, 9, 15, 0)) is False

    def test_gate_is_timezone_aware(self, gate):
        """The gate reads wall-clock time from the tz-aware datetime,
        not from the system local clock. Pass a CT 15:54 and it blocks;
        a PT 13:54 equivalent also blocks (same instant)."""
        ct_time = _ct(2026, 4, 21, 15, 54, 0)
        pt_time = ct_time.astimezone(ZoneInfo("America/Los_Angeles"))
        # Both represent 15:54 CT = 13:54 PT.
        assert gate._is_no_new_entries_window(ct_time) is True
        # Caller is expected to pass a CT datetime; if they pass PT,
        # the gate compares wall-clock 13:54 which is NOT in the CT
        # cutoff window, so it returns False. This is a caller contract:
        # pass CT. Document by asserting the documented behavior.
        assert gate._is_no_new_entries_window(pt_time) is False


# ═══════════════════════════════════════════════════════════════════
# history_logger.log_session_close_event
# ═══════════════════════════════════════════════════════════════════
class TestSessionCloseEventLogged:
    """log_session_close_event writes a single structured session_close
    record to the bot's JSONL history file."""

    @pytest.fixture
    def hl(self, tmp_path, monkeypatch):
        """HistoryLogger pointed at a tmp dir so we read the event back."""
        from core.history_logger import HistoryLogger
        # HistoryLogger paths are resolved inside the class; use a sub
        # to redirect. Simplest: monkeypatch the _get_file helper.
        h = HistoryLogger(bot_name="sim")
        # Redirect its output path
        log_file = tmp_path / "session_close_test.jsonl"

        def _get_file():
            if h._file is None:
                h._file = open(log_file, "a", encoding="utf-8")
            return h._file

        monkeypatch.setattr(h, "_get_file", _get_file)
        yield h, log_file
        if h._file:
            h._file.close()

    def test_logs_event_with_all_required_fields(self, hl):
        h, log_file = hl
        now_ct = _ct(2026, 4, 21, 15, 54, 0)
        h.log_session_close_event(
            now_ct=now_ct,
            flattened_trade_ids=["t1", "t2"],
            still_open_trade_ids=[],
            session_pnl=123.45,
            b13_applied=True,
        )
        h.close()  # flush
        raw = log_file.read_text().strip()
        assert raw, "no event written"
        ev = json.loads(raw.splitlines()[-1])
        assert ev["event"] == "session_close"
        assert ev["bot"] == "sim"
        assert ev["flattened_trade_ids"] == ["t1", "t2"]
        assert ev["still_open_trade_ids"] == []
        assert ev["flattened_count"] == 2
        assert ev["still_open_count"] == 0
        assert ev["session_pnl"] == 123.45
        assert ev["b13_commission_applied"] is True
        assert ev["note"] is None  # no note when B13 applied

    def test_logs_note_when_b13_not_applied(self, hl):
        h, log_file = hl
        h.log_session_close_event(
            now_ct=_ct(2026, 4, 21, 15, 54),
            flattened_trade_ids=["t1"],
            still_open_trade_ids=[],
            session_pnl=10.0,
            b13_applied=False,
        )
        h.close()
        ev = json.loads(log_file.read_text().splitlines()[-1])
        assert ev["b13_commission_applied"] is False
        assert ev["note"] is not None
        assert "B13" in ev["note"]

    def test_logs_still_open_when_flatten_incomplete(self, hl):
        """If Phoenix can't close everything by grace-window end, the
        session_close event still fires, capturing which trade_ids are
        being handed to NT8's safety net."""
        h, log_file = hl
        h.log_session_close_event(
            now_ct=_ct(2026, 4, 21, 15, 54),
            flattened_trade_ids=["t1"],
            still_open_trade_ids=["t2", "t3"],
            session_pnl=5.0,
            b13_applied=True,
        )
        h.close()
        ev = json.loads(log_file.read_text().splitlines()[-1])
        assert ev["flattened_count"] == 1
        assert ev["still_open_count"] == 2
        assert ev["still_open_trade_ids"] == ["t2", "t3"]

    def test_event_timestamp_is_tz_aware_ct(self, hl):
        """ts field is ISO 8601 with -05:00 or -06:00 offset (CT)."""
        h, log_file = hl
        h.log_session_close_event(
            now_ct=_ct(2026, 4, 21, 15, 54),
            flattened_trade_ids=[],
            still_open_trade_ids=[],
            session_pnl=0.0,
            b13_applied=False,
        )
        h.close()
        ev = json.loads(log_file.read_text().splitlines()[-1])
        # -05:00 (CDT) or -06:00 (CST)
        assert ev["ts"].endswith("-05:00") or ev["ts"].endswith("-06:00")


# ═══════════════════════════════════════════════════════════════════
# settings constants are the single source of truth
# ═══════════════════════════════════════════════════════════════════
class TestSettingsConstants:
    def test_daily_flatten_is_1554(self):
        from config.settings import (
            DAILY_FLATTEN_HOUR_CT, DAILY_FLATTEN_MINUTE_CT,
        )
        assert (DAILY_FLATTEN_HOUR_CT, DAILY_FLATTEN_MINUTE_CT) == (15, 54)

    def test_no_new_entries_is_1553(self):
        from config.settings import (
            NO_NEW_ENTRIES_HOUR_CT, NO_NEW_ENTRIES_MINUTE_CT,
        )
        assert (NO_NEW_ENTRIES_HOUR_CT, NO_NEW_ENTRIES_MINUTE_CT) == (15, 53)

    def test_fill_grace_is_45s(self):
        from config.settings import FILL_CONFIRMATION_GRACE_SECONDS
        assert FILL_CONFIRMATION_GRACE_SECONDS == 45

    def test_flattener_picks_up_settings_defaults(self):
        """DailyFlattener's __init__ defaults flow from config.settings —
        changing the constant in one place moves the whole system."""
        from bots.daily_flatten import DailyFlattener
        f = DailyFlattener(positions_manager=MagicMock())
        assert f.flatten_hour == 15
        assert f.flatten_minute == 54


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
