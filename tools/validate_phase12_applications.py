"""
Phoenix Bot — Phase 12 Application Validation Backtest

Tests each of the 5 exit/stop confluence applications against historical
bar data via forward-return analysis. For each bar, looks at what NQ did
over the next K bars, then stratifies by the confluence state to measure
whether the signal predicts the outcome the application cares about.

USAGE (from C:\\Trading Project\\phoenix_bot\\):
    python tools/validate_phase12_applications.py

INPUTS:
    data/historical/backtest_results.csv (from prior backtest)

OUTPUTS:
    Console comparison table — recommended build order
    data/historical/phase12_validation.csv (full stratified results)
"""

from __future__ import annotations
import csv
import statistics
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
RESULTS_CSV = PROJECT_ROOT / "data" / "historical" / "backtest_results.csv"
OUTPUT_CSV = PROJECT_ROOT / "data" / "historical" / "phase12_validation.csv"

# Tick size for MNQ
TICK_SIZE = 0.25

# Forward-looking windows
WINDOWS = [1, 3, 5, 10]  # bars to look forward

# Thresholds for "in-profit" / "drawdown" classification (in ticks)
PROFIT_THRESHOLD_TICKS = 8    # +2 points = "trade in profit"
DRAWDOWN_THRESHOLD_TICKS = 16  # -4 points = "retracement"
T1_THRESHOLD_TICKS = 12        # +3 points ≈ Phoenix's T1 area
T2_THRESHOLD_TICKS = 24        # +6 points ≈ Phoenix's T2 area


def load_data():
    """Load the per-bar confluence results from the prior backtest."""
    if not RESULTS_CSV.exists():
        print(f"❌ Missing input: {RESULTS_CSV}")
        print("Run tools/backtest_es_nq_confluence.py first to generate it.")
        return None
    
    rows = []
    with RESULTS_CSV.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                "ts": r["ts"],
                "nq_close": float(r["nq_close"]),
                "es_close": float(r["es_close"]),
                "smt_bullish": r["smt_bullish"] == "True",
                "smt_bearish": r["smt_bearish"] == "True",
                "spread_z": float(r["spread_z"]),
                "correlation": float(r["correlation"]),
                "boost_long": int(r["boost_long"]),
                "boost_short": int(r["boost_short"]),
                "skip_long": r["skip_long"] == "True",
                "skip_short": r["skip_short"] == "True",
            })
    return rows


def add_forward_metrics(rows):
    """For each bar, compute what NQ did over the next K bars."""
    n = len(rows)
    for i in range(n):
        close = rows[i]["nq_close"]
        for window in WINDOWS:
            if i + window >= n:
                rows[i][f"fwd_{window}_max_gain_ticks"] = None
                rows[i][f"fwd_{window}_max_loss_ticks"] = None
                rows[i][f"fwd_{window}_close_diff_ticks"] = None
                continue
            future_closes = [rows[i + k]["nq_close"] for k in range(1, window + 1)]
            max_high = max(future_closes)
            min_low = min(future_closes)
            end_close = future_closes[-1]
            rows[i][f"fwd_{window}_max_gain_ticks"] = (max_high - close) / TICK_SIZE
            rows[i][f"fwd_{window}_max_loss_ticks"] = (min_low - close) / TICK_SIZE
            rows[i][f"fwd_{window}_close_diff_ticks"] = (end_close - close) / TICK_SIZE


def mean(values):
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else 0


def pct_above(values, threshold):
    clean = [v for v in values if v is not None]
    if not clean:
        return 0
    return 100 * sum(1 for v in clean if v > threshold) / len(clean)


# ──────────────────────────────────────────────────────────────────
# Application validation tests
# ──────────────────────────────────────────────────────────────────

