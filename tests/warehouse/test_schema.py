# tests/warehouse/test_schema.py
"""Schema sanity checks: tables exist, view exists, JSON extension loaded."""
from __future__ import annotations


EXPECTED_TABLES = {"runs", "trades", "run_metrics", "wfa_windows", "wfa_summary"}
EXPECTED_VIEWS = {"trades_ct"}


def test_all_tables_exist(db):
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
    ).fetchall()
    actual = {r[0] for r in rows}
    assert EXPECTED_TABLES.issubset(actual), f"missing tables: {EXPECTED_TABLES - actual}"


def test_trades_ct_view_exists(db):
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'VIEW'"
    ).fetchall()
    actual = {r[0] for r in rows}
    assert EXPECTED_VIEWS.issubset(actual)


def test_json_type_works(db):
    # If JSON extension didn't load, casting to JSON raises.
    result = db.execute("SELECT CAST('{\"a\":1}' AS JSON) AS j").fetchone()
    assert result is not None


def test_apply_schema_idempotent(db):
    from tools.warehouse.db import apply_schema
    apply_schema(db)  # second call should not raise
    apply_schema(db)  # third call either
