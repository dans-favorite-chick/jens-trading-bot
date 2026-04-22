"""
S6 / B70 — Conflict logger tests.

Covers:
- log_conflict_opened writes JSON with correct schema to today's jsonl
- log_conflict_closed appends a conflict_closed event
- Missing logs/conflicts/ directory is auto-created
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import conflict_logger as cflog
from core.position_manager import PositionManager


def _open(pm, tid, strategy, direction, entry=18000.0, account="Sim101"):
    assert pm.open_position(
        trade_id=tid, direction=direction, entry_price=entry, contracts=1,
        stop_price=entry - 10 if direction == "LONG" else entry + 10,
        target_price=entry + 20 if direction == "LONG" else entry - 20,
        strategy=strategy, reason="test", account=account,
    )


@pytest.fixture
def tmp_log_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "conflicts_nested")  # nested -> tests auto-create
        monkeypatch.setenv("PHOENIX_CONFLICT_LOG_DIR", target)
        yield target


def _today_path(base: str) -> str:
    return os.path.join(base, f"{datetime.now().strftime('%Y-%m-%d')}.jsonl")


def test_log_conflict_opened_schema(tmp_log_dir):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    _open(pm, "t2", "spring_setup", "SHORT")

    new_pos = pm.get_position("t2")
    conflicts = [{
        "trade_id_a": "t1", "strategy_a": "bias_momentum", "dir_a": "LONG",
        "account_a": "Sim101", "entry_a": 18000.0, "opened_at_a": 1.0,
        "trade_id_b": "t2", "strategy_b": "spring_setup", "dir_b": "SHORT",
        "account_b": "Sim102", "entry_b": 18000.0, "opened_at_b": 2.0,
        "overlap_seconds": 5.0,
    }]
    exposure = {"net": 0, "gross": 2, "longs": [], "shorts": []}

    cflog.log_conflict_opened(new_pos, conflicts, exposure)

    path = _today_path(tmp_log_dir)
    assert os.path.exists(path), "auto-created dir + file expected"
    with open(path, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 1
    ev = lines[0]
    assert ev["event"] == "conflict_opened"
    assert "ts" in ev
    assert ev["new_position"]["strategy"] == "spring_setup"
    assert ev["new_position"]["trade_id"] == "t2"
    assert ev["conflicts"] == conflicts
    assert ev["exposure"] == exposure


def test_log_conflict_closed_appends(tmp_log_dir):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    closed = pm.get_position("t1")
    cflog.log_conflict_closed(closed, [], {"net": 0, "gross": 0, "longs": [], "shorts": []})

    path = _today_path(tmp_log_dir)
    with open(path, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 1
    assert lines[0]["event"] == "conflict_closed"
    assert lines[0]["closed_position"]["trade_id"] == "t1"


def test_log_conflict_opened_empty_list_is_noop(tmp_log_dir):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    cflog.log_conflict_opened(pm.get_position("t1"), [], {"net": 1, "gross": 1, "longs": [], "shorts": []})
    path = _today_path(tmp_log_dir)
    # No file written since there were no conflicts to log
    assert not os.path.exists(path)


def test_log_dir_autocreated(tmp_log_dir):
    # tmp_log_dir points at a path that doesn't exist yet.
    assert not os.path.exists(tmp_log_dir)
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    cflog.log_conflict_closed(pm.get_position("t1"), [], {"net": 1, "gross": 1, "longs": [], "shorts": []})
    assert os.path.exists(tmp_log_dir)
