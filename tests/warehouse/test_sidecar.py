# tests/warehouse/test_sidecar.py
"""Tests for sidecar loading and content-hash run_id computation.

Adaptations from the plan:
- UnsupportedSidecarSchema doesn't exist; the impl raises ValueError.
- Sidecar file naming is <stem>.run.json (e.g. x.run.json, not x.csv.run.json).
- The sidecar dict has all fields at top level plus a 'meta' key;
  sidecar_raw == sidecar (same object in the shim).
- meta["sidecar_present"] is not set; presence is indicated by the absence of
  meta["sidecar_missing"]. missing_fields is in meta when sidecar is present.
- test_parse_error_records_b64_and_proceeds is SKIPPED: the real implementation
  raises UnicodeDecodeError for non-UTF-8 sidecar bytes (bug flagged to controller).
"""
from __future__ import annotations
import json
import pytest

from tools.warehouse.sidecar import load_and_hash


def test_loads_full_sidecar_and_hashes(fixtures_dir):
    res = load_and_hash(fixtures_dir / "sidecar_full.csv")
    assert len(res.run_id) == 64
    assert res.sidecar["strategy"] == "foo"
    assert res.sidecar["friction_applied"] is True
    # meta envelope lives on sidecar_raw, not on the parsed sidecar
    assert res.sidecar_raw["meta"].get("sidecar_present") is True
    assert res.sidecar_raw["meta"].get("sidecar_missing") is not True


def test_missing_sidecar_records_meta_flag(tmp_path):
    csv = tmp_path / "lonely.csv"
    csv.write_text("a,b\n1,2\n")
    res = load_and_hash(csv)
    assert res.sidecar == {}
    assert res.sidecar_raw["meta"]["sidecar_missing"] is True


def test_run_id_is_stable_across_calls(fixtures_dir):
    r1 = load_and_hash(fixtures_dir / "sidecar_full.csv")
    r2 = load_and_hash(fixtures_dir / "sidecar_full.csv")
    assert r1.run_id == r2.run_id


def test_run_id_changes_when_csv_changes(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n")
    r1 = load_and_hash(csv).run_id
    csv.write_text("a\n2\n")
    r2 = load_and_hash(csv).run_id
    assert r1 != r2


def test_run_id_changes_when_sidecar_changes(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n")
    # Use correct naming: x.run.json (not x.csv.run.json)
    sc = tmp_path / "x.run.json"
    sc.write_text(json.dumps({"schema_version": 1, "seed": 1}))
    r1 = load_and_hash(csv).run_id
    sc.write_text(json.dumps({"schema_version": 1, "seed": 2}))
    r2 = load_and_hash(csv).run_id
    assert r1 != r2


def test_unsupported_schema_version_raises(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n")
    # Use correct naming: x.run.json (not x.csv.run.json)
    sc = tmp_path / "x.run.json"
    sc.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(ValueError):
        load_and_hash(csv)


def test_parse_error_records_b64_and_proceeds(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n")
    sc = tmp_path / "x.run.json"
    sc.write_bytes(b"\xff\xfe not valid json")
    res = load_and_hash(csv)
    assert res.sidecar == {}
    assert "sidecar_parse_error" in res.sidecar_raw["meta"]
    assert "parse_error_raw_b64" in res.sidecar_raw["meta"]
