"""B16 — trade_memory.record() stamps bot_id so dashboard can partition P&L."""
import json
import os
import tempfile

from core.trade_memory import TradeMemory


def test_record_stamps_bot_id_kwarg():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "tm.json")
        tm = TradeMemory(filepath=path)
        tm.record({"result": "WIN", "pnl_dollars": 10.0}, bot_id="prod")
        tm2 = TradeMemory(filepath=path)
        assert len(tm2.trades) == 1
        assert tm2.trades[0]["bot_id"] == "prod"


def test_record_preserves_existing_bot_id_when_no_kwarg():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "tm.json")
        tm = TradeMemory(filepath=path)
        tm.record({"result": "LOSS", "pnl_dollars": -5.0, "bot_id": "lab"})
        with open(path) as f:
            data = json.load(f)
        assert data[0]["bot_id"] == "lab"


def test_record_bot_id_defaults_to_unknown_when_absent():
    # B70: write-time guard — missing bot_id is coerced to "unknown" (not None)
    # to prevent null-bot_id pollution of trade_memory.json.
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "tm.json")
        tm = TradeMemory(filepath=path)
        tm.record({"result": "WIN", "pnl_dollars": 1.0})
        assert tm.trades[0]["bot_id"] == "unknown"
