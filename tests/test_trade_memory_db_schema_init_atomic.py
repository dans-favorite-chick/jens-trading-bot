"""M1 — trade_memory_db._init_schema runs inside an atomic transaction.

Subagent B 2026-06-03 flagged the PRAGMA + INSERT schema_meta sequence
as executing outside an explicit transaction. The fix wraps the whole
init block in ``with self._conn:`` so any mid-init failure rolls back
the schema_meta INSERT and the user_version PRAGMA, leaving the file
in a state that a fresh TradeMemoryDB instance can recover cleanly.

The CREATE TABLE statements are issued via ``executescript`` which
commits any pending transaction before executing — those table
creations are unconditional (IF NOT EXISTS) and idempotent across
concurrent inits, so they stay outside the rollback window by design.

Run: pytest tests/test_trade_memory_db_schema_init_atomic.py -v
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.trade_memory_db import SCHEMA_VERSION, TradeMemoryDB


# ─── helpers ─────────────────────────────────────────────────────────


def _read_schema_meta(db_file: str) -> list[tuple]:
    """Read schema_meta with a raw (non-TradeMemoryDB) connection so we
    don't re-trigger init while inspecting state."""
    raw = sqlite3.connect(db_file)
    try:
        try:
            return raw.execute("SELECT key, value FROM schema_meta").fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        raw.close()


class _FaultyCursor(sqlite3.Cursor):
    """Cursor that raises on the schema_meta INSERT, simulating a
    failure AFTER the PRAGMA but BEFORE the INSERT completes.

    executescript and other SQL pass through unchanged so the rollback
    boundary we're stressing is exactly the PRAGMA + INSERT block. The
    CREATE TABLE statements committed during executescript are expected
    to survive — only the writes inside the explicit ``with self._conn:``
    block roll back.
    """

    def execute(self, sql, *args, **kwargs):
        if "INSERT OR REPLACE INTO schema_meta" in sql:
            raise RuntimeError("simulated mid-init failure")
        return super().execute(sql, *args, **kwargs)


class _FaultyAfterPragmaConn(sqlite3.Connection):
    """Connection factory that returns _FaultyCursor by default.

    ``Connection.execute`` (used by the ALTER TABLE migration) is a C
    method that does not route through this Python override, so the
    ALTER passes through unaffected — only ``conn.cursor()`` -> cur.execute
    calls in ``_init_schema`` see the fault injection.
    """

    def cursor(self, factory=None):  # type: ignore[override]
        if factory is None:
            factory = _FaultyCursor
        return super().cursor(factory)


# ─── Assertion 1: fresh init writes schema_meta exactly once ─────────


def test_fresh_init_writes_schema_meta_row_exactly_once(tmp_path: Path):
    db_path = str(tmp_path / "fresh.db")
    db = TradeMemoryDB(db_path)
    try:
        # The transaction must have committed cleanly.
        rows = db._conn.execute(
            "SELECT key, value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchall()
        assert len(rows) == 1, (
            f"expected exactly one schema_version row, got {len(rows)}: {list(rows)}"
        )
        # And the value is SCHEMA_VERSION (stringified, per the column).
        key, value = rows[0]
        assert key == "schema_version"
        assert value == str(SCHEMA_VERSION), (
            f"schema_meta value mismatch — expected {SCHEMA_VERSION!s}, got {value!r}"
        )
        # PRAGMA user_version stamped to match.
        assert db.user_version() == SCHEMA_VERSION
    finally:
        db.close()


# ─── Assertion 2: failure mid-init rolls back the schema_meta INSERT ─


def test_failed_init_rolls_back_schema_meta_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = str(tmp_path / "rollback.db")

    real_connect = sqlite3.connect

    def faulty_connect(*args, **kwargs):
        # Inject our faulty Connection subclass — it lets the executescript
        # and PRAGMA pass through but raises on the schema_meta INSERT.
        kwargs["factory"] = _FaultyAfterPragmaConn
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("core.trade_memory_db.sqlite3.connect", faulty_connect)

    with pytest.raises(RuntimeError, match="simulated mid-init failure"):
        TradeMemoryDB(db_path)

    # Undo the patch so our verification runs against unmodified sqlite3.
    monkeypatch.undo()

    # The schema_meta TABLE exists (it was created via executescript, which
    # auto-commits before the transaction we wrap) but the ROW must NOT
    # have been written — the failure inside the ``with self._conn:`` block
    # must have rolled back the INSERT.
    rows = _read_schema_meta(db_path)
    assert rows == [], (
        f"expected no schema_meta rows after failed init (transaction "
        f"should have rolled back), got {rows}"
    )


# ─── Assertion 3: a second TradeMemoryDB on the same file recovers ──


def test_clean_init_after_partial_failure_on_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = str(tmp_path / "recover.db")

    real_connect = sqlite3.connect

    def faulty_connect(*args, **kwargs):
        kwargs["factory"] = _FaultyAfterPragmaConn
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("core.trade_memory_db.sqlite3.connect", faulty_connect)

    with pytest.raises(RuntimeError):
        TradeMemoryDB(db_path)

    # Confirm the partial state is genuinely partial (no schema_meta row).
    monkeypatch.undo()
    assert _read_schema_meta(db_path) == []

    # Fresh TradeMemoryDB on the same file should now complete init
    # cleanly — schema_meta row present, user_version stamped.
    db = TradeMemoryDB(db_path)
    try:
        rows = db._conn.execute(
            "SELECT key, value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchall()
        assert len(rows) == 1, (
            f"recovery init didn't write the row exactly once: {list(rows)}"
        )
        assert rows[0][1] == str(SCHEMA_VERSION)
        assert db.user_version() == SCHEMA_VERSION
    finally:
        db.close()
