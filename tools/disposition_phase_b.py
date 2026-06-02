"""Update logs/oracle/pending_changes.json for Phase B overnight dispositions.

3 proposals applied autonomously, 8 left pending operator review.
"""
from __future__ import annotations
import datetime as dt
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUEUE = ROOT / "logs" / "oracle" / "pending_changes.json"

NOW = dt.datetime.now(dt.timezone.utc).isoformat()
APPLIED_BY = "claude_code_overnight_2026-06-02_phase_B"

APPLIED_PARTIAL = {
    # (strategy, parameter_name_as_stored): commit_sha
    # These are bundled entries where target_rr shipped but per-direction
    # max_stop_ticks did not. status -> PARTIALLY_APPLIED.
    ("e_multi_day_breakout", "max_stop_ticks_and_target_rr"): "b406fb1",
    ("g_inside_bar_breakout", "max_stop_ticks_and_target_rr"): "a208a7b",
    ("raschke_baseline", "max_stop_ticks_and_target_rr"): "a662fbf",
}

NOT_TRIVIAL_NOTES = {
    ("a_asian_continuation", "LONG", "max_stop_ticks"):
        "current value already at 14 (the proposed value); no edit needed",
    ("a_asian_continuation", "SHORT", "max_stop_ticks"):
        "current value already at 14 (the proposed value); no edit needed",
    ("a_asian_continuation", "SHORT", "target_rr"):
        "NOT_TRIVIAL: target_rr is a single global knob; per-direction tilt "
        "would lower SHORT only. No per-direction target_rr exists. Operator "
        "decision required.",
    ("bias_momentum", "SHORT", "stop_fallback_ticks"):
        "NOT_TRIVIAL: stop_fallback_ticks is a single global knob; per-direction "
        "(SHORT=11, LONG=17) cannot be applied without per-direction schema. "
        "LIVE CANARY ALERT: this strategy is in LIVE_STRATEGY_ALLOWLIST.",
    ("bias_momentum", "LONG", "stop_fallback_ticks"):
        "NOT_TRIVIAL: same as SHORT; needs per-direction schema. LIVE CANARY ALERT.",
    ("bias_momentum", "BOTH", "target_rr"):
        "NOT_TRIVIAL: LLM said 'raise' without naming a specific value. "
        "LIVE CANARY ALERT.",
    ("e_multi_day_breakout", "BOTH", "max_stop_ticks_and_target_rr"):
        "PARTIALLY_APPLIED: target_rr 2.0->2.5 shipped (commit b406fb1). "
        "Per-direction max_stop_ticks (LONG=20, SHORT=14) NOT_TRIVIAL with "
        "single global knob; left for operator.",
    ("es_nq_confluence", "LONG", "target_ticks"):
        "DEFERRED: LLM explicitly flagged WFE ratio=139.8x as anomalous and "
        "advised operator wait on Phase C2 investigation before applying.",
    ("g_inside_bar_breakout", "BOTH", "max_stop_ticks_and_target_rr"):
        "PARTIALLY_APPLIED: target_rr 2.0->3.0 shipped (commit a208a7b). "
        "Per-direction max_stop_ticks (LONG=8, SHORT=10) NOT_TRIVIAL with "
        "single global knob; left for operator.",
    ("opening_session", "BOTH", "max_stop_ticks_and_target_rr"):
        "NOT_TRIVIAL: target_rr param does not exist on opening_session "
        "today (no per-strategy schema entry). Adding it is a code change, "
        "not a config edit. Per-direction max_stop_ticks also NOT_TRIVIAL.",
    ("raschke_baseline", "BOTH", "max_stop_ticks_and_target_rr"):
        "PARTIALLY_APPLIED: target_rr 2.0->3.5 shipped (commit a662fbf). "
        "Per-direction max_stop_ticks (LONG=13, SHORT=10) NOT_TRIVIAL with "
        "single global knob; left for operator.",
}


def main() -> None:
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    n_applied = 0
    n_notes = 0
    for entry in queue["pending"]:
        if entry.get("run_mode") != "research_v3_transcribed":
            continue
        s = entry["strategy"]
        d = entry["direction"]
        p = entry["parameter_name"]
        key_applied = (s, p)
        key_note = (s, d, p)
        if key_applied in APPLIED_PARTIAL:
            entry["status"] = "PARTIALLY_APPLIED"
            entry["approved"] = True
            entry["applied"] = True
            entry["applied_at"] = NOW
            entry["applied_by"] = APPLIED_BY
            entry["commit_sha"] = APPLIED_PARTIAL[key_applied]
            n_applied += 1
        if key_note in NOT_TRIVIAL_NOTES:
            entry.setdefault("disposition_reason", NOT_TRIVIAL_NOTES[key_note])
            n_notes += 1
    QUEUE.write_text(json.dumps(queue, indent=2, default=str), encoding="utf-8")
    print(f"marked {n_applied} as APPROVED_AUTONOMOUS")
    print(f"added disposition notes to {n_notes} NOT_TRIVIAL entries")


if __name__ == "__main__":
    main()
