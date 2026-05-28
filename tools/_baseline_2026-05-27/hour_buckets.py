"""
Hour-by-hour timing analysis per strategy (Phase C-a').

Reads a per-trade CSV with columns
  strategy, direction, entry_ts (UTC iso), entry_price, stop_price,
  target_price, exit_ts, exit_price, exit_reason, pnl_dollars,
  pnl_ticks, hold_min, year
and emits:

  out/_baseline_2026-05-27/hour_buckets_detail.csv
      one row per (strategy, bucket_ct, direction)

  out/_baseline_2026-05-27/hour_buckets_proposal.csv
      one row per strategy with proposed_session_windows_ct,
      proposed_block_windows, current config values for comparison

  out/_baseline_2026-05-27/hour_buckets_summary.md
      human-readable per-strategy report

Buckets: 30-min CT slots (00:00, 00:30, ..., 23:30) -> 48 buckets/day.

Green bucket = Wilson 95% lower bound on WR > break-even WR for that
strategy's median RR, AND n_trades >= 20.

Red bucket = Wilson 95% upper bound on WR < break-even WR with n >= 20.
(I.e. even at the favorable end of the CI, the bucket is loss-making.)

Multiprocessing: one worker per strategy via spawn context, max 8 workers.
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent

# Wilson 95% confidence interval for a binomial proportion.
Z_95 = 1.959963984540054


def wilson_interval(wins: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Returns (lo, hi) Wilson 95% CI for win rate."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


@dataclass
class BucketRow:
    strategy: str
    bucket_ct: str  # "HH:MM" start of 30-min slot
    direction: str  # LONG / SHORT / ALL
    n: int
    wins: int
    wr_pct: float
    wilson_lo_pct: float
    wilson_hi_pct: float
    total_dollars: float
    avg_dollars: float
    median_rr: float
    expectancy_R: float
    breakeven_wr_pct: float
    label: str  # GREEN / RED / NEUTRAL / SPARSE


def _bucket_label(ts_ct: pd.Timestamp) -> str:
    minute = ts_ct.minute
    bucket_min = 0 if minute < 30 else 30
    return f"{ts_ct.hour:02d}:{bucket_min:02d}"


def _per_trade_rr(row: pd.Series) -> float:
    """RR = |target - entry| / |entry - stop|. Returns nan if not computable."""
    try:
        risk = abs(float(row["entry_price"]) - float(row["stop_price"]))
        reward = abs(float(row["target_price"]) - float(row["entry_price"]))
        if risk <= 0:
            return float("nan")
        return reward / risk
    except (KeyError, ValueError, TypeError):
        return float("nan")


def analyze_strategy(
    strategy: str,
    df_strat: pd.DataFrame,
    min_n_for_label: int = 20,
) -> tuple[list[BucketRow], dict]:
    """Per-strategy worker. Pure function; safe for spawn."""

    # Add bucket + per-trade RR columns
    df = df_strat.copy()
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df["entry_ct"] = df["entry_ts"].dt.tz_convert("America/Chicago")
    df["bucket_ct"] = df["entry_ct"].apply(_bucket_label)
    df["rr"] = df.apply(_per_trade_rr, axis=1)
    df["win"] = (df["pnl_dollars"] > 0).astype(int)

    median_rr_overall = float(df["rr"].median()) if df["rr"].notna().any() else float("nan")
    if not math.isnan(median_rr_overall) and median_rr_overall > 0:
        breakeven_wr_overall = 1.0 / (1.0 + median_rr_overall)
    else:
        breakeven_wr_overall = 0.5

    rows: list[BucketRow] = []

    for direction in ["ALL", "LONG", "SHORT"]:
        sub = df if direction == "ALL" else df[df["direction"] == direction]
        if len(sub) == 0:
            continue

        for bucket, gdf in sub.groupby("bucket_ct"):
            n = len(gdf)
            wins = int(gdf["win"].sum())
            wr = wins / n if n > 0 else 0.0
            lo, hi = wilson_interval(wins, n)
            total = float(gdf["pnl_dollars"].sum())
            avg = total / n if n > 0 else 0.0
            med_rr = float(gdf["rr"].median()) if gdf["rr"].notna().any() else median_rr_overall
            be_wr = 1.0 / (1.0 + med_rr) if (not math.isnan(med_rr) and med_rr > 0) else 0.5
            expectancy_r = wr * med_rr - (1 - wr) if not math.isnan(med_rr) else float("nan")

            if n < min_n_for_label:
                label = "SPARSE"
            elif lo > be_wr:
                label = "GREEN"
            elif hi < be_wr:
                label = "RED"
            else:
                label = "NEUTRAL"

            rows.append(BucketRow(
                strategy=strategy,
                bucket_ct=bucket,
                direction=direction,
                n=n,
                wins=wins,
                wr_pct=round(100 * wr, 2),
                wilson_lo_pct=round(100 * lo, 2),
                wilson_hi_pct=round(100 * hi, 2),
                total_dollars=round(total, 2),
                avg_dollars=round(avg, 2),
                median_rr=round(med_rr, 2) if not math.isnan(med_rr) else float("nan"),
                expectancy_R=round(expectancy_r, 3) if not math.isnan(expectancy_r) else float("nan"),
                breakeven_wr_pct=round(100 * be_wr, 2),
                label=label,
            ))

    # Build per-strategy proposal from ALL-direction rows
    proposal = _build_proposal(strategy, rows, median_rr_overall, breakeven_wr_overall)
    return rows, proposal


def _bucket_to_minutes(b: str) -> int:
    h, m = b.split(":")
    return int(h) * 60 + int(m)


def _minutes_to_bucket(m: int) -> str:
    return f"{(m // 60):02d}:{(m % 60):02d}"


def _merge_contiguous(buckets: list[str]) -> list[tuple[str, str]]:
    """Merge a sorted list of 30-min bucket starts into (start, end) windows.
    end = bucket_start + 30 min (so "08:30","09:00","09:30" -> ("08:30","10:00")).
    """
    if not buckets:
        return []
    minutes = sorted(set(_bucket_to_minutes(b) for b in buckets))
    windows: list[tuple[int, int]] = []
    run_start = minutes[0]
    run_end = run_start + 30
    for m in minutes[1:]:
        if m == run_end:
            run_end = m + 30
        else:
            windows.append((run_start, run_end))
            run_start = m
            run_end = m + 30
    windows.append((run_start, run_end))
    return [(_minutes_to_bucket(s), _minutes_to_bucket(e if e < 1440 else 1439)) for s, e in windows]


def _build_proposal(
    strategy: str,
    rows: list[BucketRow],
    median_rr_overall: float,
    breakeven_wr_overall: float,
) -> dict:
    all_rows = [r for r in rows if r.direction == "ALL"]
    green_buckets = sorted(r.bucket_ct for r in all_rows if r.label == "GREEN")
    red_buckets = sorted(r.bucket_ct for r in all_rows if r.label == "RED")
    neutral_buckets = sorted(r.bucket_ct for r in all_rows if r.label == "NEUTRAL")
    sparse_buckets = sorted(r.bucket_ct for r in all_rows if r.label == "SPARSE")

    total_n = sum(r.n for r in all_rows)
    total_pnl = sum(r.total_dollars for r in all_rows)
    green_pnl = sum(r.total_dollars for r in all_rows if r.label == "GREEN")
    red_pnl = sum(r.total_dollars for r in all_rows if r.label == "RED")

    return {
        "strategy": strategy,
        "n_trades_total": total_n,
        "total_pnl": round(total_pnl, 2),
        "median_rr_overall": round(median_rr_overall, 2) if not math.isnan(median_rr_overall) else None,
        "breakeven_wr_overall_pct": round(100 * breakeven_wr_overall, 2),
        "n_green_buckets": len(green_buckets),
        "n_red_buckets": len(red_buckets),
        "n_neutral_buckets": len(neutral_buckets),
        "n_sparse_buckets": len(sparse_buckets),
        "green_buckets": green_buckets,
        "red_buckets": red_buckets,
        "neutral_buckets": neutral_buckets,
        "proposed_session_windows_ct": _merge_contiguous(green_buckets),
        "proposed_block_windows": _merge_contiguous(red_buckets),
        "green_pnl": round(green_pnl, 2),
        "red_pnl": round(red_pnl, 2),
        "pnl_if_only_green": round(green_pnl, 2),
        "pnl_if_blocked_red": round(total_pnl - red_pnl, 2),
    }


# ────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────

def _worker(args: tuple) -> tuple[str, list[BucketRow], dict]:
    strategy, df_strat, min_n = args
    rows, proposal = analyze_strategy(strategy, df_strat, min_n_for_label=min_n)
    return strategy, rows, proposal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True,
                    help="Path to per-trade CSV (e.g. backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv)")
    ap.add_argument("--extra-trades", nargs="*", default=[],
                    help="Additional per-trade CSVs to concatenate (e.g. lab outputs for Phase 13 winners)")
    ap.add_argument("--out-dir", default="out/_baseline_2026-05-27",
                    help="Directory for output CSVs and markdown")
    ap.add_argument("--min-n", type=int, default=20,
                    help="Minimum n per bucket before labelling GREEN/RED (default 20)")
    ap.add_argument("--workers", type=int, default=8,
                    help="Pool size (default 8 = physical cores)")
    args = ap.parse_args()

    # Load + concat trade CSVs
    paths = [Path(args.trades)] + [Path(p) for p in args.extra_trades]
    frames = []
    for p in paths:
        full = (ROOT / p) if not Path(p).is_absolute() else Path(p)
        if not full.exists():
            print(f"[WARN] missing: {full}")
            continue
        df = pd.read_csv(full)
        print(f"[load] {full.name}: {len(df):,} rows, strategies={sorted(df['strategy'].unique())}")
        frames.append(df)
    if not frames:
        print("[ERROR] no trade data loaded")
        sys.exit(1)
    df_all = pd.concat(frames, ignore_index=True)
    print(f"[load] combined: {len(df_all):,} trades, {df_all['strategy'].nunique()} strategies")

    # Split per strategy for worker dispatch
    strategies = sorted(df_all["strategy"].unique())
    work_items = [(s, df_all[df_all["strategy"] == s].copy(), args.min_n) for s in strategies]

    # Run pool with spawn context
    ctx = mp.get_context("spawn")
    print(f"[pool] starting {args.workers} workers (spawn) for {len(work_items)} strategies")

    all_rows: list[BucketRow] = []
    all_proposals: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx) as ex:
        futures = {ex.submit(_worker, item): item[0] for item in work_items}
        for fut in as_completed(futures):
            sname = futures[fut]
            try:
                strategy, rows, proposal = fut.result()
                all_rows.extend(rows)
                all_proposals.append(proposal)
                print(f"[done] {strategy}: {len(rows)} bucket rows, "
                      f"{proposal['n_green_buckets']}G/{proposal['n_red_buckets']}R "
                      f"of {proposal['n_green_buckets']+proposal['n_red_buckets']+proposal['n_neutral_buckets']+proposal['n_sparse_buckets']}")
            except Exception as e:
                print(f"[ERROR] {sname}: {e!r}")

    # Write outputs
    out_dir = (ROOT / args.out_dir) if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    detail_path = out_dir / "hour_buckets_detail.csv"
    detail_df = pd.DataFrame([r.__dict__ for r in all_rows])
    detail_df = detail_df.sort_values(["strategy", "direction", "bucket_ct"]).reset_index(drop=True)
    detail_df.to_csv(detail_path, index=False)
    print(f"[write] {detail_path}: {len(detail_df)} rows")

    proposal_path = out_dir / "hour_buckets_proposal.csv"
    prop_rows = []
    for p in sorted(all_proposals, key=lambda x: x["strategy"]):
        prop_rows.append({
            "strategy": p["strategy"],
            "n_trades_total": p["n_trades_total"],
            "total_pnl": p["total_pnl"],
            "median_rr_overall": p["median_rr_overall"],
            "breakeven_wr_overall_pct": p["breakeven_wr_overall_pct"],
            "n_green_buckets": p["n_green_buckets"],
            "n_red_buckets": p["n_red_buckets"],
            "n_neutral_buckets": p["n_neutral_buckets"],
            "n_sparse_buckets": p["n_sparse_buckets"],
            "proposed_session_windows_ct": json.dumps(p["proposed_session_windows_ct"]),
            "proposed_block_windows": json.dumps(p["proposed_block_windows"]),
            "green_pnl": p["green_pnl"],
            "red_pnl": p["red_pnl"],
            "pnl_if_only_green": p["pnl_if_only_green"],
            "pnl_if_blocked_red": p["pnl_if_blocked_red"],
        })
    pd.DataFrame(prop_rows).to_csv(proposal_path, index=False)
    print(f"[write] {proposal_path}: {len(prop_rows)} strategies")

    # Markdown report
    md_path = out_dir / "hour_buckets_summary.md"
    lines = ["# Hour-by-Hour Timing Windows — 2026-05-27 baseline",
             "",
             "30-min CT buckets. GREEN = Wilson 95% lower bound on WR exceeds break-even for the strategy's median RR with n>=20. RED = Wilson 95% upper bound below break-even with n>=20. NEUTRAL = ambiguous. SPARSE = n<20.",
             "",
             "Proposed `session_windows_ct` (allow) and `block_windows` (deny) are direct merges of contiguous GREEN/RED buckets. These are proposals only — operator review required before any config change.",
             ""]
    for p in sorted(all_proposals, key=lambda x: x["strategy"]):
        lines.append(f"## {p['strategy']}")
        lines.append(f"- Trades: **{p['n_trades_total']}** | Total P&L: **${p['total_pnl']:,.0f}** | Median RR: **{p['median_rr_overall']}** | Break-even WR: **{p['breakeven_wr_overall_pct']}%**")
        lines.append(f"- Buckets: {p['n_green_buckets']} GREEN / {p['n_neutral_buckets']} NEUTRAL / {p['n_red_buckets']} RED / {p['n_sparse_buckets']} SPARSE")
        lines.append(f"- Proposed `session_windows_ct`: `{p['proposed_session_windows_ct']}`")
        lines.append(f"- Proposed `block_windows`: `{p['proposed_block_windows']}`")
        lines.append(f"- P&L breakdown: GREEN ${p['green_pnl']:,.0f}, RED ${p['red_pnl']:,.0f}")
        lines.append(f"- If only-green: ${p['pnl_if_only_green']:,.0f} | If blocked-red: ${p['pnl_if_blocked_red']:,.0f}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[write] {md_path}")

    print("[done] hour_buckets analysis complete")


if __name__ == "__main__":
    # On Windows spawn requires the entry guard
    mp.freeze_support()
    main()
