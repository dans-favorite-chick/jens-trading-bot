"""SQLite-backed trade memory (P4-4, 2026-05-25).

Dual-write companion to ``logs/trade_memory_<bot>.json``. Every trade
written to JSON via :py:meth:`core.trade_memory.TradeMemory.record` is
ALSO written to ``data/trade_memory.db`` (best-effort — a SQLite hiccup
must NEVER block the canonical JSON write). Reads still flow through
:py:func:`core.trade_memory.load_all_trades` (JSON-first); a future
flip will switch to SQLite-first with JSON fallback once 30 days of
dual-write data match within a documented tolerance (~1% delta on
trade count + sum(pnl_dollars) per strategy).

Why SQLite — the 2026-05-13 12-file reader audit was symptomatic of
"multiple readers of the same on-disk JSON truth silently drifting."
ACID + a single connection per process eliminates that whole bug
class. See ``docs/audits/SYNTHESIS_2026-05-24.md`` §4 P4-4 and
``memory/MEMORY.md`` -> trade_memory_canonical_reader.md.

Schema (PRAGMA user_version = 1):
    trades (
        trade_id TEXT PRIMARY KEY,
        bot_id TEXT NOT NULL,
        strategy TEXT NOT NULL,
        sub_strategy TEXT,
        direction TEXT NOT NULL,
        entry_time REAL NOT NULL,   -- epoch seconds
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
        trace_id TEXT,                  -- P4-2 lifecycle correlation
        market_snapshot_json TEXT,      -- entire snapshot dict
        raw_json TEXT NOT NULL          -- full record for forensic recovery
    );
    strategy_halts (
        strategy TEXT NOT NULL,
        sub_strategy TEXT,
        halted_at REAL NOT NULL,
        reason TEXT,
        cleared_at REAL,
        PRIMARY KEY (strategy, sub_strategy, halted_at)
    );
    equity_state (
        ts REAL PRIMARY KEY,
        equity REAL NOT NULL,
        ath REAL,
        consecutive_losses INTEGER,
        raw_json TEXT
    );
    schema_meta (key TEXT PRIMARY KEY, value TEXT);

Indexes:
    idx_trades_strategy_time on (strategy, entry_time DESC)
    idx_trades_bot          on (bot_id, entry_time DESC)

This module is stdlib-only (sqlite3, json, os, logging, pathlib).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("TradeMemoryDB")

# Project-rooted default db path (resolves to <phoenix_bot>/data/trade_memory.db
# when this module sits at <phoenix_bot>/core/trade_memory_db.py).
PHOENIX_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PHOENIX_ROOT / "data" / "trade_memory.db"

SCHEMA_VERSION = 1

# Columns that map 1:1 from a trade dict to the trades table. Anything
# not in this list lands in ``raw_json`` only.
_TRADE_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "bot_id",
    "strategy",
    "sub_strategy",
    "direction",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "contracts",
    "stop_price",
    "target_price",
    "pnl_dollars",
    "pnl_ticks",
    "r_multiple",
    "exit_reason",
    "result",
    "account",
    "recorded_at",
    "trace_id",
)


def _to_epoch(v: Any) -> Optional[float]:
    """Coerce a value to epoch seconds.

    Accepts: float/int (already epoch), ISO 8601 strings (datetime
    fromisoformat-compatible), or None. Anything unparseable returns
    None — caller decides how to handle.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(v).timestamp()
        except (TypeError, ValueError):
            return None
    return None


