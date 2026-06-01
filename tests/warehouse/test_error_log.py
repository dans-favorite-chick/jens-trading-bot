# tests/warehouse/test_error_log.py
"""Tests for the JSONL error log.

Adaptations:
- log_error_jsonl doesn't exist in the implementation. The real _log_error() is private
  and writes to tools.warehouse.ERROR_LOG directly.
- We test the error log behavior via ingest_csv with an unknown CSV, observing that
  the ERROR_LOG file gets a JSONL record.
- We also test the IngestResult dataclass to verify error fields are populated correctly.
"""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch

from tools.warehouse.ingest import IngestResult, ingest_csv


def test_ingest_result_error_fields():
    """IngestResult captures error fields correctly."""
    r = IngestResult(
        csv_path=Path("foo.csv"),
        run_id="abc",
        status="error",
        csv_kind="trades",
        rows_inserted=0,
        metrics_inserted=0,
        error="unknown_csv_kind",
    )
    assert r.error == "unknown_csv_kind"
    assert r.status == "error"
    assert r.csv_kind == "trades"


def test_error_log_written_on_unknown_kind(tmp_path, fixtures_dir):
    """An unknown CSV kind causes a JSONL record in ERROR_LOG."""
    db = tmp_path / "test.duckdb"
    lock = tmp_path / ".lock"
    errlog = tmp_path / "err.log"
    with patch("tools.warehouse.ingest.LOCK_PATH", lock), \
         patch("tools.warehouse.lock.LOCK_PATH", lock), \
         patch("tools.warehouse.ingest.ERROR_LOG", errlog):
        r = ingest_csv(fixtures_dir / "kind_unknown.csv", db_path=db)
    assert r.status == "error"
    assert errlog.exists()
    lines = errlog.read_text().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["error_class"] == "unknown_csv_kind"
    assert "file" in record
    assert "ts" in record
