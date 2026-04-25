"""Tests for tools/routines/post_session_debrief.py — risk metrics + log scan."""

from __future__ import annotations

from pathlib import Path

from tools.routines.post_session_debrief import (
    compute_risk_metrics, scan_new_error_signatures,
)


class TestRiskMetrics:
    def test_empty_trades(self):
        m = compute_risk_metrics([])
        assert m["trades"] == 0
        assert m["total_pnl"] == 0.0

    def test_single_winner(self):
        m = compute_risk_metrics([{"pnl": 50.0}])
        assert m["trades"] == 1
        assert m["total_pnl"] == 50.0
        assert m["wins"] == 1
        assert m["losses"] == 0
        assert m["win_rate"] == 1.0

    def test_mixed(self):
        m = compute_risk_metrics([
            {"pnl": 100}, {"pnl": -50}, {"pnl": 75}, {"pnl": -25}
        ])
        assert m["trades"] == 4
        assert m["total_pnl"] == 100.0
        assert m["wins"] == 2
        assert m["losses"] == 2
        assert m["win_rate"] == 0.5
        # Profit factor: gross win 175 / gross loss 75 = 2.33
        assert m["profit_factor"] == 2.33

    def test_max_drawdown(self):
        # Equity curve: 100, 200, 150, 50. Peak=200, trough=50, DD = -150
        m = compute_risk_metrics([
            {"pnl": 100}, {"pnl": 100}, {"pnl": -50}, {"pnl": -100}
        ])
        assert m["max_drawdown"] == -150.0


class TestNewErrorSignatures:
    def test_empty_logs(self, tmp_path: Path):
        p = tmp_path / "empty.log"
        p.write_text("", encoding="utf-8")
        result = scan_new_error_signatures([p])
        assert result["new_signatures"] == []
        assert result["total_errors_today"] == 0

    def test_detects_today_only_signature(self, tmp_path: Path, monkeypatch):
        from tools.routines import post_session_debrief as psd
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
        old_date = "2026-04-20"
        log = tmp_path / "test.log"
        log.write_text(
            f"{old_date} 09:00:00,000 [Foo] ERROR Old signature\n"
            f"{today} 10:00:00,000 [Foo] ERROR New signature today\n",
            encoding="utf-8",
        )
        result = scan_new_error_signatures([log])
        assert result["total_errors_today"] == 1
        # The "new signature" should appear since old log doesn't have it
        new_sigs_text = " ".join(result["new_signatures"])
        assert "New signature today" in new_sigs_text
