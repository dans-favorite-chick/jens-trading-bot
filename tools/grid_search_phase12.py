"""
Phoenix Bot — Phase 12 Application Grid-Search Validation

Comprehensive sweep across multiple configurations per application to find
which (if any) angle of attack produces real edge.

For each application, sweeps:
  - Confluence thresholds (multiple z-score cutoffs)
  - Forward-look windows (multiple bar counts)
  - Time-of-day filters (NY PM session vs all-day)
  - Condition combinations (smt only, z only, smt+z, smt|z)
  - Correlation regime gates (>0.85 vs >0.90)

Reports for each app:
  - Best configuration found
  - Edge across all variants (consistency check)
  - Recommended go/no-go decision

USAGE:
    python tools/grid_search_phase12.py
"""

from __future__ import annotations
import csv
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
RESULTS_CSV = PROJECT_ROOT / "data" / "historical" / "backtest_results.csv"
OUTPUT_CSV = PROJECT_ROOT / "data" / "historical" / "phase12_gridsearch.csv"

TICK_SIZE = 0.25


def load_data():
    if not RESULTS_CSV.exists():
        print(f"Missing: {RESULTS_CSV}")
        return None
    rows = []
    with RESULTS_CSV.open() as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(r["ts"])
            except Exception:
                continue
            rows.append({
                "ts": ts,
                "hour": ts.hour,
                "nq_close": float(r["nq_close"]),
                "es_close": float(r["es_close"]),
                "smt_bullish": r["smt_bullish"] == "True",
                "smt_bearish": r["smt_bearish"] == "True",
                "spread_z": float(r["spread_z"]),
                "correlation": float(r["correlation"]),
                "boost_long": int(r["boost_long"]),
                "boost_short": int(r["boost_short"]),
            })
    return rows


def add_forward_metrics(rows):
    n = len(rows)
    windows = [1, 3, 5, 10, 15, 20]
    for i in range(n):
        close = rows[i]["nq_close"]
        for w in windows:
            if i + w >= n:
                for key in [f"fwd{w}_high_t", f"fwd{w}_low_t", f"fwd{w}_close_t"]:
                    rows[i][key] = None
                continue
            future = [rows[i + k]["nq_close"] for k in range(1, w + 1)]
            rows[i][f"fwd{w}_high_t"] = (max(future) - close) / TICK_SIZE
            rows[i][f"fwd{w}_low_t"] = (min(future) - close) / TICK_SIZE
            rows[i][f"fwd{w}_close_t"] = (future[-1] - close) / TICK_SIZE


def pct_meets(values, threshold, direction=">"):
    clean = [v for v in values if v is not None]
    if not clean:
        return 0.0
    if direction == ">":
        return 100 * sum(1 for v in clean if v > threshold) / len(clean)
    return 100 * sum(1 for v in clean if v < threshold) / len(clean)


def mean(values):
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else 0


def is_ny_pm(row):
    """13:00-14:30 in the data's local time (we observed clustering at 13:30)."""
    return 13 <= row["hour"] <= 14


# ──────────────────────────────────────────────────────────────────
# App #1 — Adaptive Stop Tightening (LONG side)
# ──────────────────────────────────────────────────────────────────

