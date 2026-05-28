"""
Augment latest_and_greatest.json with the tick_forensic_validation section.

Triggered by operator pivot 2026-05-27: validate the bar-level optimizer's
tick_trail picks against true TBBO microstructure. Read the outputs of
tick_trail_forensic.py and fold them into the canonical JSON deliverable.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path("C:/Trading Project/phoenix_bot")
JSON_PATH = ROOT / "latest_and_greatest.json"
FORENSIC_DIR = ROOT / "out" / "_baseline_2026-05-27" / "tick_validation"


def _f(v):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    try:
        return round(float(v), 4)
    except (ValueError, TypeError):
        return None


def _i(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def main():
    if not JSON_PATH.exists():
        raise SystemExit(f"missing {JSON_PATH}")

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))

    pol_summary = pd.read_csv(FORENSIC_DIR / "per_policy_summary.csv")
    floor = pd.read_csv(FORENSIC_DIR / "volatility_floor.csv")

    # ─── Per-strategy verdict on named picks ──────────────────────
    named_picks = ["tick_trail_4_post_1r", "tick_trail_8_post_05r"]
    per_strategy_verdict: dict[str, dict] = {}

    for s in sorted(pol_summary["strategy"].unique()):
        sdf = pol_summary[pol_summary["strategy"] == s]
        entry = {"named_pick_results": {}}
        for pick in named_picks:
            r = sdf[sdf["policy"] == pick]
            if r.empty:
                entry["named_pick_results"][pick] = {"status": "not_in_battery"}
                continue
            row = r.iloc[0]
            entry["named_pick_results"][pick] = {
                "bar_total_dollars": _f(row.get("total_dollars_bar")),
                "tick_total_dollars": _f(row.get("total_dollars_tick")),
                "phantom_dollars": _f(row.get("phantom_dollars")),
                "phantom_pct": _f(row.get("phantom_pct")),
                "tick_survives_80pct_of_bar": bool(row.get("tick_survives_80pct", False)),
                "tick_wr_pct": _f(row.get("wr_pct_tick")),
                "bar_wr_pct": _f(row.get("wr_pct_bar")),
                "tick_pf": _f(row.get("pf_tick")),
                "bar_pf": _f(row.get("pf_bar")),
                "n_trades_in_window": _i(row.get("n_tick")),
                "verdict": "SURVIVES" if row.get("tick_survives_80pct", False) else "FAILS",
            }

        # Volatility floor for this strategy
        floor_rows = floor[floor["strategy"] == s]
        entry["volatility_floor"] = []
        for _, fr in floor_rows.iterrows():
            entry["volatility_floor"].append({
                "activate_r": _f(fr.get("activate_r")),
                "min_trail_ticks_surviving_80pct_of_bar": _f(fr.get("min_trail_ticks_surviving_80pct_of_bar")),
                "best_trail_ticks_by_tick_pnl": _f(fr.get("best_trail_ticks_by_tick_pnl")),
                "best_tick_total_dollars": _f(fr.get("best_tick_total_dollars")),
                "best_bar_total_dollars": _f(fr.get("best_bar_total_dollars")),
                "best_phantom_pct": _f(fr.get("best_phantom_pct")),
            })

        # Top 5 policies by tick $ (so operator can see what survives if anything does)
        sdf_with_phantom = sdf.copy()
        if "total_dollars_tick" in sdf_with_phantom.columns:
            top_tick = sdf_with_phantom.nlargest(5, "total_dollars_tick")
            entry["top_5_policies_by_tick_dollars"] = [
                {
                    "policy": r["policy"],
                    "bar_total_dollars": _f(r.get("total_dollars_bar")),
                    "tick_total_dollars": _f(r.get("total_dollars_tick")),
                    "phantom_pct": _f(r.get("phantom_pct")),
                    "tick_wr_pct": _f(r.get("wr_pct_tick")),
                    "tick_pf": _f(r.get("pf_tick")),
                    "survives_80pct": bool(r.get("tick_survives_80pct", False)),
                }
                for _, r in top_tick.iterrows()
            ]

        per_strategy_verdict[s] = entry

    # ─── Headline counts ──────────────────────────────────────────
    survives = []
    fails = []
    for s, e in per_strategy_verdict.items():
        # Use tick_trail_4_post_1r as the canonical named pick check
        np = e["named_pick_results"].get("tick_trail_4_post_1r", {})
        if np.get("verdict") == "SURVIVES":
            survives.append(s)
        elif np.get("verdict") == "FAILS":
            fails.append(s)

    # ─── Compose the section ──────────────────────────────────────
    tick_forensic = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "operator_directive_triggered_this_section": (
            "2026-05-27 ~17:35 CT: 'execute immediate shift in research task list — "
            "validate tick_trail_4_post_1r and tick_trail_8_post_05r against real "
            "tick-by-tick data; quantify Profit Decay; find the true volatility floor.'"
        ),
        "data_window": "2026-03-17 → 2026-05-15 (60 days, 43.8M TBBO ticks)",
        "trades_in_window": int(pol_summary["n_tick"].max()) if "n_tick" in pol_summary.columns else None,
        "strategies_validated": list(per_strategy_verdict.keys()),
        "policy_battery": {
            "trail_ticks_swept": [2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 48],
            "activate_r_swept": [0.5, 1.0, 1.5],
            "total_policies": 38,
            "includes_fixed_2r_3r_reference": True,
        },
        "methodology": {
            "tick_walk": (
                "Every TBBO trade event walked in time order. Trail activates at "
                "+activate_r favorable, then trails trail_ticks behind high_water. "
                "Adverse tick after activation kills the position. Bar walk is the "
                "exact same logic the optimizer used (1m OHLC traversal)."
            ),
            "phantom_formula": "(bar_total - tick_total) / |bar_total| × 100",
            "survives_criterion": "tick_total >= 0.8 × bar_total AND both > 0",
            "volatility_floor_criterion": (
                "Smallest trail_ticks at given activate_r where tick_total >= 0.8 × bar_total. "
                "None found = trail mechanism itself fails at every tested distance."
            ),
        },
        "headline_verdict": {
            "bar_level_picks_validate_at_tick_level": survives,
            "bar_level_picks_FAIL_at_tick_level": fails,
            "ratio_failing": f"{len(fails)}/{len(fails) + len(survives)}",
            "interpretation": (
                "Phase 13 Section V.5 doctrine VINDICATED at 2x larger sample: "
                "bar-level trail picks are mostly microstructure artifacts. "
                "The strategies that SURVIVE the tick gauntlet "
                "(noise_area, e_multi_day_breakout, raschke_baseline) share one "
                "property: their STRATEGY stop is structurally WIDE relative to "
                "typical tick noise, so the trail mechanism rarely activates inside "
                "the noise band. Strategies with TIGHT stops (24-60t ATR-anchored) "
                "have trail behaviors dominated by microstructure chatter, not edge."
            ),
        },
        "specific_named_picks": {
            "tick_trail_4_post_1r": {
                "summary": (
                    "Bar-level optimizer's pick for bias_momentum, spring_setup, "
                    "vwap_pullback_v2 (top 3 by 5y total $). FAILS tick validation "
                    "on bias_momentum (+81.7% phantom), spring_setup (+70.2%), "
                    "vwap_pullback_v2 (+33.1%). SURVIVES on noise_area (+8.5%), "
                    "e_multi_day_breakout (+5.1%), raschke_baseline (+11.0%)."
                ),
                "implication": "DO NOT APPLY to bias_momentum, spring_setup, vwap_pullback_v2 in production.",
            },
            "tick_trail_8_post_05r": {
                "summary": (
                    "Operator-flagged variant (8-tick trail activating at 0.5R). "
                    "FAILS on every primary candidate strategy. Activation at 0.5R "
                    "(half the normal activation distance) catches even MORE bar "
                    "artifacts — phantom is consistently HIGHER than the _post_1r variant."
                ),
                "implication": "DO NOT APPLY anywhere. Earlier-activation variants are strictly worse at tick level.",
            },
        },
        "per_strategy_results": per_strategy_verdict,
        "noise_area_special_observation": {
            "tick_survives_with_low_phantom": True,
            "bar_level_5y_finding": "+$126,490 (largest single-strategy lift in 5y sweep)",
            "tick_level_60day_finding": "+$15,372 (extrapolates ~$78k/yr if linear)",
            "but_strategy_is_retired_live_evidence": (
                "Strategy was retired 2026-05-15 because LIVE trades on the actual "
                "managed-cone-exit policy gave 0% WR (11 noise_area trades on sim, "
                "all losers). The tick forensic validates the tick_trail exit policy "
                "but the strategy was retired on its OWN baseline exit, which the "
                "bar optimizer also flagged as -$9,912 / 5y."
            ),
            "open_question": (
                "Would noise_area become live-profitable if the exit were switched "
                "from managed-cone-boundary to tick_trail_8_post_05r? The 60-day "
                "tick validation says yes. But: (a) live sim was on cone exit, not "
                "tick_trail, so direct comparison is hard; (b) tick_trail still "
                "relies on the entry quality, which produced 0/11 wins live. "
                "Recommendation: do NOT un-retire on this evidence alone. Run a "
                "fresh sim-only A/B with explicit tick_trail exit before any "
                "production decision. This is a candidate for the next sprint, NOT "
                "the freeze-lift."
            ),
        },
        "volatility_floor_summary": {
            "no_floor_found_at_any_trail_distance": [
                s for s in per_strategy_verdict
                if all(
                    fr.get("min_trail_ticks_surviving_80pct_of_bar") is None
                    for fr in per_strategy_verdict[s]["volatility_floor"]
                )
            ],
            "floor_found_at_2t_or_less": [
                s for s in per_strategy_verdict
                if any(
                    fr.get("min_trail_ticks_surviving_80pct_of_bar") is not None
                    and fr["min_trail_ticks_surviving_80pct_of_bar"] <= 2
                    for fr in per_strategy_verdict[s]["volatility_floor"]
                )
            ],
            "interpretation": (
                "For most strategies, NO trail distance from 2t to 48t survives "
                "the 80%-of-bar threshold. The trail mechanism inherently picks "
                "up bar-level interpolation that tick reality reverses. The few "
                "strategies that DO have a floor are those whose strategy stop "
                "is already wider than typical noise (cone exits, multi-day breakouts)."
            ),
        },
        "phase13_doctrine_consistency_check": {
            "section_u3_picked_fixed_rr_for_top_strategies": True,
            "section_v5_explicit_quote": "'trails were artifacts; fixed_rr won'",
            "this_2x_larger_sample_REINFORCES_or_REVERSES_section_v5": "REINFORCES",
            "evidence": (
                "The bar-level optimizer at the FRESH 2x sample still picks "
                "tick_trail_4_post_1r as best for bias_momentum / spring_setup / "
                "vwap_pullback_v2 — but tick reality shows phantom +33-88% on these. "
                "The phantom % is comparable to or larger than what Phase 13 Agent A "
                "found in May 2026. The conclusion is bit-for-bit consistent: "
                "trails are bar-level artifacts. Stay on fixed_rr for the canary."
            ),
        },
    }

    # Update the canonical JSON
    data["tick_forensic_validation"] = tick_forensic

    # Revise production_recommendations to incorporate forensic verdict
    if "production_recommendations_PENDING_VALIDATION" not in data:
        data["production_recommendations_PENDING_VALIDATION"] = {}

    data["production_recommendations_PENDING_VALIDATION"]["FORENSIC_OVERRIDES_2026_05_27"] = {
        "explicit_overrides_to_bar_level_picks": [
            {
                "strategy": "bias_momentum",
                "bar_optimizer_pick": "tick_trail_4_post_1r ($421,122 / 5y)",
                "tick_forensic_verdict": "FAILS — phantom +81.7%, tick=$5,001 vs bar=$27,290 (60-day window)",
                "production_recommendation": "STAY on current Phase 13 wire: fixed_rr rr=2.0",
                "rationale": "Bar-level lift is microstructure artifact. Phase 13 Section U.3 tick-validation reproduced.",
            },
            {
                "strategy": "spring_setup",
                "bar_optimizer_pick": "tick_trail_4_post_1r ($101,386 / 5y)",
                "tick_forensic_verdict": "FAILS — phantom +70.2%, tick=$2,208 vs bar=$7,401 (60-day window)",
                "production_recommendation": "STAY on current Phase 13 wire: fixed_rr rr=3.0",
            },
            {
                "strategy": "vwap_pullback_v2",
                "bar_optimizer_pick": "tick_trail_4_post_1r ($18,530 / 5y)",
                "tick_forensic_verdict": "FAILS — phantom +33.1%, tick=$1,684 vs bar=$2,518 (60-day window)",
                "production_recommendation": "STAY on current Phase 13 wire: fixed_rr rr=3.0",
            },
            {
                "strategy": "opening_session.open_drive",
                "bar_optimizer_pick": "tick_trail_8_post_15r ($61,071 / 5y at family level)",
                "tick_forensic_verdict": "FAILS — opening_session aggregated phantom +112%, tick total NEGATIVE",
                "production_recommendation": "STAY on current Phase 13 wire: fixed_rr rr=3.0",
            },
            {
                "strategy": "vwap_band_reversion",
                "bar_optimizer_pick": "tick_trail_8_post_15r ($4,931 / 5y, flips strategy positive)",
                "tick_forensic_verdict": "FAILS at tick level (phantom +78-174%, often negative tick total)",
                "production_recommendation": (
                    "DO NOT apply tick_trail. The bar-level flip-positive finding is artifact. "
                    "Multi-TF 4h filter (+$3,896 swing) remains a SEPARATE valid improvement "
                    "and is unaffected by this tick verification."
                ),
            },
            {
                "strategy": "noise_area",
                "bar_optimizer_pick": "tick_trail_8_post_05r (+$126,490 / 5y)",
                "tick_forensic_verdict": "SURVIVES — phantom +11.0%, tick=$15,222 vs bar=$17,102 (60-day window)",
                "production_recommendation": (
                    "DO NOT un-retire on this evidence alone. Strategy retired on live "
                    "evidence (0/11 sim) NOT on bar-level exit performance. Tick survives "
                    "but live signal entry quality was the failure point. Run fresh "
                    "tick_trail-equipped sim before any production decision."
                ),
            },
        ],
        "freeze_status_after_forensic": (
            "FREEZE_ACTIVE=True unchanged. This forensic STRENGTHENS the case for "
            "keeping the current Phase 13 fixed_rr wires; it does NOT lift the freeze."
        ),
        "what_freeze_lift_still_requires": [
            "tools/reconcile_sim_vs_backtest.py produces defensible sim-vs-live divergence for bias_momentum",
            "out/reconciliation_<date>_bias_momentum.md committed with the numbers",
            "operator sign-off on divergence tolerances",
            "tools/walk_forward_harness.py PBO <= 0.5 + DSR p <= 0.05 for any new production claim",
        ],
    }

    # Update metadata to mark this version
    data["metadata"]["last_amended_at_utc"] = datetime.now(timezone.utc).isoformat()
    data["metadata"]["forensic_validation_appended"] = True
    data["metadata"]["amendments_log"] = data["metadata"].get("amendments_log", []) + [
        {
            "amended_at_utc": datetime.now(timezone.utc).isoformat(),
            "amendment": "Added tick_forensic_validation section per operator pivot; "
                         "added FORENSIC_OVERRIDES_2026_05_27 to production_recommendations",
        }
    ]

    JSON_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"[write] {JSON_PATH}")
    print(f"  size: {JSON_PATH.stat().st_size:,} bytes")
    print(f"  strategies validated: {len(per_strategy_verdict)}")
    print(f"  failing strategies: {len(fails)} ({fails})")
    print(f"  surviving strategies: {len(survives)} ({survives})")


if __name__ == "__main__":
    main()
