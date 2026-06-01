"""
Archive the existing wfa_summary + wfa_windows tables into dated archive tables,
then clear the originals. Idempotent -- safe to re-run (will skip if archive
already exists).

USE: python tools/warehouse/archive_stale_wfa.py [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import duckdb

WAREHOUSE = Path(r"C:\Trading Project\phoenix_bot\data\warehouse\phoenix.duckdb")
ARCHIVE_SUFFIX = f"archive_{date.today().isoformat().replace('-', '_')}"


def main(dry_run: bool = False) -> None:
    conn = duckdb.connect(str(WAREHOUSE), read_only=False)
    try:
        for base in ("wfa_summary", "wfa_windows"):
            archive_name = f"{base}_{ARCHIVE_SUFFIX}"
            n = conn.execute(f"SELECT COUNT(*) FROM {base}").fetchone()[0]
            already = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                f"WHERE table_name = '{archive_name}'"
            ).fetchone()[0]

            print(f"{base}: {n} rows. Archive '{archive_name}' "
                  f"{'EXISTS' if already else 'does NOT exist'} yet.")

            if dry_run:
                continue
            if already:
                print(f"  -> Skip archive (already exists); will only clear {base}.")
            else:
                conn.execute(f"CREATE TABLE {archive_name} AS SELECT * FROM {base}")
                print(f"  -> Archived {n} rows to {archive_name}.")
            conn.execute(f"DELETE FROM {base}")
            print(f"  -> Cleared {base}.")

        # Tag the runs rows whose WFA payloads we just archived so future
        # consumers can see which provenance got retired.
        if not dry_run:
            conn.execute(
                "UPDATE runs SET logical_group = 'wfa_summary__archived_2026_06_01' "
                "WHERE csv_kind = 'wfa_summary' "
                "AND (logical_group IS NULL OR logical_group NOT LIKE '%archived%')"
            )
            conn.execute(
                "UPDATE runs SET logical_group = 'wfa_windows__archived_2026_06_01' "
                "WHERE csv_kind = 'wfa_windows' "
                "AND (logical_group IS NULL OR logical_group NOT LIKE '%archived%')"
            )
            print("Tagged corresponding runs rows with archived_2026_06_01.")
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    main(p.parse_args().dry_run)