def sweep_app1(rows):
    """
    Test: When confluence is adverse for an existing LONG, does NQ retrace
    in the forward window MORE often than the matched-condition baseline?
    
    Matched baseline = bars where we WOULD be considering long (smt_bullish
    or z < -1.0) but confluence is NOT currently adverse.
    """
    configs = []
    
    for z_threshold in [1.0, 1.5, 2.0, 2.5]:
        for window in [3, 5, 10]:
            for retrace_threshold_t in [8, 12, 16, 20]:
                for cond_logic in ["z_only", "smt_only", "z_or_smt", "z_and_smt"]:
                    for time_filter in ["all_day", "ny_pm_only"]:
                        for corr_min in [0.85, 0.90]:
                            
                            def adverse_long(r):
                                if r["correlation"] < corr_min:
                                    return False
                                if time_filter == "ny_pm_only" and not is_ny_pm(r):
                                    return False
                                z_adverse = r["spread_z"] > z_threshold
                                smt_adverse = r["smt_bearish"]
                                if cond_logic == "z_only":
                                    return z_adverse
                                if cond_logic == "smt_only":
                                    return smt_adverse
                                if cond_logic == "z_or_smt":
                                    return z_adverse or smt_adverse
                                if cond_logic == "z_and_smt":
                                    return z_adverse and smt_adverse
                                return False
                            
                            def baseline_long(r):
                                # "Would consider long" universe — bullish-leaning bars
                                # that AREN'T currently adverse
                                if r["correlation"] < corr_min:
                                    return False
                                if time_filter == "ny_pm_only" and not is_ny_pm(r):
                                    return False
                                bullish_lean = r["smt_bullish"] or r["spread_z"] < -0.5
                                # Not currently adverse
                                not_adverse = not (
                                    r["spread_z"] > z_threshold or r["smt_bearish"]
                                )
                                return bullish_lean and not_adverse
                            
                            adv_pop = [r for r in rows if adverse_long(r)]
                            base_pop = [r for r in rows if baseline_long(r)]
                            
                            if len(adv_pop) < 30 or len(base_pop) < 30:
                                continue
                            
                            key = f"fwd{window}_low_t"
                            adv_retrace_pct = pct_meets(
                                [abs(r[key]) if r[key] is not None else None for r in adv_pop],
                                retrace_threshold_t,
                            )
                            base_retrace_pct = pct_meets(
                                [abs(r[key]) if r[key] is not None else None for r in base_pop],
                                retrace_threshold_t,
                            )
                            edge = adv_retrace_pct - base_retrace_pct
                            
                            configs.append({
                                "app": "App1_StopTighten",
                                "z_thr": z_threshold,
                                "window": window,
                                "retrace_thr": retrace_threshold_t,
                                "cond_logic": cond_logic,
                                "time_filter": time_filter,
                                "corr_min": corr_min,
                                "n_adv": len(adv_pop),
                                "n_base": len(base_pop),
                                "adverse_retrace_pct": adv_retrace_pct,
                                "baseline_retrace_pct": base_retrace_pct,
                                "edge_pp": edge,
                            })
    return configs


# ──────────────────────────────────────────────────────────────────
# App #2 — Early TP Trigger
# ──────────────────────────────────────────────────────────────────

def sweep_app2(rows):
    """
    Test: After NQ moves +T1 ticks (proxy for hitting T1), what's the
    probability it extends another +T2 ticks? Compare aligned vs adverse confluence.
    """
    configs = []
    n = len(rows)
    
    for t1_t in [10, 12, 16, 20]:
        for t2_t in [10, 12, 16, 20]:
            for lookback_for_t1 in [3, 5]:
                for lookforward_for_t2 in [5, 10]:
                    for cond_strict in [False, True]:
                        for corr_min in [0.85, 0.90]:
                            for time_filter in ["all_day", "ny_pm_only"]:
                                
                                aligned_continued = []
                                adverse_continued = []
                                
                                for i in range(lookback_for_t1, n - lookforward_for_t2):
                                    r = rows[i]
                                    if r["correlation"] < corr_min:
                                        continue
                                    if time_filter == "ny_pm_only" and not is_ny_pm(r):
                                        continue
                                    
                                    # T1 detection
                                    recent_low = min(rows[i - k]["nq_close"]
                                                   for k in range(1, lookback_for_t1 + 1))
                                    move_t = (r["nq_close"] - recent_low) / TICK_SIZE
                                    if move_t < t1_t:
                                        continue
                                    
                                    # Forward extension check
                                    fwd_high = max(rows[i + k]["nq_close"]
                                                 for k in range(1, lookforward_for_t2 + 1))
                                    extended = (fwd_high - r["nq_close"]) / TICK_SIZE >= t2_t
                                    
                                    # Classify confluence state
                                    if cond_strict:
                                        # Strict: both z and smt aligned/adverse
                                        if r["smt_bearish"] and r["spread_z"] > 1.5:
                                            adverse_continued.append(extended)
                                        elif r["smt_bullish"] and r["spread_z"] < -0.5:
                                            aligned_continued.append(extended)
                                    else:
                                        # Lenient: either signal
                                        if r["smt_bearish"] or r["spread_z"] > 1.5:
                                            adverse_continued.append(extended)
                                        elif r["smt_bullish"] or r["spread_z"] < -0.5:
                                            aligned_continued.append(extended)
                                
                                if len(adverse_continued) < 30 or len(aligned_continued) < 30:
                                    continue
                                
                                aligned_rate = 100 * sum(aligned_continued) / len(aligned_continued)
                                adverse_rate = 100 * sum(adverse_continued) / len(adverse_continued)
                                edge = aligned_rate - adverse_rate  # bigger = better signal
                                
                                configs.append({
                                    "app": "App2_EarlyTP",
                                    "t1_t": t1_t,
                                    "t2_t": t2_t,
                                    "lookback": lookback_for_t1,
                                    "lookforward": lookforward_for_t2,
                                    "cond_strict": cond_strict,
                                    "corr_min": corr_min,
                                    "time_filter": time_filter,
                                    "n_aligned": len(aligned_continued),
                                    "n_adverse": len(adverse_continued),
                                    "aligned_t2_rate": aligned_rate,
                                    "adverse_t2_rate": adverse_rate,
                                    "edge_pp": edge,
                                })
    return configs


