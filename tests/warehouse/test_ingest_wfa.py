# tests/warehouse/test_ingest_wfa.py
"""Tests for wfa_windows and wfa_summary ingest.

Adaptations:
- _ingest_wfa_windows signature: (con, csv_path, run_id) — positional, not keyword.
- _ingest_wfa_summary signature: (con, csv_path, run_id) — positional, not keyword.
- _insert_runs_row used instead of insert_run.
"""
from __future__ import annotations
from tools.warehouse.ingest import _insert_runs_row, _ingest_wfa_windows, _ingest_wfa_summary
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind


def _setup(db, csv_path, kind_expected):
    sc = load_and_hash(csv_path)
    kind, _ = sniff_csv_kind(csv_path)
    assert kind == kind_expected
    _insert_runs_row(
        db, csv_path, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    return sc


def test_wfa_windows_happy_path(db, fixtures_dir):
    sc = _setup(db, fixtures_dir / "kind_wfa_windows.csv", "wfa_windows")
    n = _ingest_wfa_windows(db, fixtures_dir / "kind_wfa_windows.csv", sc.run_id)
    assert n == 1
    row = db.execute(
        "SELECT strategy, window_idx, best_params->>'$.ema_len' FROM wfa_windows"
    ).fetchone()
    assert row[0] == "foo"
    assert row[1] == 0
    assert row[2] == "21"      # JSON path returns text


def test_wfa_windows_handles_python_repr(db, tmp_path):
    csv = tmp_path / "wfa_repr.csv"
    csv.write_text(
        "strategy,window_idx,is_start,is_end,oos_start,oos_end,best_params,"
        "is_pf,is_trades,oos_pf,oos_trades,oos_net,wfe,degraded\n"
        "foo,0,2021-01-01,2021-12-31,2022-01-01,2022-03-31,"
        "\"{'ema_len': 21}\",1.5,100,1.2,30,500.0,0.8,false\n"
    )
    sc = _setup(db, csv, "wfa_windows")
    n = _ingest_wfa_windows(db, csv, sc.run_id)
    assert n == 1
    bp = db.execute("SELECT best_params->>'$._raw' FROM wfa_windows").fetchone()
    assert bp[0] is not None       # raw payload was preserved


def test_wfa_summary(db, fixtures_dir):
    sc = _setup(db, fixtures_dir / "kind_wfa_summary.csv", "wfa_summary")
    n = _ingest_wfa_summary(db, fixtures_dir / "kind_wfa_summary.csv", sc.run_id)
    assert n == 1
    row = db.execute(
        "SELECT strategy, mean_oos_pf, robust FROM wfa_summary"
    ).fetchone()
    assert row == ("foo", 1.3, True)
