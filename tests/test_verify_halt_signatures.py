"""Halt signature verification tool — smoke test.

Sprint A's Fix E added [HALT:<strategy>] / [CAP:<scope>:<account>]
log signatures with unit tests. The verify_halt_signatures tool
exercises the production call chain end-to-end. This test is a
smoke check that the tool itself runs and produces a report.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "verify_halt_signatures.py"


def test_verify_tool_runs_and_writes_report(tmp_path):
    """Tool exits 0 (all-pass) or 1 (one-or-more-failed), and produces the report.

    Either exit code is acceptable for the smoke test — what we're
    verifying is that the *tool* runs without crashing. Whether the
    underlying production code passes the signatures is what the
    PASS/FAIL row in the report shows.
    """
    (tmp_path / "out").mkdir()
    result = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode in (0, 1), (
        f"tool crashed: rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    reports = list((tmp_path / "out").glob("halt_verify_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Phoenix Halt Signature Verification" in text
    assert "Overall:" in text
    # Each of the 4 signatures should appear in the report
    assert "[HALT:<strategy>]" in text
    assert "[HALT:bot]" in text
    assert "[CAP:daily:<account>]" in text
    assert "[CAP:weekly:<account>]" in text


def test_verify_tool_reports_pass_when_signatures_intact(tmp_path):
    """Against the current Sprint A code, all 4 signatures must PASS.

    If this test fails, Sprint A's Fix E logging has regressed and
    watcher_agent will silently miss halt events in production.
    """
    (tmp_path / "out").mkdir()
    result = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"halt-verify regression — at least one signature FAILED.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}\n"
        f"Investigate core/strategy_risk_registry.py:halt() and "
        f"core/risk_manager.py:can_trade() logging."
    )
    text = (next((tmp_path / "out").glob("halt_verify_*.md"))
            .read_text(encoding="utf-8"))
    assert "ALL PASS" in text
