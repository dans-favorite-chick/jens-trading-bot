# tests/warehouse/test_cli.py
"""Tests for the warehouse CLI.

Adaptations:
- main() routes to cmd_ingest / cmd_status.
- cmd_status returns 1 (not 0) when the DB doesn't exist (real behavior).
- Lock and error log are controlled via monkeypatching ingest.LOCK_PATH.
- No DEFAULT_LOCK attribute on cli module; the lock is in tools.warehouse.ingest.
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

from tools.warehouse.cli import main


def _make_trades_csv(p):
    p.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        "foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,84,30.0,2025\n"
    )


def test_cli_ingest_file(tmp_path):
    db = tmp_path / "phx.duckdb"
    lock = tmp_path / "lock"
    errlog = tmp_path / "err.log"
    csv = tmp_path / "t.csv"
    _make_trades_csv(csv)
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        rc = main(["--db", str(db), "ingest", str(csv)])
    assert rc == 0
    assert db.exists()


def test_cli_status_on_missing_db(tmp_path):
    """status returns 1 when DB doesn't exist (real behavior)."""
    rc = main(["--db", str(tmp_path / "missing.duckdb"), "status"])
    assert rc == 1


def test_cli_dry_run_no_write(tmp_path, capsys):
    csv = tmp_path / "t.csv"
    _make_trades_csv(csv)
    lock = tmp_path / "lock"
    errlog = tmp_path / "err.log"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        rc = main(["--db", str(tmp_path / "x.duckdb"), "ingest", str(csv), "--dry-run"])
    assert rc == 0
    # With dry_run=True the file should not exist (no DB writes)
    assert not (tmp_path / "x.duckdb").exists()