class TradeMemoryDB:
    """SQLite layer for trades, halts, and equity state.

    Single connection per process. Cheap to construct — the schema is
    idempotent (``CREATE TABLE IF NOT EXISTS``) so wiring this into a
    long-lived process or a one-shot CLI both Just Work.

    Thread-safety: ``check_same_thread=False`` is enabled because the
    bot stack may close trades on async callback threads. All writes
    are wrapped in short-lived transactions; concurrent writes serialize
    on SQLite's database-level lock.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None):
        path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._conn: sqlite3.Connection = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        # WAL keeps readers (dashboard, tools) unblocked during writes.
        try:
            self._conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.DatabaseError:
            # Some filesystems (network shares) refuse WAL; default
            # journaling still gives us ACID, just with reader/writer
            # exclusion. Not fatal.
            pass
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._init_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
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

            CREATE INDEX IF NOT EXISTS idx_trades_strategy_time
                ON trades (strategy, entry_time DESC);

            CREATE INDEX IF NOT EXISTS idx_trades_bot
                ON trades (bot_id, entry_time DESC);

            CREATE TABLE IF NOT EXISTS strategy_halts (
                strategy TEXT NOT NULL,
                sub_strategy TEXT,
                halted_at REAL NOT NULL,
                reason TEXT,
                cleared_at REAL,
                PRIMARY KEY (strategy, sub_strategy, halted_at)
            );

            CREATE TABLE IF NOT EXISTS equity_state (
                ts REAL PRIMARY KEY,
                equity REAL NOT NULL,
                ath REAL,
                consecutive_losses INTEGER,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # PRAGMA user_version isn't a parameterizable statement.
        cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
        cur.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?);",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # trades
    # ------------------------------------------------------------------

    def write_trade(self, trade: dict) -> None:
        """INSERT OR REPLACE a trade by ``trade_id``. Idempotent.

        Missing ``trade_id`` raises ValueError — every trade in the
        canonical JSON files has one (and the per-bot writer enforces
        it). We refuse silent rows because forensic recovery from
        ``raw_json`` relies on the PK being meaningful.

        ``bot_id``, ``strategy``, ``direction``, and ``entry_time``
        are also required (NOT NULL columns). The defaults below mirror
        what ``TradeMemory.record()`` would have stamped if the caller
        was sloppy — keep dual-write surviving where the JSON file
        also survived.
        """
        trade_id = trade.get("trade_id")
        if not trade_id:
            raise ValueError("write_trade: trade dict missing 'trade_id'")

        # NOT NULL columns with safe coercion.
        bot_id = trade.get("bot_id") or "unknown"
        strategy = trade.get("strategy") or "unknown"
        direction = trade.get("direction") or "UNKNOWN"
        entry_time = _to_epoch(trade.get("entry_time"))
        if entry_time is None:
            # Final fallback so an arrival-time stamp at least lets the
            # row land in chronological order rather than being dropped.
            entry_time = _to_epoch(trade.get("recorded_at")) or 0.0

        market_snapshot = trade.get("market_snapshot")
        market_snapshot_json = (
            json.dumps(market_snapshot, default=str)
            if market_snapshot is not None
            else None
        )

        cols: dict[str, Any] = {
            "trade_id": trade_id,
            "bot_id": bot_id,
            "strategy": strategy,
            "sub_strategy": trade.get("sub_strategy"),
            "direction": direction,
            "entry_time": entry_time,
            "exit_time": _to_epoch(trade.get("exit_time")),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "contracts": trade.get("contracts"),
            "stop_price": trade.get("stop_price"),
            "target_price": trade.get("target_price"),
            "pnl_dollars": trade.get("pnl_dollars"),
            "pnl_ticks": trade.get("pnl_ticks"),
            "r_multiple": trade.get("r_multiple"),
            "exit_reason": trade.get("exit_reason"),
            "result": trade.get("result"),
            "account": trade.get("account"),
            "recorded_at": _to_epoch(trade.get("recorded_at")),
            "trace_id": trade.get("trace_id"),
            "market_snapshot_json": market_snapshot_json,
            "raw_json": json.dumps(trade, default=str),
        }
        keys = list(cols.keys())
        placeholders = ",".join("?" for _ in keys)
        col_list = ",".join(keys)
        sql = (
            f"INSERT OR REPLACE INTO trades ({col_list}) "
            f"VALUES ({placeholders})"
        )
        with self._conn:
            self._conn.execute(sql, [cols[k] for k in keys])

    def read_trades(
        self,
        strategy: Optional[str] = None,
        bot_id: Optional[str] = None,
        since: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Read trades with optional filters.

        Returns dicts equivalent to the original ``raw_json`` payload
        (so callers see the full schema, including any extra fields
        not promoted to columns). When ``raw_json`` is absent or
        unparseable the row is reconstructed from columns + the parsed
        ``market_snapshot_json``.
        """
        where: list[str] = []
        params: list[Any] = []
        if strategy is not None:
            where.append("strategy = ?")
            params.append(strategy)
        if bot_id is not None:
            where.append("bot_id = ?")
            params.append(bot_id)
        if since is not None:
            where.append("entry_time >= ?")
            params.append(float(since))
        sql = "SELECT * FROM trades"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY entry_time ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = self._conn.execute(sql, params)
        out: list[dict] = []
        for row in cur.fetchall():
            d = dict(row)
            raw = d.pop("raw_json", None)
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        out.append(parsed)
                        continue
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.warning(
                        "read_trades: raw_json unparseable for trade_id=%s",
                        d.get("trade_id"),
                    )
            # Fallback: reconstruct from columns.
            snap = d.pop("market_snapshot_json", None)
            if snap:
                try:
                    d["market_snapshot"] = json.loads(snap)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # strategy halts
    # ------------------------------------------------------------------

    def write_strategy_halt(
        self,
        strategy: str,
        sub_strategy: Optional[str],
        halted_at: float,
        reason: Optional[str],
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO strategy_halts "
                "(strategy, sub_strategy, halted_at, reason, cleared_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (strategy, sub_strategy, float(halted_at), reason),
            )

    def update_strategy_halt_cleared(
        self,
        strategy: str,
        sub_strategy: Optional[str],
        halted_at: float,
        cleared_at: float,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE strategy_halts SET cleared_at = ? "
                "WHERE strategy = ? "
                "AND ((? IS NULL AND sub_strategy IS NULL) OR sub_strategy = ?) "
                "AND halted_at = ?",
                (
                    float(cleared_at),
                    strategy,
                    sub_strategy,
                    sub_strategy,
                    float(halted_at),
                ),
            )

    def read_strategy_halts(
        self,
        strategy: Optional[str] = None,
        active_only: bool = False,
    ) -> list[dict]:
        where: list[str] = []
        params: list[Any] = []
        if strategy is not None:
            where.append("strategy = ?")
            params.append(strategy)
        if active_only:
            where.append("cleared_at IS NULL")
        sql = "SELECT * FROM strategy_halts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY halted_at DESC"
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # equity state
    # ------------------------------------------------------------------

    def write_equity_state(
        self,
        ts: float,
        equity: float,
        ath: Optional[float],
        consecutive_losses: Optional[int],
        raw_json: Optional[str] = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_state "
                "(ts, equity, ath, consecutive_losses, raw_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    float(ts),
                    float(equity),
                    None if ath is None else float(ath),
                    None if consecutive_losses is None else int(consecutive_losses),
                    raw_json,
                ),
            )

    def read_equity_state(
        self, since: Optional[float] = None, limit: Optional[int] = None,
    ) -> list[dict]:
        sql = "SELECT * FROM equity_state"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(float(since))
        sql += " ORDER BY ts ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # bulk + lifecycle
    # ------------------------------------------------------------------

    def write_trades_bulk(self, trades: Iterable[dict]) -> int:
        """Backfill helper. Returns count of rows attempted (skipped
        rows are logged but don't abort the run)."""
        n = 0
        for t in trades:
            try:
                self.write_trade(t)
                n += 1
            except Exception as e:
                logger.warning(
                    "write_trades_bulk skip trade_id=%s: %s",
                    t.get("trade_id"), e,
                )
        return n

    def trade_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM trades")
        return int(cur.fetchone()[0])

    def user_version(self) -> int:
        cur = self._conn.execute("PRAGMA user_version;")
        return int(cur.fetchone()[0])

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as e:
            logger.warning("TradeMemoryDB close failed: %s", e)

    def __enter__(self) -> "TradeMemoryDB":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
