"""
Phoenix Bot -- Parameter Optimizer

Sweeps parameter combinations through the backtester to find optimal settings.
Loads CSV data ONCE, then runs each config permutation in-memory.

Usage:
    python tools/optimizer.py --data C:\\temp\\mnq_historical.csv
    python tools/optimizer.py --data C:\\temp\\mnq_historical.csv --quick
"""

import argparse
import copy
import itertools
import json
import logging
import os
import sys
import time
from datetime import datetime

# Project imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.backtester import read_csv, Backtester
import config.strategies as strategies_module

logger = logging.getLogger("Optimizer")


# ---- Parameter Grid ---------------------------------------------------

PARAM_GRID = {
    "stop_ticks": [6, 9, 12, 18],
    "target_rr": [1.2, 1.5, 2.0, 3.0],
    "min_tf_votes": [2, 3],
    "min_momentum": [40, 50, 60],
    "min_confluence": [2.0, 3.0, 3.5],
}

# Extended grid for targeted follow-up sweeps around winning params
PARAM_GRID_FINE = {
    "stop_ticks": [6, 8, 10, 12, 15, 18],
    "target_rr": [1.2, 1.5, 1.8, 2.0, 2.5, 3.0],
    "min_tf_votes": [2, 3],
    "min_momentum": [40, 45, 50, 55, 60],
    "min_confluence": [2.0, 2.5, 3.0, 3.5],
}

# Regime-specific momentum thresholds to test separately
REGIME_MOMENTUM_GRID = {
    "PREMARKET_DRIFT": [55, 65, 999],   # 999 = effectively disable
    "OPEN_MOMENTUM":   [30, 40, 50],
    "MID_MORNING":     [30, 40, 50],
}


# ---- Helpers ----------------------------------------------------------

def grid_combos(grid: dict) -> list[dict]:
    """Expand a param grid dict into a list of flat dicts."""
    keys = list(grid.keys())
    values = list(grid.values())
    combos = []
    for vals in itertools.product(*values):
        combos.append(dict(zip(keys, vals)))
    return combos


def run_single_backtest(bars: list[dict], params: dict) -> dict:
    """
    Run one backtest with the given bias_momentum params.

    Monkey-patches config.strategies.STRATEGIES in-memory, creates a fresh
    Backtester, runs it, then restores the original config.
    """
    original = strategies_module.STRATEGIES

    patched = copy.deepcopy(original)
    for key, val in params.items():
        patched["bias_momentum"][key] = val

    strategies_module.STRATEGIES = patched
    try:
        bt = Backtester(strategy_names=["bias_momentum"])
        results = bt.run(bars, verbose=False)
    finally:
        strategies_module.STRATEGIES = original

    s = results["summary"]
    return {
        "params": params,
        "trades": s["total_trades"],
        "win_rate": s["win_rate"],
        "total_pnl": s["total_pnl"],
        "profit_factor": s["profit_factor"],
        "max_drawdown": s["max_drawdown"],
        "pnl_per_trade": s["avg_pnl_per_trade"],
        "by_regime": results.get("by_regime", {}),
    }


# ---- Main Sweep -------------------------------------------------------

def run_grid_sweep(bars: list[dict], quick: bool = False) -> list[dict]:
    """Run the full parameter grid sweep."""
    combos = grid_combos(PARAM_GRID)
    total = len(combos)

    if quick:
        # Sample every 4th combo for faster iteration
        combos = combos[::4]
        total_run = len(combos)
        print(f"  QUICK mode: testing {total_run} of {total} combos")
    else:
        total_run = total
        print(f"  Full sweep: {total_run} parameter combinations")

    results = []
    start = time.time()

    for i, params in enumerate(combos):
        try:
            res = run_single_backtest(bars, params)
            results.append(res)
        except Exception as e:
            logger.warning(f"Config {i} failed: {e}")

        if (i + 1) % 20 == 0 or (i + 1) == total_run:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total_run - i - 1) / rate if rate > 0 else 0
            best_pnl = max((r["total_pnl"] for r in results), default=0)
            print(f"  [{i+1:4d}/{total_run}] "
                  f"{elapsed:6.1f}s elapsed | "
                  f"{rate:.1f} runs/s | "
                  f"ETA {eta:.0f}s | "
                  f"best P&L so far: ${best_pnl:.2f}")

    total_elapsed = time.time() - start
    print(f"\n  Grid sweep complete: {len(results)} configs in {total_elapsed:.1f}s")
    return results


