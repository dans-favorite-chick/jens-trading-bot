"""Build handoff bundles for the evaluator Claude.

Splits the codebase into two attachable .txt files:

  out/handoff_strategies.txt
      All 14 strategy files + base_strategy.py + _nq_stop.py +
      config/strategies.py + roster table.
      THIS IS THE BUSINESS LOGIC THE EVALUATOR CARES ABOUT.

  out/handoff_infrastructure.txt
      config/settings.py + core/big_move_detector.py +
      core/exit_decision.py + targeted base_bot.py sections +
      sampled eval-log from today's session.
      THE PLATFORM THAT RUNS THE STRATEGIES.

Each file has === SECTION === headers per file so the evaluator can
cite exact locations.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(exist_ok=True)


def header(name: str) -> str:
    return f"\n\n{'='*78}\n=== {name}\n{'='*78}\n\n"


def read(rel: str) -> str:
    p = ROOT / rel
    return p.read_text(encoding="utf-8", errors="replace")


def extract_lines(rel: str, start: int, end: int, note: str = "") -> str:
    """Inclusive line range from a file."""
    lines = read(rel).splitlines()
    chunk = "\n".join(lines[start-1:end])
    head = f"\n# === SUBSECTION lines {start}-{end} of {rel}"
    if note:
        head += f"\n# {note}"
    head += "\n"
    return head + chunk


def sample_eval_log(rel: str, max_records: int = 30) -> str:
    """Pull eval events sampled across the day showing each strategy's
    per-bar result. The full log is 1.5MB / 1457 lines — we just want a
    representative slice."""
    lines = read(rel).splitlines()
    eval_lines = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("event") != "eval":
            continue
        eval_lines.append(obj)
    if not eval_lines:
        return "(no eval events found)\n"
    step = max(1, len(eval_lines) // max_records)
    sampled = eval_lines[::step][:max_records]
    out_lines = []
    for e in sampled:
        ts = e.get("ts", "?")
        regime = e.get("regime", "?")
        risk_blocked = e.get("risk_blocked", None)
        strategies = e.get("strategies", [])
        out_lines.append(f"{ts}  regime={regime}  risk_blocked={risk_blocked}")
        for s in strategies:
            nm = s.get("name", "?")
            result = s.get("result", "?")
            reason = (s.get("reason") or "")[:80]
            direction = s.get("direction", "")
            extra = f"  dir={direction}" if direction else ""
            out_lines.append(f"  {nm:28s} {result:18s}{extra}  {reason}")
        out_lines.append("")
    return "\n".join(out_lines)


def _stamp() -> str:
    return ("PHOENIX BOT — HANDOFF BUNDLE FOR EVALUATOR CLAUDE\n"
            "Generated " + __import__("datetime").datetime.now().isoformat() + "\n"
            "Branch: weekly-evolution/2026-05-10\n"
            "HEAD: 7f54844 (BigMoveSignal strategy + Big-Move Detector + $50 budget gate)\n"
            "Test suite: 1,987 pass / 4 skip / 0 fail\n"
            "Repo: C:\\Trading Project\\phoenix_bot\\\n")


def _roster() -> str:
    return """\
ALL 14 STRATEGIES (state as of 2026-05-17):

bias_momentum                VALIDATED+ENABLED   ← only strategy in prod
big_move_signal              ENABLED (sim only)  ← NEW 5/15, never traded yet
compression_breakout         ENABLED (sim only)  ← un-retired 5/15
dom_pullback                 ENABLED (sim only)
footprint_cvd_reversal       ENABLED (sim only)
high_precision (high_precision_only)
                             RETIRED             ← 557 trades / 29% WR / -$1,082
ib_breakout                  ENABLED (sim only)  ← session-anchor fix 5/15
noise_area                   RETIRED 5/15        ← 10% WR / MFE/MAE 0.44x
opening_session              ENABLED (sim only)  ← un-retired 5/14, 6 sub-strategies
orb                          ENABLED (sim only)  ← session-anchor fix 5/15
spring_setup                 DISABLED
vwap_band_pullback           ENABLED (sim only)
vwap_band_reversion          ENABLED (sim only)
vwap_pullback                ENABLED (sim only)

