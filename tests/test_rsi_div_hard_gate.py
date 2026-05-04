"""Fix (2026-05-03): rsi_div_hard_gate runtime hook.

Tests the should_reject_on_rsi_div() helper that gates entries when
opposing RSI divergence is present and the strategy has the hard-gate
enabled.

Forensic context: in the bias_momentum dataset, signals that fired with
an *opposing* RSI divergence ("Warning: opposing RSI div" confluence)
were 0W/6L. Research backs this: regular RSI divergence during an
established trend is a documented ~65%+ accurate momentum-exhaustion
signal. So instead of logging a warning we now block the entry when
the per-strategy `rsi_div_hard_gate` flag is True.

Aligned cases (no rejection):
  bullish-div + LONG  → momentum confirms
  bearish-div + SHORT → momentum confirms

Opposing cases (REJECT when hard-gate enabled):
  bullish-div + SHORT → momentum opposes the short
  bearish-div + LONG  → momentum opposes the long
"""
from __future__ import annotations

import pytest

from bots.base_bot import should_reject_on_rsi_div


# ─── opposing divergence + hard-gate enabled → REJECT ────────────────

def test_reject_bullish_div_on_short_signal():
    """bullish RSI div opposes a SHORT — hard gate fires."""
    assert should_reject_on_rsi_div(
        signal_direction="SHORT", div_type="bullish",
        div_strength=50, hard_gate_enabled=True,
    ) is True


def test_reject_bearish_div_on_long_signal():
    """bearish RSI div opposes a LONG — hard gate fires."""
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bearish",
        div_strength=50, hard_gate_enabled=True,
    ) is True


# ─── aligned divergence → NO rejection (it's confirming the signal) ──

def test_no_reject_bullish_div_on_long_signal():
    """bullish div + LONG = momentum confirms — never reject."""
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bullish",
        div_strength=80, hard_gate_enabled=True,
    ) is False


def test_no_reject_bearish_div_on_short_signal():
    """bearish div + SHORT = momentum confirms — never reject."""
    assert should_reject_on_rsi_div(
        signal_direction="SHORT", div_type="bearish",
        div_strength=80, hard_gate_enabled=True,
    ) is False


# ─── flag disabled → fall back to legacy "soft warning" behavior ─────

def test_no_reject_when_hard_gate_disabled():
    """hard_gate_enabled=False short-circuits — even on opposing div."""
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bearish",
        div_strength=80, hard_gate_enabled=False,
    ) is False


# ─── div_strength below floor → NO rejection (too weak to act on) ────

def test_no_reject_when_strength_below_min():
    """div_strength=10 < default 20 → too weak, don't reject."""
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bearish",
        div_strength=10, hard_gate_enabled=True,
    ) is False


def test_no_reject_at_strength_just_under_min():
    """div_strength=19.9 vs min=20 → strict-less-than, no reject."""
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bearish",
        div_strength=19.9, hard_gate_enabled=True,
    ) is False


def test_reject_at_strength_exact_min():
    """div_strength=20 == min=20 → reject (>= boundary)."""
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bearish",
        div_strength=20, hard_gate_enabled=True,
    ) is True


# ─── custom min_strength threshold honored ───────────────────────────

def test_custom_min_strength_threshold():
    """Caller can raise the bar — strength=25 with min=30 → no reject."""
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bearish",
        div_strength=25, hard_gate_enabled=True, min_strength=30,
    ) is False
    # Same strength with default min=20 → reject
    assert should_reject_on_rsi_div(
        signal_direction="LONG", div_type="bearish",
        div_strength=25, hard_gate_enabled=True,
    ) is True


# ─── bias_momentum config flag wired through ─────────────────────────

def test_bias_momentum_config_has_rsi_div_hard_gate():
    """The config knob added in commit 1d7ca77 must still be present
    and default to True (forensic: 0W/6L on opposing-div signals)."""
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["bias_momentum"]
    assert "rsi_div_hard_gate" in cfg
    assert cfg["rsi_div_hard_gate"] is True
