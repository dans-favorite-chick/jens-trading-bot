"""Sprint K2 — dashboard panel data builders.

Pure data-building functions that the Flask server exposes via
/api/bias-panel and /api/tape-reader. The HTML/JS template polls
those endpoints and renders the panels.

Two panels:
  1. build_bias_panel_data(state) — 3-column market bias summary
     (STRUCTURE / MOMENTUM / TAPE) with overall verdict
  2. build_tape_reader_panel_data() — live pattern callouts from
     data/tape_read_latest.json (written by footprint_cvd_reversal
     on each evaluation, Sprint K1)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Tests monkeypatch _DATA_ROOT via setattr to redirect file lookups.
_DATA_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────
# Panel 1: Market Bias Summary (3-column synthesis)
# ──────────────────────────────────────────────────────────────────

def build_bias_panel_data(state: dict | None = None) -> dict:
    """Synthesize 3 columns + overall verdict from bot state.

    state: typically the bot's most-recent eval snapshot. Reads:
      - state['structure_bias'] (Sprint I PriceActionLevels) → STRUCTURE column
      - state['cvd_delta_5'] (sum of last 5 bar deltas) → MOMENTUM column
      - state['tape_read'] (cached read of tape_read_latest.json) → TAPE column

    Returns dict shaped for the dashboard template:
      {
        "structure": {"verdict": "BULLISH|BEARISH|NEUTRAL", "reasons": [...]},
        "momentum":  {"verdict": "BULLISH|BEARISH|NEUTRAL", "reasons": [...]},
        "tape":      {"verdict": "BULLISH|BEARISH|NEUTRAL", "reasons": [...]},
        "overall":   {"verdict": "STRONG_BULLISH|MODERATE_BULLISH|MIXED|...",
                      "summary": "...one-line..."},
      }
    """
    state = state or {}

    # ── STRUCTURE column ──────────────────────────────────────
    structure_bias = str(state.get("structure_bias", "NEUTRAL")).upper()
    structure_reasons: list[str] = []
    if structure_bias == "BULLISH":
        structure_reasons = ["price > VWAP", "price > prior day high"]
    elif structure_bias == "BEARISH":
        structure_reasons = ["price < VWAP", "price < prior day low"]
    else:
        structure_reasons = ["between VWAP and prior-day extremes"]

    structure = {
        "verdict": structure_bias,
        "reasons": structure_reasons,
    }

    # ── MOMENTUM column (CVD delta over last 5 bars) ──────────
    cvd_delta_5 = state.get("cvd_delta_5")
    momentum_reasons: list[str] = []
    if cvd_delta_5 is None:
        momentum_verdict = "NEUTRAL"
        momentum_reasons = ["no recent CVD data"]
    else:
        cvd_delta_5 = float(cvd_delta_5)
        if cvd_delta_5 > 200:
            momentum_verdict = "BULLISH"
            momentum_reasons = [f"CVD delta +{cvd_delta_5:.0f} over last 5 bars"]
        elif cvd_delta_5 < -200:
            momentum_verdict = "BEARISH"
            momentum_reasons = [f"CVD delta {cvd_delta_5:.0f} over last 5 bars"]
        else:
            momentum_verdict = "NEUTRAL"
            momentum_reasons = [f"CVD delta {cvd_delta_5:+.0f} (within ±200)"]

    momentum = {
        "verdict": momentum_verdict,
        "reasons": momentum_reasons,
    }

    # ── TAPE column (derived from tape_read state) ────────────
    tape_state = state.get("tape_read") or {}
    tape_verdict, tape_reasons = _classify_tape(tape_state)
    tape = {
        "verdict": tape_verdict,
        "reasons": tape_reasons,
    }

    # ── OVERALL synthesis ─────────────────────────────────────
    verdicts = [structure_bias, momentum_verdict, tape_verdict]
    bullish_count = verdicts.count("BULLISH")
    bearish_count = verdicts.count("BEARISH")

    if bullish_count == 3:
        overall = {"verdict": "STRONG_BULLISH",
                   "summary": "All 3 columns bullish — high-conviction setup"}
    elif bearish_count == 3:
        overall = {"verdict": "STRONG_BEARISH",
                   "summary": "All 3 columns bearish — high-conviction setup"}
    elif bullish_count == 2 and bearish_count == 0:
        overall = {"verdict": "MODERATE_BULLISH",
                   "summary": "2 of 3 columns bullish — moderate signal"}
    elif bearish_count == 2 and bullish_count == 0:
        overall = {"verdict": "MODERATE_BEARISH",
                   "summary": "2 of 3 columns bearish — moderate signal"}
    elif bullish_count >= 1 and bearish_count >= 1:
        overall = {"verdict": "MIXED",
                   "summary": "Mixed columns — wait for confirmation"}
    else:
        overall = {"verdict": "NEUTRAL",
                   "summary": "All columns neutral — no clear bias"}

    return {
        "structure": structure,
        "momentum": momentum,
        "tape": tape,
        "overall": overall,
    }


def _classify_tape(tape_state: dict) -> tuple[str, list[str]]:
    """Synthesize the tape-state event into a bullish/bearish/neutral
    verdict + reason list.

    Bullish tape:
      - Stacked buy + bullish CVD divergence at HTF level
      - Or finished auction (selling exhausted) + trapped shorts
    Bearish tape: mirror
    Neutral: anything else (or empty/missing tape state)
    """
    if not tape_state:
        return "NEUTRAL", ["no tape data"]

    reasons: list[str] = []
    bullish_signals = 0
    bearish_signals = 0

    # Stacked imbalance is the strongest tape signal
    if tape_state.get("stacked_buy"):
        bullish_signals += 1
        reasons.append("stacked buy imbalance")
    if tape_state.get("stacked_sell"):
        bearish_signals += 1
        reasons.append("stacked sell imbalance")

    # CVD divergence
    cvd_div = tape_state.get("cvd_divergence", "")
    if cvd_div == "BULLISH_DIV":
        bullish_signals += 1
        reasons.append("bullish CVD divergence")
    elif cvd_div == "BEARISH_DIV":
        bearish_signals += 1
        reasons.append("bearish CVD divergence")

    # Trapped traders (already direction-tagged in tape state)
    trapped = tape_state.get("trapped_traders", "")
    if trapped == "shorts_trapped":
        bullish_signals += 1
        reasons.append("shorts trapped at level")
    elif trapped == "longs_trapped":
        bearish_signals += 1
        reasons.append("longs trapped at level")

    # Finished auction — direction inferred from would_fire/fire_direction
    if tape_state.get("finished_auction"):
        fire_dir = tape_state.get("fire_direction", "")
        if fire_dir == "LONG":
            bullish_signals += 1
            reasons.append("finished auction at lows")
        elif fire_dir == "SHORT":
            bearish_signals += 1
            reasons.append("finished auction at highs")

    if bullish_signals > bearish_signals:
        return "BULLISH", reasons or ["mild bullish tape"]
    if bearish_signals > bullish_signals:
        return "BEARISH", reasons or ["mild bearish tape"]
    return "NEUTRAL", reasons or ["balanced tape"]


# ──────────────────────────────────────────────────────────────────
# Panel 2: Tape Reader (live pattern callouts)
# ──────────────────────────────────────────────────────────────────

def build_tape_reader_panel_data(root: Path | None = None) -> dict:
    """Read data/tape_read_latest.json and shape it for the dashboard.

    Returns:
      {
        "available": bool,        # False if no tape_read file yet
        "ts": str,
        "structure_bias": str,
        "iqs_score": int,
        "iqs_breakdown": {L, D, F, C, bonus},
        "nearest_htf_level": str,
        "patterns": [             # Pretty-formatted pattern list
          {"label": "Absorption (buy)", "active": True,  "tag": "🔵"},
          {"label": "Stacked buy",      "active": True,  "tag": "🟢"},
          {"label": "CVD bearish div",  "active": False, "tag": "⚪"},
          ...
        ],
        "would_fire": bool,
        "fire_direction": str,
        "tier": str,
      }
    """
    base = Path(root) if root is not None else _DATA_ROOT
    tape_file = base / "data" / "tape_read_latest.json"

    if not tape_file.exists():
        return {
            "available": False,
            "message": "No tape data yet — strategy hasn't evaluated, "
                       "or volumetric data not flowing from NT8.",
        }

    try:
        event = json.loads(tape_file.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "available": False,
            "message": f"tape_read_latest.json read error: {e!r}",
        }

    # Build the patterns list — same order each time so the UI is stable
    patterns: list[dict[str, Any]] = []

    # Absorption
    patterns.append({
        "label": "Absorption",
        "active": bool(event.get("absorption_detected")),
        "tag": "blue",
    })
    # Stacked imbalance (combined into one row showing direction)
    if event.get("stacked_buy"):
        patterns.append({"label": "Stacked buy", "active": True, "tag": "green"})
    elif event.get("stacked_sell"):
        patterns.append({"label": "Stacked sell", "active": True, "tag": "red"})
    else:
        patterns.append({"label": "Stacked imbalance", "active": False, "tag": "off"})
    # CVD divergence
    cvd_div = event.get("cvd_divergence", "")
    patterns.append({
        "label": (
            "CVD bullish divergence" if cvd_div == "BULLISH_DIV"
            else "CVD bearish divergence" if cvd_div == "BEARISH_DIV"
            else "CVD divergence"
        ),
        "active": cvd_div in ("BULLISH_DIV", "BEARISH_DIV"),
        "tag": "yellow" if cvd_div else "off",
    })
    # Finished auction
    patterns.append({
        "label": "Finished auction",
        "active": bool(event.get("finished_auction")),
        "tag": "yellow",
    })
    # Trapped traders
    trapped = event.get("trapped_traders", "")
    patterns.append({
        "label": (
            "Shorts trapped" if trapped == "shorts_trapped"
            else "Longs trapped" if trapped == "longs_trapped"
            else "Trapped traders"
        ),
        "active": bool(trapped),
        "tag": "red" if trapped else "off",
    })

    return {
        "available": True,
        "ts": event.get("ts", ""),
        "structure_bias": event.get("structure_bias", "NEUTRAL"),
        "iqs_score": int(event.get("iqs_score", 0)),
        "iqs_breakdown": event.get("iqs_breakdown", {}),
        "nearest_htf_level": event.get("nearest_htf_level", ""),
        "patterns": patterns,
        "would_fire": bool(event.get("would_fire")),
        "fire_direction": event.get("fire_direction", ""),
        "tier": event.get("tier", ""),
        "bar_ts": event.get("bar_ts", ""),
    }