WHAT TO EVALUATE — operator's questions:
  1. Does each strategy have a real edge, or are some just noise?
  2. Are entry/exit choices appropriate for MNQ ($0.50/tick) on a $50/trade
     budget? Stops can't exceed 100 ticks = $50.
  3. Anything that looks like an anti-pattern (tick-touch comparisons,
     missing min-hold, wrong session anchor, etc.)?
  4. The BigMoveDetector + BigMoveSignal pair is new — does the composite
     score (vol_collapse + cvd_divergence + failed_break + dom_absorption)
     hold up as an entry signature?
"""


# All 14 strategies (alphabetical), with one-line context per file.
_STRATEGY_FILES = [
    ("strategies/base_strategy.py",
     "Strategy interface — every strategy inherits from here."),
    ("strategies/_nq_stop.py",
     "Shared stop-distance helper used by several strategies."),
    ("strategies/bias_momentum.py",
     "VALIDATED+PROD — only strategy currently trading live. "
     "Multi-TF bias + momentum confirm."),
    ("strategies/big_move_signal.py",
     "NEW 2026-05-15 — fires LONG/SHORT when BigMoveDetector composite "
     "score >= 90. Validated live 5/15 at 15:11 (score=100 → +47pt in 8 min). "
     "Has NOT traded yet."),
    ("strategies/compression_breakout.py",
     "TTM Squeeze + ATR/Vol/Range — 3-of-4 voting, instrumented 5/15. "
     "Un-retired 5/15."),
    ("strategies/dom_pullback.py",
     "ATR-anchored pullback to EMA9/VWAP with DOM absorption confirm."),
    ("strategies/footprint_cvd_reversal.py",
     "Footprint + CVD divergence reversal — largest file (1,577 lines), "
     "currently DATA_STALE on volumetric stream."),
    ("strategies/high_precision.py",
     "RETIRED — 557 trades / 29% WR / -$1,082. Kept for context."),
    ("strategies/ib_breakout.py",
     "Initial Balance breakout — session-anchor fix 5/15 "
     "(was using ET-midnight, now uses 9:30 ET cash open)."),
    ("strategies/noise_area.py",
     "RETIRED 5/15 — 10% WR, MFE/MAE 0.44x (anti-edge). Included for "
     "context — the bar-close/min-hold fix is in here but the verdict "
     "was retirement, not tuning."),
    ("strategies/opening_session.py",
     "821 lines — 6 sub-strategies dispatched in one file: OPEN_DRIVE, "
     "OPEN_TEST_DRIVE, OPEN_REJECTION, OPEN_AUCTION, etc. "
     "Un-retired 5/14."),
    ("strategies/orb.py",
     "Zarattini ORB — 9:30 ET cash-open anchor, session-anchor fix 5/15."),
    ("strategies/spring_setup.py",
     "DISABLED — Wyckoff spring/upthrust setup, on the bench."),
    ("strategies/vwap_band_pullback.py",
     "1σ/2σ VWAP band pullback + RSI(2). TF gate dropped 3→2 in #15."),
    ("strategies/vwap_band_reversion.py",
     "Pure 2.1σ mean reversion, skips on trend days."),
    ("strategies/vwap_pullback.py",
     "Classic VWAP pullback. MFE/MAE 0.65x → ANTI_EDGE despite 63% WR "
     "(avgMAE $51 > avgMFE $33)."),
]


def build_strategies_bundle() -> str:
    """All 14 strategies + base_strategy.py + _nq_stop.py + the strategy
    config block. This is the file the evaluator should read first."""
    chunks = [_stamp(),
              "\nBUNDLE 1 OF 2 — ALL STRATEGY FILES + STRATEGY CONFIG.\n"
              "Pair with handoff_infrastructure.txt for base_bot.py + core/* + eval log.\n"]

    chunks.append(header("REFERENCE — strategy roster + state + what to evaluate"))
    chunks.append(_roster())

    for rel, note in _STRATEGY_FILES:
        line_count = len(read(rel).splitlines())
        chunks.append(header(f"{rel}  ({line_count} lines — {note})"))
        chunks.append(read(rel))

    chunks.append(header(
        "config/strategies.py  (per-strategy LIVE config — params, "
        "enable flags, retirement markers)"
    ))
    chunks.append(read("config/strategies.py"))

    return "".join(chunks)


def build_infrastructure_bundle() -> str:
    """The platform that runs the strategies: global settings, the
    BigMoveDetector + exit-cascade core helpers, the targeted base_bot.py
    sections that wire it all together, and a sampled eval-log so the
    evaluator can see what actually happened on a real session."""
    chunks = [_stamp(),
              "\nBUNDLE 2 OF 2 — INFRASTRUCTURE (configs + core/* + base_bot sections + eval log).\n"
              "Pair with handoff_strategies.txt for the 14 strategy files.\n"]

    chunks.append(header(
        "config/settings.py  (global risk limits + tick size + "
        "MAX_ACTUAL_STOP_DOLLARS_PER_TRADE=$50)"
    ))
    chunks.append(read("config/settings.py"))

    chunks.append(header(
        "core/big_move_detector.py  (NEW 5/15 — composite pre-move + "
        "exhaustion scoring; feeds big_move_signal strategy + BIG_MOVE_EXIT)"
    ))
    chunks.append(read("core/big_move_detector.py"))

    chunks.append(header(
        "core/exit_decision.py  (exit-cascade priority table — "
        "big_move_exhaustion at rank 5)"
    ))
    chunks.append(read("core/exit_decision.py"))

    chunks.append(header(
        "bots/base_bot.py SUBSECTIONS  (full file is 5,224 lines — these "
        "are the bits that touch strategies)"
    ))

    chunks.append(extract_lines(
        "bots/base_bot.py", 1197, 1265,
        note="load_strategies() — registry of every strategy class + enabled/validated filter",
    ))
    chunks.append(extract_lines(
        "bots/base_bot.py", 2702, 2780,
        note="_evaluate_strategies() entry — market context build + day-type/regime suppression",
    ))
    chunks.append(extract_lines(
        "bots/base_bot.py", 2775, 2820,
        note="market enrichment — cvd_health + big_move_pre detector hookup",
    ))
    chunks.append(extract_lines(
        "bots/base_bot.py", 3088, 3210,
        note="_evaluate_strategies main loop — per-strategy gate cascade (the order signals get filtered)",
    ))
    chunks.append(extract_lines(
        "bots/base_bot.py", 3618, 3760,
        note="_execute_signal — sizing, stop/target resolve, $50 BUDGET_SKIP gate, sanity check",
    ))
    chunks.append(extract_lines(
        "bots/base_bot.py", 1985, 2110,
        note="position-management — cvd_flip, cvd_divergence, BIG_MOVE_EXIT exhaustion exit",
    ))

    chunks.append(header(
        "logs/history/2026-05-15_sim.jsonl  (SAMPLED — 30 eval records "
        "across the day, showing per-strategy result + reason)"
    ))
    chunks.append(sample_eval_log("logs/history/2026-05-15_sim.jsonl", max_records=30))

    return "".join(chunks)


def main():
    s = build_strategies_bundle()
    s_path = OUT_DIR / "handoff_strategies.txt"
    s_path.write_text(s, encoding="utf-8")
    print(f"Wrote {s_path}  ({len(s):,} chars, {s.count(chr(10)):,} lines)")

    i = build_infrastructure_bundle()
    i_path = OUT_DIR / "handoff_infrastructure.txt"
    i_path.write_text(i, encoding="utf-8")
    print(f"Wrote {i_path}  ({len(i):,} chars, {i.count(chr(10)):,} lines)")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
