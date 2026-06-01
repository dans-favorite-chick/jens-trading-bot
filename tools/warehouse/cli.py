"""
tools.warehouse.cli — Command-line interface for the Phoenix backtest warehouse.

Usage:
    python -m tools.warehouse ingest <path>                  # single file or directory
    python -m tools.warehouse ingest <path> --recursive
    python -m tools.warehouse ingest <path> --logical-group phase13_wfa
    python -m tools.warehouse ingest <path> --mark-friction-applied
    python -m tools.warehouse ingest <path> --dry-run
    python -m tools.warehouse status
"""

import argparse
import logging
import sys
from pathlib import Path

from tools.warehouse import DB_PATH
from tools.warehouse.ingest import IngestResult, ingest_csv, scan_dir

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def cmd_ingest(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve()
    db_path = Path(args.db) if args.db else DB_PATH
    common = dict(
        db_path=db_path,
        logical_group=args.logical_group,
        mark_friction_applied=True if args.mark_friction_applied else None,
        dry_run=args.dry_run,
    )

    if target.is_file():
        r = ingest_csv(target, **common)
        _print_result(r)
        return 0 if r.status != "error" else 1

    elif target.is_dir():
        results = scan_dir(
            target,
            recursive=args.recursive,
            **common,
        )
        _print_summary(results)
        errors = [r for r in results if r.status == "error"]
        return 0 if not errors else 1

    else:
        log.error("path not found: %s", target)
        return 2


def cmd_status(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        print(f"Warehouse not found at {db_path}")
        print("Run: python -m tools.warehouse ingest <path>")
        return 1

    import duckdb
    from tools.warehouse.ingest import _ensure_schema

    con = duckdb.connect(str(db_path))
    _ensure_schema(con)

    tables = ["runs", "trades", "wfa_windows", "wfa_summary", "run_metrics"]
    print(f"\n{'Table':<30} {'Rows':>10}")
    print("-" * 42)
    for t in tables:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            n = "N/A"
        print(f"{t:<30} {n:>10}")

    # Import tables
    all_tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    import_tables = sorted(t for t in all_tables if t.startswith("import_"))
    for t in import_tables:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            n = "N/A"
        print(f"{t:<30} {n:>10}")

    # Last ingest
    try:
        row = con.execute(
            "SELECT source_filename, ingested_at FROM runs ORDER BY ingested_at DESC LIMIT 1"
        ).fetchone()
        if row:
            print(f"\nLast ingest: {row[0]}  at {row[1]}")
    except Exception:
        pass

    con.close()
    return 0


def _print_result(r: IngestResult) -> None:
    # ASCII-only: Windows console default codec is cp1252.
    icon = {"inserted": "[OK]", "skipped_duplicate": "[--]", "error": "[ER]"}.get(r.status, "[??]")
    print(f"{icon}  {r.csv_path.name:<50}  status={r.status}")
    if r.status == "inserted":
        print(f"    kind={r.csv_kind}  rows={r.rows_inserted}  metrics={r.metrics_inserted}")
        print(f"    run_id={r.run_id[:16]}...")
    elif r.status == "error":
        print(f"    error={r.error}")


def _print_summary(results: list[IngestResult]) -> None:
    inserted = [r for r in results if r.status == "inserted"]
    dupes    = [r for r in results if r.status == "skipped_duplicate"]
    errors   = [r for r in results if r.status == "error"]
    total_rows    = sum(r.rows_inserted for r in inserted)
    total_metrics = sum(r.metrics_inserted for r in inserted)

    # ASCII-only output: Windows default console codec (cp1252) can't encode
    # box-drawing or emoji characters and crashes the whole CLI mid-print.
    print(f"\n{'-'*60}")
    print(f"  Files processed : {len(results)}")
    print(f"  Inserted        : {len(inserted)}  ({total_rows} rows, {total_metrics} metrics)")
    print(f"  Duplicates skip : {len(dupes)}")
    print(f"  Errors          : {len(errors)}")
    if errors:
        print("\n  Failed files:")
        for r in errors:
            print(f"    [ERROR] {r.csv_path.name}: {r.error}")
    print(f"{'-'*60}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.warehouse",
        description="Phoenix backtest warehouse CLI",
    )
    parser.add_argument(
        "--db", default=None, metavar="DB_PATH",
        help="Override warehouse DuckDB path (default: tools.warehouse.DB_PATH)",
    )
    sub = parser.add_subparsers(dest="command")

    # ingest subcommand
    p_ingest = sub.add_parser("ingest", help="Ingest a CSV file or directory")
    p_ingest.add_argument("path", help="CSV file or directory to ingest")
    p_ingest.add_argument("--recursive", action="store_true",
                          help="Recurse into subdirectories")
    p_ingest.add_argument("--logical-group", default=None, metavar="GROUP",
                          help="Tag all runs in this batch with a logical group label")
    p_ingest.add_argument("--mark-friction-applied", action="store_true",
                          help="Override friction_applied=true for all runs in this batch")
    p_ingest.add_argument("--dry-run", action="store_true",
                          help="Report what would happen; make no DB writes")

    # status subcommand
    p_status = sub.add_parser("status", help="Show row counts and last ingest timestamp")

    args = parser.parse_args(argv)

    if args.command == "ingest":
        return cmd_ingest(args)
    elif args.command == "status":
        return cmd_status(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
