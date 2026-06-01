# tests/warehouse/test_trades_ct.py
"""Verify trades_ct view yields correct CT-derived columns.

The view computes session_date, market_open_minutes, entry_ts_ct, exit_ts_ct on
read by converting `entry_ts` (UTC TIMESTAMPTZ) into America/Chicago. Globex /
pre-market trades produce NEGATIVE market_open_minutes by design.
"""
from __future__ import annotations
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

from tools.warehouse.ingest import ingest_csv


def _trade_csv(tmp_path, entry_ts):
    p = tmp_path / "trade.csv"
    p.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        f"foo,LONG,{entry_ts},21000.0,42.0,84,30.0,2025\n"
    )
    return p


def _load_single_trade(tmp_path, entry_ts):
    db_path = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    csv = _trade_csv(tmp_path, entry_ts)
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", tmp_path / "err.log"):
        r = ingest_csv(csv, db_path=db_path)
    assert r.status == "inserted", f"ingest failed: {r.error}"
    return duckdb.connect(str(db_path))


def test_session_open_zero(tmp_path):
    # 08:30 CST = 14:30 UTC on a winter day (CT = UTC-6 in winter when CST applies).
    con = _load_single_trade(tmp_path, "2025-01-02 14:30:00+00:00")
    row = con.execute(
        "SELECT session_date, market_open_minutes FROM trades_ct"
    ).fetchone()
    con.close()
    assert row[0] == date(2025, 1, 2)
    assert abs(row[1] - 0.0) < 1e-6, f"expected ~0, got {row[1]}"


def test_session_open_plus_30(tmp_path):
    con = _load_single_trade(tmp_path, "2025-01-02 15:00:00+00:00")   # 09:00 CT
    val = con.execute("SELECT market_open_minutes FROM trades_ct").fetchone()[0]
    con.close()
    assert abs(val - 30.0) < 1e-6


def test_globex_negative_minutes(tmp_path):
    # 06:00 CT = 12:00 UTC (winter). market_open_minutes should be ~-150.
    con = _load_single_trade(tmp_path, "2025-01-02 12:00:00+00:00")
    val = con.execute("SELECT market_open_minutes FROM trades_ct").fetchone()[0]
    con.close()
    assert val < 0
    assert abs(val - (-150.0)) < 1e-6


def test_session_date_uses_ct_calendar(tmp_path):
    # 23:30 CT on 2025-01-02 = 05:30 UTC on 2025-01-03. session_date must be 2025-01-02.
    con = _load_single_trade(tmp_path, "2025-01-03 05:30:00+00:00")
    sd = con.execute("SELECT session_date FROM trades_ct").fetchone()[0]
    con.close()
    assert sd == date(2025, 1, 2)
