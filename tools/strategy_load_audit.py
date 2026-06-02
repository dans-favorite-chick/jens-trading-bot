"""Phase F overnight audit: print which strategies the prod/sim loader
will instantiate today vs which it will fence off.

READ-ONLY. No edits. Operator runs this manually if curious; the
overnight master run uses its output to populate
logs/oracle/research/2026-06-02_strategy_load_audit.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.strategies import STRATEGIES  # noqa: E402

# Mirror the strategy_classes dict in bots/base_bot.py (lines 1680-1720
# as of commit 55a542b). Updates here should be matched by base_bot
# whenever a new strategy lands -- diverging the two would silently
# fence an enabled strategy off in production.
KNOWN_CLASSES: set[str] = {
    "bias_momentum",
    "spring_setup",
    "vwap_pullback",
    "vwap_band_pullback",
    "vwap_band_reversion",
    "high_precision_only",
    "ib_breakout",
    "compression_breakout",
    "orb",
    "noise_area",
    "opening_session",
    "footprint_cvd_reversal",
    "big_move_signal",
    "nq_lsr",
    "orb_fade",
    "orb_v2",
    "compression_breakout_v2",
    "compression_breakout_micro",
    "vwap_pullback_v2",
    "es_nq_confluence",
    "a_asian_continuation",
    "e_multi_day_breakout",
    "g_inside_bar_breakout",
    "raschke_baseline",
}


def classify(bot_only_validated: bool) -> list[dict]:
    """Replicate base_bot.load_strategies' filtering for a given bot tier."""
    out = []
    for name, cfg in STRATEGIES.items():
        enabled = bool(cfg.get("enabled", True))
        validated = bool(cfg.get("validated", False))
        wfg = cfg.get("walk_forward_gate")
        if name not in KNOWN_CLASSES:
            decision = "FENCED_NO_CLASS"
        elif not enabled:
            decision = "FENCED_DISABLED"
        elif bot_only_validated and not validated:
            decision = "FENCED_UNVALIDATED_LIVE"
        else:
            decision = "WILL_LOAD"
        out.append({
            "name": name,
            "enabled": enabled,
            "validated": validated,
            "walk_forward_gate": wfg,
            "decision": decision,
        })
    return out


def render(label: str, rows: list[dict]) -> str:
    lines = [f"## {label}\n",
             f"{'strategy':<28} {'enabled':>8} {'valid':>7} {'wfg':<14} decision",
             "-" * 78]
    for r in rows:
        wfg = r["walk_forward_gate"] or "-"
        lines.append(
            f"{r['name']:<28} {str(r['enabled']):>8} {str(r['validated']):>7} {str(wfg):<14} {r['decision']}"
        )
    will_load = sum(1 for r in rows if r["decision"] == "WILL_LOAD")
    lines.append(f"\nWILL_LOAD count: {will_load} of {len(rows)} total")
    return "\n".join(lines)


def main() -> int:
    sim_rows = classify(bot_only_validated=False)
    prod_rows = classify(bot_only_validated=False)  # ProdBot.only_validated=False

    out = []
    out.append("# Strategy Load Audit — 2026-06-02 overnight Phase F\n")
    out.append("Replicates `bots/base_bot.py::load_strategies` filter "
                "(silent-skip for unknown class names; FENCED_DISABLED for "
                "enabled=False; FENCED_UNVALIDATED_LIVE only when "
                "`only_validated=True`).")
    out.append("")
    out.append("Per `tests/test_prod_bot_validated_gate.py::test_prod_bot_only_validated_is_false`, "
                "ProdBot.only_validated is False today (Sprint H operator decision), "
                "so SIM and PROD load the exact same roster.")
    out.append("")
    out.append(render("SIM (only_validated=False)", sim_rows))
    out.append("")
    out.append(render("PROD (only_validated=False -- same as SIM today)", prod_rows))
    out.append("")
    expected_will_load = 7
    will_load_names = sorted(r["name"] for r in sim_rows
                              if r["decision"] == "WILL_LOAD")
    actual_will_load = len(will_load_names)
    out.append("## Cross-check vs operator expected list")
    expected_winners = sorted([
        "a_asian_continuation", "bias_momentum", "e_multi_day_breakout",
        "es_nq_confluence", "g_inside_bar_breakout", "opening_session",
        "raschke_baseline",
    ])
    out.append(f"Expected WILL_LOAD ({expected_will_load}): {expected_winners}")
    out.append(f"Actual   WILL_LOAD ({actual_will_load}): {will_load_names}")
    missing = set(expected_winners) - set(will_load_names)
    extra = set(will_load_names) - set(expected_winners)
    if not missing and not extra:
        out.append("\nResult: MATCH — roster is exactly the 7 expected winners.")
        rc = 0
    else:
        out.append(f"\nResult: MISMATCH — missing={sorted(missing)}, extra={sorted(extra)}.")
        rc = 1
    text = "\n".join(out)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