def test_app1_stop_tighten(rows):
    """
    App #1 — Adaptive stop tightening for LONG positions.
    
    Hypothesis: When confluence diverges AGAINST a long (z > +1.5 or smt_bearish),
    NQ retraces in the next 3 bars MORE often than baseline.
    
    Method: For bars where confluence-adverse-to-long fires, measure forward
    3-bar max drawdown. Compare to baseline (all bars).
    """
    adverse_long = [r for r in rows
                    if r["correlation"] > 0.85
                    and (r["spread_z"] > 1.5 or r["smt_bearish"])]
    
    baseline = [r for r in rows if r["correlation"] > 0.85]
    
    if not adverse_long or not baseline:
        return None
    
    adverse_max_dd = mean([r["fwd_3_max_loss_ticks"] for r in adverse_long])
    baseline_max_dd = mean([r["fwd_3_max_loss_ticks"] for r in baseline])
    
    adverse_retrace_pct = pct_above([abs(r["fwd_3_max_loss_ticks"] or 0) for r in adverse_long],
                                     DRAWDOWN_THRESHOLD_TICKS)
    baseline_retrace_pct = pct_above([abs(r["fwd_3_max_loss_ticks"] or 0) for r in baseline],
                                      DRAWDOWN_THRESHOLD_TICKS)
    
    return {
        "name": "App #1 — Adaptive Stop Tightening (LONG)",
        "samples_adverse": len(adverse_long),
        "samples_baseline": len(baseline),
        "metric": "Forward 3-bar max drawdown (ticks)",
        "adverse_value": f"{adverse_max_dd:+.2f}",
        "baseline_value": f"{baseline_max_dd:+.2f}",
        "edge_metric": "% bars with >16t retracement",
        "adverse_rate": f"{adverse_retrace_pct:.1f}%",
        "baseline_rate": f"{baseline_retrace_pct:.1f}%",
        "edge_pct": adverse_retrace_pct - baseline_retrace_pct,
    }


def test_app2_early_tp(rows):
    """
    App #2 — Early TP trigger.
    
    Hypothesis: When NQ has moved +12t (T1 area) AND confluence diverges against,
    extending to +24t (T2) is LESS likely than baseline.
    
    Method: Find bars in a "T1 hit" state (back-look 3 bars to see if NQ moved +12t),
    then forward-look 5 bars to see if NQ extended another +12t. Stratify by
    current confluence state.
    """
    # Find bars where a +12t move just completed (proxy for T1 hit)
    n = len(rows)
    t1_bars_aligned = []
    t1_bars_diverged = []
    t1_bars_neutral = []
    
    for i in range(3, n - 5):
        # T1 detection: did NQ rise +12t in the prior 3 bars?
        recent_low = min(rows[i - k]["nq_close"] for k in range(1, 4))
        if (rows[i]["nq_close"] - recent_low) / TICK_SIZE < T1_THRESHOLD_TICKS:
            continue
        
        # Now classify by current confluence state
        r = rows[i]
        if r["correlation"] < 0.85:
            continue
        
        # Look forward 5 bars: did NQ continue another +12t?
        fwd_high = max(rows[i + k]["nq_close"] for k in range(1, 6))
        continued = (fwd_high - r["nq_close"]) / TICK_SIZE >= T1_THRESHOLD_TICKS
        
        # Classify state
        if r["smt_bearish"] or r["spread_z"] > 2.0:
            t1_bars_diverged.append(continued)
        elif r["smt_bullish"] or r["spread_z"] < -1.0:
            t1_bars_aligned.append(continued)
        else:
            t1_bars_neutral.append(continued)
    
    if not t1_bars_diverged or not t1_bars_aligned:
        return None
    
    diverged_t2_rate = 100 * sum(t1_bars_diverged) / len(t1_bars_diverged)
    aligned_t2_rate = 100 * sum(t1_bars_aligned) / len(t1_bars_aligned)
    neutral_t2_rate = 100 * sum(t1_bars_neutral) / len(t1_bars_neutral) if t1_bars_neutral else 0
    
    return {
        "name": "App #2 — Early TP Trigger",
        "samples_adverse": len(t1_bars_diverged),
        "samples_baseline": len(t1_bars_aligned) + len(t1_bars_neutral),
        "metric": "P(T2 hit | T1 hit) by confluence state",
        "adverse_value": f"diverged: {diverged_t2_rate:.1f}%",
        "baseline_value": f"aligned: {aligned_t2_rate:.1f}% / neutral: {neutral_t2_rate:.1f}%",
        "edge_metric": "T2 hit-rate degradation when diverged",
        "adverse_rate": f"{diverged_t2_rate:.1f}%",
        "baseline_rate": f"{aligned_t2_rate:.1f}%",
        "edge_pct": aligned_t2_rate - diverged_t2_rate,
    }


