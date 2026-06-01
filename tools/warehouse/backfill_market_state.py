"""Historical backfill of market_state_bars (Phase 8, 2026-06-01).

Walks every 5m bar in the historical MNQ feed, computes the three
classifier signals (realized_vol, trend_strength, choppiness_index) and
the composite label using ONLY data up to and including each bar (no
look-ahead), and UPSERTs the result into the warehouse table
`market_state_bars`.

Idempotent: re-running the script overwrites existing rows with the
latest computed values (which matches the deterministic classifier --
same inputs => same outputs, so re-running is also a free integrity
check).

Source of bars
--------------
The warehouse currently has no `bar_events` table; the canonical 5m
bar history lives at `data/historical/mnq_5min_databento.csv` (verified
2026-06-01: 354,270 bars, ts_utc + open/high/low/close). The CSV path
is overridable via --bars for ad-hoc replay against a different file.

Usage
-----
    python tools/warehouse/backfill_market_state.py
    python tools/warehouse/backfill_market_state.py --bars path\\to\\bars.csv
    python tools/warehouse/backfill_market_state.py --db data\\warehouse\\phoenix.duckdb

Output: prints rows_written / rows_skipped / total_rows on stdout (ASCII
only -- Windows cp1252 console safe).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Iterable

import duckdb

# Make the project importable when this script is invoked directly.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.market_state import MarketState  # noqa: E402
from tools.warehouse.db import apply_schema  # noqa: E402


logger = logging.getLogger(__name__)

DEFAULT_DB = _PROJECT_ROOT / "data" / "warehouse" / "phoenix.duckdb"
DEFAULT_BARS = _PROJECT_ROOT / "data" / "historical" / "mnq_5min_databento.csv"

_BATCH_SIZE = 10_000


def _open_db(db_path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path))
    apply_schema(con)
    return con


def _load_bars(con: duckdb.DuckDBPyConnection, bars_csv: Path) -> list[tuple]:
    """Return ALL (ts_utc, high, low, close) rows sorted by ts_utc asc.

    Reads the CSV in one shot (the file is ~50 MB; comfortably in RAM).
    Returning a materialised list avoids cursor invalidation when the
    same connection issues UPSERTs interleaved with iteration.
    """
    sql = """
        SELECT
            ts_utc::TIMESTAMP WITH TIME ZONE AS bar_ts,
            high::DOUBLE   AS high,
            low::DOUBLE    AS low,
            close::DOUBLE  AS close
        FROM read_csv_auto(?, header=TRUE)
        ORDER BY ts_utc ASC
    """
    return con.execute(sql, [str(bars_csv)]).fetchall()


def _existing_bar_ts(con: duckdb.DuckDBPyConnection) -> set:
    """Set of bar_ts values already in market_state_bars (idempotency check)."""
    rows = con.execute("SELECT bar_ts FROM market_state_bars").fetchall()
    return {r[0] for r in rows}


def _upsert_rows(con: duckdb.DuckDBPyConnection,
                 rows: list[tuple]) -> tuple[int, int]:
    """UPSERT a batch of (bar_ts, label, rv, ts_strength, chop) rows.

    Returns (n_inserted_or_updated, n_skipped). With ON CONFLICT DO
    UPDATE the row count semantics are: every supplied row is
    written, so n_skipped is always 0 here.
    """
    if not rows:
        return (0, 0)
    con.executemany(
        """
        INSERT INTO market_state_bars (
            bar_ts, label, realized_vol, trend_strength, choppiness_index
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (bar_ts) DO UPDATE SET
            label            = EXCLUDED.label,
            realized_vol     = EXCLUDED.realized_vol,
            trend_strength   = EXCLUDED.trend_strength,
            choppiness_index = EXCLUDED.choppiness_index
        """,
        rows,
    )
    return (len(rows), 0)


def backfill(db_path: Path, bars_csv: Path) -> dict:
    """Run the backfill. Returns a summary dict.

    Summary keys:
        rows_written  -- bars whose label was inserted or updated this run
        rows_skipped  -- bars already present with identical key (never
                         increments under ON CONFLICT semantics, but
                         retained for legacy callers / tests)
        total_rows    -- final count of market_state_bars
        elapsed_s     -- wall-clock seconds
    """
    if not bars_csv.exists():
        raise FileNotFoundError(f"bars CSV not found: {bars_csv}")

    t0 = time.time()
    con = _open_db(db_path)
    try:
        existing = _existing_bar_ts(con)
        ms = MarketState(tick_aggregator=None)

        batch: list[tuple] = []
        rows_written = 0
        rows_skipped = 0
        bars_seen = 0

        for bar_ts, high, low, close in _load_bars(con, bars_csv):
            bars_seen += 1
            snap = ms.on_synthetic_bar(close=close, high=high, low=low,
                                       bar_ts=bar_ts)
            # The classifier needs warm-up; the first ~2 bars produce
            # realized_vol == 0 and label == "NEUTRAL". We still write
            # them so the table is one-row-per-bar (joins won't drop
            # early trades).
            batch.append((
                bar_ts,
                snap["label"],
                snap["realized_vol"],
                snap["trend_strength"],
                snap["choppiness_index"],
            ))
            if bar_ts in existing:
                # Row exists -- the upsert will refresh it. Counted
                # separately for the operator's situational awareness.
                rows_skipped += 1
            if len(batch) >= _BATCH_SIZE:
                w, _ = _upsert_rows(con, batch)
                rows_written += w
                batch.clear()
                logger.info("market_state backfill progress: %d bars",
                            bars_seen)

        if batch:
            w, _ = _upsert_rows(con, batch)
            rows_written += w
            batch.clear()

        total = con.execute("SELECT COUNT(*) FROM market_state_bars").fetchone()[0]
        elapsed = time.time() - t0
        return {
            "rows_written": rows_written,
            "rows_skipped": rows_skipped,
            "total_rows": int(total),
            "elapsed_s": elapsed,
            "bars_seen": bars_seen,
        }
    finally:
        con.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill market_state_bars from historical 5m feed.",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB,
                   help="Path to phoenix.duckdb")
    p.add_argument("--bars", type=Path, default=DEFAULT_BARS,
                   help="Path to historical 5m bar CSV")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress info-level progress logs")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = backfill(args.db, args.bars)
    # ASCII only -- Windows cp1252 console safe.
    print(
        "market_state backfill complete: "
        f"rows_written={summary['rows_written']} "
        f"rows_already_present={summary['rows_skipped']} "
        f"total_rows={summary['total_rows']} "
        f"bars_seen={summary['bars_seen']} "
        f"elapsed_s={summary['elapsed_s']:.1f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
