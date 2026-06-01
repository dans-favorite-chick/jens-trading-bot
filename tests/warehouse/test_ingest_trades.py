# tests/warehouse/test_ingest_trades.py
"""Tests for trades-kind ingest.

Adaptations:
- _ingest_trades signature: (con, csv_path, run_id, header) — positional, not keyword.
- _insert_runs_row signature: (con, csv_path, run_id, kind, sidecar, *, logical_group,
  mark_friction_applied).
- friction_applied() in sidecar.py replaces _resolve_friction.
"""
from __future__ import annotations
from tools.warehouse.ingest import _insert_runs_row, _ingest_trades
from tools.warehouse.sidecar import load_and_hash, friction_applied
from tools.warehouse.sniff import sniff_csv_kind


def _ingest(db, csv_path):
    sc = load_and_hash(csv_path)
    kind, header = sniff_csv_kind(csv_path)
    assert kind == "trades"
    _insert_runs_row(
        db, csv_path, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    return _ingest_trades(db, csv_path, sc.run_id, header)


def test_legacy_trades_ingest(db, fixtures_dir):
    n = _ingest(db, fixtures_dir / "kind_trades.csv")
    assert n == 1
    row = db.execute(
        "SELECT strategy, direction, entry_price, pnl_dollars, mae_ticks "
        "FROM trades"
    ).fetchone()
    assert row[0] == "foo"
    assert row[1] == "LONG"
    assert row[2] == 21000.0
    assert row[3] == 42.0
    assert row[4] is None        # mae_ticks absent in legacy schema


def test_extended_trades_ingest(db, fixtures_dir):
    n = _ingest(db, fixtures_dir / "kind_trades_extended.csv")
    assert n == 1
    row = db.execute(
        "SELECT mae_ticks, regime, tod_bucket FROM trades"
    ).fetchone()
    assert row == (12.0, "LOW_VOL_TREND", "Opening Drive")


def test_direction_normalized_uppercase(db, tmp_path):
    csv = tmp_path / "lower.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_min,year\n"
        "foo,long,2025-01-02 14:30:00+00:00,21000.0,42.0,84,30.0,2025\n"
    )
    _ingest(db, csv)
    row = db.execute("SELECT direction FROM trades").fetchone()
    assert row == ("LONG",)


def test_hold_min_falls_back_to_hold_minutes(db, tmp_path):
    csv = tmp_path / "newcol.csv"
    csv.write_text(
        "strategy,direction,entry_ts,entry_price,pnl_dollars,pnl_ticks,hold_minutes,year\n"
        "foo,LONG,2025-01-02 14:30:00+00:00,21000.0,42.0,84,45.0,2025\n"
    )
    _ingest(db, csv)
    row = db.execute("SELECT hold_minutes FROM trades").fetchone()
    assert row == (45.0,)
