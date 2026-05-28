"""
Multi-timeframe trend-filter overlay analysis.

Per operator scope (Section 1 of expanded blueprint):
  - Strategies run on their native 1m / 5m timeframe (no code change here)
  - Test whether ALIGNMENT with higher-TF trend (5m, 15m, 1h, 4h) improves
    per-strategy expectancy
  - Higher TFs derived in main process via pandas resample, broadcast to
    workers via spawn init

For each (strategy, higher_tf):
  Per trade, look up the most-recently-CLOSED higher-TF bar before
  entry_ts and read EMA9/EMA21 stack + bar-direction.
  Tag trade as 'aligned' (trade.direction == htf_trend) or 'counter'.
  Aggregate: WR, total $, PF, expectancy.

Output: out/_baseline_2026-05-27/multi_tf_overlay.csv
        out/_baseline_2026-05-27/multi_tf_winners.csv
        out/_baseline_2026-05-27/multi_tf_summary.md

Worker pool: 8 spawn workers, one strategy each; higher-TF frames preloaded
into worker globals in initializer.
"""
from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent

# Higher timeframes to test
HIGHER_TFS = ["5min", "15min", "30min", "1h", "4h"]
EMA_SHORT = 9
EMA_LONG = 21


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def resample_and_indicators(mnq_1m: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample 1m bars to TF; compute EMA9/21 + bar direction.
    Returns df indexed by bar CLOSE timestamp (UTC) with cols:
      open, high, low, close, ema9, ema21, ema_diff, ema_trend (-1/0/+1),
      bar_dir (-1/+1).
    """
    o = mnq_1m["open"].resample(tf, label="right", closed="right").first()
    h = mnq_1m["high"].resample(tf, label="right", closed="right").max()
    l = mnq_1m["low"].resample(tf, label="right", closed="right").min()
    c = mnq_1m["close"].resample(tf, label="right", closed="right").last()
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}).dropna()
    df["ema9"] = _ema(df["close"], EMA_SHORT)
    df["ema21"] = _ema(df["close"], EMA_LONG)
    df["ema_diff"] = df["ema9"] - df["ema21"]
    df["ema_trend"] = np.sign(df["ema_diff"]).astype(int)
    df["bar_dir"] = np.sign(df["close"] - df["open"]).astype(int)
    return df


# ────────────────────────────────────────────────────────────────────
# Worker globals (preloaded via initializer)
# ────────────────────────────────────────────────────────────────────

_WORKER_HTF: dict[str, pd.DataFrame] = {}


def _worker_init(htf_pickled: bytes) -> None:
    global _WORKER_HTF
    _WORKER_HTF = pickle.loads(htf_pickled)
    print(f"[worker-{mp.current_process().pid}] HTFs loaded: "
          f"{ {k: len(v) for k, v in _WORKER_HTF.items()} }", flush=True)


def _classify_trade(entry_ts: pd.Timestamp, direction: str, htf_df: pd.DataFrame) -> dict:
    """Look up the most-recent CLOSED higher-TF bar BEFORE entry_ts.
    Returns dict with htf_trend (-1/0/+1), htf_bar_dir (-1/+1), aligned (bool).
    """
    # Use searchsorted (LEFT side -> strictly before entry_ts)
    idx = htf_df.index.searchsorted(entry_ts, side="left") - 1
    if idx < 0:
        return {"htf_trend": None, "htf_bar_dir": None, "aligned": None}
    row = htf_df.iloc[idx]
    trend = int(row["ema_trend"])
    bar_dir = int(row["bar_dir"])
    trade_dir_sign = 1 if direction == "LONG" else -1
    aligned = (trend != 0) and (np.sign(trend) == trade_dir_sign)
    return {"htf_trend": trend, "htf_bar_dir": bar_dir, "aligned": bool(aligned)}


def _wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - s), min(1.0, c + s))


def _worker_strategy(args: tuple) -> dict:
    strat_name, trades_pickled = args
    trades = pickle.loads(trades_pickled)
    trades = trades.copy()
    trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)

    results = []
    for tf, htf_df in _WORKER_HTF.items():
        # Classify each trade against this HTF
        classes = trades.apply(
            lambda r: _classify_trade(r["entry_ts"], r["direction"], htf_df),
            axis=1, result_type="expand"
        )
        ctrades = pd.concat([trades.reset_index(drop=True), classes.reset_index(drop=True)], axis=1)

        for align_key in [True, False, None]:
            subset = ctrades[ctrades["aligned"] == align_key] if align_key is not None else ctrades[ctrades["aligned"].isna()]
            n = len(subset)
            if n == 0:
                continue
            wins = int((subset["pnl_dollars"] > 0).sum())
            wr = wins / n
            total = float(subset["pnl_dollars"].sum())
            avg = total / n
            gross_win = float(subset.loc[subset["pnl_dollars"] > 0, "pnl_dollars"].sum())
            gross_loss = -float(subset.loc[subset["pnl_dollars"] < 0, "pnl_dollars"].sum())
            pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
            lo, hi = _wilson(wins, n)
            results.append({
                "strategy": strat_name,
                "higher_tf": tf,
                "alignment": "ALIGNED" if align_key is True else ("COUNTER" if align_key is False else "NO_HTF"),
                "n": n,
                "wr_pct": round(100 * wr, 2),
                "wilson_lo_pct": round(100 * lo, 2),
                "wilson_hi_pct": round(100 * hi, 2),
                "total_dollars": round(total, 2),
                "avg_dollars": round(avg, 2),
                "pf": round(pf, 2) if not math.isinf(pf) else 99.0,
            })

        # Baseline (no filter, all trades)
        n = len(ctrades)
        wins = int((ctrades["pnl_dollars"] > 0).sum())
        total = float(ctrades["pnl_dollars"].sum())
        gross_win = float(ctrades.loc[ctrades["pnl_dollars"] > 0, "pnl_dollars"].sum())
        gross_loss = -float(ctrades.loc[ctrades["pnl_dollars"] < 0, "pnl_dollars"].sum())
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
        lo, hi = _wilson(wins, n)
        results.append({
            "strategy": strat_name,
            "higher_tf": tf,
            "alignment": "BASELINE",
            "n": n,
            "wr_pct": round(100 * wins / n, 2) if n else 0,
            "wilson_lo_pct": round(100 * lo, 2),
            "wilson_hi_pct": round(100 * hi, 2),
            "total_dollars": round(total, 2),
            "avg_dollars": round(total / n, 2) if n else 0,
            "pf": round(pf, 2) if not math.isinf(pf) else 99.0,
        })

    return {"strategy": strat_name, "rows": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True)
    ap.add_argument("--extra-trades", nargs="*", default=[])
    ap.add_argument("--mnq-1m", default="data/historical/mnq_1min_databento.csv")
    ap.add_argument("--out-dir", default="out/_baseline_2026-05-27")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    # Load + resample 1m bars once in main
    mnq_path = ROOT / args.mnq_1m if not Path(args.mnq_1m).is_absolute() else Path(args.mnq_1m)
    print(f"[load] {mnq_path}")
    mnq_1m = pd.read_csv(mnq_path)
    mnq_1m["ts"] = pd.to_datetime(mnq_1m["ts_utc"], utc=True)
    mnq_1m = mnq_1m.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]]
    print(f"[load] {len(mnq_1m):,} 1m bars")

    print(f"[resample] computing HTFs: {HIGHER_TFS}")
    t0 = time.time()
    htf_map: dict[str, pd.DataFrame] = {}
    for tf in HIGHER_TFS:
        htf_map[tf] = resample_and_indicators(mnq_1m, tf)
        print(f"  {tf}: {len(htf_map[tf]):,} bars  cols={list(htf_map[tf].columns)}")
    print(f"[resample] done in {time.time() - t0:.1f}s")

    # Load + concat per-trade CSVs
    paths = [args.trades] + list(args.extra_trades)
    frames = []
    for p in paths:
        full = ROOT / p if not Path(p).is_absolute() else Path(p)
        if not full.exists():
            print(f"[WARN] missing: {full}")
            continue
        df = pd.read_csv(full)
        frames.append(df)
    if not frames:
        print("[ERROR] no trade data loaded")
        sys.exit(1)
    df_all = pd.concat(frames, ignore_index=True)
    print(f"[load] {len(df_all):,} trades, {df_all['strategy'].nunique()} strategies")

    strategies = sorted(df_all["strategy"].unique())
    work_items = [(s, pickle.dumps(df_all[df_all["strategy"] == s].copy())) for s in strategies]

    # Pickle the HTF map once for worker init
    htf_pickled = pickle.dumps(htf_map)
    print(f"[init] HTF pickle = {len(htf_pickled) / 1024 / 1024:.1f} MB")

    ctx = mp.get_context("spawn")
    print(f"[pool] {args.workers} workers (spawn) for {len(work_items)} strategies")

    all_rows = []
    with ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=ctx,
        initializer=_worker_init,
        initargs=(htf_pickled,),
    ) as ex:
        futures = {ex.submit(_worker_strategy, item): item[0] for item in work_items}
        for fut in as_completed(futures):
            sname = futures[fut]
            try:
                r = fut.result()
                all_rows.extend(r["rows"])
                print(f"[done] {sname}: {len(r['rows'])} (tf,alignment) cells")
            except Exception as e:
                print(f"[ERROR] {sname}: {e!r}")

    out_dir = ROOT / args.out_dir if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    detail = pd.DataFrame(all_rows).sort_values(["strategy", "higher_tf", "alignment"]).reset_index(drop=True)
    detail_path = out_dir / "multi_tf_overlay.csv"
    detail.to_csv(detail_path, index=False)
    print(f"[write] {detail_path}: {len(detail)} rows")

    # Pick per-strategy best HTF filter (lift in $/trade for ALIGNED vs BASELINE)
    winners = []
    for sname in detail["strategy"].unique():
        sdf = detail[detail["strategy"] == sname]
        for tf in HIGHER_TFS:
            baseline = sdf[(sdf["higher_tf"] == tf) & (sdf["alignment"] == "BASELINE")]
            aligned = sdf[(sdf["higher_tf"] == tf) & (sdf["alignment"] == "ALIGNED")]
            if baseline.empty or aligned.empty:
                continue
            b_avg = baseline.iloc[0]["avg_dollars"]
            a_avg = aligned.iloc[0]["avg_dollars"]
            lift = a_avg - b_avg
            winners.append({
                "strategy": sname,
                "higher_tf": tf,
                "n_baseline": int(baseline.iloc[0]["n"]),
                "n_aligned": int(aligned.iloc[0]["n"]),
                "baseline_avg_$": b_avg,
                "aligned_avg_$": a_avg,
                "lift_per_trade_$": round(lift, 2),
                "aligned_total_$": aligned.iloc[0]["total_dollars"],
                "aligned_wr_pct": aligned.iloc[0]["wr_pct"],
                "aligned_pf": aligned.iloc[0]["pf"],
            })

    winners_df = pd.DataFrame(winners).sort_values(["strategy", "lift_per_trade_$"], ascending=[True, False])
    winners_path = out_dir / "multi_tf_winners.csv"
    winners_df.to_csv(winners_path, index=False)
    print(f"[write] {winners_path}: {len(winners_df)} rows")

    # Markdown summary: per-strategy best HTF filter
    md = ["# Multi-Timeframe Trend Filter — 2026-05-27 baseline",
          "",
          "For each strategy, the higher-TF that, when EMA9>EMA21 (LONG) or "
          "EMA9<EMA21 (SHORT) at trade entry, provides the largest $/trade lift "
          "over taking ALL trades regardless of higher-TF trend.",
          "",
          "ALIGNED = trade direction matches higher-TF EMA9/21 trend direction. "
          "Higher TFs tested: " + ", ".join(HIGHER_TFS) + ". EMA9/21 on close.",
          ""]
    for sname in sorted(winners_df["strategy"].unique()):
        sdf = winners_df[winners_df["strategy"] == sname].sort_values("lift_per_trade_$", ascending=False)
        if sdf.empty:
            continue
        best = sdf.iloc[0]
        md.append(f"## {sname}")
        md.append(f"- Best HTF filter: **{best['higher_tf']}** "
                  f"(lift = ${best['lift_per_trade_$']:+.2f}/trade)")
        md.append(f"- Aligned: n={int(best['n_aligned'])} of {int(best['n_baseline'])} | "
                  f"WR={best['aligned_wr_pct']}% | PF={best['aligned_pf']} | "
                  f"total ${best['aligned_total_$']:,.0f}")
        md.append("- All TFs ranked by lift/trade:")
        for _, r in sdf.iterrows():
            md.append(f"  - {r['higher_tf']}: lift=${r['lift_per_trade_$']:+.2f}, "
                      f"aligned n={int(r['n_aligned'])}, total ${r['aligned_total_$']:,.0f}")
        md.append("")

    md_path = out_dir / "multi_tf_summary.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"[write] {md_path}")

    print("[done] multi-tf lab complete")


if __name__ == "__main__":
    mp.freeze_support()
    main()
