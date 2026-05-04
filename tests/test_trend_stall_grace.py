"""Fix A (2026-05-03): trend_stall_grace_s runtime hook.

Tests the should_suppress_trend_stall() helper that gates the trend_stall
exit decision in bots/base_bot.py. Forensic context: 12 of the 71 audit
trades exited at duration_s ≤ 0 via trend_stall (entry-vs-exit gates
disagreed on the same bar's data). The grace window prevents this.
"""
from __future__ import annotations

import pytest

from bots.base_bot import should_suppress_trend_stall


# ─── default-grace=60s blocks within window ──────────────────────────

def test_suppress_within_grace_window():
    """held=30s, grace=60s → suppressed."""
    assert should_suppress_trend_stall(30.0, 60) is True


def test_no_suppress_after_grace_elapsed():
    """held=90s, grace=60s → not suppressed (grace elapsed)."""
    assert should_suppress_trend_stall(90.0, 60) is False


def test_no_suppress_at_exact_boundary():
    """held=60s, grace=60s → not suppressed (>= elapses grace)."""
    assert should_suppress_trend_stall(60.0, 60) is False


def test_no_suppress_at_zero_grace():
    """grace=0 disables the feature (legacy behavior — instant exits OK)."""
    assert should_suppress_trend_stall(0.5, 0) is False


def test_no_suppress_at_negative_grace():
    """Defensive: negative grace also disables."""
    assert should_suppress_trend_stall(0.5, -1) is False


def test_suppress_at_zero_held_with_grace():
    """held=0 (entry tick), grace=60 → suppressed (the duration=0 bug)."""
    assert should_suppress_trend_stall(0.0, 60) is True


def test_suppress_just_before_boundary():
    """held=59.9s, grace=60s → suppressed."""
    assert should_suppress_trend_stall(59.9, 60) is True


# ─── config-default knob is exposed correctly ────────────────────────

def test_bias_momentum_config_has_trend_stall_grace_s():
    """The config knob added in commit 1d7ca77 must still be present
    and default to a sensible value (>=30s)."""
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["bias_momentum"]
    assert "trend_stall_grace_s" in cfg
    assert cfg["trend_stall_grace_s"] >= 30