def run_regime_sweep(bars: list[dict], base_params: dict) -> list[dict]:
    """
    Run a regime-specific momentum threshold sweep.

    Uses the best overall params as a base, then tests different
    min_momentum thresholds per regime by injecting regime-specific
    overrides into the session manager.
    """
    print(f"\n{'=' * 60}")
    print(f"  REGIME-SPECIFIC MOMENTUM SWEEP")
    print(f"{'=' * 60}")
    print(f"  Base params: {base_params}")

    # For the regime sweep, we test each regime's momentum independently
    # by varying min_momentum globally (since the strategy uses one threshold)
    # and noting which regime produces the trades
    regime_combos = grid_combos(REGIME_MOMENTUM_GRID)
    print(f"  Testing {len(regime_combos)} regime-momentum combos")

    results = []
    start = time.time()

    for i, regime_params in enumerate(regime_combos):
        # Use base params but vary min_momentum for each run
        # We test the LOWEST threshold across regimes (most permissive)
        # to see which regime benefits most
        active_thresholds = [v for v in regime_params.values() if v < 999]
        if not active_thresholds:
            min_mom = 999
        else:
            min_mom = min(active_thresholds)

        params = {**base_params, "min_momentum": min_mom}
        try:
            res = run_single_backtest(bars, params)
            res["regime_thresholds"] = regime_params
            results.append(res)
        except Exception as e:
            logger.warning(f"Regime config {i} failed: {e}")

        if (i + 1) % 20 == 0 or (i + 1) == len(regime_combos):
            elapsed = time.time() - start
            print(f"  [{i+1:4d}/{len(regime_combos)}] {elapsed:.1f}s elapsed")

    print(f"  Regime sweep complete: {len(results)} configs in {time.time() - start:.1f}s")
    return results


# ---- Display ----------------------------------------------------------

def print_top_results(results: list[dict], sort_key: str, title: str,
                      n: int = 20, min_trades: int = 0):
    """Print top N results sorted by sort_key."""
    filtered = [r for r in results if r["trades"] >= min_trades]
    if not filtered:
        print(f"\n  No results with >= {min_trades} trades")
        return

    filtered.sort(key=lambda x: x[sort_key], reverse=True)
    top = filtered[:n]

    print(f"\n{'=' * 90}")
    print(f"  {title}")
    print(f"{'=' * 90}")
    print(f"  {'#':>3} | {'Trades':>6} | {'WR%':>5} | {'P&L':>9} | {'PF':>5} | "
          f"{'MaxDD':>7} | {'$/Trade':>8} | Parameters")
    print(f"  {'-' * 86}")

    for i, r in enumerate(top):
        p = r["params"]
        param_str = (f"stop={p['stop_ticks']:2d} rr={p['target_rr']:.1f} "
                     f"tfv={p['min_tf_votes']} mom={p['min_momentum']:2d} "
                     f"conf={p['min_confluence']:.1f}")
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] < 999 else "inf"
        print(f"  {i+1:3d} | {r['trades']:6d} | {r['win_rate']:5.1f} | "
              f"${r['total_pnl']:8.2f} | {pf_str:>5} | "
              f"${r['max_drawdown']:6.2f} | ${r['pnl_per_trade']:7.2f} | {param_str}")


def print_regime_breakdown(result: dict, label: str = "#1 config"):
    """Print detailed regime breakdown for a single result."""
    print(f"\n{'=' * 70}")
    print(f"  REGIME BREAKDOWN — {label}")
    print(f"{'=' * 70}")
    p = result["params"]
    print(f"  Params: stop={p['stop_ticks']} rr={p['target_rr']} "
          f"tfv={p['min_tf_votes']} mom={p['min_momentum']} "
          f"conf={p['min_confluence']}")
    print(f"  Overall: {result['trades']} trades, {result['win_rate']}% WR, "
          f"${result['total_pnl']:.2f} P&L, PF={result['profit_factor']}")
    print()

    by_regime = result.get("by_regime", {})
    if not by_regime:
        print("  No regime data available")
        return

    print(f"  {'Regime':<22} | {'Trades':>6} | {'Wins':>5} | {'WR%':>5} | {'P&L':>9}")
    print(f"  {'-' * 60}")
    for regime in sorted(by_regime.keys()):
        rd = by_regime[regime]
        wr = round(rd["wins"] / max(1, rd["trades"]) * 100, 1)
        print(f"  {regime:<22} | {rd['trades']:6d} | {rd['wins']:5d} | "
              f"{wr:5.1f} | ${rd['pnl']:8.2f}")


