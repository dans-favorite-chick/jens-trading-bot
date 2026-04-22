"""
P0.1 — Trade Memory Persistence (D13)

PositionManager.trade_history must hydrate from logs/trade_memory.json at
__init__ time when load_history=True, so dashboard P&L and any in-process
consumer of closed-trade history survive bot/dashboard restart.

Graceful failure modes:
- missing file: empty list, INFO logged (not WARNING)
- corrupt JSON: empty list, WARNING logged, no crash
- bad shape (not a list): empty list, WARNING logged, no crash

Schema-preserving: rows in trade_memory.json carry pnl_dollars, exit_time,
bot_id, strategy, sub_strategy (and other fields). All fields retained.
"""

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import position_manager as pm_mod
from core.position_manager import PositionManager


# ─── Helpers ────────────────────────────────────────────────────────

def _fake_rows():
    return [
        {
            "trade_id": "t1",
            "pnl_dollars": 12.50,
            "exit_time": 1776800000.0,
            "bot_id": "prod",
            "strategy": "bias_momentum",
            "sub_strategy": None,
            "result": "WIN",
        },
        {
            "trade_id": "t2",
            "pnl_dollars": -8.00,
            "exit_time": 1776800100.0,
            "bot_id": "prod",
            "strategy": "orb",
            "sub_strategy": None,
            "result": "LOSS",
        },
        {
            "trade_id": "t3",
            "pnl_dollars": 20.0,
            "exit_time": 1776800200.0,
            "bot_id": "prod",
            "strategy": "spring_setup",
            "sub_strategy": None,
            "result": "WIN",
        },
    ]


# ─── 1. Fresh start ─────────────────────────────────────────────────

def test_fresh_start_no_file(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "trade_memory.json"  # intentionally does not exist
    monkeypatch.setattr(pm_mod, "TRADE_MEMORY_PATH", str(missing))

    with caplog.at_level(logging.INFO, logger="PositionManager"):
        pm = PositionManager(load_history=True)

    assert pm.trade_history == []
    assert any(
        "no trade_memory.json found" in r.getMessage() and r.levelno == logging.INFO
        for r in caplog.records
    ), "expected INFO log about missing file"


# ─── 2. Existing file loads ─────────────────────────────────────────

def test_existing_file_loads(tmp_path, monkeypatch):
    path = tmp_path / "trade_memory.json"
    rows = _fake_rows()
    path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(pm_mod, "TRADE_MEMORY_PATH", str(path))

    pm = PositionManager(load_history=True)

    assert len(pm.trade_history) == 3
    total = sum(r["pnl_dollars"] for r in pm.trade_history)
    assert total == pytest.approx(12.5 - 8.0 + 20.0)
    # Schema preservation
    for key in ("pnl_dollars", "exit_time", "bot_id", "strategy"):
        assert key in pm.trade_history[0]


# ─── 3. Corrupt file graceful ────────────────────────────────────────

def test_corrupt_file_graceful(tmp_path, monkeypatch, caplog):
    path = tmp_path / "trade_memory.json"
    path.write_bytes(b"\x00\x01not json at all}{")
    monkeypatch.setattr(pm_mod, "TRADE_MEMORY_PATH", str(path))

    with caplog.at_level(logging.WARNING, logger="PositionManager"):
        pm = PositionManager(load_history=True)  # must not crash

    assert pm.trade_history == []
    assert any(r.levelno == logging.WARNING for r in caplog.records), \
        "expected WARNING log on corrupt JSON"


def test_wrong_shape_graceful(tmp_path, monkeypatch, caplog):
    path = tmp_path / "trade_memory.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    monkeypatch.setattr(pm_mod, "TRADE_MEMORY_PATH", str(path))

    with caplog.at_level(logging.WARNING, logger="PositionManager"):
        pm = PositionManager(load_history=True)

    assert pm.trade_history == []


# ─── 4. Real file loads without error ───────────────────────────────

def test_real_file_loads_without_error():
    """If logs/trade_memory.json exists in repo, it must load cleanly
    and every row must carry pnl_dollars."""
    real = pm_mod.TRADE_MEMORY_PATH
    if not os.path.exists(real):
        pytest.skip("logs/trade_memory.json not present in this checkout")

    pm = PositionManager(load_history=True)
    assert isinstance(pm.trade_history, list)
    assert len(pm.trade_history) > 0
    for i, row in enumerate(pm.trade_history):
        assert isinstance(row, dict), f"row {i} is not a dict"
        assert "pnl_dollars" in row, f"row {i} missing pnl_dollars"


# ─── 5. Idempotent load — P&L sum stable across reloads ─────────────

def test_integrity_pnl_sum_matches_file():
    real = pm_mod.TRADE_MEMORY_PATH
    if not os.path.exists(real):
        pytest.skip("logs/trade_memory.json not present in this checkout")

    pm1 = PositionManager(load_history=True)
    sum1 = sum(float(r.get("pnl_dollars") or 0.0) for r in pm1.trade_history)

    pm2 = PositionManager(load_history=True)
    sum2 = sum(float(r.get("pnl_dollars") or 0.0) for r in pm2.trade_history)

    assert len(pm1.trade_history) == len(pm2.trade_history)
    assert sum1 == pytest.approx(sum2)


# ─── 6. Default opt-out preserves legacy test expectations ──────────

def test_default_does_not_load_history():
    """Default construction keeps trade_history empty so existing unit
    tests that assert len(trade_history)==1 after one close still pass."""
    pm = PositionManager()
    assert pm.trade_history == []
