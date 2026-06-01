"""
Post-Phase-3 sanity check for the fresh WFA ingest.

Runs the queries specified in docs/FRESH_WFA_PLAN_2026-06-01.md Phase 4
and flags any red-flag patterns (WFE > 5, every strategy passing, etc.).

USE: python tools/warehouse/verify_fresh_wfa.py
"""
from __future__ import annotations

from pathlib import Path

import duckdb

WAREHOUSE = Path(r"C:\Trading Project\phoenix_bot\data\warehouse\phoenix.duckdb")


def main() -> int:
    conn = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        # 1. Confirm fresh data present
        most_recent = conn.execute("""
            SELECT MAX(ingested_at) AS most_recent, COUNT(*) AS row_count
            FROM runs
            WHERE csv_kind IN ('wfa_summary', 'wfa_windows')
              AND ingested_at >= CURRENT_DATE
        """).fetchone()
        print(f"Fresh runs ingested today: count={most_recent[1]}, "
              f"most_recent={most_recent[0]}")
        if most_recent[1] == 0:
            print("RED FLAG: no WFA runs ingested today.")
            return 1
        print()

        # 2. Per-strategy WFE distribution
        rows = conn.execute("""
            SELECT
                strategy,
                n_windows,
                ROUND(mean_is_pf, 3) AS mean_is_pf,
                ROUND(mean_oos_pf, 3) AS mean_oos_pf,
                ROUND(mean_oos_pf / NULLIF(mean_is_pf, 0), 3) AS wfe,
                ROUND(pct_windows_degraded, 3) AS pct_degraded,
                robust
            FROM wfa_summary
            ORDER BY mean_oos_pf DESC
        """).fetchall()

        if not rows:
            print("RED FLAG: wfa_summary is empty after ingest.")
            return 1

        # Render table
        header = ("strategy", "n_wnd", "is_pf", "oos_pf",
                   "wfe", "pct_deg", "robust")
        widths = [22, 6, 8, 8, 8, 9, 7]
        line = "  ".join(h.rjust(w) if i > 0 else h.ljust(w)
                          for i, (h, w) in enumerate(zip(header, widths)))
        print(line)
        print("-" * len(line))
        for r in rows:
            cells = [
                str(r[0]).ljust(widths[0]),
                str(r[1] or "-").rjust(widths[1]),
                f"{r[2]:.3f}".rjust(widths[2]) if r[2] is not None else "-".rjust(widths[2]),
                f"{r[3]:.3f}".rjust(widths[3]) if r[3] is not None else "-".rjust(widths[3]),
                f"{r[4]:.3f}".rjust(widths[4]) if r[4] is not None else "-".rjust(widths[4]),
                f"{r[5]:.3f}".rjust(widths[5]) if r[5] is not None else "-".rjust(widths[5]),
                str(r[6]).rjust(widths[6]),
            ]
            print("  ".join(cells))
        print()

        # 3. Red-flag analysis
        red_flags = []
        for r in rows:
            strategy, n_windows, is_pf, oos_pf, wfe, pct_deg, robust = r
            if wfe is not None and wfe > 5:
                red_flags.append(
                    f"  [{strategy}] WFE={wfe} > 5 -- divide-by-near-zero "
                    f"or genuine OOS outperformance. Inspect window detail."
                )
            if n_windows is None or n_windows < 2:
                red_flags.append(
                    f"  [{strategy}] n_windows={n_windows} -- window math is off; "
                    f"results not meaningful."
                )

        # Aggregate red flag: every strategy passing
        n_pass = sum(1 for r in rows if r[6])
        if n_pass == len(rows) and len(rows) >= 5:
            red_flags.append(
                f"  EVERY strategy ({n_pass}/{len(rows)}) shows robust=True -- "
                f"threshold may be too loose OR data may have a systematic issue."
            )

        if red_flags:
            print("RED FLAGS DETECTED:")
            for f in red_flags:
                print(f)
            return 2
        else:
            print("No red flags detected. WFA distribution looks plausible.")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
