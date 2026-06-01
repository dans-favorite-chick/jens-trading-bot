# tests/warehouse/test_ingest_derived.py
"""Tests for derived-kind ingest.

Adaptations:
- _ingest_derived signature: (con, csv_path, run_id) -> int  (returns row count only).
  The plan expected a (table_name, rows) tuple; adapt by querying table name separately.
- safe_import_table_name is called separately to verify the table name.
- All other behavior (lazy CREATE, additive drift, destructive drift rejection) is tested.
"""
from __future__ import annotations
import pytest

from tools.warehouse.ingest import _insert_runs_row, _ingest_derived
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind, safe_import_table_name


def _ingest(db, csv_path):
    sc = load_and_hash(csv_path)
    kind, _ = sniff_csv_kind(csv_path)
    assert kind == "derived"
    _insert_runs_row(
        db, csv_path, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    n = _ingest_derived(db, csv_path, sc.run_id)
    return safe_import_table_name(csv_path), n


def test_derived_creates_table_and_inserts(db, fixtures_dir):
    table, n = _ingest(db, fixtures_dir / "phase1_strategy_summary_sample.csv")
    assert table == "import_phase1_strategy_summary_sample"
    assert n == 1
    row = db.execute(f"SELECT strat_key, net_pnl, run_id FROM {table}").fetchone()
    assert row[0] == "foo" and float(row[1]) == 5000.0 and row[2] is not None


def test_derived_additive_drift_alter(db, tmp_path):
    # Use strat_key (not strategy/name) so the CSV doesn't match the summary rule.
    csv1 = tmp_path / "phase1_drift.csv"
    csv1.write_text("strat_key,n\nfoo,100\n")
    _ingest(db, csv1)
    # Second ingest with an additional column
    tmp2 = tmp_path / "round2"
    tmp2.mkdir()
    csv2 = tmp2 / "phase1_drift.csv"
    csv2.write_text("strat_key,n,extra\nbar,200,x\n")
    _ingest(db, csv2)
    cols = {r[0].lower() for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='import_phase1_drift'"
    ).fetchall()}
    assert "extra" in cols


def test_derived_destructive_drift_rejected(db, tmp_path):
    # Use strat_key (not strategy/name) so the CSV doesn't match the summary rule.
    csv1 = tmp_path / "phase1_destr.csv"
    csv1.write_text("strat_key,n,gone\nfoo,100,x\n")
    _ingest(db, csv1)
    tmp2 = tmp_path / "round2"
    tmp2.mkdir()
    csv2 = tmp2 / "phase1_destr.csv"
    csv2.write_text("strat_key,n\nbar,200\n")
    with pytest.raises(ValueError, match="column.s. removed"):
        _ingest(db, csv2)
