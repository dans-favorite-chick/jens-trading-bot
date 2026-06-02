"""Build `trades_with_state` view + per-strategy x per-state PF report.

Phase 5 of the 2026-06-02 triangulated overnight review.

This script is idempotent:
- DROP VIEW IF EXISTS trades_with_state at top
- CREATE VIEW trades_with_state via ASOF lookback against market_state_bars
- Aggregate per (strategy, entry_market_state) using ONLY friction_applied runs,
  excluding the contaminated noise_area logical group archived on 2026-06-01.
- Write a markdown table to logs/oracle/research/2026-06-02_per_strategy_per_state_pf.md

Run:
    python -m tools.warehouse.build_trades_with_state

No CLI args; deliberately parameterless to make re-runs trivial.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import duckdb

from tools.warehouse.lock import ingest_lock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_trades_with_state")

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "warehouse" / "phoenix.duckdb"
REPORT_PATH = REPO_ROOT / "logs" / "oracle" / "research" / "2026-06-02_per_strategy_per_state_pf.md"

# Preview claims from the operator prompt (Phase 5 step 6).
PREVIEW_CLAIMS = {
    ("WHIPSAW_HIGH_VOL", "bias_momentum"): {"pf": 1.70, "n": 198},
    ("CHOPPY", "bias_momentum"): {"pf": 1.41, "n": 307},
}

# Threshold for flagging disagreement
DELTA_THRESHOLD = 0.20  # 20%


# ──────────────────────────────────────────────────────────────
# View definition (the headline artifact). Semantics: for each trade, look up
# the most recent market_state_bars row with bar_ts <= trade timestamp.
#
# The literal correlated-subquery formulation specified in the Phase 5 brief is
# preserved here verbatim because the view IS the artifact other tooling will
# query. DuckDB's planner does not currently push this down efficiently
# (each trade triggers a per-row index scan and the GROUP BY spills 12+ GB to
# disk), so for our own aggregation step we materialize the same lookup once
# via an ASOF JOIN. The ASOF JOIN is semantically equivalent — `MATCH_CONDITION
# (m.bar_ts <= t.entry_ts)` returns exactly the same row that the
# `ORDER BY bar_ts DESC LIMIT 1` subquery does — so the per-cell numbers are
# identical to what the view would produce given infinite time.
# ──────────────────────────────────────────────────────────────
CREATE_VIEW_SQL = """
CREATE VIEW trades_with_state AS
SELECT
    t.*,
    (SELECT label FROM market_state_bars m
        WHERE m.bar_ts <= t.entry_ts
        ORDER BY m.bar_ts DESC LIMIT 1) AS entry_market_state,
    (SELECT label FROM market_state_bars m
        WHERE m.bar_ts <= t.exit_ts
        ORDER BY m.bar_ts DESC LIMIT 1) AS exit_market_state
FROM trades t
"""

# Materialize the ASOF lookup ONCE so step-3 (state distribution) and step-4
# (per-strategy x per-state aggregate) finish in seconds instead of hours.
MATERIALIZE_TMP_SQL = """
CREATE OR REPLACE TEMP TABLE _trades_with_state_mat AS
SELECT t.run_id, t.strategy, t.entry_ts, t.exit_ts, t.pnl_dollars,
       m_entry.label AS entry_market_state,
       m_exit.label  AS exit_market_state
FROM trades t
ASOF LEFT JOIN market_state_bars m_entry
  ON m_entry.bar_ts <= t.entry_ts
ASOF LEFT JOIN market_state_bars m_exit
  ON m_exit.bar_ts  <= t.exit_ts
"""

AGG_SQL = """
SELECT t.strategy,
       t.entry_market_state,
       COUNT(*)                                                       AS n,
       AVG(CASE WHEN t.pnl_dollars > 0 THEN 1.0 ELSE 0.0 END)          AS win_rate,
       SUM(CASE WHEN t.pnl_dollars > 0 THEN t.pnl_dollars ELSE 0 END)  AS gross_w,
       SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END) AS gross_l,
       AVG(t.pnl_dollars)                                              AS avg_pnl,
       SUM(t.pnl_dollars)                                              AS total_pnl
FROM _trades_with_state_mat t
JOIN runs r ON r.run_id = t.run_id
WHERE r.friction_applied = TRUE
  AND r.logical_group IS DISTINCT FROM 'noise_area__contaminated_archived_2026_06_01'
