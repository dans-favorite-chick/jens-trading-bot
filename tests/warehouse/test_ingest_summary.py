# tests/warehouse/test_ingest_summary.py
"""Tests for summary-kind ingest (unpivot to run_metrics).

Adaptations:
- _ingest_summary signature: (con, csv_path, run_id, header) — positional, not keyword.
- metric_name is namespaced as "<strategy>.<col>" to avoid PK collisions.
"""
from __future__ import annotations
from tools.warehouse.ingest import _insert_runs_row, _ingest_summary
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def test_summary_unpivot(db, fixtures_dir):
    csv = fixtures_dir / "kind_summary.csv"
    sc = load_and_hash(csv)
    kind, header = sniff_csv_kind(csv)
    assert kind == "summary"
    _insert_runs_row(
        db, csv, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    n = _ingest_summary(db, csv, sc.run_id, header)
    assert n == 4    # profit_factor, sharpe, win_rate, max_dd
    rows = db.execute(
        "SELECT metric_name, metric_value FROM run_metrics ORDER BY metric_name"
    ).fetchall()
    names = {r[0] for r in rows}
    # metric_name is namespaced as "foo.<col>"
    assert names == {"foo.profit_factor", "foo.sharpe", "foo.win_rate", "foo.max_dd"}