def test_app3_be_accel(rows):
    """
    App #3 — Breakeven acceleration.
    
    Hypothesis: When confluence diverges early in a trade (NQ only +4-8t in profit),
    the trade goes to stop more often than baseline.
    
    Method: Find bars where confluence diverged. Forward-look 5 bars to see
    if NQ retraced beyond entry by more than 8t (where BE stop would protect).
    """
    diverged_long = [r for r in rows
                     if r["correlation"] > 0.85
                     and (r["smt_bearish"] or r["spread_z"] > 1.5)]
    baseline = [r for r in rows if r["correlation"] > 0.85]
    
    if not diverged_long or not baseline:
        return None
    
    # How often does NQ retrace > 8t in next 5 bars?
    diverged_retrace_pct = pct_above(
        [abs(r["fwd_5_max_loss_ticks"] or 0) for r in diverged_long], 
        8,
    )
    baseline_retrace_pct = pct_above(
        [abs(r["fwd_5_max_loss_ticks"] or 0) for r in baseline],
        8,
    )
    
    return {
        "name": "App #3 — Breakeven Acceleration",
        "samples_adverse": len(diverged_long),
        "samples_baseline": len(baseline),
        "metric": "Forward 5-bar retrace > 8t rate",
        "adverse_value": f"{diverged_retrace_pct:.1f}%",
        "baseline_value": f"{baseline_retrace_pct:.1f}%",
        "edge_metric": "Retracement-rate uplift on adverse confluence",
        "adverse_rate": f"{diverged_retrace_pct:.1f}%",
        "baseline_rate": f"{baseline_retrace_pct:.1f}%",
        "edge_pct": diverged_retrace_pct - baseline_retrace_pct,
    }


def test_app4_position_scaling(rows):
    """
    App #4 — Position scaling on alignment.
    
    Hypothesis: When confluence is ALIGNED with direction, the subsequent
    move is LARGER than baseline.
    
    Method: For bars where boost_long >= 5 (strong long alignment), measure
    forward 10-bar max gain. Compare to baseline.
    """
    aligned_long = [r for r in rows if r["boost_long"] >= 5 and r["correlation"] > 0.85]
    baseline = [r for r in rows if r["correlation"] > 0.85]
    
    if not aligned_long or not baseline:
        return None
    
    aligned_max_gain = mean([r["fwd_10_max_gain_ticks"] for r in aligned_long])
    baseline_max_gain = mean([r["fwd_10_max_gain_ticks"] for r in baseline])
    
    aligned_big_move_pct = pct_above(
        [r["fwd_10_max_gain_ticks"] for r in aligned_long],
        T2_THRESHOLD_TICKS,
    )
    baseline_big_move_pct = pct_above(
        [r["fwd_10_max_gain_ticks"] for r in baseline],
        T2_THRESHOLD_TICKS,
    )
    
    return {
        "name": "App #4 — Position Scaling on Alignment",
        "samples_adverse": len(aligned_long),
        "samples_baseline": len(baseline),
        "metric": "Forward 10-bar max gain (ticks)",
        "adverse_value": f"+{aligned_max_gain:.2f}",
        "baseline_value": f"+{baseline_max_gain:.2f}",
        "edge_metric": f"% bars with >24t (T2) forward gain",
        "adverse_rate": f"{aligned_big_move_pct:.1f}%",
        "baseline_rate": f"{baseline_big_move_pct:.1f}%",
        "edge_pct": aligned_big_move_pct - baseline_big_move_pct,
    }


