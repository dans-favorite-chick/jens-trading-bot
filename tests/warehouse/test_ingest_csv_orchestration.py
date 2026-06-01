# tests/warehouse/test_ingest_csv_orchestration.py
"""Tests for the ingest_csv orchestrator.

Adaptations:
- ingest_csv signature: (csv_path, *, db_path, logical_group, mark_friction_applied, ...)
  It takes db_path instead of a connection object. Tests use tmp_path for isolation
  and monkeypatch LOCK_PATH and ERROR_LOG to avoid touching real files.
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import patch

import duckdb

from tools.warehouse.ingest import ingest_csv


def _make_ingest(tmp_path, csv_path, **kwargs):
    """Call ingest_csv with isolated db_path, LOCK_PATH and ERROR_LOG."""
    db = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    errlog = tmp_path / "err.log"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        return ingest_csv(csv_path, db_path=db, **kwargs)


def test_dedup_on_second_call(tmp_path, fixtures_dir):
    db = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    errlog = tmp_path / "err.log"
    csv = fixtures_dir / "kind_trades.csv"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        r1 = ingest_csv(csv, db_path=db)
        r2 = ingest_csv(csv, db_path=db)
    assert r1.status == "inserted"
    assert r2.status == "skipped_duplicate"
    assert r2.run_id == r1.run_id
    con = duckdb.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    con.close()
    assert n == 1


def test_unknown_csv_returns_error_no_run_row(tmp_path, fixtures_dir):
    r = _make_ingest(tmp_path, fixtures_dir / "kind_unknown.csv")
    assert r.status == "error"
    assert r.error == "unknown_csv_kind"
    db = tmp_path / "test.duckdb"
    if db.exists():
        con = duckdb.connect(str(db))
        n = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        con.close()
        assert n == 0


def test_error_inside_transaction_rolls_back(tmp_path):
    # Malformed trades CSV: bad timestamp will trip read_csv_auto inside the tx.
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        "foo,LONG,not-a-timestamp,21000.0,42.0,84,30.0,2025\n"
    )
    db = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    errlog = tmp_path / "err.log"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        r = ingest_csv(csv, db_path=db)
    assert r.status == "error"
    if db.exists():
        con = duckdb.connect(str(db))
        runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        trades = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        con.close()
        assert runs == 0
        assert trades == 0


def test_logical_group_persisted(tmp_path, fixtures_dir):
    db = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    errlog = tmp_path / "err.log"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        r = ingest_csv(
            fixtures_dir / "kind_wfa_windows.csv",
            db_path=db,
            logical_group="phase13_wfa",
        )
    assert r.status == "inserted"
    con = duckdb.connect(str(db))
    lg = con.execute("SELECT logical_group FROM runs").fetchone()[0]
    con.close()
    assert lg == "phase13_wfa"
