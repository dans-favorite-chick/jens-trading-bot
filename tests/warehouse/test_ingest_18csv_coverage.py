# tests/warehouse/test_ingest_18csv_coverage.py
"""Tests for the 18-CSV coverage gap fix.

Tasks A-D: expanded DERIVED_PATTERNS, strategy-from-filename fallback for trade-shape
CSVs missing 'strategy' column, and empty-file handling for _dom_pullback_5y_verdict.csv.

Each test follows the established pattern: use the `db` fixture from conftest.py and
call internal helpers directly (same pattern as test_ingest_derived.py etc.).
"""
from __future__ import annotations
import json
import pytest

from tools.warehouse.ingest import (
    _insert_runs_row,
    _ingest_derived,
    _ingest_trades,
    strategy_from_filename,
)
from tools.warehouse.sidecar import load_and_hash
from tools.warehouse.sniff import sniff_csv_kind, safe_import_table_name


# ──────────────────────────────────────────────────────────────
# Shared helper
# ──────────────────────────────────────────────────────────────

def _do_ingest_derived(db, csv_path):
    sc = load_and_hash(csv_path)
    kind, _ = sniff_csv_kind(csv_path)
    assert kind == "derived", f"expected 'derived' for {csv_path.name}, got {kind!r}"
    _insert_runs_row(
        db, csv_path, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    n = _ingest_derived(db, csv_path, sc.run_id)
    return sc.run_id, n


def _do_ingest_trades(db, csv_path):
    sc = load_and_hash(csv_path)
    kind, header = sniff_csv_kind(csv_path)
    assert kind == "trades", f"expected 'trades' for {csv_path.name}, got {kind!r}"
    _insert_runs_row(
        db, csv_path, sc.run_id, kind, sc.sidecar,
        logical_group=None,
        mark_friction_applied=None,
    )
    n = _ingest_trades(db, csv_path, sc.run_id, header)
    return sc.run_id, n


# ──────────────────────────────────────────────────────────────
# Task A: sniff_kind returns "derived" for new filename patterns
# ──────────────────────────────────────────────────────────────

class TestSniffDerivedPatterns:
    def test_phoenix_compounding_summary_is_derived(self, fixtures_dir):
        kind, _ = sniff_csv_kind(fixtures_dir / "phoenix_compounding_summary_sample.csv")
        assert kind == "derived"

    def test_phoenix_es_nq_attribution_is_derived(self, fixtures_dir):
        kind, _ = sniff_csv_kind(fixtures_dir / "phoenix_es_nq_attribution_sample.csv")
        assert kind == "derived"

    def test_backtest_v3_sweep_is_derived(self, fixtures_dir):
        kind, _ = sniff_csv_kind(fixtures_dir / "backtest_v3_sweep_sample.csv")
        assert kind == "derived"

    def test_dom_verdict_empty_is_derived(self, fixtures_dir):
        kind, _ = sniff_csv_kind(fixtures_dir / "_dom_pullback_verdict_sample.csv")
        assert kind == "derived"

    def test_backtest_v3_trades_still_trades(self, fixtures_dir):
        """backtest_v3_trades_* has entry_ts+entry_price+pnl_dollars — must stay 'trades'."""
        kind, _ = sniff_csv_kind(
            fixtures_dir / "backtest_v3_trades_LONG_sample.csv"
        )
        assert kind == "trades"

    def test_opening_session_sub_is_trades_not_derived(self, fixtures_dir):
        """opening_session_sub_breakdown has full trade shape — must stay 'trades'."""
        kind, _ = sniff_csv_kind(
            fixtures_dir / "opening_session_sub_breakdown_sample.csv"
        )
        assert kind == "trades"


# ──────────────────────────────────────────────────────────────
# Task A: derived ingest creates import_* table with expected cols + run_id
# ──────────────────────────────────────────────────────────────

class TestDerivedIngest:
    def test_phoenix_compounding_summary_ingests(self, db, fixtures_dir):
        run_id, n = _do_ingest_derived(
            db, fixtures_dir / "phoenix_compounding_summary_sample.csv"
        )
        table = safe_import_table_name(
            fixtures_dir / "phoenix_compounding_summary_sample.csv"
        )
        assert n == 1
        cols = {
            r[0].lower()
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{table}'"
            ).fetchall()
        }
        assert "policy" in cols
        assert "final_equity" in cols
        assert "run_id" in cols

    def test_phoenix_es_nq_attribution_ingests(self, db, fixtures_dir):
        run_id, n = _do_ingest_derived(
            db, fixtures_dir / "phoenix_es_nq_attribution_sample.csv"
        )
        table = safe_import_table_name(
            fixtures_dir / "phoenix_es_nq_attribution_sample.csv"
        )
        assert n == 1
        cols = {
            r[0].lower()
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{table}'"
            ).fetchall()
        }
        assert "strategy" in cols
        assert "pnl_dollars" in cols
        assert "alignment" in cols
        assert "run_id" in cols

    def test_backtest_v3_sweep_ingests(self, db, fixtures_dir):
        run_id, n = _do_ingest_derived(
            db, fixtures_dir / "backtest_v3_sweep_sample.csv"
        )
        table = safe_import_table_name(
            fixtures_dir / "backtest_v3_sweep_sample.csv"
        )
        assert n == 1
        cols = {
            r[0].lower()
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{table}'"
            ).fetchall()
        }
        assert "direction" in cols
        assert "total_pnl" in cols
        assert "run_id" in cols


