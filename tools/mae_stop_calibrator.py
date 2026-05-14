"""MAE-calibrated initial stop recommender (#17, 2026-05-13).

Framework + tool. Once 30+ post-#2 trades accumulate per strategy
(MAE tracking landed 2026-05-13), this tool will surface a recommended
initial stop_ticks per strategy based on the empirical MAE distribution
of WINNING trades.

Sweeney's Maximum Adverse Excursion methodology (Campaign Trading, 1996):
- Compute MAE of every winning trade in tick units.
- The 75th-percentile MAE of winners is the "edge of the storm" — a
  stop just outside it would have held all those winners.
- The 95th-percentile MAE of winners is the "outlier stop" — wider but
  safer for tail-heavy strategies.

This tool DOES NOT auto-apply recommendations. The operator reviews the
output, decides which strategies have enough data, and edits config/
strategies.py manually. Auto-tuning the stops introduces a feedback
loop with the strategy itself — that's a separate project.

Usage:
    python tools/mae_stop_calibrator.py
    python tools/mae_stop_calibrator.py --min-trades 30 --strategy bias_momentum
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _percentile(values: list[float], p: float) -> float:
    """Linear-interp percentile. Copied from validation_tracker — kept
    duplicated here so this tool stands alone."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    rank = (p / 100.0) * (n - 1)
    lo = int(rank); hi = min(lo + 1, n - 1)
    frac = rank - lo
    return float(s[lo] * (1 - frac) + s[hi] * frac)


def collect_winner_maes(trades: list[dict]) -> dict[str, list[float]]:
    """Group winning trades' MAE-in-ticks by strategy.

    Only counts trades that:
      1. Have a non-None mae_ticks (post-#2, 2026-05-13)
      2. Are winners (pnl_dollars_net > 0)
    """
    out: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        mae = t.get("mae_ticks")
        if mae is None:
            continue
        pnl = float(
            t.get("pnl_dollars_net", t.get("pnl_dollars", 0.0)) or 0.0
        )
        if pnl <= 0:
            continue
        strat = t.get("strategy", "unknown")
        out[strat].append(float(mae))
    return out


def recommend_stop(winner_maes: list[float], buffer_pct: float = 20.0) -> dict:
    """Compute recommended stop from a strategy's winning-trade MAEs.

    Returns:
      n_winners        : sample size
      mae_median       : 50th percentile MAE of winners
      mae_p75          : 75th percentile MAE of winners
      mae_p95          : 95th percentile MAE of winners
      recommended      : ceil(p75 * (1 + buffer_pct/100))
      conservative     : ceil(p95 * (1 + buffer_pct/100))
      confidence       : "INSUFFICIENT" (<30), "LOW" (<50), "OK" (50+),
                         "HIGH" (200+)
    """
    n = len(winner_maes)
    if n == 0:
        return {"n_winners": 0, "confidence": "INSUFFICIENT"}
    import math
    p50 = _percentile(winner_maes, 50)
    p75 = _percentile(winner_maes, 75)
    p95 = _percentile(winner_maes, 95)
    buffer = 1.0 + (buffer_pct / 100.0)
    if n < 30:
        confidence = "INSUFFICIENT"
    elif n < 50:
        confidence = "LOW"
    elif n < 200:
        confidence = "OK"
    else:
        confidence = "HIGH"
    return {
        "n_winners": n,
        "mae_median": round(p50, 1),
        "mae_p75": round(p75, 1),
        "mae_p95": round(p95, 1),
        "recommended": math.ceil(p75 * buffer),
        "conservative": math.ceil(p95 * buffer),
        "confidence": confidence,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-trades", type=int, default=30,
                    help="Suppress recommendations for strategies with "
                         "fewer than N winning trades (default 30 = "
                         "INSUFFICIENT_SAMPLE tier).")
    ap.add_argument("--strategy",
                    help="Only emit for this strategy name.")
    ap.add_argument("--buffer-pct", type=float, default=20.0,
                    help="Pad the recommended stop by this percent "
                         "above the empirical MAE percentile (default 20).")
    args = ap.parse_args()

    from tools.validation_tracker import load_all_trades, _data_root
    trades = load_all_trades(_data_root())
    by_strat = collect_winner_maes(trades)
    if args.strategy:
        by_strat = {args.strategy: by_strat.get(args.strategy, [])}

    print(f"# MAE-calibrated stop recommendations (#17)")
    print(f"# buffer_pct={args.buffer_pct}, min_trades={args.min_trades}")
    print()
    print(f"{'strategy':30s} {'n_win':>5} {'p50':>6} {'p75':>6} "
          f"{'p95':>6} {'rec':>5} {'cons':>5}  confidence")
    print("-" * 90)
    for strat in sorted(by_strat.keys()):
        r = recommend_stop(by_strat[strat], buffer_pct=args.buffer_pct)
        if r["n_winners"] < args.min_trades:
            print(f"{strat:30s} {r['n_winners']:>5}     —      —      "
                  f"—     —     —   {r['confidence']}")
            continue
        print(f"{strat:30s} {r['n_winners']:>5} "
              f"{r['mae_median']:>6.1f} {r['mae_p75']:>6.1f} "
              f"{r['mae_p95']:>6.1f} {r['recommended']:>5d} "
              f"{r['conservative']:>5d}  {r['confidence']}")
    print()
    print("Recommended = ceil(p75 * (1 + buffer_pct/100)) — covers 75% of")
    print("winners with a noise pad. Conservative = ceil(p95 * (1 + buffer))")
    print("— covers 95% of winners; use when winners are tail-heavy.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
