"""
Fold the day-2 (2026-05-28) friction / hour-filter / ORB analyses into
latest_and_greatest.json as a new canonical section.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path("C:/Trading Project/phoenix_bot")
JSON_PATH = ROOT / "latest_and_greatest.json"
FRIC = ROOT / "out/_baseline_2026-05-27/friction"
HF = ROOT / "out/_baseline_2026-05-27/hour_filter"


def _csv(p):
    return pd.read_csv(p) if Path(p).exists() else None


def main():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))

    # ── Execution friction ───────────────────────────────────────────
    fprofile = json.loads((FRIC / "friction_profile.json").read_text(encoding="utf-8"))
    survives = _csv(FRIC / "survives_friction_ranking.csv")
    surv_pos = survives[survives.net > 0].sort_values("net", ascending=False) if survives is not None else None
    surv_neg = survives[survives.net <= 0].sort_values("net") if survives is not None else None

    section = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Execution friction + time-of-day walk-forward + opening_session sub-breakdown + ORB win-rate analysis. All read-only research; no production strategy/config changed; FREEZE_ACTIVE remains True.",

        "execution_friction": {
            "commission_per_round_turn_dollars": fprofile["commission"]["round_turn_fees_per_contract_$"],
            "commission_source": fprofile["commission"]["source"],
            "slippage_finding": fprofile["slippage"]["KEY_FINDING"],
            "operator_decision": "Use CONSERVATIVE friction in the harness (commission $2.82 + 2 ticks/side adverse = $4.82/RT), NOT the favorable TBBO-measured slippage (which is a bar-close-reference artifact).",
            "harness_injection": "tools/phoenix_real_backtest.py now has flag-gated --apply-decay (default OFF). Verified to deduct exactly $4.82/RT; gross ticks preserved.",
            "survives_friction_verdict": {
                "robust_rule": "A strategy survives only if avg gross $/trade > $2.82 commission floor.",
                "profitable_after_friction": (
                    surv_pos[["strategy", "n", "gross", "net"]].to_dict("records") if surv_pos is not None else []
                ),
                "DIE_under_friction": (
                    surv_neg[["strategy", "n", "gross", "net"]].to_dict("records") if surv_neg is not None else []
                ),
                "headline": (
                    "spring_setup (+$19,326 gross -> -$80,747 net) and vwap_pullback_v2 "
                    "(+$10,448 -> -$15,759) cannot cover commission: their per-trade edge "
                    "($0.93, $1.92) is below the $2.82 floor. Verdict holds on commission "
                    "ALONE, independent of any slippage assumption. bias_momentum is the "
                    "dominant survivor ($11.04/trade, +$177,748 net)."
                ),
            },
        },

        "time_of_day_walkforward": {
            "method": "Expanding-window walk-forward (select friction-positive 30-min CT windows on history, trade forward; test years 2023-2026). Robust = filtered beats all-hours in majority of OOS folds AND summed OOS net positive.",
            "single_split_caveat": "A single train/test split flagged vwap_band_reversion as 'rescued' (+$928); the multi-fold walk-forward OVERTURNED that (-$415 OOS). Trust the walk-forward.",
            "verdicts": {
                "vwap_pullback_v2": "ROBUST — only strategy time-of-day filtering rescues. Overnight windows: 5y net -$4,885 (all hrs) -> +$7,817 (windowed). Config proposal queued.",
                "vwap_band_reversion": "NOT ROBUST (-$415 OOS; single-split rescue did not hold)",
                "spring_setup": "NOT ROBUST (-$7,797 OOS even filtered; only 2026 positive)",
                "orb_fade": "technically passes but +$147 OOS = noise",
                "ib_breakout / compression_breakout_v2": "NOT ROBUST (compression has no profitable hour at all)",
                "noise_area": "NOT rescuable — zero 30-min windows ever clear the commission floor",
                "profitable_strategies": "NONE benefit from hour-filtering — they have broad edge across hours; trimming hurts. Time-of-day filtering is a single-strategy tool (vwap_pullback_v2 only).",
            },
        },

        "opening_session_subs": {
            "note": "Re-run post Bug-B2-fix. Only 2 of 6 sub-evaluators fired (orb, open_drive); the other 4 produced zero (worth a separate look).",
            "orb": {"n": 3483, "wr_pct": 44.6, "gross": 47112, "net_after_commission": 37289,
                    "avg_per_trade": 13.53, "verdict": "Strongly profitable. The opening_session family's engine. Edge spans 08:30-14:00 CT, concentrated at the open."},
            "open_drive": {"n": 275, "wr_pct": 37.1, "gross": 4164, "net_after_commission": 3388,
                           "avg_per_trade": 15.14, "note": "Bug B2 (pivot_pp target) FIXED 2026-05-18 — no longer targets pivot point. Small contributor; naturally trades 08:30-09:00."},
        },

        "orb_win_rate_analysis": {
            "question": "Operator asked: how to raise orb/opening-range win rate. Looked tick-by-tick.",
            "exit_WR_vs_net_INVERSE": {
                "highest_WR_shippable": "profit_lock_05r: WR 77.5% but net $27,177 (below baseline)",
                "baseline": "WR 44.6%, net $37,290",
                "highest_NET_shippable": "time_30min: net $42,699 but WR 35.8% (lowest)",
                "conclusion": "WR and net move in OPPOSITE directions. Raising orb WR to 77% (profit_lock_05r) costs ~27% of net. No shippable exit raises both (tick_trail variants that do are bar-level artifacts; mfe_oracle is look-ahead).",
            },
            "entry_features": {
                "small_OR_whipsaw": "stop<=30t (small opening range) -> WR 27.9% (vs 44.6% baseline). Skipping raises WR but removes net-positive trades.",
                "best_hour": "08:30 CT = 50% WR; degrades to 32% by 11:00 CT.",
            },
            "tick_by_tick_2mo": {
                "n_trades": 78,
                "finding": "NO clean false-breakout signature. Winners and losers both go adverse ~73% of the time in first 30s. If anything, LOSERS run MORE favorable early (+21t at 30s vs winners +8t) — false breakouts spike then collapse. No confirmation-delay or retest threshold robustly raises WR.",
                "caveat": "2-month TBBO window only; 78 trades.",
            },
            "bottom_line": "orb's 44.6% WR is structural to breakout trading (many small false-breakout losses fund the rare trend-day winners). It is already net-profitable BECAUSE of low-WR/high-payoff. No lever raises WR without sacrificing net or relying on artifacts.",
        },

        "orb_v2_diagnosis": {
            "symptom": "orb_v2 produces 1 trade in 5y (effectively untestable).",
            "hypotheses_tested_and_DISPROVEN": [
                "CVD-alignment gate: ran orb_v2 with require_cvd_aligned=False -> still 1 trade. NOT the cause.",
                "OR-size band: measured all 1,290 days' 15-min opening ranges; 51% fall within orb_v2's [11,80]pt band. NOT the cause.",
            ],
            "real_status": "Implementation bug deeper in the gate chain (OR construction / entry-window / 5m-confirmation). NOT a data limitation.",
            "key_correction": "ORB breakout IS fully testable on OHLCV — opening_session.orb proves it (3,483 trades, +$37,289 net, profitable across all 4 OOS folds). orb_v2 is a REDUNDANT, broken reimplementation.",
            "recommendation": "Do not invest in debugging orb_v2; use the working, profitable opening_session.orb instead.",
        },

        "strategy_test_coverage_complete_roster": {
            "tested_16": "bias_momentum, spring_setup, ib_breakout, noise_area, opening_session, vwap_band_pullback, vwap_band_reversion, orb_fade, compression_breakout_v2, compression_breakout_micro, vwap_pullback_v2, es_nq_confluence, a_asian_continuation, e_multi_day_breakout, g_inside_bar_breakout, raschke_baseline",
            "did_not_test": {
                "orb_v2": "1 trade (implementation bug, not data)",
                "nq_lsr": "data dep (liquidity/TPO)",
                "dom_pullback": "data dep (DOM); 0 trades",
                "footprint_cvd_reversal": "data dep (volumetric, now restored)",
                "big_move_signal": "disabled, 0 signals",
                "vwap_pullback_v1 / orb_v1 / compression_breakout_v1": "disabled, superseded by v2 (v2s tested)",
                "high_precision_only": "retired/disabled",
            },
            "footprint_testability": "Cannot test footprint edge on 5y (only close-open proxy). 2-month TBBO footprint too thin per-strategy (opening_session ~29 trades). Restored volumetric stream needed to accumulate ~30+ days.",
        },
    }

    data["day2_2026_05_28_friction_hourfilter_orb"] = section
    data["metadata"]["last_amended_at_utc"] = datetime.now(timezone.utc).isoformat()
    data["metadata"]["amendments_log"] = data["metadata"].get("amendments_log", []) + [{
        "amended_at_utc": datetime.now(timezone.utc).isoformat(),
        "amendment": "Added day2_2026_05_28 section: execution friction + survives-friction ranking, "
                     "time-of-day walk-forward, opening_session sub-breakdown, ORB win-rate analysis "
                     "(WR/net inverse), orb_v2 diagnosis, complete test-coverage roster.",
    }]

    JSON_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"[write] {JSON_PATH}  ({JSON_PATH.stat().st_size:,} bytes)")
    print(f"  top-level keys: {list(data.keys())}")


if __name__ == "__main__":
    main()
