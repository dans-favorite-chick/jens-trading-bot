"""
B69 / B70 — trade_memory.json integrity + write-guard tests.
"""

import json
import logging
import os
from pathlib import Path

import pytest

from core.trade_memory import TradeMemory

VALID_BOT_IDS = {"prod", "sim", "legacy", "unknown", "lab"}
MEMORY_FILE = Path("logs/trade_memory.json")


@pytest.mark.skipif(not MEMORY_FILE.exists(), reason="trade_memory.json not present")
def test_every_row_has_valid_bot_id():
    with MEMORY_FILE.open("r") as f:
        trades = json.load(f)
    bad = [
        (i, t.get("bot_id"))
        for i, t in enumerate(trades)
        if t.get("bot_id") is None or t.get("bot_id") not in VALID_BOT_IDS
    ]
    assert not bad, f"{len(bad)} rows have invalid/null bot_id; first few: {bad[:5]}"


def test_record_without_bot_id_defaults_to_unknown(tmp_path, caplog):
    fp = tmp_path / "tm.json"
    tm = TradeMemory(filepath=str(fp))
    with caplog.at_level(logging.WARNING, logger="TradeMemory"):
        tm.record({"trade_id": "t1", "result": "WIN"})
    assert tm.trades[-1]["bot_id"] == "unknown"
    assert any("no bot_id" in rec.message for rec in caplog.records)


def test_record_with_explicit_bot_id_is_preserved(tmp_path):
    fp = tmp_path / "tm.json"
    tm = TradeMemory(filepath=str(fp))
    tm.record({"trade_id": "t1"}, bot_id="prod")
    assert tm.trades[-1]["bot_id"] == "prod"
