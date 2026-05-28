"""
Parallel wrapper around tools/phoenix_stop_target_optimizer.py.

Runs per-strategy `analyze_strategy()` in a ProcessPoolExecutor with the
'spawn' start method, one strategy per worker, max N workers (default 8 =
physical cores). Each worker loads the 1m MNQ bars ONCE in its initializer
and reuses them across all assigned strategies — avoids pickling the
1.77M-row DataFrame for each task.

Output schema matches backtest_results/phoenix_stop_target_recommendations.csv:
  strategy, n_trades, best_policy, best_total, best_wr_pct, best_pf,
  baseline_total, lift_vs_baseline, profitable, years_positive,
  mfe_mae_ratio, mfe_mean_ticks, mae_mean_ticks, first_entry, last_entry

Plus per-policy detail dump for every strategy (for Section 4 transparency):
  out/_baseline_2026-05-27/policy_sweep_per_strategy.csv

Usage:
    python tools/_baseline_2026-05-27/parallel_optimizer.py \
        --trades backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv \
        --extra-trades backtest_results/phoenix_new_strategy_lab.csv \
                       backtest_results/phoenix_trend_pullback_lab.csv \
        --out-dir out/_baseline_2026-05-27 \
        --workers 8
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# These are imported into the worker via spawn; the optimizer's heavy lifting
# (POLICIES list, compute_mfe_mae, analyze_strategy) lives there.
from tools import phoenix_stop_target_optimizer as opt  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Worker-side singleton state (avoids pickling the 1m bars per task)
# ────────────────────────────────────────────────────────────────────

_WORKER_MNQ_1M: pd.DataFrame | None = None


def _worker_init(mnq_1m_csv: str) -> None:
    """Each worker loads MNQ 1m bars once and reuses for every assigned strategy."""
    global _WORKER_MNQ_1M
    print(f"[worker-{mp.current_process().pid}] loading {mnq_1m_csv}", flush=True)
    df = pd.read_csv(mnq_1m_csv)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.set_index("ts").sort_index()
    _WORKER_MNQ_1M = df[["open", "high", "low", "close", "volume"]]
    print(f"[worker-{mp.current_process().pid}] {_WORKER_MNQ_1M.shape[0]:,} bars loaded", flush=True)


def _worker_analyze(strat_name: str, trades_pickled: bytes) -> dict | None:
    """Analyze a single strategy. trades_pickled is the per-strategy slice."""
    global _WORKER_MNQ_1M
    import pickle
    trades_df = pickle.loads(trades_pickled)
    t0 = time.time()
    result = opt.analyze_strategy(strat_name, trades_df, _WORKER_MNQ_1M)
    if result is not None:
        result["_elapsed_sec"] = round(time.time() - t0, 1)
    return result


# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True,
                    help="Primary per-trade CSV (fresh 5y)")
    ap.add_argument("--extra-trades", nargs="*", default=[],
                    help="Additional per-trade CSVs (e.g. lab outputs)")
    ap.add_argument("--mnq-1m",
                    default="data/historical/mnq_1min_databento.csv",
                    help="Path to MNQ 1m bars CSV")
    ap.add_argument("--out-dir", default="out/_baseline_2026-05-27",
                    help="Output directory")
    ap.add_argument("--workers", type=int, default=8,
                    help="Pool size (default 8)")
    ap.add_argument("--strategies", default=None,
                    help="Comma-separated subset; default = all WINNERS in optimizer")
    args = ap.parse_args()

    # Resolve paths
    mnq_1m_path = ROOT / args.mnq_1m if not Path(args.mnq_1m).is_absolute() else Path(args.mnq_1m)
    out_dir = ROOT / args.out_dir if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load + concat trade CSVs
    paths = [args.trades] + list(args.extra_trades)
    frames = []
    for p in paths:
        full = ROOT / p if not Path(p).is_absolute() else Path(p)
        if not full.exists():
            print(f"[WARN] missing trade CSV: {full}")
            continue
        df = pd.read_csv(full)
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
        print(f"[load] {full.name}: {len(df):,} trades, strategies={sorted(df['strategy'].unique())}")
        frames.append(df)
    if not frames:
        print("[ERROR] no trade data loaded")
        sys.exit(1)
    df_all = pd.concat(frames, ignore_index=True)
    print(f"[load] combined: {len(df_all):,} trades, {df_all['strategy'].nunique()} strategies")

    # Determine strategies to analyze
    if args.strategies:
        strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    else:
        # Use the optimizer's canonical WINNERS list intersected with what we have
        strategies = [s for s in opt.WINNERS if s in df_all["strategy"].unique()]
        # Plus any extra strategies present in the combined data (covers backfills)
        extras = sorted(set(df_all["strategy"].unique()) - set(strategies))
        if extras:
            print(f"[scope] extending WINNERS with {len(extras)} extras present in data: {extras}")
            strategies.extend(extras)

    print(f"[scope] {len(strategies)} strategies queued: {strategies}")

    # Pickle per-strategy slices (faster than re-slicing in workers)
    import pickle
    work_items = []
    for s in strategies:
        s_df = df_all[df_all["strategy"] == s].copy()
        if len(s_df) == 0:
            print(f"[skip] {s}: 0 trades")
            continue
        work_items.append((s, pickle.dumps(s_df)))

    # Run pool
    ctx = mp.get_context("spawn")
    print(f"[pool] starting {args.workers} workers (spawn) for {len(work_items)} strategies")
    t_pool_start = time.time()

    analyses: dict[str, dict] = {}
    with ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=ctx,
        initializer=_worker_init,
        initargs=(str(mnq_1m_path),),
    ) as ex:
        futures = {
            ex.submit(_worker_analyze, sname, pkl): sname
            for sname, pkl in work_items
        }
        for fut in as_completed(futures):
            sname = futures[fut]
            try:
                result = fut.result()
                if result:
                    analyses[sname] = result
                    print(f"[done] {sname}: n={result['n_trades']} "
                          f"elapsed={result.get('_elapsed_sec', '?')}s")
            except Exception as e:
                print(f"[ERROR] {sname}: {e!r}")

    print(f"[pool] all done in {time.time() - t_pool_start:.1f}s")

    # ── Write recommendations CSV (matches existing schema) ──
    rec_rows = []
    detail_rows = []
    for sname, a in sorted(analyses.items()):
        best_pname, best = opt.recommend_best_policy(a, exclude_oracle=True)
        baseline = a["policy_results"].get("baseline") or a["policy_results"].get(list(a["policy_results"].keys())[0])
        baseline_total = baseline["total"] if baseline else 0.0
        lift = (best["total"] - baseline_total) if best else 0.0

        rec_rows.append({
            "strategy": sname,
            "n_trades": a["n_trades"],
            "best_policy": best_pname,
            "best_total": best["total"] if best else None,
            "best_wr_pct": best["wr_pct"] if best else None,
            "best_pf": best["pf"] if best else None,
            "baseline_total": baseline_total,
            "lift_vs_baseline": lift,
            "profitable": "YES" if best and best["total"] > 0 else "NO",
            "years_positive": best["years_positive"] if best else None,
            "mfe_mae_ratio": a["mfe_mae_ratio"],
            "mfe_mean_ticks": a["mfe_mean_ticks"],
            "mae_mean_ticks": a["mae_mean_ticks"],
            "first_entry": str(a["first_entry"])[:10] if a.get("first_entry") is not None else None,
            "last_entry": str(a["last_entry"])[:10] if a.get("last_entry") is not None else None,
        })

        # Per-policy detail (one row per policy, for Section 4 transparency)
        for pname, pdata in a["policy_results"].items():
            detail_rows.append({
                "strategy": sname,
                "policy": pname,
                "n": pdata["n"],
                "wr_pct": pdata["wr_pct"],
                "total": pdata["total"],
                "avg": pdata["avg"],
                "pf": pdata["pf"],
                "years_positive": pdata["years_positive"],
            })

    rec_path = out_dir / f"phoenix_stop_target_recommendations_2026-05-27.csv"
    pd.DataFrame(rec_rows).to_csv(rec_path, index=False)
    print(f"[write] {rec_path}: {len(rec_rows)} strategies")

    detail_path = out_dir / "policy_sweep_per_strategy.csv"
    pd.DataFrame(detail_rows).sort_values(["strategy", "total"], ascending=[True, False]).to_csv(detail_path, index=False)
    print(f"[write] {detail_path}: {len(detail_rows)} (strategy, policy) cells")

    # MFE/MAE summary table separately (Section 3 deliverable b)
    mae_rows = []
    for sname, a in sorted(analyses.items()):
        mae_rows.append({
            "strategy": sname,
            "n_trades": a["n_trades"],
            "mfe_mean_ticks": a["mfe_mean_ticks"],
            "mfe_p50": a["mfe_p50"],
            "mfe_p75": a["mfe_p75"],
            "mae_mean_ticks": a["mae_mean_ticks"],
            "mae_p50": a["mae_p50"],
            "mae_p75": a["mae_p75"],
            "mfe_mae_ratio": a["mfe_mae_ratio"],
        })
    mae_path = out_dir / "mfe_mae_per_strategy.csv"
    pd.DataFrame(mae_rows).to_csv(mae_path, index=False)
    print(f"[write] {mae_path}: {len(mae_rows)} strategies")

    print("[done] parallel optimizer complete")


if __name__ == "__main__":
    mp.freeze_support()
    main()
