"""P4-4 (2026-05-25): SQLite trade memory dual-write tests.

Covers:
- schema created + user_version stamped on first open
- round-trip write_trade -> read_trades preserves dict (incl. market_snapshot)
- INSERT OR REPLACE idempotency by trade_id
- filters: strategy, bot_id, since
- strategy_halts CRUD (insert + clear)
- equity_state CRUD
- SQLite write failure inside TradeMemory.record() does NOT raise
  from the JSON path (canonical writer is unaffected by SQLite hiccups)
- migrate_trade_memory_to_sqlite CLI backfill is idempotent
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from core.trade_memory import TradeMemory
from core.trade_memory_db import TradeMemoryDB, SCHEMA_VERSION


# ---------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "trade_memory.db")


@pytest.fixture
def db(db_path: str) -> TradeMemoryDB:
    inst = TradeMemoryDB(db_path)
    yield inst
    inst.close()


def _sample_trade(**overrides) -> dict:
    base = {
        "trade_id": "T-0001",
        "bot_id": "sim",
        "strategy": "dom_pullback",
        "sub_strategy": None,
        "direction": "LONG",
        "entry_time": 1778617699.8,
        "exit_time": 1778617780.26,
        "entry_price": 29120.75,
        "exit_price": 29118.0,
        "contracts": 1,
        "stop_price": 29119.38,
        "target_price": 29720.75,
        "pnl_dollars": -10.32,
        "pnl_ticks": -11,
        "r_multiple": -0.72,
        "exit_reason": "stop_loss",
        "result": "LOSS",
        "account": "SimDom",
        "recorded_at": "2026-05-25T09:30:00",
        "trace_id": "trace-abc",
        "market_snapshot": {
            "price": 29120.5,
            "bid": 29120.0,
            "ask": 29120.75,
            "vwap": 29077.4,
            "regime": "VOL_HIGH",
        },
        # extra fields not promoted to columns survive via raw_json
        "commission_dollars": 1.72,
        "hold_time_s": 80.5,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------


def test_schema_created_on_first_open(db_path: str):
    db = TradeMemoryDB(db_path)
    try:
        assert os.path.exists(db_path)
        assert db.user_version() == SCHEMA_VERSION
        # Tables present.
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = [r[0] for r in cur.fetchall()]
        for tbl in ("equity_state", "schema_meta", "strategy_halts", "trades"):
            assert tbl in names, f"missing table {tbl}: {names}"
        # Indexes present.
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_%' ORDER BY name"
        )
        idx_names = [r[0] for r in cur.fetchall()]
        assert "idx_trades_strategy_time" in idx_names
        assert "idx_trades_bot" in idx_names
    finally:
        db.close()


def test_schema_idempotent_on_reopen(db_path: str):
    TradeMemoryDB(db_path).close()
    db = TradeMemoryDB(db_path)
    try:
        assert db.user_version() == SCHEMA_VERSION
    finally:
        db.close()


# ---------------------------------------------------------------------
# trades round-trip
# ---------------------------------------------------------------------


def test_round_trip_preserves_dict(db: TradeMemoryDB):
    t = _sample_trade()
    db.write_trade(t)
    rows = db.read_trades()
    assert len(rows) == 1
    got = rows[0]
    assert got["trade_id"] == t["trade_id"]
    assert got["bot_id"] == t["bot_id"]
    assert got["strategy"] == t["strategy"]
    assert got["direction"] == t["direction"]
    assert got["pnl_dollars"] == t["pnl_dollars"]
    # nested snapshot survives
    assert got["market_snapshot"]["regime"] == "VOL_HIGH"
    # extra (non-column) field survives via raw_json
    assert got["commission_dollars"] == 1.72


def test_insert_or_replace_idempotent(db: TradeMemoryDB):
    t = _sample_trade(pnl_dollars=-10.0)
    db.write_trade(t)
    db.write_trade(t)
    db.write_trade(t)
    assert db.trade_count() == 1
    # update on same trade_id replaces
    t2 = _sample_trade(pnl_dollars=99.99)
    db.write_trade(t2)
    assert db.trade_count() == 1
    rows = db.read_trades()
    assert rows[0]["pnl_dollars"] == 99.99


def test_missing_trade_id_raises(db: TradeMemoryDB):
    with pytest.raises(ValueError):
        db.write_trade({"strategy": "x", "direction": "LONG"})


def test_filter_by_strategy(db: TradeMemoryDB):
    db.write_trade(_sample_trade(trade_id="A", strategy="dom_pullback"))
    db.write_trade(_sample_trade(trade_id="B", strategy="vwap_pullback_v2"))
    db.write_trade(_sample_trade(trade_id="C", strategy="dom_pullback"))
    rows = db.read_trades(strategy="dom_pullback")
    assert {r["trade_id"] for r in rows} == {"A", "C"}


def test_filter_by_bot_id(db: TradeMemoryDB):
    db.write_trade(_sample_trade(trade_id="A", bot_id="prod"))
    db.write_trade(_sample_trade(trade_id="B", bot_id="sim"))
    db.write_trade(_sample_trade(trade_id="C", bot_id="prod"))
    rows = db.read_trades(bot_id="prod")
    assert {r["trade_id"] for r in rows} == {"A", "C"}


def test_filter_by_since(db: TradeMemoryDB):
    db.write_trade(_sample_trade(trade_id="OLD", entry_time=1000.0))
    db.write_trade(_sample_trade(trade_id="MID", entry_time=2000.0))
    db.write_trade(_sample_trade(trade_id="NEW", entry_time=3000.0))
    rows = db.read_trades(since=1500.0)
    assert {r["trade_id"] for r in rows} == {"MID", "NEW"}


def test_iso_entry_time_coerced_to_epoch(db: TradeMemoryDB):
    # The canonical JSON sometimes stores entry_time as ISO string
    # (e.g. when the legacy writer used datetime.isoformat()).
    iso = "2026-05-25T09:30:00"
    db.write_trade(_sample_trade(trade_id="ISO", entry_time=iso))
    cur = db._conn.execute("SELECT entry_time FROM trades WHERE trade_id='ISO'")
    et = cur.fetchone()[0]
    assert isinstance(et, float)
    assert et > 1_700_000_000  # sometime after 2023


# ---------------------------------------------------------------------
# strategy halts
# ---------------------------------------------------------------------


def test_strategy_halt_crud(db: TradeMemoryDB):
    db.write_strategy_halt("dom_pullback", None, halted_at=100.0, reason="DD")
    db.write_strategy_halt("vwap_pullback_v2", "agg", 110.0, "PF<0.7")
    halts = db.read_strategy_halts(active_only=True)
    assert len(halts) == 2
    # clear one
    db.update_strategy_halt_cleared("dom_pullback", None, 100.0, 200.0)
    active = db.read_strategy_halts(active_only=True)
    assert len(active) == 1
    assert active[0]["strategy"] == "vwap_pullback_v2"
    all_halts = db.read_strategy_halts()
    assert len(all_halts) == 2


# ---------------------------------------------------------------------
# equity state
# ---------------------------------------------------------------------


def test_equity_state_crud(db: TradeMemoryDB):
    db.write_equity_state(ts=1.0, equity=300.0, ath=350.0, consecutive_losses=0)
    db.write_equity_state(ts=2.0, equity=295.0, ath=350.0, consecutive_losses=1)
    rows = db.read_equity_state()
    assert len(rows) == 2
    assert rows[0]["equity"] == 300.0
    assert rows[1]["consecutive_losses"] == 1
    # PK on ts -> replace works
    db.write_equity_state(ts=2.0, equity=305.0, ath=350.0, consecutive_losses=0)
    rows = db.read_equity_state(since=2.0)
    assert len(rows) == 1
    assert rows[0]["equity"] == 305.0


# ---------------------------------------------------------------------
# dual-write resilience: SQLite failure MUST NOT raise from JSON path
# ---------------------------------------------------------------------


def test_sqlite_failure_does_not_break_json_record(
    tmp_path: Path, monkeypatch, caplog
):
    """If the SQLite write inside TradeMemory.record() raises, the JSON
    write still happens and the call returns normally."""
    json_path = tmp_path / "tm.json"
    tm = TradeMemory(filepath=str(json_path))

    # Force the SQLite layer to explode on instantiation. This mirrors
    # "disk full / lock contention / DB file corrupted" cases.
    import core.trade_memory_db as tmdb_mod

    def _boom(*a, **kw):
        raise RuntimeError("simulated SQLite failure")

    monkeypatch.setattr(tmdb_mod, "TradeMemoryDB", _boom)

    caplog.set_level(logging.WARNING, logger="TradeMemory")
    # No raise here is the assertion.
    tm.record(
        {
            "trade_id": "T-failsafe",
            "strategy": "dom_pullback",
            "direction": "LONG",
            "entry_time": 1.0,
            "result": "WIN",
            "pnl_dollars": 5.0,
        },
        bot_id="sim",
    )

    # JSON file written successfully.
    with open(json_path) as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["trade_id"] == "T-failsafe"
    assert data[0]["bot_id"] == "sim"

    # Warning logged.
    assert any(
        "SQLite dual-write failed" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_dual_write_lands_in_sqlite_too(tmp_path: Path, monkeypatch):
    """Happy path: JSON record() flows into SQLite when the default db
    path is redirected into tmp_path. Smoke test for the hook itself."""
    json_path = tmp_path / "tm.json"
    db_path = tmp_path / "data" / "trade_memory.db"

    import core.trade_memory_db as tmdb_mod
    monkeypatch.setattr(tmdb_mod, "DEFAULT_DB_PATH", db_path)

    tm = TradeMemory(filepath=str(json_path))
    trade = {
        "trade_id": "T-happy",
        "strategy": "dom_pullback",
        "direction": "LONG",
        "entry_time": 1234567.0,
        "result": "WIN",
        "pnl_dollars": 5.0,
        "market_snapshot": {"regime": "TREND_UP"},
    }
    tm.record(trade, bot_id="prod")

    assert db_path.exists()
    db = TradeMemoryDB(str(db_path))
    try:
        rows = db.read_trades()
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "T-happy"
    assert rows[0]["market_snapshot"]["regime"] == "TREND_UP"


# ---------------------------------------------------------------------
# migration CLI is idempotent
# ---------------------------------------------------------------------


def test_migrate_cli_idempotent(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    db_path = tmp_path / "data" / "trade_memory.db"

    # Seed two per-bot JSON files (canonical reader merges them).
    with open(logs / "trade_memory_prod.json", "w") as f:
        json.dump([
            _sample_trade(trade_id="P1", bot_id="prod"),
            _sample_trade(trade_id="P2", bot_id="prod"),
        ], f)
    with open(logs / "trade_memory_sim.json", "w") as f:
        json.dump([
            _sample_trade(trade_id="S1", bot_id="sim"),
        ], f)

    from tools.migrate_trade_memory_to_sqlite import main as migrate

    rc = migrate(["--logs-dir", str(logs), "--db", str(db_path)])
    assert rc == 0

    db = TradeMemoryDB(str(db_path))
    try:
        assert db.trade_count() == 3
    finally:
        db.close()

    # Re-run: count must stay at 3 (INSERT OR REPLACE).
    rc = migrate(["--logs-dir", str(logs), "--db", str(db_path)])
    assert rc == 0
    db = TradeMemoryDB(str(db_path))
    try:
        assert db.trade_count() == 3
    finally:
        db.close()
