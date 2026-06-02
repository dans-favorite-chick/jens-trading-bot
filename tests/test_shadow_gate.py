"""Phase 7 (2026-06-02 overnight) tests for the shadow market-state gate.

Covers:
  - default block-list (WHIPSAW_HIGH_VOL only) returns would_block=True
  - other states return would_block=False with reason
  - per-strategy shadow_allowed_market_states override
  - unknown / missing market_state -> fail-OPEN (would_block=False)
  - summarize_shadow_decisions arithmetic
  - the helper is pure (does not mutate inputs)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.market_state_gate import (  # noqa: E402
    VALID_MARKET_STATES,
    compute_market_state_gate_decision,
    summarize_shadow_decisions,
)


# ─── default block-list ───────────────────────────────────────────

def test_whipsaw_blocked_by_default():
    r = compute_market_state_gate_decision({}, "WHIPSAW_HIGH_VOL")
    assert r["would_block"] is True
    assert "WHIPSAW_HIGH_VOL" in r["reason"]


def test_trending_normal_allowed_by_default():
    r = compute_market_state_gate_decision({}, "TRENDING_NORMAL")
    assert r["would_block"] is False
    assert "TRENDING_NORMAL" in r["reason"]


def test_choppy_allowed_by_default():
    """Codex preview shows CHOPPY bias_momentum PF=1.41 (profitable).
    The default keeps CHOPPY allowed; only WHIPSAW gets blocked
    until per-strategy data refines."""
    r = compute_market_state_gate_decision({}, "CHOPPY")
    assert r["would_block"] is False


def test_all_non_whipsaw_states_allowed_by_default():
    for state in VALID_MARKET_STATES - {"WHIPSAW_HIGH_VOL"}:
        r = compute_market_state_gate_decision({}, state)
        assert r["would_block"] is False, f"state={state}"


# ─── per-strategy override ────────────────────────────────────────

def test_per_strategy_allowed_list_blocks_states_not_in_it():
    cfg = {"shadow_allowed_market_states": ["TRENDING_NORMAL", "TRENDING_HIGH_VOL"]}
    r = compute_market_state_gate_decision(cfg, "CHOPPY")
    assert r["would_block"] is True
    assert "CHOPPY" in r["reason"]


def test_per_strategy_allowed_list_passes_state_in_it():
    cfg = {"shadow_allowed_market_states": ["CHOPPY", "TRENDING_NORMAL"]}
    r = compute_market_state_gate_decision(cfg, "CHOPPY")
    assert r["would_block"] is False
    assert "CHOPPY" in r["reason"]


def test_per_strategy_override_supersedes_default_for_whipsaw():
    """If the operator explicitly allows WHIPSAW for a strategy, the
    per-strategy list wins over the global default."""
    cfg = {"shadow_allowed_market_states": ["WHIPSAW_HIGH_VOL", "TRENDING_NORMAL"]}
    r = compute_market_state_gate_decision(cfg, "WHIPSAW_HIGH_VOL")
    assert r["would_block"] is False


# ─── unknown / missing state ──────────────────────────────────────

def test_none_state_fails_open():
    r = compute_market_state_gate_decision({}, None)
    assert r["would_block"] is False
    assert r["reason"] == "market_state_unknown"


def test_empty_string_state_fails_open():
    r = compute_market_state_gate_decision({}, "")
    assert r["would_block"] is False


def test_typo_state_fails_open():
    """A future schema-add typo (e.g. 'TRENDING_NORMA') should not
    silently block every signal."""
    r = compute_market_state_gate_decision({}, "TRENDING_NORMA")
    assert r["would_block"] is False
    assert r["reason"] == "market_state_unknown"


def test_none_strategy_cfg_uses_default():
    r = compute_market_state_gate_decision(None, "WHIPSAW_HIGH_VOL")
    assert r["would_block"] is True


# ─── purity: does not mutate inputs ───────────────────────────────

def test_cfg_not_mutated():
    cfg = {"shadow_allowed_market_states": ["TRENDING_NORMAL"], "stop_atr_mult": 2.0}
    cfg_before = dict(cfg)
    compute_market_state_gate_decision(cfg, "TRENDING_NORMAL")
    compute_market_state_gate_decision(cfg, "CHOPPY")
    compute_market_state_gate_decision(cfg, "WHIPSAW_HIGH_VOL")
    assert cfg == cfg_before


# ─── summarize_shadow_decisions ───────────────────────────────────

def test_summarize_empty():
    s = summarize_shadow_decisions([])
    assert s["n_signals"] == 0
    assert s["n_would_block"] == 0
    assert s["would_block_pct"] == 0.0


def test_summarize_mixed():
    rows = [
        # 5 fired signals; 3 would have been blocked
        {"shadow_would_block": True,  "pnl_dollars": -50.0},  # blocked loser (good)
        {"shadow_would_block": True,  "pnl_dollars":  30.0},  # blocked winner (bad)
        {"shadow_would_block": True,  "pnl_dollars": -20.0},  # blocked loser (good)
        {"shadow_would_block": False, "pnl_dollars":  40.0},  # allowed winner
        {"shadow_would_block": False, "pnl_dollars": -10.0},  # allowed loser
    ]
    s = summarize_shadow_decisions(rows)
    assert s["n_signals"] == 5
    assert s["n_would_block"] == 3
    assert s["would_block_pct"] == 0.6
    # gate would have avoided -70 + missed +30 = net +40 effect
    assert s["would_block_pnl_total"] == -40.0  # 30 - 50 - 20
    assert s["would_block_pnl_winners"] == 30.0
    assert s["would_block_pnl_losers"] == 70.0  # |−50| + |−20|


def test_summarize_handles_rows_without_field():
    """Pre-Phase-7 trade records didn't have shadow_would_block.
    They should count toward n_signals but not n_would_block."""
    rows = [
        {"pnl_dollars": 10.0},  # legacy, no field
        {"shadow_would_block": True, "pnl_dollars": -5.0},
    ]
    s = summarize_shadow_decisions(rows)
    assert s["n_signals"] == 2
    assert s["n_would_block"] == 1
