"""Tests for tools.watcher_agent — severity classification + key parsers.

Does not exercise the async loops — those are covered in operational
smoke tests rather than unit tests. Focuses on:
  * Finding severity + SMS format
  * Log-tail helper
  * Market-hours gating
  * InvestigatorAgent file-action allow-list (rejects out-of-scope paths)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from tools import watcher_agent as wa


class TestFinding:
    def test_sms_includes_severity_category_detail(self):
        f = wa.Finding(severity="RED_ALERT", category="process_down",
                       detail="sim_bot not running")
        sms = f.as_sms()
        assert "RED_ALERT" in sms
        assert "process_down" in sms
        assert "sim_bot not running" in sms
        assert "Check logs/incidents/" in sms

    def test_sms_truncates_long_detail(self):
        f = wa.Finding(severity="MAJOR", category="x", detail="y" * 500)
        sms = f.as_sms()
        # Body should have been clipped to 140 chars
        assert len(sms) < 400


class TestMarketHours:
    def test_weekday_within_hours(self):
        # 2026-04-22 = Wednesday, 10:00 CT
        dt = datetime(2026, 4, 22, 10, 0, tzinfo=wa.CT_TZ)
        assert wa._is_market_hours(dt)

    def test_weekend_always_false(self):
        dt = datetime(2026, 4, 25, 10, 0, tzinfo=wa.CT_TZ)  # Saturday
        assert not wa._is_market_hours(dt)

    def test_before_open(self):
        dt = datetime(2026, 4, 22, 7, 0, tzinfo=wa.CT_TZ)
        assert not wa._is_market_hours(dt)

    def test_after_close(self):
        dt = datetime(2026, 4, 22, 16, 0, tzinfo=wa.CT_TZ)
        assert not wa._is_market_hours(dt)


class TestTailHelper:
    def test_tail_empty_path_returns_empty(self, tmp_path):
        p = tmp_path / "does_not_exist.log"
        assert wa._tail(p) == []

    def test_tail_last_n_lines(self, tmp_path):
        p = tmp_path / "t.log"
        p.write_text("\n".join(f"line{i}" for i in range(1, 101)), encoding="utf-8")
        lines = wa._tail(p, n=5)
        assert lines == ["line96", "line97", "line98", "line99", "line100"]


class TestInvestigatorFileAction:
    """Exercise the _execute_file_action allow-list without calling Gemini."""

    @pytest.fixture
    def investigator(self, monkeypatch, tmp_path):
        # Build a dry-ish Investigator — no Gemini key needed since we only
        # test the file-action branch directly.
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        alerter = wa.Alerter(dry_run=True)
        return wa.InvestigatorAgent(alerter, dry_run=False)

    def test_rejects_path_outside_whitelist(self, investigator, tmp_path):
        random = tmp_path / "random.txt"
        random.write_text("hi")
        ok, msg = investigator._execute_file_action(f"delete {random}")
        assert not ok
        assert "allow-list" in msg.lower()

    def test_rejects_fresh_oif(self, investigator, monkeypatch, tmp_path):
        # Point OIF_INCOMING at a tempdir and drop a fresh file.
        monkeypatch.setattr(wa, "OIF_INCOMING", tmp_path)
        f = tmp_path / "oif123_phoenix_test_entry.txt"
        f.write_text("PLACE;...", encoding="utf-8")
        ok, msg = investigator._execute_file_action(f"delete {f}")
        assert not ok
        assert "too fresh" in msg

    def test_accepts_stale_oif(self, investigator, monkeypatch, tmp_path):
        monkeypatch.setattr(wa, "OIF_INCOMING", tmp_path)
        f = tmp_path / "oif456_phoenix_test_entry.txt"
        f.write_text("PLACE;...", encoding="utf-8")
        # Backdate the file
        old = time.time() - 10 * 60
        os.utime(f, (old, old))
        ok, msg = investigator._execute_file_action(f"delete {f}")
        assert ok
        assert not f.exists()

    def test_rejects_unparseable_detail(self, investigator):
        ok, msg = investigator._execute_file_action("please fix the thing")
        assert not ok
        assert "parse" in msg.lower()


class TestInvestigatorRestartAllowList:
    @pytest.fixture
    def investigator(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        alerter = wa.Alerter(dry_run=True)
        return wa.InvestigatorAgent(alerter, dry_run=True)

    def test_rejects_non_allowlisted_target(self, investigator):
        ok, msg = investigator._execute_restart("dashboard")  # not in allow-list
        assert not ok
        assert "not allowed" in msg

    def test_rejects_unknown_target(self, investigator):
        ok, msg = investigator._execute_restart("random_process")
        assert not ok
        assert "not allowed" in msg

    def test_accepts_sim_bot_in_dry_run(self, investigator):
        ok, msg = investigator._execute_restart("sim_bot")
        assert ok
        assert "DRY-RUN" in msg

    def test_accepts_bridge_in_dry_run(self, investigator):
        # Per Jennifer 2026-04-24: bridge restart allowed
        ok, msg = investigator._execute_restart("bridge")
        assert ok
        assert "DRY-RUN" in msg


class TestInvestigatorDegraded:
    def test_missing_api_key_returns_manual_required(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        alerter = wa.Alerter(dry_run=True)
        inv = wa.InvestigatorAgent(alerter, dry_run=True)
        f = wa.Finding(severity="MAJOR", category="test", detail="synthetic")
        ai = inv._ask_gemini(f)
        assert ai["fix_type"] == "manual_required"
        assert "unavailable" in ai["root_cause"].lower()
