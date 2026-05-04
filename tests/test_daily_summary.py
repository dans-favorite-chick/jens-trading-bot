"""Daily session summary smoke + anomaly tests."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "daily_session_summary.py"


def _write_history(root: Path, d: date, bot: str, events: list[dict]) -> None:
    p = root / f"logs/history/{d}_{bot}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _run(tmp_path: Path, *cli_args: str) -> tuple[int, str, str]:
    """Run the daily summary tool. Returns (rc, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(TOOL), *cli_args],
        cwd=tmp_path, capture_output=True, text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_missing_history_reported_not_crashed(tmp_path):
    """Tool produces a 'missing' note when no history file exists for the day."""
    (tmp_path / "out").mkdir()
    (tmp_path / "logs/history").mkdir(parents=True)
    rc, out, err = _run(tmp_path, "--date", "2099-12-31", "--bot", "sim")
    assert rc == 0, f"non-zero exit: {err}"
    reports = list((tmp_path / "out").glob("daily_summary_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "No history JSONL" in text


def test_anomaly_detected_when_strategy_goes_silent(tmp_path):
    """Strategy with active 7-day baseline going silent → anomaly flagged."""
    today = date.today()
    # 7 days of baseline: 5 signals/day for bias_momentum
    for d_offset in range(1, 8):
        d = today - timedelta(days=d_offset)
        _write_history(tmp_path, d, "sim", [
            {"event": "eval", "ts": f"{d}T09:30:00", "strategies": [
                {"name": "bias_momentum", "result": "SIGNAL"} for _ in range(5)
            ]}
        ])
    # Today: ZERO signals
    _write_history(tmp_path, today, "sim", [
        {"event": "eval", "ts": f"{today}T09:30:00", "strategies": []}
    ])
    rc, out, err = _run(tmp_path, "--date", str(today), "--bot", "sim")
    assert rc == 0, f"non-zero exit: {err}"
    text = (tmp_path / f"out/daily_summary_{today}.md").read_text(encoding="utf-8")
    assert "silent" in text.lower() or "anomalies" in text.lower()
    assert "bias_momentum" in text


def test_normal_day_shows_no_anomaly(tmp_path):
    """A day matching baseline → no anomaly section warning."""
    today = date.today()
    for d_offset in range(0, 8):
        d = today - timedelta(days=d_offset)
        _write_history(tmp_path, d, "sim", [
            {"event": "eval", "ts": f"{d}T09:30:00", "strategies": [
                {"name": "bias_momentum", "result": "SIGNAL"} for _ in range(3)
            ]}
        ])
    rc, out, err = _run(tmp_path, "--date", str(today), "--bot", "sim")
    assert rc == 0, f"non-zero exit: {err}"
    text = (tmp_path / f"out/daily_summary_{today}.md").read_text(encoding="utf-8")
    assert "No anomalies detected" in text


def test_per_strategy_table_includes_fills_and_pnl(tmp_path):
    """Entry + exit events flow through to the per-strategy table."""
    today = date.today()
    _write_history(tmp_path, today, "sim", [
        {"event": "eval", "ts": f"{today}T09:30:00", "strategies": [
            {"name": "bias_momentum", "result": "SIGNAL"},
        ]},
        {"event": "entry", "ts": f"{today}T09:30:01", "strategy": "bias_momentum"},
        {"event": "exit",  "ts": f"{today}T09:35:00", "strategy": "bias_momentum",
         "pnl_dollars": -10.0},
    ])
    rc, out, err = _run(tmp_path, "--date", str(today), "--bot", "sim")
    assert rc == 0, f"non-zero exit: {err}"
    text = (tmp_path / f"out/daily_summary_{today}.md").read_text(encoding="utf-8")
    assert "bias_momentum" in text
    assert "-10.00" in text or "-$10" in text or "10.00" in text


def test_rejection_reasons_included(tmp_path):
    """Top rejection reasons table appears when rejections present."""
    today = date.today()
    _write_history(tmp_path, today, "sim", [
        {"event": "eval", "ts": f"{today}T09:30:00", "strategies": [
            {"name": "bias_momentum", "result": "REJECTED",
             "reason": "VWAP gate (LONG, dist=12)"}
        ]} for _ in range(3)
    ])
    rc, out, err = _run(tmp_path, "--date", str(today), "--bot", "sim")
    assert rc == 0, f"non-zero exit: {err}"
    text = (tmp_path / f"out/daily_summary_{today}.md").read_text(encoding="utf-8")
    assert "rejection" in text.lower()
    assert "VWAP gate" in text
