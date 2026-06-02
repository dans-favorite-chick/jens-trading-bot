"""Phase D.5 (2026-06-02 overnight) tests for the
/api/market_state/per_strategy dashboard endpoint.

NOTE: The companion change to core/trade_memory.py (auto-stamping
entry_market_state on TradeMemory.record()) is DEFERRED to Monday
operator review per .claude/PROTECTED_FILES.md (trade_memory.py is
on the protected list and the overnight run rules say to skip
protected-file edits and document them).

Until that wiring lands, every new trade has
entry_market_state=None (absent), which this endpoint buckets as
"(unknown)". Tests below exercise the endpoint's aggregation logic
either way (records WITH the field, records WITHOUT, mixed).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─── dashboard /api/market_state/per_strategy aggregation ─────────

def test_dashboard_per_state_aggregation(tmp_path, monkeypatch):
    """The endpoint groups trailing-30-day trades by (strategy,
    entry_market_state) and computes n/WR/PF."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    legacy = tmp_path / "logs" / "trade_memory.json"
    legacy.write_text(json.dumps([
        # strategy=A, state=TRENDING_NORMAL: 2 wins, 1 loss
        {"trade_id": "a1", "strategy": "A", "entry_market_state": "TRENDING_NORMAL",
         "pnl_dollars": 10.0, "recorded_at": now_iso},
        {"trade_id": "a2", "strategy": "A", "entry_market_state": "TRENDING_NORMAL",
         "pnl_dollars": 5.0,  "recorded_at": now_iso},
        {"trade_id": "a3", "strategy": "A", "entry_market_state": "TRENDING_NORMAL",
         "pnl_dollars": -3.0, "recorded_at": now_iso},
        # strategy=A, state=CHOPPY: 1 loss
        {"trade_id": "a4", "strategy": "A", "entry_market_state": "CHOPPY",
         "pnl_dollars": -8.0, "recorded_at": now_iso},
        # strategy=B, state=(unknown) -- pre-D.5 record (no entry_market_state)
        {"trade_id": "b1", "strategy": "B", "pnl_dollars": 4.0,
         "recorded_at": now_iso},
    ]))

    def mock_load_all_trades():
        return json.loads(legacy.read_text())

    from dashboard import server as srv
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with patch("core.trade_memory.load_all_trades", mock_load_all_trades):
            r = c.get("/api/market_state/per_strategy")
    assert r.status_code == 200
    d = r.get_json()
    rows = {(x["strategy"], x["entry_market_state"]): x for x in d["rows"]}
    # A x TRENDING_NORMAL: 3 trades, 2 wins, WR=0.667, gross_w=15, gross_l=3, PF=5.0
    a_tn = rows[("A", "TRENDING_NORMAL")]
    assert a_tn["n_trades"] == 3
    assert a_tn["win_rate"] == 0.667
    assert a_tn["gross_win"] == 15.0
    assert a_tn["gross_loss"] == 3.0
    assert a_tn["profit_factor"] == 5.0
    # A x CHOPPY: 1 trade, 0 wins, WR=0. gross_w=0 / gross_l=8 = PF 0.0
    a_c = rows[("A", "CHOPPY")]
    assert a_c["n_trades"] == 1
    assert a_c["win_rate"] == 0.0
    assert a_c["profit_factor"] == 0.0
    # B x (unknown) bucket exists for pre-D.5 records
    assert ("B", "(unknown)") in rows
    assert rows[("B", "(unknown)")]["n_trades"] == 1


def test_dashboard_per_state_excludes_older_than_30d(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    old_iso = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(days=45)).isoformat()
    new_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    legacy = tmp_path / "logs" / "trade_memory.json"
    legacy.write_text(json.dumps([
        {"trade_id": "old", "strategy": "X",
         "entry_market_state": "TRENDING_NORMAL",
         "pnl_dollars": 10.0, "recorded_at": old_iso},
        {"trade_id": "new", "strategy": "X",
         "entry_market_state": "TRENDING_NORMAL",
         "pnl_dollars": 5.0,  "recorded_at": new_iso},
    ]))

    def mock_load_all_trades():
        return json.loads(legacy.read_text())

    from dashboard import server as srv
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with patch("core.trade_memory.load_all_trades", mock_load_all_trades):
            r = c.get("/api/market_state/per_strategy")
    d = r.get_json()
    # Only the newer trade should be counted.
    assert len(d["rows"]) == 1
    assert d["rows"][0]["n_trades"] == 1


def test_dashboard_per_state_handles_all_unknown_pre_phase_d5(tmp_path, monkeypatch):
    """Until the trade_memory wiring lands, every new trade has
    entry_market_state absent, which buckets to '(unknown)'.
    The endpoint must still return useful counts for monitoring."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    legacy = tmp_path / "logs" / "trade_memory.json"
    legacy.write_text(json.dumps([
        {"trade_id": str(i), "strategy": "bias_momentum",
         "pnl_dollars": 1.0 if i % 2 == 0 else -1.0,
         "recorded_at": now_iso}
        for i in range(10)
    ]))

    def mock_load_all_trades():
        return json.loads(legacy.read_text())

    from dashboard import server as srv
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with patch("core.trade_memory.load_all_trades", mock_load_all_trades):
            r = c.get("/api/market_state/per_strategy")
    d = r.get_json()
    rows = {(x["strategy"], x["entry_market_state"]): x for x in d["rows"]}
    assert ("bias_momentum", "(unknown)") in rows
    assert rows[("bias_momentum", "(unknown)")]["n_trades"] == 10
    assert rows[("bias_momentum", "(unknown)")]["win_rate"] == 0.5