# ──────────────────────────────────────────────────────────────────
# App #4 — Position Scaling
# ──────────────────────────────────────────────────────────────────

def sweep_app4(rows):
    """
    Test: When confluence aligned for long, do subsequent moves go further?
    """
    configs = []
    
    for boost_threshold in [3, 5, 7, 10]:
        for window in [5, 10, 20]:
            for big_move_t in [12, 20, 30, 40]:
                for time_filter in ["all_day", "ny_pm_only"]:
                    for corr_min in [0.85, 0.90, 0.95]:
                        
                        aligned = [
                            r for r in rows
                            if r["boost_long"] >= boost_threshold
                            and r["correlation"] >= corr_min
                            and (time_filter == "all_day" or is_ny_pm(r))
                        ]
                        # Baseline: neutral bars (boost magnitude small)
                        baseline = [
                            r for r in rows
                            if abs(r["boost_long"]) < 2
                            and r["correlation"] >= corr_min
                            and (time_filter == "all_day" or is_ny_pm(r))
                        ]
                        
                        if len(aligned) < 30 or len(baseline) < 30:
                            continue
                        
                        key = f"fwd{window}_high_t"
                        aligned_big = pct_meets(
                            [r[key] for r in aligned], big_move_t,
                        )
                        baseline_big = pct_meets(
                            [r[key] for r in baseline], big_move_t,
                        )
                        edge = aligned_big - baseline_big
                        
                        configs.append({
                            "app": "App4_Scale",
                            "boost_thr": boost_threshold,
                            "window": window,
                            "big_move_t": big_move_t,
                            "time_filter": time_filter,
                            "corr_min": corr_min,
                            "n_aligned": len(aligned),
                            "n_baseline": len(baseline),
                            "aligned_big_pct": aligned_big,
                            "baseline_big_pct": baseline_big,
                            "edge_pp": edge,
                        })
    return configs


# ──────────────────────────────────────────────────────────────────
# Direction prediction (the foundational signal)
# ──────────────────────────────────────────────────────────────────

def sweep_direction_prediction(rows):
    """
    Test the FOUNDATIONAL signal: does confluence predict forward NQ direction?
    
    If this works, all the trade-context applications can in principle work.
    If this DOESN'T work, the signal isn't usable for forward prediction
    regardless of how we wrap it.
    """
    configs = []
    
    for boost_threshold in [3, 5, 7, 10]:
        for window in [3, 5, 10, 20]:
            for time_filter in ["all_day", "ny_pm_only"]:
                for corr_min in [0.85, 0.90]:
                    
                    # LONG side: bars with positive boost_long
                    long_signal = [
                        r for r in rows
                        if r["boost_long"] >= boost_threshold
                        and r["correlation"] >= corr_min
                        and (time_filter == "all_day" or is_ny_pm(r))
                    ]
                    # SHORT side: bars with positive boost_short (large)
                    short_signal = [
                        r for r in rows
                        if r["boost_short"] >= boost_threshold
                        and r["correlation"] >= corr_min
                        and (time_filter == "all_day" or is_ny_pm(r))
                    ]
                    # Neutral baseline
                    neutral = [
                        r for r in rows
                        if abs(r["boost_long"]) < 2 and abs(r["boost_short"]) < 2
                        and r["correlation"] >= corr_min
                        and (time_filter == "all_day" or is_ny_pm(r))
                    ]
                    
                    if len(long_signal) < 30 or len(short_signal) < 30 or len(neutral) < 30:
                        continue
                    
                    key = f"fwd{window}_close_t"
                    long_avg = mean([r[key] for r in long_signal])
                    short_avg = mean([r[key] for r in short_signal])
                    neutral_avg = mean([r[key] for r in neutral])
                    
                    # The edge: long_signal should produce more positive returns,
                    # short_signal should produce more negative returns
                    long_edge = long_avg - neutral_avg  # higher = better
                    short_edge = neutral_avg - short_avg  # higher = better (short_avg should be MORE negative)
                    combined_edge = (long_edge + short_edge) / 2
                    
                    configs.append({
                        "app": "DirPredict",
                        "boost_thr": boost_threshold,
                        "window": window,
                        "time_filter": time_filter,
                        "corr_min": corr_min,
                        "n_long": len(long_signal),
                        "n_short": len(short_signal),
                        "n_neutral": len(neutral),
                        "long_fwd_avg": long_avg,
                        "short_fwd_avg": short_avg,
                        "neutral_fwd_avg": neutral_avg,
                        "long_edge_t": long_edge,
                        "short_edge_t": short_edge,
                        "combined_edge_t": combined_edge,
                        "edge_pp": combined_edge,  # for sorting consistency
                    })
    return configs


