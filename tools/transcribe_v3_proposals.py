"""One-shot: transcribe the 11 narrative proposals from the 2026-06-01 v3
Oracle research run into logs/oracle/pending_changes.json.

Per the overnight master-run prompt, the LLM produced 11 explicit
stop/target proposals in its debrief but ran out of token budget before
calling propose_change for each. This script appends them to the queue
with status=PENDING_OPERATOR_REVIEW so the operator can decide on
each in the morning.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics import prepared_queries as pq  # noqa: E402
QUEUE_PATH = ROOT / "logs" / "oracle" / "pending_changes.json"
FACTS_PATH = ROOT / "logs" / "oracle" / "research" / "2026-06-01_facts.json"
DEBRIEF_PATH = ROOT / "logs" / "oracle" / "research" / "2026-06-01_debrief.md"


def metrics_for(strategy: str) -> dict:
    facts = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
    m = facts["strategies"].get(strategy, {}).get("metrics", {})
    return {
        "n_trades": m.get("n_trades"),
        "profit_factor": m.get("profit_factor"),
        "dsr": m.get("dsr"),
        "psr": m.get("psr"),
        "hlz_t_stat": m.get("hlz_t_stat"),
        "bhy_p_adjusted": m.get("bhy_p_adjusted"),
        "wfe_ratio": m.get("wfe_ratio"),
    }


def safe_current(strategy: str, param: str):
    try:
        return pq.current_param_value(strategy, param)
    except Exception as e:  # noqa: BLE001
        return f"<lookup failed: {type(e).__name__}: {e}>"


NOW = dt.datetime.now(dt.timezone.utc).isoformat()

# Each entry mirrors the 11 numbered proposals from the master report
# Section "Intended proposals". rationale carries MAE elbow + MFE p90
# verbatim as the prompt requires.

PROPOSALS = [
    # 1
    {
        "strategy": "a_asian_continuation",
        "direction": "LONG",
        "parameter_name": "max_stop_ticks",
        "proposed_value": 14,
        "rationale": (
            "v3 narrative (a_asian_continuation): MAE elbows at 14 ticks for "
            "both LONG and SHORT; MFE p90 LONG=17 ticks, SHORT=22 ticks. "
            "Hours CT 4-5 are the strongest (PF 4.70-5.25). Proposal: tighten "
            "LONG stop to MAE elbow at 14 ticks."
        ),
        "expected_improvement": (
            "Current value already at 14 ticks; verify max_stop_ticks "
            "actually binds in live config (LONG stop fallback path)."
        ),
        "confidence": "HIGH",
    },
    # 2
    {
        "strategy": "a_asian_continuation",
        "direction": "SHORT",
        "parameter_name": "max_stop_ticks",
        "proposed_value": 14,
        "rationale": (
            "v3 narrative (a_asian_continuation): MAE elbow SHORT=14 ticks. "
            "Tighten SHORT stop to MAE elbow at 14 ticks. MFE p90 SHORT=22 "
            "ticks, so post-tightening RR=22/14=1.57 informs the target side."
        ),
        "expected_improvement": (
            "Current value already at 14 ticks; verify max_stop_ticks "
            "actually binds in live config (SHORT stop fallback path)."
        ),
        "confidence": "HIGH",
    },
    # 3
    {
        "strategy": "a_asian_continuation",
        "direction": "SHORT",
        "parameter_name": "target_rr",
        "proposed_value": 1.57,
        "rationale": (
            "v3 narrative (a_asian_continuation): SHORT MFE p90=22 ticks; "
            "stop=14 ticks; target_rr=22/14=1.57. Current target_rr=2.0 "
            "implies 28-tick target -- past MFE p90, fills tail. Lower SHORT "
            "target_rr to MFE-p90-justified 1.57 to capture more wins."
        ),
        "expected_improvement": (
            "WR likely rises (more fills at the tighter target); per-trade "
            "PnL drops slightly; PF should be flat or up."
        ),
        "confidence": "MEDIUM",
    },
    # 4
    {
        "strategy": "bias_momentum",
        "direction": "SHORT",
        "parameter_name": "stop_fallback_ticks",
        "proposed_value": 11,
        "rationale": (
            "v3 narrative (bias_momentum): MAE elbow SHORT=11 ticks "
            "(materially tighter than LONG=17). OOS PF (1.34) exceeds IS PF "
            "(1.33) -- strong out-of-sample signal at n=28,530. Tighten "
            "SHORT stop_fallback_ticks to 11 to align with MAE elbow."
        ),
        "expected_improvement": (
            "Smaller per-trade loss on the SHORT side; expected PF lift "
            "concentrated in the SHORT direction. LIVE CANARY ALERT applies."
        ),
        "confidence": "HIGH",
    },
    # 5
    {
        "strategy": "bias_momentum",
        "direction": "LONG",
        "parameter_name": "stop_fallback_ticks",
        "proposed_value": 17,
        "rationale": (
            "v3 narrative (bias_momentum): MAE elbow LONG=17 ticks. Current "
            "stop_fallback_ticks=64 is far past the MAE elbow. Tighten LONG "
            "stop_fallback_ticks to 17 (MAE elbow). n=28,530, PF=1.28, OOS "
            "PF 1.34 > IS 1.33."
        ),
        "expected_improvement": (
            "Smaller per-trade loss on the LONG side; expected PF lift on "
            "LONG. LIVE CANARY ALERT applies."
        ),
        "confidence": "HIGH",
    },
    # 6
    {
        "strategy": "bias_momentum",
        "direction": "BOTH",
        "parameter_name": "target_rr",
        "proposed_value": "raise (LLM did not name a specific value)",
        "rationale": (
            "v3 narrative (bias_momentum): MFE p90 values of 140 ticks "
            "(LONG) and 127 ticks (SHORT) are very large relative to the "
            "MAE elbows (17 / 11). Current target_rr=2.5 leaves significant "
            "tail unrealized. Raise target_rr to align with MFE p90."
        ),
        "expected_improvement": (
            "Per-trade PnL on winners climbs materially; WR may dip "
            "slightly. LIVE CANARY ALERT applies."
        ),
        "confidence": "MEDIUM",
    },
    # 7 -- bundled per master report numbered list
    {
        "strategy": "e_multi_day_breakout",
        "direction": "BOTH",
        "parameter_name": "max_stop_ticks_and_target_rr",
        "proposed_value": {
            "LONG.max_stop_ticks": 20,
            "SHORT.max_stop_ticks": 14,
            "target_rr": 2.5,
        },
        "rationale": (
            "v3 narrative (e_multi_day_breakout): MAE elbow LONG=20 ticks, "
            "SHORT=14 ticks. MFE p90 LONG=50, SHORT=56 ticks. Tighten LONG "
            "max_stop_ticks 30->20 and SHORT 30->14; raise target_rr "
            "2.0->2.5 (based on LONG MFE p90 / stop ratio)."
        ),
        "expected_improvement": (
            "Tighter per-trade loss on losers; bigger capture on winners. "
            "n=1,360, PF=3.48."
        ),
        "confidence": "HIGH",
    },
    # 8
    {
        "strategy": "es_nq_confluence",
        "direction": "LONG",
        "parameter_name": "target_ticks",
        "proposed_value": 100,
        "rationale": (
            "v3 narrative (es_nq_confluence): LONG-only strategy; MAE elbow "
            "insufficient sample; MFE p90 LONG=100 ticks. Current "
            "target_ticks=96 is essentially at the MFE p90. Marginal raise to "
            "100. **CAUTION**: WFE ratio 139.8x is anomalous -- investigate "
            "OOS fold composition (Phase C2 / open item #4) before acting."
        ),
        "expected_improvement": (
            "Marginal -- already near MFE p90. Operator should wait on the "
            "WFE 139.8 investigation before applying."
        ),
        "confidence": "MEDIUM",
    },
    # 9 -- bundled
    {
        "strategy": "g_inside_bar_breakout",
        "direction": "BOTH",
        "parameter_name": "max_stop_ticks_and_target_rr",
        "proposed_value": {
            "LONG.max_stop_ticks": 8,
            "SHORT.max_stop_ticks": 10,
            "target_rr": 3.0,
        },
        "rationale": (
            "v3 narrative (g_inside_bar_breakout): LONG MAE elbow=8 ticks "
            "(tightest elbow in the panel); SHORT elbow=10 ticks. MFE p90 "
            "LONG=32, SHORT=30 ticks. OOS PF (3.46) exceeds IS PF (2.57). "
            "Tighten LONG max_stop_ticks 30->8, SHORT 30->10; raise "
            "target_rr 2.0->3.0."
        ),
        "expected_improvement": (
            "Major tightening on stop side (~73% smaller loss budget per "
            "trade); bigger capture on winners. n=1,990, PF=2.47."
        ),
        "confidence": "HIGH",
    },
    # 10 -- bundled
    {
        "strategy": "opening_session",
        "direction": "BOTH",
        "parameter_name": "max_stop_ticks_and_target_rr",
        "proposed_value": {
            "LONG.max_stop_ticks": 13,
            "SHORT.max_stop_ticks": 9,
            "target_rr": "raise (LLM did not name specific value)",
        },
        "rationale": (
            "v3 narrative (opening_session): SHORT MAE elbow=9 ticks (very "
            "tight); LONG elbow=13 ticks. MFE p90 LONG=103, SHORT=102 ticks. "
            "Current max_stop_ticks=200 is enormous relative to MAE elbows. "
            "Tighten LONG 200->13, SHORT 200->9; raise target_rr. **NOTE**: "
            "target_rr param does not currently exist on opening_session; "
            "needs schema add before any target_rr edit can land."
        ),
        "expected_improvement": (
            "Dramatic tightening on the stop side; possible target_rr addition "
            "blocked by missing param. n=3,711, PF=1.44."
        ),
        "confidence": "MEDIUM",
    },
    # 11 -- bundled
    {
        "strategy": "raschke_baseline",
        "direction": "BOTH",
        "parameter_name": "max_stop_ticks_and_target_rr",
        "proposed_value": {
            "LONG.max_stop_ticks": 13,
            "SHORT.max_stop_ticks": 10,
            "target_rr": 3.5,
        },
        "rationale": (
            "v3 narrative (raschke_baseline): MAE elbow LONG=13 ticks, "
            "SHORT=10 ticks. MFE p90 LONG=46, SHORT=38 ticks. SHORT direction "
            "materially stronger (PF=3.67, WR=74.3%) vs LONG (PF=2.22, "
            "WR=65.7%). OOS PF (3.72) exceeds IS PF (3.23). Tighten LONG "
            "max_stop_ticks 40->13, SHORT 40->10; raise target_rr 2.0->3.5."
        ),
        "expected_improvement": (
            "Tighter per-trade loss; bigger winner capture. n=1,692, PF=2.65."
        ),
        "confidence": "HIGH",
    },
]


def main() -> None:
    queue = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    appended = 0
    for p in PROPOSALS:
        strategy = p["strategy"]
        finding_id = f"confirmed_{strategy}_2026-06-01"
        entry = {
            "proposed_at": NOW,
            "run_mode": "research_v3_transcribed",
            "source_file": "logs/oracle/research/2026-06-01_debrief.md",
            "strategy": strategy,
            "direction": p["direction"],
            "parameter_name": p["parameter_name"],
            "current_value": (
                safe_current(strategy, p["parameter_name"])
                if isinstance(p["parameter_name"], str)
                and "_and_" not in p["parameter_name"]
                else "see rationale (multi-param bundle)"
            ),
            "proposed_value": p["proposed_value"],
            "rationale": p["rationale"],
            "expected_improvement": p["expected_improvement"],
            "confidence": p["confidence"],
            "sample_size": metrics_for(strategy).get("n_trades") or 0,
            "finding_id": finding_id,
            "metrics": metrics_for(strategy),
            "status": "PENDING_OPERATOR_REVIEW",
            "approved": False,
            "applied": False,
        }
        queue["pending"].append(entry)
        appended += 1

    QUEUE_PATH.write_text(
        json.dumps(queue, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"appended {appended} proposals; queue now has {len(queue['pending'])} entries")


if __name__ == "__main__":
    main()
