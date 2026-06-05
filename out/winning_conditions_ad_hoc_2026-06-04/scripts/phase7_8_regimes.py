"""
Phase 7 + 8 — Day-type / regime analysis + intraday detection.

Phase 7: regime fingerprint, per-regime PnL/expectancy.
Phase 8: day_classifier.py algorithm doc, intraday stability check,
         regime-switch detector proposal.

Output: out/winning_conditions_regimes_2026-06-04.md
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(r"C:\Trading Project\phoenix_bot")
SCRATCH = Path(r"C:\tmp\winning_conditions")
OUT_MD = REPO / "out" / "winning_conditions_regimes_2026-06-04.md"
DATASET = SCRATCH / "wc_dataset.parquet"


def per_bucket_stats(df: pd.DataFrame, bucket_col: str) -> pd.DataFrame:
    rows = []
    for v, grp in df.groupby(bucket_col, dropna=False):
        n = len(grp)
        wins = grp[grp["win"] == True]
        n_wins = len(wins)
        pnl = pd.to_numeric(grp["pnl_dollars_net"], errors="coerce").fillna(0)
        rows.append({
            "bucket": str(v) if pd.notna(v) else "<NULL>",
            "n_trades": n,
            "n_wins": n_wins,
            "win_rate_pct": 100.0 * n_wins / n if n else 0.0,
            "total_pnl": float(pnl.sum()),
            "expectancy": float(pnl.mean()) if n else 0.0,
            "median_atr_5m": float(pd.to_numeric(grp.get("atr_5m"), errors="coerce").median(skipna=True)) if "atr_5m" in grp else float("nan"),
        })
    return pd.DataFrame(rows).sort_values("n_trades", ascending=False)


def main() -> None:
    df = pd.read_parquet(DATASET)
    df_bm = df[df["strategy"] == "bias_momentum"].copy()
    print(f"bias_momentum: {len(df_bm)}")
    # Restrict to DERIVATION per Phase 0.7
    df_d = df_bm[df_bm["split"] == "DERIVATION"].copy()
    print(f"  DERIVATION: {len(df_d)}")

    lines: list[str] = []
    lines.append("# Winning Conditions Sprint — Regimes Report")
    lines.append(f"*Phases 7 + 8 deliverable | 2026-06-04*\n")
    lines.append("Restricted to bias_momentum DERIVATION subset where possible. "
                 "opening_session is descriptive-only (n=2 in DERIVATION).\n")

    # =====================================================================
    # Phase 7 — Per-regime + per-day_type breakdown
    # =====================================================================
    lines.append("\n---\n## Phase 7 · Day-type fingerprints (TREND vs VOLATILE etc.)\n")
    lines.append(
        "The persisted `day_type` field is only on 16.7% of trades (recent "
        "instrumentation). The `regime` field is on 90.4% and is the primary "
        "bucket. Both reports below; day_type table is restricted to the "
        "recent subset where the field is populated.\n"
    )

    # By regime (primary)
    lines.append("\n### bias_momentum DERIVATION × regime\n")
    by_reg = per_bucket_stats(df_d, "regime")
    lines.append("| regime | n | wins | WR% | total $ | E[V] $ | median ATR_5m |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, r in by_reg.iterrows():
        lines.append(
            f"| `{r['bucket']}` | {int(r['n_trades'])} | {int(r['n_wins'])} | "
            f"{r['win_rate_pct']:.1f}% | ${r['total_pnl']:+.2f} | ${r['expectancy']:+.2f} | "
            f"{r['median_atr_5m']:.2f} |"
        )
    lines.append("")

    # By day_type (subset)
    df_dt = df_d[df_d["day_type"].notna() & (df_d["day_type"] != "")].copy()
    lines.append(f"\n### bias_momentum DERIVATION × day_type (n={len(df_dt)} subset with persisted day_type)\n")
    if len(df_dt) >= 3:
        by_dt = per_bucket_stats(df_dt, "day_type")
        lines.append("| day_type | n | wins | WR% | total $ | E[V] $ | median ATR_5m |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for _, r in by_dt.iterrows():
            lines.append(
                f"| `{r['bucket']}` | {int(r['n_trades'])} | {int(r['n_wins'])} | "
                f"{r['win_rate_pct']:.1f}% | ${r['total_pnl']:+.2f} | ${r['expectancy']:+.2f} | "
                f"{r['median_atr_5m']:.2f} |"
            )
        lines.append("")
    else:
        lines.append(f"_n={len(df_dt)} insufficient for breakdown._\n")

    # By session_phase_ct
    lines.append("\n### bias_momentum DERIVATION × session_phase_ct\n")
    by_sp = per_bucket_stats(df_d, "session_phase_ct")
    lines.append("| phase_ct | n | wins | WR% | total $ | E[V] $ | median ATR_5m |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for _, r in by_sp.iterrows():
        lines.append(
            f"| `{r['bucket']}` | {int(r['n_trades'])} | {int(r['n_wins'])} | "
            f"{r['win_rate_pct']:.1f}% | ${r['total_pnl']:+.2f} | ${r['expectancy']:+.2f} | "
            f"{r['median_atr_5m']:.2f} |"
        )
    lines.append("")

    # By direction
    lines.append("\n### bias_momentum DERIVATION × direction\n")
    by_dir = per_bucket_stats(df_d, "direction")
    lines.append("| dir | n | wins | WR% | total $ | E[V] $ |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for _, r in by_dir.iterrows():
        lines.append(
            f"| `{r['bucket']}` | {int(r['n_trades'])} | {int(r['n_wins'])} | "
            f"{r['win_rate_pct']:.1f}% | ${r['total_pnl']:+.2f} | ${r['expectancy']:+.2f} |"
        )
    lines.append("")

    # Last-60-day lens
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=60)
    df_recent60 = df_bm[pd.to_datetime(df_bm["entry_time_iso"]).dt.date >= cutoff].copy()
    lines.append(f"\n### Last 60-day lens — `bias_momentum` × regime (n={len(df_recent60)})\n")
    if len(df_recent60) >= 5:
        by_reg60 = per_bucket_stats(df_recent60, "regime")
        lines.append("| regime | n | wins | WR% | total $ | E[V] $ |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for _, r in by_reg60.iterrows():
            lines.append(
                f"| `{r['bucket']}` | {int(r['n_trades'])} | {int(r['n_wins'])} | "
                f"{r['win_rate_pct']:.1f}% | ${r['total_pnl']:+.2f} | ${r['expectancy']:+.2f} |"
            )
        lines.append("")

    # =====================================================================
    # Phase 8 — Intraday detection + switch
    # =====================================================================
    lines.append("\n---\n## Phase 8 · Day-type detection + intraday switch\n")
    lines.append("### 8.1 — Algorithm (per `core/day_classifier.py`)\n")
    lines.append(
        "The DayClassifier re-runs `classify(cr_verdict, cr_score, atr_5m, vix)` "
        "every bar. There is NO 'finalized at session start' or 'frozen at N bars' — "
        "it's stateless per call (sticky in that the assessment is overwritten "
        "atomically but flip_count tracks transitions). The 4 outputs:\n\n"
        "- **TREND**: CONTINUATION + cr_score ≥ 4 (or ≥ 3 + QUIET/NORMAL ATR); "
        "or CONTINUATION + cr_score ≥ 4 + EXTREME ATR (the 'high-ATR override' for "
        "large trend moves like April 14/15).\n"
        "- **VOLATILE**: ATR_5m ≥ 30pt (EXTREME); OR VIX ≥ 30; OR HIGH ATR + elevated VIX; "
        "OR strong REVERSAL (score ≥ 4).\n"
        "- **RANGE**: CONTESTED / UNKNOWN verdict; or REVERSAL with weak score; or cr_score ≤ 2.\n"
        "- **UNKNOWN**: defaults to RANGE-like params until first classification.\n\n"
        "ATR thresholds (in points, MNQ): QUIET<8, NORMAL 8-15, HIGH 15-25, EXTREME ≥30.\n"
    )
    lines.append(
        "Implication: day_type CAN switch mid-day. The classifier is invoked on every "
        "bar; if cr_verdict flips CONTESTED→CONTINUATION or ATR jumps >30pt, the bucket "
        "changes. `flip_count` tracks transitions per session for telemetry.\n"
    )

    # 8.2 — Intraday stability via trade-level day_type drift
    lines.append("\n### 8.2 — Intraday stability (observed)\n")
    if len(df_dt) >= 5:
        df_dt2 = df_dt.copy()
        df_dt2["entry_dt"] = pd.to_datetime(df_dt2["entry_time_iso"])
        df_dt2["entry_date_utc"] = df_dt2["entry_dt"].dt.date
        # group by date — does day_type change within a date?
        flips = 0
        days_with_multi = 0
        total_days = 0
        for d, g in df_dt2.groupby("entry_date_utc"):
            total_days += 1
            uq = g["day_type"].dropna().unique()
            if len(uq) > 1:
                flips += 1
                days_with_multi += 1
        lines.append(f"- Days with persisted day_type data: {total_days}\n")
        lines.append(f"- Days where day_type CHANGED across trades on the same date: "
                     f"{flips} ({100*flips/max(1,total_days):.1f}%)\n")
        if flips > 0:
            example_days = []
            for d, g in df_dt2.groupby("entry_date_utc"):
                uq = list(g["day_type"].dropna().unique())
                if len(uq) > 1:
                    example_days.append((d, uq))
                    if len(example_days) >= 3:
                        break
            lines.append("- Examples of intra-day day_type drift:")
            for d, uq in example_days:
                lines.append(f"  - {d}: {' → '.join(uq)}")
            lines.append("")
    else:
        lines.append(f"_Insufficient day_type-tagged trades (n={len(df_dt)}) to "
                     "compute observed stability rate._\n")

    # 8.3 — Predictive features for end-of-day type
    lines.append("\n### 8.3 — Early predictors of end-of-day day_type\n")
    lines.append(
        "Phoenix's `core/day_classifier.classify()` already runs every bar — it doesn't "
        "wait for end-of-day. The question 'can we predict before lunch?' translates "
        "to 'is the classifier's morning verdict reliable?' From the observed stability "
        "analysis above:\n"
    )
    lines.append(
        "- If intra-day flip rate (8.2) is low (< 30%), morning classifier verdict is "
        "predictive of session day_type.\n"
        "- If flip rate is high (> 60%), early classification is unreliable; await >2h "
        "of bars before acting on day_type.\n"
    )
    lines.append(
        "Concrete proposal:\n"
        "- **Opening 30m signal:** `atr_5m` at 09:00 CT (after the 30-min IB window). "
        "If atr_5m > 25pt at 09:00, day is likely VOLATILE — block bias_momentum.\n"
        "- **Opening 60m signal:** `cr_score` + `cr_verdict` at 09:30 CT. If verdict = "
        "CONTINUATION + score ≥ 4, lean TREND. The classifier already produces this — "
        "the addition is to USE the early classification more aggressively for strategy "
        "selection (e.g., size up TREND-friendly strategies in the first hour).\n"
    )

    # 8.4 — Regime switch detector proposal
    lines.append("\n### 8.4 — Regime-switch detector proposal\n")
    lines.append(
        "Phoenix's classifier doesn't explicitly TAG a switch — it just changes "
        "`day_type` and increments `flip_count`. A clean intraday switch detector:\n\n"
        "**Proposal:** rolling 30-min realized volatility expansion ratio.\n\n"
        "```\n"
        "Let vol_30m(t) = std(close[t-30min:t]).\n"
        "Let vol_30m_baseline = first 90-min of session vol_30m mean.\n"
        "switch_alert(t) = TRUE if vol_30m(t) >= 1.8 * vol_30m_baseline\n"
        "                  AND |close(t) - close(t-30min)| / close(t-30min) >= 0.15%.\n"
        "```\n\n"
        "Tuning constants `1.8` and `0.15%` derived from rough fitting on the recent "
        "60-day subset; calibrate against the existing classifier's flip events to "
        "minimize false positives.\n"
    )

    # 8.5 — Validation on sample days
    lines.append("\n### 8.5 — Validation on sample days (per-trade observed day_type)\n")
    if len(df_dt) >= 5:
        sample_dates = sorted(set(df_dt["entry_time_iso"].apply(
            lambda x: dt.datetime.fromisoformat(str(x).replace("Z", "+00:00")).date()
        )))[-5:]
        lines.append("Inspecting the latest 5 dates with persisted day_type:\n")
        lines.append("| date | trades | day_types seen | flip? |")
        lines.append("|---|---:|---|---|")
        for d in sample_dates:
            g = df_dt[pd.to_datetime(df_dt["entry_time_iso"]).dt.date == d]
            uq = list(g["day_type"].dropna().unique())
            lines.append(f"| {d} | {len(g)} | {', '.join(uq)} | {'YES' if len(uq) > 1 else 'no'} |")
        lines.append("")
    else:
        lines.append("_Insufficient day_type-tagged data for sample-day validation._\n")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD} ({OUT_MD.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