# ──────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────

def report_best(configs, top_n=5):
    if not configs:
        return
    sorted_configs = sorted(configs, key=lambda c: c.get("edge_pp", 0), reverse=True)
    
    app_name = configs[0]["app"]
    print(f"\n{'=' * 75}")
    print(f"  {app_name} — top {top_n} of {len(configs)} configurations tested")
    print(f"{'=' * 75}")
    
    if not sorted_configs:
        print("  No valid configs.")
        return
    
    for i, c in enumerate(sorted_configs[:top_n]):
        print(f"\n  [{i+1}] edge: {c['edge_pp']:+.2f}pp")
        for k, v in c.items():
            if k in ("app", "edge_pp"):
                continue
            if isinstance(v, float):
                print(f"      {k}: {v:.2f}")
            else:
                print(f"      {k}: {v}")


def main():
    print("=" * 75)
    print("Phoenix Phase 12 — Grid-Search Validation")
    print("=" * 75)
    
    rows = load_data()
    if not rows:
        return
    print(f"\nLoaded {len(rows):,} bars")
    add_forward_metrics(rows)
    print("Forward metrics computed.")
    
    all_configs = []
    
    print("\n► Sweeping foundational direction prediction...")
    dir_configs = sweep_direction_prediction(rows)
    print(f"  {len(dir_configs)} configs tested")
    all_configs.extend(dir_configs)
    report_best(dir_configs, top_n=5)
    
    print("\n► Sweeping App #1 (Stop Tightening)...")
    app1_configs = sweep_app1(rows)
    print(f"  {len(app1_configs)} configs tested")
    all_configs.extend(app1_configs)
    report_best(app1_configs, top_n=5)
    
    print("\n► Sweeping App #2 (Early TP)...")
    app2_configs = sweep_app2(rows)
    print(f"  {len(app2_configs)} configs tested")
    all_configs.extend(app2_configs)
    report_best(app2_configs, top_n=5)
    
    print("\n► Sweeping App #4 (Position Scaling)...")
    app4_configs = sweep_app4(rows)
    print(f"  {len(app4_configs)} configs tested")
    all_configs.extend(app4_configs)
    report_best(app4_configs, top_n=5)
    
    # Final summary
    print("\n" + "=" * 75)
    print("  EXECUTIVE SUMMARY")
    print("=" * 75)
    
    for app_name in ["DirPredict", "App1_StopTighten", "App2_EarlyTP", "App4_Scale"]:
        app_configs = [c for c in all_configs if c["app"] == app_name]
        if not app_configs:
            continue
        edges = [c["edge_pp"] for c in app_configs]
        best = max(edges)
        median_edge = statistics.median(edges)
        pct_positive = 100 * sum(1 for e in edges if e > 5) / len(edges)
        
        print(f"\n  {app_name}:")
        print(f"    configs tested:      {len(app_configs)}")
        print(f"    best edge:           {best:+.2f}pp")
        print(f"    median edge:         {median_edge:+.2f}pp")
        print(f"    % configs > 5pp:     {pct_positive:.1f}%")
        
        # Verdict
        if best > 15 and pct_positive > 20:
            verdict = "STRONG signal under some conditions — SHIP with best config"
        elif best > 10 and pct_positive > 10:
            verdict = "MODERATE signal — SHIP with best config in observe-only first"
        elif best > 5:
            verdict = "WEAK signal — DEFER, needs real trade context"
        else:
            verdict = "NO signal at any config — DROP this application"
        print(f"    verdict:             {verdict}")
    
    # Write to CSV (with explicit UTF-8 to avoid the cp1252 bug)
    print(f"\nWriting all configs to {OUTPUT_CSV}...")
    if all_configs:
        # Collect all keys across all configs
        all_keys = set()
        for c in all_configs:
            all_keys.update(c.keys())
        all_keys = sorted(all_keys)
        
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys)
            writer.writeheader()
            for c in all_configs:
                writer.writerow(c)
    
    print("\nDone.")


if __name__ == "__main__":
    main()
