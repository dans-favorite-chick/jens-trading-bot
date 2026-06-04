"""D-SQL — trade_memory_db column-maps initial_stop_price.

Pins the 2026-06-03 follow-up to Finding D: position_manager began
emitting ``initial_stop_price`` on every trade dict, but the SQLite
shadow writer (trade_memory_db.write_trade) silently dropped that
field because it wasn't promoted to a real column. SQL-direct
consumers (analytics tools, future dashboard SQLite path) saw NULL.

This sprint adds initial_stop_price as a REAL column, includes it
in _TRADE_COLUMNS + the CREATE TABLE statement, includes it in
write_trade's cols dict, bumps SCHEMA_VERSION 1 → 2, and ships an
idempotent ALTER migration for already-existing v1 DBs in the wild.

The JSON path (core.trade_memory.TradeMemory.record) is unaffected —
it's a schemaless dict writer and was always carrying the field.
load_all_trades reads JSON-first, so the dashboard is unaffected
too. This test is the SQL-direct contract.

Run: pytest tests/test_trade_memory_db_initial_stop_price.py -v
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.trade_memory_db import SCHEMA_VERSION, TradeMemoryDB


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "trade_memory.db")


def _sample_trade(**overrides) -> dict:
    base = {
        "trade_id": "T-D-0001",
        "bot_id": "sim",
        "strategy": "bias_momentum",
        "sub_strategy": None,
        "direction": "LONG",
        "entry_time": 1778617699.8,
        "exit_time": 1778617780.26,
        "entry_price": 28000.0,
        "exit_price": 28005.0,
        "contracts": 1,
        "stop_price": 27995.0,
        "initial_stop_price": 27990.0,
        "target_price": 28010.0,
        "pnl_dollars": 9.50,
        "pnl_ticks": 20,
        "r_multiple": 0.5,
        "exit_reason": "target_hit",
        "result": "WIN",
        "account": "SimBias Momentum",
        "recorded_at": 1778617780.26,
    }
    base.update(overrides)
    return base


# ─── Assertion 1: schema includes initial_stop_price as REAL ─────────


def test_initial_stop_price_column_present_as_real_on_fresh_db(db_path):
    """A fresh DB MUST expose initial_stop_price as a REAL column on
    the trades table."""
    db = TradeMemoryDB(db_path)
    try:
        cur = db._conn.execute("PRAGMA table_info(trades)")
        cols = {row["name"]: row["type"].upper() for row in cur.fetchall()}
        assert "initial_stop_price" in cols, (
            f"trades table missing initial_stop_price column; got {list(cols)}"
        )
        assert cols["initial_stop_price"] == "REAL", (
            f"initial_stop_price expected REAL type; got {cols['initial_stop_price']!r}"
        )
        # And SCHEMA_VERSION bumped to 2.
        assert SCHEMA_VERSION == 2, (
            f"SCHEMA_VERSION must be 2 for D-SQL fix; got {SCHEMA_VERSION}"
        )
        assert db.user_version() == 2, (
            f"PRAGMA user_version must be stamped 2; got {db.user_version()}"
        )
    finally:
        db.close()


# ─── Assertion 2: write+read round-trip with value ───────────────────


def test_write_trade_with_initial_stop_price_round_trips(db_path):
    """Writing a trade with initial_stop_price=27990.0 and reading it
    back must surface 99.5-style numeric equality on the column."""
    db = TradeMemoryDB(db_path)
    try:
        db.write_trade(_sample_trade(initial_stop_price=27990.25))
        # Read via raw SQL to bypass raw_json reconstruction — we want
        # to prove the column itself holds the value, not just the
        # JSON forensic blob.
        cur = db._conn.execute(
            "SELECT initial_stop_price FROM trades WHERE trade_id = ?",
            ("T-D-0001",),
        )
        row = cur.fetchone()
        assert row is not None, "trade row not written"
        assert row["initial_stop_price"] == 27990.25, (
            f"round-trip value mismatch: got {row['initial_stop_price']!r}"
        )

        # And read_trades surfaces it too (column-fallback path).
        trades = db.read_trades(strategy="bias_momentum")
        assert len(trades) == 1
        # raw_json reconstruction includes initial_stop_price too.
        assert trades[0]["initial_stop_price"] == 27990.25
    finally:
        db.close()


# ─── Assertion 3: write trade WITHOUT field → NULL, no exception ────


def test_write_trade_without_initial_stop_price_yields_null(db_path):
    """Writing a trade dict that omits initial_stop_price MUST NOT
    raise — write_trade uses trade.get() and the column is nullable."""
    db = TradeMemoryDB(db_path)
    try:
        trade = _sample_trade()
        trade.pop("initial_stop_price", None)
        assert "initial_stop_price" not in trade
        # Must not raise.
        db.write_trade(trade)
        cur = db._conn.execute(
            "SELECT initial_stop_price FROM trades WHERE trade_id = ?",
            ("T-D-0001",),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["initial_stop_price"] is None, (
            f"omitted value should land as NULL; got {row['initial_stop_price']!r}"
        )
    finally:
        db.close()


# ─── Assertion 4: idempotent migration from v1 schema ───────────────


def test_migration_from_v1_schema_is_idempotent(tmp_path):
    """An old v1 DB (no initial_stop_price column) must gain the
    column when TradeMemoryDB opens it. Re-opening the same DB must
    be a no-op (no duplicate-column error, no schema drift)."""
    db_file = tmp_path / "v1_legacy.db"

    # Manually craft a v1-shaped trades table — NO initial_stop_price.
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(
            """
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                sub_strategy TEXT,
                direction TEXT NOT NULL,
                entry_time REAL NOT NULL,
                exit_time REAL,
                entry_price REAL,
                exit_price REAL,
                contracts INTEGER,
                stop_price REAL,
                target_price REAL,
                pnl_dollars REAL,
                pnl_ticks INTEGER,
                r_multiple REAL,
                exit_reason TEXT,
                result TEXT,
                account TEXT,
                recorded_at REAL,
                trace_id TEXT,
                market_snapshot_json TEXT,
                raw_json TEXT NOT NULL
            );
            """
        )
        conn.execute("PRAGMA user_version = 1;")
        conn.commit()
    finally:
        conn.close()

    # Sanity: column is genuinely absent before TradeMemoryDB touches it.
    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.execute("PRAGMA table_info(trades)")
        v1_cols = {r[1] for r in cur.fetchall()}
        assert "initial_stop_price" not in v1_cols, (
            "v1 fixture is contaminated — initial_stop_price already "
            "present before migration ran"
        )
    finally:
        conn.close()

    # First open: ALTER fires and adds the column.
    db = TradeMemoryDB(str(db_file))
    try:
        cur = db._conn.execute("PRAGMA table_info(trades)")
        cols_after_first = {r["name"] for r in cur.fetchall()}
        assert "initial_stop_price" in cols_after_first, (
            "migration did NOT add initial_stop_price to existing v1 DB"
        )
        assert db.user_version() == 2

        # Round-trip survives migration: can write + read the field.
        db.write_trade(_sample_trade(initial_stop_price=27990.0))
        cur = db._conn.execute(
            "SELECT initial_stop_price FROM trades WHERE trade_id = ?",
            ("T-D-0001",),
        )
        assert cur.fetchone()["initial_stop_price"] == 27990.0
    finally:
        db.close()

    # Second open on the SAME DB: must NOT raise, must NOT duplicate
    # the column, must NOT lose data.
    db = TradeMemoryDB(str(db_file))
    try:
        cur = db._conn.execute("PRAGMA table_info(trades)")
        cols_after_second = [r["name"] for r in cur.fetchall()]
        assert cols_after_second.count("initial_stop_price") == 1, (
            f"second open duplicated initial_stop_price column: "
            f"{cols_after_second}"
        )
        # Earlier write is still there.
        cur = db._conn.execute(
            "SELECT initial_stop_price FROM trades WHERE trade_id = ?",
            ("T-D-0001",),
        )
        assert cur.fetchone()["initial_stop_price"] == 27990.0
    finally:
        db.close()
