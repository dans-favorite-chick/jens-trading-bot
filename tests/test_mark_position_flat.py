"""Operator tool: mark a stuck position as resolved in trade_memory.

Tests cover:
  - preview mode never writes
  - --apply mutates the matching record + writes audit log
  - unknown trade_id exits non-zero
  - exit_price option overlays correctly
  - schema preservation (list vs {"trades": [...]})
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "mark_position_flat.py"


def _seed(tmp_path: Path, trades, schema="list"):
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    if schema == "list":
        data = trades
    else:
        data = {"trades": trades}
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _run(tmp_path: Path, *cli_args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(TOOL), *cli_args],
        cwd=tmp_path, capture_output=True, text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _stuck_trade(tid="abc123") -> dict:
    return {
        "trade_id": tid,
        "strategy": "bias_momentum",
        "account": "SimBias Momentum",
        "direction": "LONG",
        "entry_price": 27800.0,
        "entry_time": "2026-05-04T10:00:00",
        "exit_price": None,
        "exit_time": None,
        "state": "exit_pending",
    }


def _resolved_trade(tid="def456") -> dict:
    return {
        "trade_id": tid,
        "strategy": "bias_momentum",
        "account": "SimBias Momentum",
        "direction": "LONG",
        "entry_price": 27800.0,
        "entry_time": "2026-05-04T09:00:00",
        "exit_price": 27810.0,
        "exit_time": "2026-05-04T09:05:00",
        "state": "closed",
        "exit_reason": "target_hit",
    }


# ─── preview mode never writes ───────────────────────────────────────

def test_preview_mode_never_writes(tmp_path):
    _seed(tmp_path, [_stuck_trade("abc123")])
    tm = tmp_path / "logs" / "trade_memory.json"
    before = tm.read_text(encoding="utf-8")
    rc, out, _ = _run(tmp_path, "--trade-id", "abc123")
    assert rc == 0
    assert "[PREVIEW]" in out
    after = tm.read_text(encoding="utf-8")
    assert before == after, "preview must not modify the file"
    # No audit log either
    assert not (tmp_path / "memory" / "audit_log.jsonl").exists()


def test_preview_shows_unresolved_flag(tmp_path):
    _seed(tmp_path, [_stuck_trade("abc123"), _resolved_trade("def456")])
    rc, out, _ = _run(tmp_path, "--trade-id", "abc123")
    assert rc == 0
    assert "UNRESOLVED" in out


def test_preview_shows_already_resolved_flag(tmp_path):
    _seed(tmp_path, [_resolved_trade("def456")])
    rc, out, _ = _run(tmp_path, "--trade-id", "def456")
    assert rc == 0
    assert "already resolved" in out


# ─── --apply path ────────────────────────────────────────────────────

def test_apply_mutates_state_and_writes_audit(tmp_path):
    _seed(tmp_path, [_stuck_trade("abc123")])
    rc, out, _ = _run(tmp_path, "--trade-id", "abc123", "--apply")
    assert rc == 0, out
    assert "[APPLIED]" in out

    # state is now manually_closed
    raw = json.loads((tmp_path / "logs" / "trade_memory.json").read_text())
    assert raw[0]["state"] == "manually_closed"
    assert raw[0]["state_change_reason"] == "operator_manual_flatten"
    assert "state_change_ts" in raw[0]

    # audit log written
    audit = tmp_path / "memory" / "audit_log.jsonl"
    assert audit.exists()
    line = audit.read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["event"] == "manual_mark_flat"
    assert entry["trade_id"] == "abc123"
    assert entry["operator"] is True
    assert entry["before"]["state"] == "exit_pending"
    assert entry["after"]["state"] == "manually_closed"


def test_apply_with_custom_exit_price(tmp_path):
    _seed(tmp_path, [_stuck_trade("abc123")])
    rc, out, _ = _run(tmp_path, "--trade-id", "abc123", "--apply",
                       "--exit-price", "27795.5")
    assert rc == 0
    raw = json.loads((tmp_path / "logs" / "trade_memory.json").read_text())
    assert raw[0]["exit_price"] == 27795.5
    assert raw[0]["exit_time"]  # auto-populated since it was empty


def test_apply_with_custom_reason(tmp_path):
    _seed(tmp_path, [_stuck_trade("abc123")])
    rc, _, _ = _run(tmp_path, "--trade-id", "abc123", "--apply",
                    "--reason", "operator_flatten_after_disconnect")
    assert rc == 0
    raw = json.loads((tmp_path / "logs" / "trade_memory.json").read_text())
    assert raw[0]["state_change_reason"] == "operator_flatten_after_disconnect"


# ─── unknown trade_id ────────────────────────────────────────────────

def test_unknown_trade_id_exits_nonzero(tmp_path):
    _seed(tmp_path, [_stuck_trade("abc123")])
    rc, out, _ = _run(tmp_path, "--trade-id", "DOESNOTEXIST")
    assert rc == 1
    assert "No trade" in out
    # Original file untouched
    raw = json.loads((tmp_path / "logs" / "trade_memory.json").read_text())
    assert raw[0]["state"] == "exit_pending"


# ─── schema preservation ─────────────────────────────────────────────

def test_dict_schema_preserved(tmp_path):
    """{"trades": [...]} schema must round-trip."""
    _seed(tmp_path, [_stuck_trade("abc123")], schema="dict")
    rc, _, _ = _run(tmp_path, "--trade-id", "abc123", "--apply")
    assert rc == 0
    raw = json.loads((tmp_path / "logs" / "trade_memory.json").read_text())
    assert isinstance(raw, dict)
    assert "trades" in raw
    assert raw["trades"][0]["state"] == "manually_closed"


# ─── multiple matches (duplicate trade_ids) ──────────────────────────

def test_multiple_matches_all_updated(tmp_path):
    """If trade_id duplicates exist (rare but possible), all are updated."""
    a = _stuck_trade("abc123")
    b = _stuck_trade("abc123")
    b["account"] = "SimOther"  # different account, same id
    _seed(tmp_path, [a, b])
    rc, _, _ = _run(tmp_path, "--trade-id", "abc123", "--apply")
    assert rc == 0
    raw = json.loads((tmp_path / "logs" / "trade_memory.json").read_text())
    assert raw[0]["state"] == "manually_closed"
    assert raw[1]["state"] == "manually_closed"
    # Audit log has 1 entry per match
    audit = (tmp_path / "memory" / "audit_log.jsonl").read_text(encoding="utf-8")
    assert audit.count("manual_mark_flat") == 2


# ─── never modifies running bot state (smoke check) ──────────────────

def test_tool_does_not_call_oif_writer(tmp_path):
    """Tool must NEVER place/cancel orders. We assert by checking that
    no incoming/ folder is touched."""
    _seed(tmp_path, [_stuck_trade("abc123")])
    (tmp_path / "incoming").mkdir()
    before = list((tmp_path / "incoming").iterdir())
    rc, _, _ = _run(tmp_path, "--trade-id", "abc123", "--apply")
    assert rc == 0
    after = list((tmp_path / "incoming").iterdir())
    assert before == after, "tool must not write to incoming/"
