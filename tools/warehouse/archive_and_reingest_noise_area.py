"""One-shot: archive contaminated noise_area trades + re-ingest fresh.

Phase C1 of overnight master run 2026-06-01 -> 2026-06-02. The harness
contamination audit (commit 7de51ee) fixed the zero-distance target
synthesis bug. The warehouse still holds the pre-fix noise_area rows
(~9,467 trades / 0% WR / -$45k). This script:

  1. Snapshots the contaminated noise_area rows into
     trades_archive_noise_area_contaminated_2026_06_01.
  2. Deletes those rows from trades.
  3. Tags the source runs with
     logical_group='noise_area__contaminated_archived_2026_06_01'.
  4. Ingests backtest_results/phoenix_real_trades.csv (the post-fix
     re-backtest) with --mark-friction-applied.
  5. Sanity-checks the new row count.

Idempotent only at step (1) if the archive table already exists -- the
script aborts in that case rather than overwriting prior history.
"""
from __future__ import annotations

import logging
import pathlib
import sys

import duckdb

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.warehouse import DB_PATH  # noqa: E402
from tools.warehouse.ingest import ingest_csv  # noqa: E402

CSV = ROOT / "backtest_results" / "phoenix_real_trades.csv"
ARCHIVE_TABLE = "trades_archive_noise_area_contaminated_2026_06_01"
LOGICAL_GROUP_TAG = "noise_area__contaminated_archived_2026_06_01"

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    conn = duckdb.connect(str(DB_PATH), read_only=False)
    try:
        # ── Step 0: pre-flight + idempotence check ──────────────
        archive_exists = conn.execute(
            f"SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_name = '{ARCHIVE_TABLE}'"
        ).fetchone()[0]
        n_before = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy='noise_area'"
        ).fetchone()[0]

        if archive_exists and n_before == 0:
            log.info("archive table already exists and trades.noise_area "
                     "is already empty -- assuming Steps 1-3 succeeded on a "
                     "prior run; skipping straight to ingest.")
        elif archive_exists and n_before > 0:
            log.error("archive table %s exists AND trades.noise_area still "
                      "has %d rows -- ambiguous state, aborting",
                      ARCHIVE_TABLE, n_before)
            return 2
        else:
            if n_before == 0:
                log.error("no noise_area rows present and no archive "
                          "table -- aborting (nothing to do)")
                return 2

            n_runs = conn.execute(
                "SELECT COUNT(DISTINCT run_id) FROM trades WHERE strategy='noise_area'"
            ).fetchone()[0]
            log.info("pre-archive: noise_area rows=%d across %d runs",
                     n_before, n_runs)

            # ── Step 1: archive ─────────────────────────────────
            conn.execute(
                f"CREATE TABLE {ARCHIVE_TABLE} AS "
                f"SELECT * FROM trades WHERE strategy='noise_area'"
            )
            n_archived = conn.execute(
                f"SELECT COUNT(*) FROM {ARCHIVE_TABLE}"
            ).fetchone()[0]
            log.info("archived %d rows into %s", n_archived, ARCHIVE_TABLE)
            assert n_archived == n_before

            # ── Step 2: tag the source runs ─────────────────────
            affected_runs = [
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT run_id FROM {ARCHIVE_TABLE}"
                ).fetchall()
            ]
            if affected_runs:
                placeholders = ",".join("?" * len(affected_runs))
                conn.execute(
                    f"UPDATE runs SET logical_group=? WHERE run_id IN ({placeholders})",
                    [LOGICAL_GROUP_TAG, *affected_runs],
                )
                log.info("tagged %d runs with logical_group=%s",
                         len(affected_runs), LOGICAL_GROUP_TAG)

            # ── Step 3: delete from live trades ─────────────────
            conn.execute("DELETE FROM trades WHERE strategy='noise_area'")
            n_after_delete = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE strategy='noise_area'"
            ).fetchone()[0]
            log.info("after delete: noise_area rows=%d", n_after_delete)
            assert n_after_delete == 0
            conn.commit()
    finally:
        conn.close()

    # ── Step 4: ingest the fixed CSV ────────────────────────────
    # DuckDB read_csv_auto misclassifies a target_price column that is
    # 100% inf/-inf (the managed-exit sentinel introduced in commit
    # 7de51ee). Pre-sanitize: write a sibling .for_ingest.csv with
    # target_price=NULL on those rows. The original CSV stays untouched.
    sanitized = CSV.with_suffix(".for_ingest.csv")
    import csv as _csv
    n_rewritten = 0
    with open(CSV, newline="", encoding="utf-8") as fin, \
            open(sanitized, "w", newline="", encoding="utf-8") as fout:
        rdr = _csv.DictReader(fin)
        wtr = _csv.DictWriter(fout, fieldnames=rdr.fieldnames)
        wtr.writeheader()
        for row in rdr:
            if row.get("target_price") in ("inf", "-inf", "nan", "NaN"):
                row["target_price"] = ""
                n_rewritten += 1
            wtr.writerow(row)
    log.info("sanitized %d inf/-inf target_price values into %s",
             n_rewritten, sanitized.name)
    log.info("ingesting %s ...", sanitized)
    result = ingest_csv(sanitized, mark_friction_applied=True)
    log.info("ingest result: status=%s rows=%d run_id=%s",
             result.status, result.rows_inserted, result.run_id)
    if result.status not in ("inserted", "skipped_duplicate"):
        log.error("ingest failed: %s", result.error)
        return 1

    # ── Step 5: sanity check ────────────────────────────────────
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        n_after = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy='noise_area'"
        ).fetchone()[0]
        wr = conn.execute("""
            SELECT
              AVG(CASE WHEN pnl_dollars > 0 THEN 1.0 ELSE 0.0 END) AS wr,
              SUM(CASE WHEN pnl_dollars > 0 THEN pnl_dollars ELSE 0 END) AS gross_w,
              SUM(CASE WHEN pnl_dollars < 0 THEN -pnl_dollars ELSE 0 END) AS gross_l,
              SUM(pnl_dollars) AS net
            FROM trades WHERE strategy='noise_area'
        """).fetchone()
        log.info("post-ingest: rows=%d WR=%.2f%% gross_w=$%.2f gross_l=$%.2f net=$%.2f",
                 n_after, (wr[0] or 0) * 100, wr[1] or 0, wr[2] or 0, wr[3] or 0)
        if abs(n_after - 2453) > 20:
            log.error("sanity FAIL: expected ~2,453 rows; got %d", n_after)
            return 1
        log.info("sanity OK")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
