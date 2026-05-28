"""
Aggregate all 2026-05-27 baseline outputs into a single JSON file.

Sources:
  out/_baseline_2026-05-27/phoenix_stop_target_recommendations_2026-05-27.csv
  out/_baseline_2026-05-27/phase13_winners/phoenix_stop_target_recommendations_2026-05-27.csv
  out/_baseline_2026-05-27/policy_sweep_per_strategy.csv
  out/_baseline_2026-05-27/phase13_winners/policy_sweep_per_strategy.csv
  out/_baseline_2026-05-27/mfe_mae_per_strategy.csv
  out/_baseline_2026-05-27/phase13_winners/mfe_mae_per_strategy.csv
  out/_baseline_2026-05-27/hour_buckets_detail.csv
  out/_baseline_2026-05-27/hour_buckets_proposal.csv
  out/_baseline_2026-05-27/multi_tf_overlay.csv
  out/_baseline_2026-05-27/multi_tf_winners.csv
  backtest_results/_baseline_2026-05-18/phoenix_stop_target_recommendations.csv  (May-18 diff)
  backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv  (fresh per-trade)
  backtest_results/phoenix_new_strategy_lab.csv  (Phase 13 a/e/g)
  backtest_results/phoenix_trend_pullback_lab.csv  (raschke variants)

Output: C:/Trading Project/phoenix_bot/latest_and_greatest.json
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

ROOT = Path("C:/Trading Project/phoenix_bot")
OUT_BASE = ROOT / "out" / "_baseline_2026-05-27"
OUT_P13 = OUT_BASE / "phase13_winners"
JSON_PATH = ROOT / "latest_and_greatest.json"


def _safe_float(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except (ValueError, TypeError):
        return None


def _safe_int(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _read_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        print(f"[WARN] missing: {path}")
        return None
    return pd.read_csv(path)


def main():
    # ─── Load everything ─────────────────────────────────────────────
    rec_main = _read_csv(OUT_BASE / "phoenix_stop_target_recommendations_2026-05-27.csv")
    rec_p13 = _read_csv(OUT_P13 / "phoenix_stop_target_recommendations_2026-05-27.csv")
    sweep_main = _read_csv(OUT_BASE / "policy_sweep_per_strategy.csv")
    sweep_p13 = _read_csv(OUT_P13 / "policy_sweep_per_strategy.csv")
    mae_main = _read_csv(OUT_BASE / "mfe_mae_per_strategy.csv")
    mae_p13 = _read_csv(OUT_P13 / "mfe_mae_per_strategy.csv")
    hour_detail = _read_csv(OUT_BASE / "hour_buckets_detail.csv")
    hour_prop = _read_csv(OUT_BASE / "hour_buckets_proposal.csv")
    mtf_overlay = _read_csv(OUT_BASE / "multi_tf_overlay.csv")
    mtf_winners = _read_csv(OUT_BASE / "multi_tf_winners.csv")
    may18_rec = _read_csv(ROOT / "backtest_results" / "_baseline_2026-05-18" / "phoenix_stop_target_recommendations.csv")
    fresh_trades = _read_csv(ROOT / "backtest_results" / "_reproduction_2026-05-27" / "phoenix_real_5year.csv")
    lab_new = _read_csv(ROOT / "backtest_results" / "phoenix_new_strategy_lab.csv")
    lab_trend = _read_csv(ROOT / "backtest_results" / "phoenix_trend_pullback_lab.csv")

    # Combine recs (main + phase13 winners)
    if rec_p13 is not None and rec_main is not None:
        rec_all = pd.concat([rec_main, rec_p13], ignore_index=True)
    elif rec_main is not None:
        rec_all = rec_main
    else:
        rec_all = rec_p13

    if sweep_p13 is not None and sweep_main is not None:
        sweep_all = pd.concat([sweep_main, sweep_p13], ignore_index=True)
    elif sweep_main is not None:
        sweep_all = sweep_main
    else:
        sweep_all = sweep_p13

    if mae_p13 is not None and mae_main is not None:
        mae_all = pd.concat([mae_main, mae_p13], ignore_index=True)
    elif mae_main is not None:
        mae_all = mae_main
    else:
        mae_all = mae_p13

    # All trades combined for per-strategy totals
    all_trade_frames = [f for f in [fresh_trades, lab_new, lab_trend] if f is not None]
    all_trades = pd.concat(all_trade_frames, ignore_index=True) if all_trade_frames else pd.DataFrame()

    # ─── Per-strategy synthesis ──────────────────────────────────────
    per_strategy: dict[str, dict] = {}
    strategies = sorted(rec_all["strategy"].unique()) if rec_all is not None else []

    for s in strategies:
        rec_row = rec_all[rec_all["strategy"] == s].iloc[0] if rec_all is not None else None
        sweep_rows = sweep_all[sweep_all["strategy"] == s] if sweep_all is not None else pd.DataFrame()
        mae_row = mae_all[mae_all["strategy"] == s].iloc[0] if mae_all is not None and len(mae_all[mae_all["strategy"] == s]) else None
        hour_rows = hour_detail[hour_detail["strategy"] == s] if hour_detail is not None else pd.DataFrame()
        hour_prop_row = hour_prop[hour_prop["strategy"] == s].iloc[0] if hour_prop is not None and len(hour_prop[hour_prop["strategy"] == s]) else None
        mtf_rows = mtf_overlay[mtf_overlay["strategy"] == s] if mtf_overlay is not None else pd.DataFrame()
        mtf_winners_rows = mtf_winners[mtf_winners["strategy"] == s] if mtf_winners is not None else pd.DataFrame()
        may18_row = may18_rec[may18_rec["strategy"] == s].iloc[0] if may18_rec is not None and len(may18_rec[may18_rec["strategy"] == s]) else None
        strat_trades = all_trades[all_trades["strategy"] == s] if not all_trades.empty else pd.DataFrame()

        entry = {"strategy": s}

        # ─── 5y backtest summary ─────────────────────────────────
        if rec_row is not None:
            entry["backtest_5y"] = {
                "n_trades": _safe_int(rec_row.get("n_trades")),
                "first_entry": str(rec_row.get("first_entry", "")) or None,
                "last_entry": str(rec_row.get("last_entry", "")) or None,
                "baseline_total_dollars": _safe_float(rec_row.get("baseline_total")),
                "profitable_baseline": rec_row.get("profitable") == "YES",
                "years_positive": str(rec_row.get("years_positive", "")) or None,
            }

        # Per-direction split + per-year breakdown
        if not strat_trades.empty:
            strat_trades = strat_trades.copy()
            strat_trades["entry_ts"] = pd.to_datetime(strat_trades["entry_ts"], utc=True, errors="coerce")
            strat_trades["year"] = strat_trades["entry_ts"].dt.year
            by_year = strat_trades.groupby("year").agg(
                n=("pnl_dollars", "size"),
                total=("pnl_dollars", "sum"),
                wins=("pnl_dollars", lambda s: int((s > 0).sum())),
            ).round(0).reset_index()
            entry["per_year_breakdown"] = [
                {
                    "year": _safe_int(r["year"]),
                    "n_trades": _safe_int(r["n"]),
                    "wins": _safe_int(r["wins"]),
                    "wr_pct": _safe_float((r["wins"] / r["n"] * 100) if r["n"] else 0),
                    "total_dollars": _safe_float(r["total"]),
                }
                for _, r in by_year.iterrows()
            ]
            by_dir = strat_trades.groupby("direction").agg(
                n=("pnl_dollars", "size"),
                total=("pnl_dollars", "sum"),
                wins=("pnl_dollars", lambda s: int((s > 0).sum())),
            ).round(0).reset_index()
            entry["by_direction"] = [
                {
                    "direction": str(r["direction"]),
                    "n_trades": _safe_int(r["n"]),
                    "wins": _safe_int(r["wins"]),
                    "wr_pct": _safe_float((r["wins"] / r["n"] * 100) if r["n"] else 0),
                    "total_dollars": _safe_float(r["total"]),
                }
                for _, r in by_dir.iterrows()
            ]

        # ─── MFE/MAE (Section 3 — precision SL substrate) ───────
        if mae_row is not None:
            entry["mfe_mae"] = {
                "mfe_mean_ticks": _safe_float(mae_row.get("mfe_mean_ticks")),
                "mfe_p50_ticks": _safe_float(mae_row.get("mfe_p50")),
                "mfe_p75_ticks": _safe_float(mae_row.get("mfe_p75")),
                "mae_mean_ticks": _safe_float(mae_row.get("mae_mean_ticks")),
                "mae_p50_ticks": _safe_float(mae_row.get("mae_p50")),
                "mae_p75_ticks": _safe_float(mae_row.get("mae_p75")),
                "mfe_mae_ratio": _safe_float(mae_row.get("mfe_mae_ratio")),
                "interpretation": _mae_interpretation(mae_row),
            }

        # ─── Optimizer / exit policy (Section 4) ─────────────────
        if rec_row is not None:
            entry["best_exit_policy"] = {
                "policy": rec_row.get("best_policy"),
                "total_dollars": _safe_float(rec_row.get("best_total")),
                "wr_pct": _safe_float(rec_row.get("best_wr_pct")),
                "pf": _safe_float(rec_row.get("best_pf")),
                "lift_vs_baseline_dollars": _safe_float(rec_row.get("lift_vs_baseline")),
            }
        if not sweep_rows.empty:
            entry["all_25_policies_ranked"] = [
                {
                    "policy": r["policy"],
                    "n": _safe_int(r["n"]),
                    "wr_pct": _safe_float(r["wr_pct"]),
                    "total_dollars": _safe_float(r["total"]),
                    "avg_per_trade_dollars": _safe_float(r["avg"]),
                    "pf": _safe_float(r["pf"]),
                    "years_positive": str(r["years_positive"]),
                }
                for _, r in sweep_rows.sort_values("total", ascending=False).iterrows()
            ]

        # ─── Hour-by-hour timing (Section 1.b) ──────────────────
        if hour_prop_row is not None:
            entry["hour_buckets"] = {
                "n_trades_total": _safe_int(hour_prop_row.get("n_trades_total")),
                "median_rr_overall": _safe_float(hour_prop_row.get("median_rr_overall")),
                "breakeven_wr_overall_pct": _safe_float(hour_prop_row.get("breakeven_wr_overall_pct")),
                "n_green_buckets": _safe_int(hour_prop_row.get("n_green_buckets")),
                "n_red_buckets": _safe_int(hour_prop_row.get("n_red_buckets")),
                "n_neutral_buckets": _safe_int(hour_prop_row.get("n_neutral_buckets")),
                "n_sparse_buckets": _safe_int(hour_prop_row.get("n_sparse_buckets")),
                "proposed_session_windows_ct": json.loads(hour_prop_row.get("proposed_session_windows_ct") or "[]"),
                "proposed_block_windows": json.loads(hour_prop_row.get("proposed_block_windows") or "[]"),
                "pnl_breakdown": {
                    "green_dollars": _safe_float(hour_prop_row.get("green_pnl")),
                    "red_dollars": _safe_float(hour_prop_row.get("red_pnl")),
                    "if_only_green_dollars": _safe_float(hour_prop_row.get("pnl_if_only_green")),
                    "if_blocked_red_dollars": _safe_float(hour_prop_row.get("pnl_if_blocked_red")),
                },
            }

        if not hour_rows.empty:
            all_dir_rows = hour_rows[hour_rows["direction"] == "ALL"]
            entry["hour_bucket_detail_all_direction"] = [
                {
                    "bucket_ct": r["bucket_ct"],
                    "n": _safe_int(r["n"]),
                    "wins": _safe_int(r["wins"]),
                    "wr_pct": _safe_float(r["wr_pct"]),
                    "wilson_lo_pct": _safe_float(r["wilson_lo_pct"]),
                    "wilson_hi_pct": _safe_float(r["wilson_hi_pct"]),
                    "total_dollars": _safe_float(r["total_dollars"]),
                    "avg_dollars": _safe_float(r["avg_dollars"]),
                    "expectancy_R": _safe_float(r["expectancy_R"]),
                    "label": r["label"],
                }
                for _, r in all_dir_rows.iterrows()
            ]

        # ─── Multi-TF filter overlay (Section 1.a) ──────────────
        if not mtf_winners_rows.empty:
            entry["multi_tf_filter"] = {
                "best_higher_tf_by_per_trade_lift": mtf_winners_rows.iloc[0]["higher_tf"],
                "ranked_by_lift_per_trade": [
                    {
                        "higher_tf": r["higher_tf"],
                        "n_baseline": _safe_int(r["n_baseline"]),
                        "n_aligned": _safe_int(r["n_aligned"]),
                        "baseline_avg_dollars": _safe_float(r["baseline_avg_$"]),
                        "aligned_avg_dollars": _safe_float(r["aligned_avg_$"]),
                        "lift_per_trade_dollars": _safe_float(r["lift_per_trade_$"]),
                        "aligned_total_dollars": _safe_float(r["aligned_total_$"]),
                        "aligned_wr_pct": _safe_float(r["aligned_wr_pct"]),
                        "aligned_pf": _safe_float(r["aligned_pf"]),
                    }
                    for _, r in mtf_winners_rows.iterrows()
                ],
            }

        if not mtf_rows.empty:
            entry["multi_tf_overlay_full"] = [
                {
                    "higher_tf": r["higher_tf"],
                    "alignment": r["alignment"],
                    "n": _safe_int(r["n"]),
                    "wr_pct": _safe_float(r["wr_pct"]),
                    "wilson_lo_pct": _safe_float(r["wilson_lo_pct"]),
                    "wilson_hi_pct": _safe_float(r["wilson_hi_pct"]),
                    "total_dollars": _safe_float(r["total_dollars"]),
                    "avg_dollars": _safe_float(r["avg_dollars"]),
                    "pf": _safe_float(r["pf"]),
                }
                for _, r in mtf_rows.iterrows()
            ]

        # ─── May-18 baseline comparison (Phase A.3) ─────────────
        if may18_row is not None:
            entry["may18_baseline_comparison"] = {
                "may18_n_trades": _safe_int(may18_row.get("n_trades")),
                "may18_best_policy": may18_row.get("best_policy"),
                "may18_best_total": _safe_float(may18_row.get("best_total")),
                "may18_baseline_total": _safe_float(may18_row.get("baseline_total")),
                "drift_n_trades": _safe_int((rec_row.get("n_trades") - may18_row.get("n_trades")) if rec_row is not None else None),
                "drift_baseline_dollars": _safe_float((rec_row.get("baseline_total") - may18_row.get("baseline_total")) if rec_row is not None else None),
                "best_policy_changed": (rec_row.get("best_policy") != may18_row.get("best_policy")) if rec_row is not None else None,
                "best_total_change_dollars": _safe_float((rec_row.get("best_total") - may18_row.get("best_total")) if rec_row is not None else None),
            }
        else:
            entry["may18_baseline_comparison"] = {
                "status": "no_may18_baseline_row",
                "note": "Strategy not present in May-18 recommendations CSV (likely a Phase 13 winner or freshly backfilled)",
            }

        per_strategy[s] = entry

    # ─── Cross-cutting findings ──────────────────────────────────────
    cross_findings = {
        "discrepancy_7_phase13_mismatches": {
            "verdict": "DELIBERATE — not regressions",
            "evidence": "docs/PHASE_13_IMPLEMENTATION_PLAN.md Sections U.3 and V.5",
            "summary": (
                "Bar-level optimizer picks tick_trail_* for bias_momentum, spring_setup, "
                "vwap_pullback_v2, ib_breakout, opening_session.open_drive. Phase 13 instead "
                "wires fixed_rr for these per tick-level Agent A verification (commit bb927bb). "
                "Section V.5 explicit: 'trails were artifacts; fixed_rr won' at tick granularity. "
                "Bar-level reproduction this run confirms the same mismatch — but the mismatch is "
                "EXPECTED, not a regression. Bar-level trail wins are a 4-tick OHLCV-granularity artifact."
            ),
            "bar_level_optimizer_picks_reproduced": True,
            "tick_level_overrides_remain_authoritative": True,
        },
        "discrepancy_9_missing_strategies": {
            "verdict": "RESOLVED — 5 backfilled, 2 structurally untestable",
            "backfilled_in_this_run": [
                "orb_v2 (1 trade, sparse)",
                "orb_fade (905 trades)",
                "compression_breakout_v2 (288 trades)",
                "compression_breakout_micro (254 trades)",
                "vwap_band_reversion (2,769 trades)",
            ],
            "untestable": {
                "nq_lsr": (
                    "Strategy needs liquidity_levels + TPO + volume_profile_lsr enrichment "
                    "not in CSV pipeline (phoenix_real_backtest.py:34-36 explicitly lists "
                    "this as a Cannot-test strategy)"
                ),
                "dom_pullback": (
                    "Deleted 2026-05-21 after 0 trades / 5y CSV-mode confirmed. Re-running "
                    "produces same 0-trade result because pipeline has no DOM history."
                ),
            },
        },
        "lab_reproductions_bit_exact": {
            "verdict": "All 4 Phase 13 winners match May-18 numbers EXACTLY",
            "details": {
                "a_asian_continuation": "596 trades / +$5,909 / 80.5% WR / 8.29 PF — exact",
                "e_multi_day_breakout": "685 trades / +$9,097.5 / 77.8% WR / 6.79 PF — exact",
                "g_inside_bar_breakout": "1015 trades / +$11,300 / 70.0% WR / 4.88 PF — exact",
                "raschke_baseline": "927 trades / +$12,779 / 67.7% WR / 4.10 PF — exact",
            },
            "implication": (
                "Lab code is deterministic AND 5y data hasn't drifted. May-18 numbers "
                "for these 4 winners can be trusted as current."
            ),
        },
        "noise_area_surprise_finding": {
            "verdict": "RETIRED STRATEGY HAS LARGEST BACKTEST EDGE IN SWEEP",
            "details": {
                "baseline_total": "-$9,912 (strategy's own managed cone exit)",
                "best_policy_total": "+$126,490 (tick_trail_8_post_05r)",
                "lift": "+$136,402 — largest single-strategy lift",
                "n_trades": 9467,
                "wr_pct": 57.7,
                "pf": 1.22,
            },
            "caveat": (
                "This is bar-level optimization, same selection-bias concern as Section V.5. "
                "Strategy was retired 2026-05-15 for live underperformance. Hour buckets show "
                "all 14 populated buckets are RED on baseline exits. Tick-level verification "
                "MUST run before any un-retire consideration. Walk-forward / CPCV / DSR / PBO "
                "via tools/walk_forward_harness.py required as the freeze-lift gate."
            ),
        },
        "multi_tf_filter_paradox": {
            "verdict": (
                "Most strategies show POSITIVE per-trade lift from higher-TF alignment "
                "BUT NEGATIVE total $ change. Per-trade lift is selection bias; filter "
                "throws away profitable counter-trend opportunities."
            ),
            "strategies_helped_by_filter": [
                "vwap_band_reversion (4h filter: -$3,012 → +$884, +$3,896 swing — flips to positive)",
                "ib_breakout (4h filter: +$722 → +$890, +23%)",
                "vwap_band_pullback (30min filter: +$1,692 → +$1,997, +18%)",
            ],
            "strategies_hurt_by_filter": [
                "bias_momentum (4h: -44% total despite +$0.49/trade lift)",
                "opening_session (4h: -46%)",
                "g_inside_bar_breakout (4h: -45%)",
                "a_asian_continuation (4h: -46%)",
                "vwap_pullback_v2 (4h: -41%)",
                "spring_setup (4h: -8%)",
                "e_multi_day_breakout (15min: -20%)",
            ],
            "structural_pattern": (
                "MEAN-REVERSION strategies benefit from higher-TF regime filter (wrong-regime "
                "MR is negative-expectancy). TREND/BREAKOUT strategies are already self-confirming "
                "at native TF and the filter discards profitable counter-trend opportunities."
            ),
            "recommendation": (
                "DO NOT apply higher-TF trend filter to trend-following / breakout strategies. "
                "ONLY apply to: vwap_band_reversion, vwap_band_pullback, ib_breakout."
            ),
        },
        "bias_momentum_signal_count_drift": {
            "verdict": "Strategy fires 2.07x more often than May-18 baseline",
            "may18_n_trades": 13790,
            "fresh_n_trades": 28557,
            "drift_pct": 107,
            "may18_total_baseline": 178379,
            "fresh_total_baseline": 315392,
            "interpretation": (
                "Likely cause: V2 config relaxations between May-18 and May-27 (the SIM "
                "heavy-test re-enables, the bias_momentum gate loosenings). Strategy IS more "
                "profitable in absolute terms ($315K vs $178K baseline). Per-trade economics "
                "appear preserved. Worth confirming via reconcile_sim_vs_backtest.py."
            ),
        },
        "hour_bucket_actionable_blocks": {
            "verdict": "Two strategies have explicit RED windows worth blocking",
            "spring_setup": {
                "proposed_block_windows_ct": [["04:00", "04:30"], ["18:00", "18:30"]],
                "lift_if_applied": "+$3,455 (+18% over baseline)",
                "evidence": "Wilson 95% upper bound on WR < breakeven (40%) with n>=20 in both buckets",
            },
            "compression_breakout_v2": {
                "proposed_block_windows_ct": [["18:00", "18:30"]],
                "lift_if_applied": "+$510",
                "evidence": "1 RED bucket; modest sample",
            },
            "noise_area": {
                "proposed_block_windows_ct": [["08:30", "15:30"]],
                "comment": "Strategy is already retired; this block applies the entire RTH window in case of un-retire consideration",
            },
        },
        "bias_momentum_historical_block_windows_NOT_reproduced": {
            "claim_in_config_strategies_py_lines_142_156": (
                "08:30-08:59 = 1W/9L = 10% WR (open volatility trap), "
                "10:00-13:29 = 0W/7L = 0% WR (mid-day chop)"
            ),
            "fresh_5y_data_finding": (
                "Strategy has GREEN bucket on 45 of 46 populated buckets (28,557 trades). "
                "No RED windows. The original block-window claim was likely tiny-sample "
                "(<20 trade buckets) AND/OR rendered obsolete by V2 config relaxations."
            ),
            "implication": (
                "The current session_block_windows=[] (empty) decision in bias_momentum config "
                "is now evidence-supported, not just debug-visibility convenience as the "
                "in-code comment suggests."
            ),
        },
    }

    # ─── Methodology + constraints ───────────────────────────────────
    methodology = {
        "data_window": "2021-05-17 → 2026-05-15 (5y MNQ futures, Databento)",
        "data_source": "data/historical/glbx-mdp3-20210517-20260517.ohlcv-1m.csv (raw) → mnq_1min_databento.csv (derived)",
        "bar_data_only": (
            "OHLCV only per operator scope clarification. No CVD/DOM/delta/footprint "
            "filters used. Order-flow data (TBBO 2026-03-17→2026-05-15) covered separately "
            "in Phase 13 Section U; not repeated here."
        ),
        "backtester": "tools/phoenix_real_backtest.py (14 of 18 TESTABLE_STRATEGIES instantiate)",
        "optimizer_battery": "tools/phoenix_stop_target_optimizer.py — 25 exit policies sweep",
        "parallelism": "Python multiprocessing.ProcessPoolExecutor with 'spawn' start method, 8 workers (physical cores)",
        "hour_bucket_methodology": {
            "granularity": "30-min CT slots",
            "ci_method": "Wilson 95% confidence interval on binomial WR",
            "green_criterion": "Wilson lower bound > breakeven WR (= 1 / (1+median_RR)) with n>=20",
            "red_criterion": "Wilson upper bound < breakeven WR with n>=20",
            "neutral": "Ambiguous CI overlap",
            "sparse": "n<20 — no claim either way",
        },
        "multi_tf_methodology": {
            "higher_tfs": ["5min", "15min", "30min", "1h", "4h"],
            "indicator": "EMA9 vs EMA21 stack on close + bar direction",
            "lookup": "Most-recently-CLOSED higher-TF bar strictly BEFORE entry_ts (no look-ahead)",
            "alignment_definition": "Trade direction matches higher-TF EMA stack direction",
        },
        "limitations": [
            "Bar-level granularity; trail and chandelier policies suffer the 4-tick OHLCV "
            "artifact that Phase 13 Section U corrected via tick-level verification. "
            "Use Section U.3 tick-validated picks for production decisions, not this report's bar-level picks.",
            "No walk-forward / CPCV / DSR / PBO statistical validation applied. "
            "tools/walk_forward_harness.py exists and should be run before any production "
            "recommendation is acted on. Multiple-testing penalty across 25 policies × ~17 "
            "strategies = 425 trials means selection-bias inflation is real.",
            "FREEZE_ACTIVE=True at config/strategies.py:52 — no config changes will be applied "
            "from this baseline until reconcile_sim_vs_backtest.py produces a defensible "
            "sim-vs-live divergence number for bias_momentum.",
            "Only OHLCV-derivable features. No order-flow / DOM / CVD-based filters tested.",
            "Strategy classes with structural data dependencies (nq_lsr, dom_pullback) cannot "
            "be tested through this pipeline.",
        ],
    }

    constraints_respected = {
        "no_code_edits_to_strategy_or_config": True,
        "no_validated_flips": True,
        "no_enabled_flips": True,
        "no_phase13_verdict_shipping": True,
        "freeze_active_status": "True (config/strategies.py:52) — unchanged",
        "live_broker_untouched": True,
        "live_strategy_allowlist": "('bias_momentum',) (config/settings.py:42) — unchanged",
        "branch": "weekly-evolution/2026-05-24 (no branch switch)",
        "new_files_written": [
            "tools/_baseline_2026-05-27/hour_buckets.py (research script)",
            "tools/_baseline_2026-05-27/parallel_optimizer.py (research script)",
            "tools/_baseline_2026-05-27/multi_tf_lab.py (research script)",
            "tools/_baseline_2026-05-27/synthesize_json.py (this synthesizer)",
            "out/_baseline_2026-05-27/*.csv (research outputs)",
            "out/_baseline_2026-05-27/*.md (research outputs)",
            "backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv (fresh per-trade)",
            "backtest_results/_baseline_2026-05-18/*.csv (preserved May-18 baseline)",
            "latest_and_greatest.json (this file)",
        ],
        "files_touched_in_canonical_paths": [
            "backtest_results/phoenix_new_strategy_lab.csv (overwritten by re-running lab; bit-exact reproduction)",
            "backtest_results/phoenix_trend_pullback_lab.csv (overwritten by re-running lab; bit-exact reproduction)",
            "backtest_results/phoenix_new_strategy_summary.csv (overwritten; summary table)",
            "backtest_results/phoenix_trend_pullback_summary.csv (overwritten; summary table)",
        ],
    }

    open_questions = {
        "high_priority": [
            "Should the bar-level optimizer's tick_trail picks for bias_momentum / "
            "spring_setup / vwap_pullback_v2 / opening_session.open_drive be RE-VERIFIED "
            "at tick level now that the sample is 2x larger? Phase 13 Section U used 60-day "
            "TBBO window (Mar-May 2026). The fresh recommendation reproduces the bar-level "
            "trail-wins pattern. Phase 13 doctrine says trails are artifacts but the larger "
            "sample may strengthen or weaken that conclusion. Recommend: re-run "
            "phoenix_tick_trail_verification.py on fresh per-trade CSV.",
            "noise_area shows +$126K bar-level edge with tick_trail_8_post_05r — should "
            "this trigger un-retirement consideration? Requires: tick-level validation + "
            "walk-forward gate + operator sign-off. Strategy was retired on LIVE evidence "
            "(0% WR on noise_area sim trades 2026-05-14) and managed-cone-exit bar-level "
            "evidence — the tick_trail exit was never tried live.",
            "bias_momentum 2.07x signal-count drift vs May-18: verify via "
            "reconcile_sim_vs_backtest.py whether live signal frequency matches the fresh "
            "backtest. If live fires at ~15/day and backtest says ~20/day, the sim-vs-live "
            "gap is the reconciliation harness's job (the freeze-lift precondition).",
            "vwap_band_reversion FLIPS from -$3K to +$5K with tick_trail_8_post_15r AND from "
            "-$3K to +$884 with 4h-HTF filter. These are INDEPENDENT lifts. Combined, "
            "strategy might be substantially profitable. Worth a focused study.",
        ],
        "medium_priority": [
            "Hour-bucket proposed blocks: spring_setup [04:00-04:30] + [18:00-18:30] = +$3,455 "
            "lift. Free win if applied; needs config change after freeze lifts.",
            "Multi-TF anti-pattern: confirm with operator that we will NOT add higher-TF "
            "filters to trend/breakout strategies despite the per-trade lift numbers looking attractive.",
            "Per-strategy per-year breakdown shows 5/6 strategies positive in 2026 partial-year. "
            "Worth comparing to actual sim P&L in 2026 YTD for sanity check.",
        ],
        "deferred_explicitly_out_of_scope_for_this_session": [
            "Section 5 combined-portfolio sim with correlation matrix (~200 LOC new tool, needs approval)",
            "Section 7b regime classifier + per-trade tagging (~150 LOC new tool, needs approval)",
            "Section 7c walk-forward harness wrapper for all strategies (~50 LOC wrapper)",
            "ATR-multiple stop sweep (~150 LOC; sweeps alternative SL placement at 1.0x-4.0x ATR)",
            "Section 6 novel-alpha mining (open-ended research, low expected yield per Phase 13 Section V)",
        ],
    }

    # ─── Top-level shape ─────────────────────────────────────────────
    report = {
        "metadata": {
            "title": "Phoenix Bot 5-Year NQ Optimization Baseline — 2026-05-27",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "session_branch": "weekly-evolution/2026-05-24",
            "session_freeze_active": True,
            "session_live_allowlist": ["bias_momentum"],
            "data_window": "2021-05-17 → 2026-05-15 (5 years)",
            "scope": (
                "OHLCV-only 5y analysis per operator clarification. "
                "Order-flow / DOM / CVD validation deferred to Phase 13 Section U "
                "(which already covers the 2-month TBBO window). "
                "NT8 volumetric recordings are stale-locked and unusable."
            ),
            "strategies_covered": len(strategies),
            "strategies_list": strategies,
            "total_trades_analyzed": int(rec_all["n_trades"].sum()) if rec_all is not None else 0,
        },
        "per_strategy": per_strategy,
        "cross_findings": cross_findings,
        "methodology": methodology,
        "constraints_respected": constraints_respected,
        "open_questions_and_followups": open_questions,
        "production_recommendations_PENDING_VALIDATION": {
            "DO_NOT_APPLY_WITHOUT": [
                "Tick-level verification via phoenix_tick_trail_verification.py",
                "Walk-forward / CPCV / DSR / PBO gates via walk_forward_harness.py",
                "reconcile_sim_vs_backtest.py producing defensible divergence number",
                "Operator review + explicit sign-off",
                "FREEZE_ACTIVE flip + commit message naming the lift authorization",
            ],
            "candidate_actions": {
                "hour_window_blocks_low_risk": [
                    {
                        "strategy": "spring_setup",
                        "action": "add block_windows=[('04:00','04:30'),('18:00','18:30')]",
                        "expected_lift_5y": 3455,
                    },
                    {
                        "strategy": "compression_breakout_v2",
                        "action": "add block_windows=[('18:00','18:30')]",
                        "expected_lift_5y": 510,
                    },
                ],
                "exit_policy_changes_HIGH_RISK_tick_unverified": [
                    {
                        "strategy": "bias_momentum",
                        "current_phase13_wire": "fixed_rr rr=2.0",
                        "bar_level_optimizer_pick": "tick_trail_4_post_1r",
                        "bar_level_lift": 105730,
                        "verdict": "DO NOT APPLY — Phase 13 Section V.5 says trails are bar-level artifacts",
                    },
                    {
                        "strategy": "spring_setup",
                        "current_phase13_wire": "fixed_rr rr=3.0",
                        "bar_level_optimizer_pick": "tick_trail_4_post_1r",
                        "bar_level_lift": 82060,
                        "verdict": "DO NOT APPLY — same caveat",
                    },
                    {
                        "strategy": "noise_area",
                        "current_status": "retired (enabled=False) 2026-05-15",
                        "bar_level_optimizer_pick": "tick_trail_8_post_05r → +$126K",
                        "verdict": "WORTH INVESTIGATING but requires full tick validation + walk-forward + operator decision before un-retire",
                    },
                ],
                "multi_tf_filter_changes": [
                    {
                        "strategy": "vwap_band_reversion",
                        "action": "consider 4h trend filter (require EMA9>EMA21 on 4h for LONG, opposite for SHORT)",
                        "expected_5y_total_change": "from -$3,012 → +$884 (+$3,896 swing)",
                        "verdict": "high-probability lift; combine with tick_trail_8_post_15r exit for cumulative gain",
                    },
                ],
                "negative_recommendations": [
                    "Do NOT add higher-TF trend filter to: bias_momentum, opening_session, "
                    "g_inside_bar_breakout, a_asian_continuation, vwap_pullback_v2, "
                    "spring_setup, e_multi_day_breakout. Per-trade lift is selection bias; "
                    "total $ collapses 8-46% on these.",
                ],
            },
        },
    }

    # ─── Write ────────────────────────────────────────────────────────
    JSON_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"[write] {JSON_PATH}")
    print(f"  size: {JSON_PATH.stat().st_size:,} bytes")
    print(f"  strategies: {len(per_strategy)}")
    print(f"  cross_findings: {len(cross_findings)}")
    print(f"  open_questions: {sum(len(v) if isinstance(v, list) else 1 for v in open_questions.values())}")


def _mae_interpretation(mae_row) -> str:
    """Plain-English read of the MAE/MFE pattern."""
    ratio = mae_row.get("mfe_mae_ratio")
    mae_p75 = mae_row.get("mae_p75")
    mfe_p75 = mae_row.get("mfe_p75")
    if pd.isna(ratio):
        return "insufficient data"
    if ratio > 1.5:
        return f"Healthy edge: winners reach {mfe_p75}t (p75) before losers hit {mae_p75}t MAE (p75). MFE/MAE ratio {ratio:.2f}."
    if ratio > 1.0:
        return f"Marginal edge: MFE/MAE ratio {ratio:.2f}. p75 MFE {mfe_p75}t vs p75 MAE {mae_p75}t."
    return f"NEGATIVE bar-level edge profile: MFE/MAE ratio {ratio:.2f} < 1.0 means losers move further than winners on average."


if __name__ == "__main__":
    main()