# ──────────────────────────────────────────────────────────────
# Task C: empty-file handling
# ──────────────────────────────────────────────────────────────

class TestEmptyFileHandling:
    def test_empty_dom_verdict_returns_zero_rows(self, db, fixtures_dir):
        run_id, n = _do_ingest_derived(
            db, fixtures_dir / "_dom_pullback_verdict_sample.csv"
        )
        assert n == 0

    def test_empty_dom_verdict_sets_empty_file_meta(self, db, fixtures_dir):
        run_id, _ = _do_ingest_derived(
            db, fixtures_dir / "_dom_pullback_verdict_sample.csv"
        )
        row = db.execute(
            "SELECT sidecar_raw FROM runs WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row is not None
        raw = json.loads(row[0])
        assert raw.get("meta", {}).get("empty_file") is True

    def test_empty_dom_verdict_no_import_table_created(self, db, fixtures_dir):
        _do_ingest_derived(db, fixtures_dir / "_dom_pullback_verdict_sample.csv")
        tables = {
            r[0]
            for r in db.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name LIKE 'import_%'"
            ).fetchall()
        }
        # No import_ table should be created for an empty file
        assert not any("dom" in t for t in tables)

    def test_empty_file_run_kind_is_derived(self, db, fixtures_dir):
        run_id, _ = _do_ingest_derived(
            db, fixtures_dir / "_dom_pullback_verdict_sample.csv"
        )
        row = db.execute(
            "SELECT csv_kind FROM runs WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row is not None
        assert row[0] == "derived"


# ──────────────────────────────────────────────────────────────
# Task B: strategy_from_filename helper
# ──────────────────────────────────────────────────────────────

class TestStrategyFromFilename:
    def test_opening_session_sub_breakdown(self, tmp_path):
        p = tmp_path / "opening_session_sub_breakdown_v2.csv"
        p.touch()
        assert strategy_from_filename(p) == "opening_session"

    def test_backtest_v3_trades(self, tmp_path):
        p = tmp_path / "backtest_v3_trades_LONG_b7.0.csv"
        p.touch()
        assert strategy_from_filename(p) == "backtest_v3"

    def test_phoenix_sr_confluence_per_trade(self, tmp_path):
        p = tmp_path / "phoenix_sr_confluence_per_trade.csv"
        p.touch()
        assert strategy_from_filename(p) == "sr_confluence"

    def test_unknown_filename_returns_none(self, tmp_path):
        p = tmp_path / "some_unknown_trades.csv"
        p.touch()
        assert strategy_from_filename(p) is None


# ──────────────────────────────────────────────────────────────
# Task B: missing-strategy trade ingest — rows land with correct strategy
# ──────────────────────────────────────────────────────────────

class TestMissingStrategyTradesIngest:
    def test_opening_session_sub_breakdown_strategy(self, db, fixtures_dir):
        run_id, n = _do_ingest_trades(
            db,
            fixtures_dir / "opening_session_sub_breakdown_sample.csv",
        )
        assert n == 1
        row = db.execute(
            "SELECT strategy FROM trades WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row[0] == "opening_session"

    def test_opening_session_sub_direction_normalized(self, db, fixtures_dir):
        run_id, _ = _do_ingest_trades(
            db,
            fixtures_dir / "opening_session_sub_breakdown_sample.csv",
        )
        row = db.execute(
            "SELECT direction FROM trades WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row[0] == "SHORT"

    def test_opening_session_sub_name_meta_recorded(self, db, fixtures_dir):
        """sub_name_column_present should be recorded in runs.sidecar_raw.meta."""
        csv_path = fixtures_dir / "opening_session_sub_breakdown_sample.csv"
        sc = load_and_hash(csv_path)
        kind, header = sniff_csv_kind(csv_path)
        _insert_runs_row(
            db, csv_path, sc.run_id, kind, sc.sidecar,
            logical_group=None,
            mark_friction_applied=None,
        )
        meta_updates = {"sub_name_column_present": True} if (
            "sub_name" in set(header) and "strategy" not in set(header)
        ) else None
        _ingest_trades(db, csv_path, sc.run_id, header, meta_updates=meta_updates)
        row = db.execute(
            "SELECT sidecar_raw FROM runs WHERE run_id = ?", [sc.run_id]
        ).fetchone()
        raw = json.loads(row[0])
        assert raw.get("meta", {}).get("sub_name_column_present") is True

    def test_backtest_v3_trades_strategy(self, db, fixtures_dir):
        run_id, n = _do_ingest_trades(
            db,
            fixtures_dir / "backtest_v3_trades_LONG_sample.csv",
        )
        assert n == 1
        row = db.execute(
            "SELECT strategy FROM trades WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row[0] == "backtest_v3"

    def test_sr_confluence_per_trade_strategy(self, db, fixtures_dir):
        run_id, n = _do_ingest_trades(
            db,
            fixtures_dir / "phoenix_sr_confluence_per_trade_sample.csv",
        )
        assert n == 1
        row = db.execute(
            "SELECT strategy FROM trades WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row[0] == "sr_confluence"

    def test_sr_confluence_pnl_present(self, db, fixtures_dir):
        run_id, _ = _do_ingest_trades(
            db,
            fixtures_dir / "phoenix_sr_confluence_per_trade_sample.csv",
        )
        row = db.execute(
            "SELECT pnl_dollars FROM trades WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row[0] == 52.5
