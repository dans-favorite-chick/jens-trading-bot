"""Flow-reversal exit reason in exit cascade (#19, 2026-05-13).

CVD-based exits (`cvd_flip`, `cvd_divergence`) previously had unknown
priority in the exit cascade, defaulting to rank 99 (below scale_out_
partial). #19 adds them explicitly at rank 5 (above trend_stall at 6
but below managed_exit at 4) and provides a generic `flow_reversal`
alias for clean grouping in analytics.

Rationale for the rank:
- managed_exit (rank 4) = strategy says "this setup is invalid"
- flow_reversal (rank 5) = order flow says "the market is fighting us"
- trend_stall (rank 6) = momentum just isn't advancing (absence of
  progress, not positive opposing evidence)

These tests pin the rank ordering + the alias family.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.exit_decision import EXIT_PRIORITY, FLOW_REVERSAL_REASONS


def test_flow_reversal_family_all_have_explicit_priority():
    for r in FLOW_REVERSAL_REASONS:
        assert r in EXIT_PRIORITY, (
            f"{r} should be in EXIT_PRIORITY — otherwise it defaults to "
            f"rank 99 (below scale_out_partial), which is wrong."
        )


def test_flow_reversal_ranks_below_managed_exit():
    """managed_exit (strategy-says-invalid) is rank 4 — flow_reversal
    is rank 5 because order flow is positive evidence, not as strong
    as a strategy-level invalidation."""
    for r in FLOW_REVERSAL_REASONS:
        assert EXIT_PRIORITY[r] > EXIT_PRIORITY["managed_exit"], (
            f"{r} should rank below managed_exit (higher number = lower "
            f"priority). Found {EXIT_PRIORITY[r]} vs "
            f"{EXIT_PRIORITY['managed_exit']}."
        )


def test_flow_reversal_ranks_above_trend_stall():
    """trend_stall (rank 6) is "no progress" — flow_reversal (rank 5)
    is "active opposing flow", positive evidence. Should outrank stall."""
    for r in FLOW_REVERSAL_REASONS:
        assert EXIT_PRIORITY[r] < EXIT_PRIORITY["trend_stall"], (
            f"{r} should rank ABOVE trend_stall (lower number). Found "
            f"{EXIT_PRIORITY[r]} vs {EXIT_PRIORITY['trend_stall']}."
        )


def test_cvd_flip_and_cvd_div_have_same_priority():
    """They're the same family — analytics should treat them uniformly."""
    assert EXIT_PRIORITY["cvd_flip"] == EXIT_PRIORITY["cvd_divergence"]


def test_flow_reversal_alias_matches_specific_reasons():
    """The generic flow_reversal alias should sort at the same rank as
    the specific reasons — they're interchangeable for cascade purposes."""
    assert EXIT_PRIORITY["flow_reversal"] == EXIT_PRIORITY["cvd_flip"]


def test_flow_reversal_family_membership_complete():
    """The frozenset should contain exactly the three reasons — no more,
    no less. If a new flow-reversal trigger is added, it must be added
    here too so analytics catches it."""
    assert FLOW_REVERSAL_REASONS == frozenset({
        "flow_reversal", "cvd_flip", "cvd_divergence",
    })