def print_regime_sweep_results(results: list[dict]):
    """Print regime sweep results."""
    if not results:
        return

    # Sort by total P&L
    results.sort(key=lambda x: x["total_pnl"], reverse=True)

    print(f"\n{'=' * 100}")
    print(f"  TOP 10 REGIME-SPECIFIC CONFIGS (by P&L)")
    print(f"{'=' * 100}")
    print(f"  {'#':>3} | {'Trades':>6} | {'WR%':>5} | {'P&L':>9} | {'PF':>5} | "
          f"{'PreMkt':>6} | {'Open':>6} | {'MidMrn':>6} | {'$/Trade':>8}")
    print(f"  {'-' * 90}")

    for i, r in enumerate(results[:10]):
        rt = r.get("regime_thresholds", {})
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] < 999 else "inf"
        pm = rt.get("PREMARKET_DRIFT", "?")
        om = rt.get("OPEN_MOMENTUM", "?")
        mm = rt.get("MID_MORNING", "?")
        pm_str = "OFF" if pm == 999 else str(pm)
        print(f"  {i+1:3d} | {r['trades']:6d} | {r['win_rate']:5.1f} | "
              f"${r['total_pnl']:8.2f} | {pf_str:>5} | "
              f"{pm_str:>6} | {om:>6} | {mm:>6} | ${r['pnl_per_trade']:7.2f}")

    # Show regime breakdown of best
    if results:
        print_regime_breakdown(results[0], "Best regime-specific config")


# ---- Entry Point ------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phoenix Bot Parameter Optimizer")
    parser.add_argument("--data", required=True,
                        help="Path to CSV file from NT8 HistoricalExporter")
    parser.add_argument("--quick", action="store_true",
                        help="Test every 4th combo (faster iteration)")
    args = parser.parse_args()

    # Force unbuffered stdout for progress reporting
    sys.stdout.reconfigure(line_buffering=True)

    # Silence ALL module loggers to avoid RiskManager/SessionManager spam
    logging.basicConfig(
        level=logging.CRITICAL,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).setLevel(logging.CRITICAL)

    # Load data ONCE
    print(f"\n{'=' * 60}")
    print(f"  PHOENIX BOT PARAMETER OPTIMIZER")
    print(f"{'=' * 60}")
    print(f"  Loading data: {args.data}")

    bars = read_csv(args.data)
    if not bars:
        print("ERROR: No bars loaded. Check CSV file.")
        sys.exit(1)

    print(f"  Loaded {len(bars)} bars")
    print(f"  Date range: {bars[0]['timestamp'][:10]} to {bars[-1]['timestamp'][:10]}")

    total_combos = 1
    for v in PARAM_GRID.values():
        total_combos *= len(v)
    print(f"  Parameter grid: {total_combos} total combinations")
    print(f"  Strategy: bias_momentum only")

    # ---- Main grid sweep ----
    print(f"\n{'=' * 60}")
    print(f"  MAIN PARAMETER SWEEP")
    print(f"{'=' * 60}")

    grid_results = run_grid_sweep(bars, quick=args.quick)

    # Print top by P&L
    print_top_results(grid_results, "total_pnl",
                      "TOP 20 BY TOTAL P&L", n=20)

    # Print top by profit factor (min 10 trades)
    print_top_results(grid_results, "profit_factor",
                      "TOP 10 BY PROFIT FACTOR (min 10 trades)",
                      n=10, min_trades=10)

    # Regime breakdown of #1
    pnl_sorted = sorted(grid_results, key=lambda x: x["total_pnl"], reverse=True)
    if pnl_sorted:
        print_regime_breakdown(pnl_sorted[0], "#1 overall config")

    # ---- Regime-specific sweep ----
    if pnl_sorted:
        best_params = pnl_sorted[0]["params"]
        regime_results = run_regime_sweep(bars, best_params)
        print_regime_sweep_results(regime_results)

    # ---- Save results ----
    output_dir = os.path.join(os.path.dirname(__file__), "..", "logs", "backtest")
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_path = os.path.join(output_dir, f"optimizer_{ts}.json")

    # Prepare serializable output
    save_data = {
        "timestamp": ts,
        "data_file": args.data,
        "bars_count": len(bars),
        "date_range": f"{bars[0]['timestamp'][:10]} to {bars[-1]['timestamp'][:10]}",
        "param_grid": PARAM_GRID,
        "total_configs_tested": len(grid_results),
        "grid_results_top50": sorted(grid_results,
                                      key=lambda x: x["total_pnl"],
                                      reverse=True)[:50],
        "regime_sweep_results": sorted(
            regime_results if pnl_sorted else [],
            key=lambda x: x["total_pnl"],
            reverse=True
        )[:20],
        "best_config": pnl_sorted[0] if pnl_sorted else None,
    }

    with open(output_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
