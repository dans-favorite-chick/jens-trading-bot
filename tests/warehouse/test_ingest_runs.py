# tests/warehouse/test_ingest_runs.py
"""Tests for runs row insertion and friction resolution.

Adaptations from plan:
- insert_run doesn't exist; the real function is _insert_runs_row with a different
  signature: (con, csv_path, run_id, kind, sidecar, *, logical_group, mark_friction_applied)
- _resolve_friction doesn't exist; the real function is friction_applied() in sidecar.py
  with signature: friction_applied(sidecar_data, *, cli_override=None)
- load_and_hash returns SidecarResult where .sidecar is the sidecar dict with all fields
  at top level (meta is also at top level as 'meta' key).
"""
from __future__ import annotations
import json
from tools.warehouse.ingest import _insert_runs_row
from tools.warehouse.sidecar import load_and_hash, friction_applied


def test_insert_run_writes_row(db, fixtures_dir):
    sc = load_and_hash(fixtures_dir / "sidecar_full.csv")
    fa = friction_applied(sc.sidecar, cli_override=None)
    _insert_runs_row(
        db,
        fixtures_dir / "sidecar_full.csv",
        sc.run_id,
        "trades",
        sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    row = db.execute("SELECT source_filename, strategy, friction_applied FROM runs").fetchone()
    assert row == ("sidecar_full.csv", "foo", True)


def test_friction_resolution_no_sidecar(tmp_path):
    csv = tmp_path / "no_sidecar.csv"
    csv.write_text("a\n1\n")
    sc = load_and_hash(csv)
    assert friction_applied(sc.sidecar, cli_override=None) is False


def test_friction_resolution_sidecar_explicit_true(fixtures_dir):
    sc = load_and_hash(fixtures_dir / "sidecar_full.csv")
    assert friction_applied(sc.sidecar, cli_override=None) is True


def test_friction_resolution_cli_override_true(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n")
    sc = load_and_hash(csv)
    assert friction_applied(sc.sidecar, cli_override=True) is True


def test_friction_resolution_sidecar_without_friction_field(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n")
    # Use correct sidecar naming: x.run.json
    sc_path = tmp_path / "x.run.json"
    sc_path.write_text(json.dumps({"schema_version": 1, "seed": 1}))
    sc = load_and_hash(csv)
    assert friction_applied(sc.sidecar, cli_override=None) is False
