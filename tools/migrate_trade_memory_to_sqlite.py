"""One-shot backfill: JSON trade memory -> SQLite (P4-4, 2026-05-25).

Reads every ``logs/trade_memory*.json`` (the legacy shared file +
all per-bot files) via the canonical reader
``core.trade_memory.load_all_trades`` and writes them to
``data/trade_memory.db`` using ``INSERT OR REPLACE``. Idempotent: re-run
safely after any sim/prod session.

This does NOT touch the JSON files. The dual-write hook inside
``TradeMemory.record()`` keeps SQLite in sync going forward; this CLI
exists to seed history (1,250+ legacy trades + recent per-bot trades).

Usage:
    cd "C:\\Trading Project\\phoenix_bot"
    python tools/migrate_trade_memory_to_sqlite.py
    python tools/migrate_trade_memory_to_sqlite.py --logs-dir logs --db data/trade_memory.db
    python tools/migrate_trade_memory_to_sqlite.py --dry-run

Exit codes:
    0  Backfill completed (count printed; possibly 0 if no JSON found).
    1  Backfill aborted (IO error, schema failure).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow `python tools/migrate_trade_memory_to_sqlite.py` from project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.trade_memory import load_all_trades  # noqa: E402
from core.trade_memory_db import TradeMemoryDB, DEFAULT_DB_PATH  # noqa: E402

logger = logging.getLogger("migrate_trade_memory_to_sqlite")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill SQLite trade memory from JSON files.",
    )
    parser.add_argument(
        "--logs-dir",
        default=str(PROJECT_ROOT / "logs"),
        help="Directory containing trade_memory*.json files "
        "(default: <project>/logs).",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite db path (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read JSON, report counts, do not write to SQLite.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    logs_dir = args.logs_dir
    try:
        trades = load_all_trades(logs_dir=logs_dir)
    except Exception as e:
        logger.error("load_all_trades failed: %s", e)
        return 1

    print(f"Found {len(trades)} trades in {logs_dir}")
    by_bot: dict[str, int] = {}
    no_id = 0
    for t in trades:
        if not t.get("trade_id"):
            no_id += 1
        b = t.get("bot_id") or "unknown"
        by_bot[b] = by_bot.get(b, 0) + 1
    print("  by bot_id:", dict(sorted(by_bot.items())))
    if no_id:
        print(f"  WARNING: {no_id} trades missing trade_id (will be skipped)")

    if args.dry_run:
        print("Dry-run: no SQLite write.")
        return 0

    db_path = args.db
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    print(f"Writing to {db_path}")
    with TradeMemoryDB(db_path) as db:
        before = db.trade_count()
        written = db.write_trades_bulk(trades)
        after = db.trade_count()
    print(f"  rows before: {before}")
    print(f"  attempted:   {written}")
    print(f"  rows after:  {after}")
    print(f"  delta:       {after - before}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
