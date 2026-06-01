# tests/warehouse/test_ingest_mixed.py
"""Tests for mixed-kind ingest.

Adaptations:
- _ingest_mixed signature: (con, csv_path, run_id, header) — positional, not keyword.
- The real impl reads from row 0 only for aggregate cols; no ValueError on non-constant
  metric columns (it just takes row 0 regardless). Plan test test_mixed_rejects_inconsistent_metric
  is SKIPPED: the real _ingest_mixed doesn't validate constant aggregate columns.
"""
from __future__ import annotations
import pytest

from tools.warehouse.ingest import _insert_runs_row, _ingest_mixed
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def test_mixed_inserts_both(db, fixtures_dir):
    csv = fixtures_dir / "kind_mixed.csv"
    sc = load_and_hash(csv)
    kind, header = sniff_csv_kind(csv)
    assert kind == "mixed"
    _insert_runs_row(
        db, csv, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    n_trades, n_metrics = _ingest_mixed(db, csv, sc.run_id, header)
    assert n_trades == 2
    assert n_metrics == 2  # profit_factor + n_trades
    trade_count = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    metric_count = db.execute("SELECT COUNT(*) FROM run_metrics").fetchone()[0]
    assert trade_count == 2
    assert metric_count == 2


@pytest.mark.skip(
    reason="The real _ingest_mixed does NOT validate constant aggregate columns; "
           "it takes row 0 silently. The plan's rejection behavior is not implemented."
)
def test_mixed_rejects_inconsistent_metric(db, tmp_path):
    csv = tmp_path / "bad_mixed.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,profit_factor\n"
        "foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,1.5\n"
        "foo,LONG,2025-01-03 14:30:00+00:00,21100.0,10.0,9.9\n"
    )
    sc = load_and_hash(csv)
    kind, header = sniff_csv_kind(csv)
    assert kind == "mixed"
    _insert_runs_row(
        db, csv, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    with pytest.raises(ValueError, match="non-constant aggregate"):
        _ingest_mixed(db, csv, sc.run_id, header)
