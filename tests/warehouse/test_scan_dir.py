# tests/warehouse/test_scan_dir.py
"""Tests for scan_dir.

Adaptations:
- scan_dir signature: (dir_path, *, db_path, glob, recursive, ...) — takes db_path
  not a connection object.
- Tests use tmp_path for isolation and monkeypatch LOCK_PATH and ERROR_LOG.
- The real scan_dir doesn't accept an error_log parameter directly; it writes to
  tools.warehouse.ERROR_LOG. We patch that instead.
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import patch

import duckdb

from tools.warehouse.ingest import scan_dir


def _make_trades_csv(p, val=42.0):
    p.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        f"foo,LONG,2025-01-02 14:30:00+00:00,21000.0,{val},84,30.0,2025\n"
    )


def _scan(tmp_path, dir_path, **kwargs):
    db = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    errlog = tmp_path / "err.log"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        return scan_dir(dir_path, db_path=db, **kwargs), db, errlog


def test_default_glob_one_level(tmp_path):
    scan_dir_tmp = tmp_path / "data"
    scan_dir_tmp.mkdir()
    _make_trades_csv(scan_dir_tmp / "a.csv", val=1.0)
    sub = scan_dir_tmp / "sub"
    sub.mkdir()
    _make_trades_csv(sub / "b.csv", val=2.0)
    rs, db, _ = _scan(tmp_path, scan_dir_tmp)
    files = {r.csv_path.name for r in rs}
    assert files == {"a.csv"}, "default scan must not recurse"


def test_recursive_flag(tmp_path):
    scan_dir_tmp = tmp_path / "data"
    scan_dir_tmp.mkdir()
    _make_trades_csv(scan_dir_tmp / "a.csv", val=1.0)
    sub = scan_dir_tmp / "sub"
    sub.mkdir()
    _make_trades_csv(sub / "b.csv", val=2.0)
    rs, db, _ = _scan(tmp_path, scan_dir_tmp, recursive=True)
    files = {r.csv_path.name for r in rs}
    assert files == {"a.csv", "b.csv"}


def test_skip_components(tmp_path):
    scan_dir_tmp = tmp_path / "data"
    scan_dir_tmp.mkdir()
    _make_trades_csv(scan_dir_tmp / "a.csv", val=1.0)
    fix = scan_dir_tmp / "fixtures"
    fix.mkdir()
    _make_trades_csv(fix / "b.csv", val=2.0)
    rs, db, _ = _scan(tmp_path, scan_dir_tmp, recursive=True)
    files = {r.csv_path.name for r in rs}
    assert files == {"a.csv"}, "fixtures path must be skipped"


def test_error_log_written(tmp_path):
    scan_dir_tmp = tmp_path / "data"
    scan_dir_tmp.mkdir()
    bad = scan_dir_tmp / "junk.csv"
    bad.write_text("nope_col\n1\n")          # unknown_csv_kind
    db = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    errlog = tmp_path / "errors.log"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        scan_dir(scan_dir_tmp, db_path=db)
    assert errlog.exists()
    assert "unknown_csv_kind" in errlog.read_text()