def test_app5_reentry(rows):
    """
    App #5 — Re-entry intelligence.
    
    Hypothesis: After a counter-move (stop-out simulation), bars where
    confluence STILL aligned with original direction show resumption
    of the original direction more often than baseline.
    
    Method: Find bars where prior 5 bars showed NQ DOWN > 8t (proxy for
    a long stop-out). Now check current confluence. If still bullish,
    does NQ recover (move +8t up) in next 5 bars?
    """
    n = len(rows)
    still_aligned = []  # bullish confluence after counter-move
    no_confluence = []  # no confluence after counter-move (baseline)
    
    for i in range(5, n - 5):
        # Counter-move detection: prior 5 bars NQ fell > 8t
        prior_high = max(rows[i - k]["nq_close"] for k in range(1, 6))
        if (prior_high - rows[i]["nq_close"]) / TICK_SIZE < 8:
            continue
        
        r = rows[i]
        if r["correlation"] < 0.85:
            continue
        
        # Did NQ recover in next 5 bars?
        fwd_high = max(rows[i + k]["nq_close"] for k in range(1, 6))
        recovered = (fwd_high - r["nq_close"]) / TICK_SIZE >= 8
        
        # Stratify
        if r["boost_long"] >= 5:
            still_aligned.append(recovered)
        elif abs(r["boost_long"]) < 3:
            no_confluence.append(recovered)
    
    if not still_aligned or not no_confluence:
        return None
    
    aligned_recovery = 100 * sum(still_aligned) / len(still_aligned)
    no_conf_recovery = 100 * sum(no_confluence) / len(no_confluence)
    
    return {
        "name": "App #5 — Re-entry Intelligence",
        "samples_adverse": len(still_aligned),
        "samples_baseline": len(no_confluence),
        "metric": "P(recovery after counter-move) by confluence",
        "adverse_value": f"still aligned: {aligned_recovery:.1f}%",
        "baseline_value": f"no confluence: {no_conf_recovery:.1f}%",
        "edge_metric": "Recovery-rate uplift on retained alignment",
        "adverse_rate": f"{aligned_recovery:.1f}%",
        "baseline_rate": f"{no_conf_recovery:.1f}%",
        "edge_pct": aligned_recovery - no_conf_recovery,
    }


def grade_edge(edge_pct):
    """Convert edge percentage to a recommendation grade."""
    abs_edge = abs(edge_pct)
    if abs_edge > 15: return "🥇 STRONG — build first"
    if abs_edge > 8:  return "🥈 MODERATE — build after #1 confirmed"
    if abs_edge > 4:  return "🥉 MILD — observe in production first"
    if abs_edge > 1:  return "⚪ MARGINAL — defer or skip"
    return "❌ NONE — design needs work"


def main():
    print("=" * 75)
    print("Phoenix Phase 12 — Application Validation Backtest")
    print("=" * 75)
    
    print(f"\nLoading {RESULTS_CSV}...")
    rows = load_data()
    if not rows:
        return
    print(f"  Loaded {len(rows):,} bars")
    
    print("Computing forward-looking metrics...")
    add_forward_metrics(rows)
    
    print("\nRunning application tests...\n")
    
    results = []
    for test_fn in [test_app1_stop_tighten, test_app2_early_tp,
                    test_app3_be_accel, test_app4_position_scaling,
                    test_app5_reentry]:
        result = test_fn(rows)
        if result is None:
            print(f"  ⚠ Skipped {test_fn.__name__} (insufficient data)")
            continue
        results.append(result)
    
    # Print comparison table
    print("\n" + "=" * 75)
    print("RESULTS — Application-by-Application Validation")
    print("=" * 75)
    
    for r in results:
        print()
        print(f"  {r['name']}")
        print(f"    Samples: adverse/aligned={r['samples_adverse']:,}, baseline={r['samples_baseline']:,}")
        print(f"    Metric: {r['metric']}")
        print(f"      adverse/aligned: {r['adverse_value']}")
        print(f"      baseline:        {r['baseline_value']}")
        print(f"    {r['edge_metric']}: {r['edge_pct']:+.1f}pp")
        print(f"    Grade: {grade_edge(r['edge_pct'])}")
    
    # Recommendation
    print("\n" + "=" * 75)
    print("RECOMMENDED BUILD ORDER")
    print("=" * 75)
    sorted_apps = sorted(results, key=lambda r: abs(r["edge_pct"]), reverse=True)
    for i, r in enumerate(sorted_apps, 1):
        print(f"  {i}. {r['name']}")
        print(f"     edge: {r['edge_pct']:+.1f}pp  →  {grade_edge(r['edge_pct'])}")
    
    # Save CSV
    print(f"\nWriting detailed results to {OUTPUT_CSV}...")
    with OUTPUT_CSV.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["application", "samples_adverse", "samples_baseline",
                        "metric", "adverse_value", "baseline_value",
                        "edge_metric", "adverse_rate", "baseline_rate",
                        "edge_pct", "grade"])
        for r in results:
            writer.writerow([r["name"], r["samples_adverse"], r["samples_baseline"],
                           r["metric"], r["adverse_value"], r["baseline_value"],
                           r["edge_metric"], r["adverse_rate"], r["baseline_rate"],
                           f"{r['edge_pct']:+.2f}", grade_edge(r["edge_pct"])])
    
    print(f"\n✓ Done. Sorted by edge magnitude — build the top-graded apps first.")


if __name__ == "__main__":
    main()