GROUP BY 1, 2
ORDER BY 1, 2
"""


def _format_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{100.0 * v:.2f}"


def _format_pf(gw: float, gl: float) -> str:
    if gl is None or gl == 0:
        return "inf" if gw and gw > 0 else "-"
    return f"{gw / gl:.2f}"


def _pf_value(gw: float, gl: float) -> float | None:
    if gl is None or gl == 0:
        return None
    return gw / gl


def _format_dollar(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}"


def _flag_for(strategy: str, state: str, n: int, pf: float | None) -> str:
    key = (state, strategy)
    if key not in PREVIEW_CLAIMS:
        return ""
    claim = PREVIEW_CLAIMS[key]
    claim_pf = claim["pf"]
    claim_n = claim["n"]
    if pf is None:
        return f"DISAGREE (preview PF={claim_pf}, n={claim_n}; warehouse PF=undef [gross_l=0])"
    pf_delta = abs(pf - claim_pf) / max(claim_pf, 1e-9)
    n_delta = abs(n - claim_n) / max(claim_n, 1)
    if pf_delta > DELTA_THRESHOLD or n_delta > DELTA_THRESHOLD:
        return (
            f"DISAGREE (preview PF={claim_pf}/n={claim_n}; warehouse PF={pf:.2f}/n={n}; "
            f"pf_delta={pf_delta*100:.1f}%, n_delta={n_delta*100:.1f}%)"
        )
    return f"AGREE (preview PF={claim_pf}/n={claim_n}; warehouse PF={pf:.2f}/n={n})"


def _md_table(header: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    header = list(header)
    rows = [list(r) for r in rows]
    out_lines = []
    out_lines.append("| " + " | ".join(header) + " |")
    out_lines.append("|" + "|".join("---" for _ in header) + "|")
    for row in rows:
        out_lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out_lines)


def build(con: duckdb.DuckDBPyConnection) -> dict:
    """Build view + report. Returns summary dict for caller logging."""

    log.info("dropping existing trades_with_state view (if any) ...")
    con.execute("DROP VIEW IF EXISTS trades_with_state;")

    log.info("creating trades_with_state view ...")
    con.execute(CREATE_VIEW_SQL)

    log.info("materializing ASOF-join temp table (one-shot, fast) ...")
    con.execute(MATERIALIZE_TMP_SQL)

    # Row count of the view (= COUNT(*) FROM trades, since view has no filter).
    view_n = con.execute("SELECT COUNT(*) FROM _trades_with_state_mat").fetchone()[0]
    log.info("trades_with_state row count: %s", f"{view_n:,}")

    # State distribution (across ALL trades in view, not filtered)
    dist_rows = con.execute(
        "SELECT entry_market_state, COUNT(*) AS n "
        "FROM _trades_with_state_mat GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    log.info("state distribution (entry_market_state, n):")
    for state, n in dist_rows:
        log.info("  %-30s %s", state, f"{n:,}")

    # Per-strategy x per-state aggregation, friction only, excluding contaminated noise_area
    agg_rows = con.execute(AGG_SQL).fetchall()
    log.info("aggregated %d (strategy, state) cells", len(agg_rows))

    # Build markdown rows
    md_rows = []
    enriched = []  # for top/bottom analysis
    for strategy, state, n, win_rate, gross_w, gross_l, avg_pnl, total_pnl in agg_rows:
        pf = _pf_value(gross_w, gross_l)
        pf_str = _format_pf(gross_w, gross_l)
        flag = _flag_for(strategy, state, n, pf)
        md_rows.append([
            strategy or "-",
            state or "(NULL)",
            f"{n:,}",
            _format_pct(win_rate),
            pf_str,
            _format_dollar(avg_pnl),
            flag,
        ])
        enriched.append({
            "strategy": strategy,
            "state": state,
            "n": n,
            "win_rate": win_rate,
            "pf": pf,
            "avg_pnl": avg_pnl,
            "total_pnl": total_pnl,
        })

    # Top 5 by total_pnl (winners); bottom 5 by total_pnl (losers)
    by_pnl = sorted(enriched, key=lambda r: (r["total_pnl"] is None, r["total_pnl"] or 0.0))
    bottom5 = by_pnl[:5]
    top5 = list(reversed(by_pnl[-5:]))

    # Build markdown document
    lines = []
    lines.append("# Per-Strategy x Per-State PF Table (2026-06-02)")
    lines.append("")
    lines.append("Phase 5 of triangulated overnight review.")
    lines.append("")
    lines.append("**Source**: `trades_with_state` view (`trades` ASOF-joined to `market_state_bars` on `entry_ts`).")
    lines.append("")
    lines.append("**Filters applied**:")
    lines.append("- `runs.friction_applied = TRUE`")
    lines.append("- Exclude `runs.logical_group = 'noise_area__contaminated_archived_2026_06_01'`")
    lines.append("")
    lines.append(f"- View row count (all trades, unfiltered): **{view_n:,}**")
    lines.append("")
    lines.append("## Entry-state distribution (all trades in view)")
    lines.append("")
    lines.append(_md_table(["entry_market_state", "n"], [(s or "(NULL)", f"{n:,}") for s, n in dist_rows]))
    lines.append("")
    lines.append("## Per-strategy x per-state PF (friction runs only, noise_area archived excluded)")
    lines.append("")
    lines.append("Sorted by strategy, then state.")
    lines.append("")
    lines.append(_md_table(
        ["strategy", "state", "n", "win_rate (%)", "PF", "avg_pnl ($)", "flag"],
        md_rows,
    ))
    lines.append("")
    lines.append("## TOP 5 (strategy, state) cells by total PnL contribution")
    lines.append("")
    top_md = []
    for r in top5:
        top_md.append([
            r["strategy"] or "-",
            r["state"] or "(NULL)",
            f"{r['n']:,}",
            _format_pct(r["win_rate"]),
            f"{r['pf']:.2f}" if r["pf"] is not None else "inf",
            _format_dollar(r["avg_pnl"]),
            _format_dollar(r["total_pnl"]),
        ])
    lines.append(_md_table(
        ["strategy", "state", "n", "win_rate (%)", "PF", "avg_pnl ($)", "total_pnl ($)"],
        top_md,
    ))
    lines.append("")
    lines.append("## BOTTOM 5 (strategy, state) cells by total PnL loss")
    lines.append("")
    bot_md = []
    for r in bottom5:
        bot_md.append([
            r["strategy"] or "-",
            r["state"] or "(NULL)",
            f"{r['n']:,}",
            _format_pct(r["win_rate"]),
            f"{r['pf']:.2f}" if r["pf"] is not None else "inf",
            _format_dollar(r["avg_pnl"]),
            _format_dollar(r["total_pnl"]),
        ])
    lines.append(_md_table(
        ["strategy", "state", "n", "win_rate (%)", "PF", "avg_pnl ($)", "total_pnl ($)"],
        bot_md,
    ))
    lines.append("")
    lines.append("## Preview reconciliation")
    lines.append("")

    # For each preview claim, also compute the NON-friction-filtered cell (still
    # excluding contaminated noise_area) so the reader can see where the
    # preview's number came from. This is informational only — the warehouse
    # row reported above is the spec's friction-only answer.
    rec_rows = []
    for (state, strategy), claim in PREVIEW_CLAIMS.items():
        row = next((r for r in enriched if r["strategy"] == strategy and r["state"] == state), None)
        nf = con.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN t.pnl_dollars > 0 THEN t.pnl_dollars ELSE 0 END),
                   SUM(CASE WHEN t.pnl_dollars < 0 THEN -t.pnl_dollars ELSE 0 END)
            FROM _trades_with_state_mat t
            JOIN runs r ON r.run_id = t.run_id
            WHERE t.strategy = ?
              AND t.entry_market_state = ?
              AND r.logical_group IS DISTINCT FROM 'noise_area__contaminated_archived_2026_06_01'
        """, [strategy, state]).fetchone()
        nf_n, nf_gw, nf_gl = nf
        nf_pf = (nf_gw / nf_gl) if (nf_gl and nf_gl > 0) else None
        nf_pf_str = f"{nf_pf:.2f}" if nf_pf is not None else ("inf" if nf_gw else "-")
        if row is None:
            rec_rows.append([
                state, strategy,
                f"{claim['pf']}", f"{claim['n']}",
                "MISSING (0 friction trades)", "0",
                f"{nf_pf_str} ({nf_n:,})",
                "DISAGREE — cell has 0 trades under friction_applied=TRUE; the preview's number came from a non-friction run",
            ])
            continue
        pf_str = f"{row['pf']:.2f}" if row["pf"] is not None else "inf"
        flag = _flag_for(strategy, state, row["n"], row["pf"])
        rec_rows.append([
            state, strategy,
            f"{claim['pf']}", f"{claim['n']}",
            pf_str, f"{row['n']:,}",
            f"{nf_pf_str} ({nf_n:,})",
            flag,
        ])
    lines.append(_md_table(
        ["state", "strategy", "preview PF", "preview n",
         "warehouse PF (friction)", "warehouse n (friction)",
         "PF (no friction filter, n)", "verdict"],
        rec_rows,
    ))
    lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote report to %s", REPORT_PATH)

    return {
        "view_n": view_n,
        "state_distribution": dist_rows,
        "agg_n_cells": len(agg_rows),
        "top5": top5,
        "bottom5": bottom5,
        "enriched": enriched,
    }


def main() -> int:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"warehouse db not found: {DB_PATH}")

    with ingest_lock():
        con = duckdb.connect(str(DB_PATH))
        try:
            build(con)
        finally:
            con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
