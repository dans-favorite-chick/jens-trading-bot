"""Backfill baseline quality assessment.

Smoke-tests + per-flag tests for tools/backfill_commissions.py. Each
test creates a synthetic trade_memory.json in tmp_path, runs the tool
as a subprocess (so it exercises the real CLI surface), and asserts
the report contains the expected warning string.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "backfill_commissions.py"


def _run(tmp_path: Path) -> tuple[int, str]:
    """Run the backfill tool with cwd=tmp_path, return (returncode, report_text)."""
    result = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    reports = list((tmp_path / "out").glob("historical_pnl_recompute_*.md"))
    text = reports[0].read_text(encoding="utf-8") if reports else ""
    return result.returncode, text


def _seed(tmp_path: Path, trades: list[dict]) -> None:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )


# ─── smoke ───────────────────────────────────────────────────────────

def test_backfill_runs_and_writes_report(tmp_path):
    """Tool runs, returns 0, and produces a report."""
    _seed(tmp_path, [
        {"strategy": "bias_momentum", "contracts": 1,
         "pnl_dollars": 10.0, "entry_time": "2026-04-01T09:30:00"},
        {"strategy": "bias_momentum", "contracts": 1,
         "pnl_dollars_gross": 12.0, "pnl_dollars_net": 7.0,
         "pnl_dollars": 7.0, "cost_total_dollars": 5.0,
         "entry_time": "2026-05-01T09:30:00"},
    ])
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "Phoenix B13 Historical Recompute" in text
    assert "## Overall" in text
    assert "## Per Strategy" in text
    assert "## Contract Size Distribution" in text


# ─── flag: legacy strategies ─────────────────────────────────────────

def test_quality_flag_detects_legacy_strategies(tmp_path):
    """Trades with strategies not in current STRATEGIES dict are flagged."""
    _seed(tmp_path, [
        {"strategy": "unknown_legacy_v1", "contracts": 1,
         "pnl_dollars": 10.0, "entry_time": "2025-01-01T09:30:00"},
        {"strategy": "another_dead_strat", "contracts": 1,
         "pnl_dollars": -5.0, "entry_time": "2025-01-02T09:30:00"},
    ])
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "Legacy/unknown strategies" in text
    assert "unknown_legacy_v1" in text


# ─── flag: suspicious gross loss > $200 ─────────────────────────────

def test_quality_flag_detects_suspicious_loss(tmp_path):
    """Trades with gross < -$200 raise the suspicious-loss warning."""
    _seed(tmp_path, [
        {"strategy": "bias_momentum", "contracts": 1,
         "pnl_dollars": -350.0, "entry_time": "2026-04-01T09:30:00"},
        {"strategy": "bias_momentum", "contracts": 1,
         "pnl_dollars": 5.0, "entry_time": "2026-04-02T09:30:00"},
    ])
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "gross loss > $200" in text
    assert "suspicious" in text.lower()


# ─── flag: high avg gross with low contracts ─────────────────────────

def test_quality_flag_detects_high_avg_gross_with_low_contracts(tmp_path):
    """Avg |gross|/trade > $100 with avg contracts < 2 -> legacy NQ flag."""
    # 10 trades all 1-contract, all $-150 gross -> avg |gross|/trade = $150
    _seed(tmp_path, [
        {"strategy": "bias_momentum", "contracts": 1,
         "pnl_dollars": -150.0,
         "entry_time": f"2026-04-{i:02d}T09:30:00"}
        for i in range(1, 11)
    ])
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "legacy NQ data" in text or "1 tick = $5" in text


# ─── flag: contracts >= 5 ────────────────────────────────────────────

def test_quality_flag_detects_big_contracts(tmp_path):
    """Trades with contracts >= 5 raise the sizing-error warning."""
    _seed(tmp_path, [
        {"strategy": "bias_momentum", "contracts": 5,
         "pnl_dollars": 50.0, "entry_time": "2026-04-01T09:30:00"},
    ])
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "contracts >= 5" in text


# ─── happy path: clean data, no flags ────────────────────────────────

def test_no_flags_when_data_is_clean(tmp_path):
    """Single 1-contract MNQ-realistic trade with known strategy raises no warnings."""
    _seed(tmp_path, [
        {"strategy": "bias_momentum", "contracts": 1,
         "pnl_dollars_gross": 12.0, "pnl_dollars_net": 7.0,
         "pnl_dollars": 7.0, "cost_total_dollars": 5.0,
         "entry_time": "2026-04-01T09:30:00"},
        {"strategy": "bias_momentum", "contracts": 1,
         "pnl_dollars_gross": -10.0, "pnl_dollars_net": -15.0,
         "pnl_dollars": -15.0, "cost_total_dollars": 5.0,
         "entry_time": "2026-04-02T09:30:00"},
    ])
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "No baseline quality flags raised" in text


# ─── flag: missing contracts field ───────────────────────────────────

def test_quality_flag_detects_missing_contracts(tmp_path):
    """Trades missing 'contracts' key are flagged as defaulted."""
    _seed(tmp_path, [
        {"strategy": "bias_momentum",
         "pnl_dollars": 10.0, "entry_time": "2026-04-01T09:30:00"},
    ])
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "missing `contracts` field" in text
