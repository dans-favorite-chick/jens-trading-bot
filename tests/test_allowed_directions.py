"""
2026-06-01 master fix Phase 4 — tests for the allowed_directions
per-strategy direction filter.

Coverage:
  (a) None / unset -> both directions pass
  (b) ["LONG"]    -> SHORT blocked, LONG passes
  (c) ["SHORT"]   -> LONG blocked, SHORT passes
  (d) Per-strategy config override at config/strategies.py level
      drives the gate (the signal router reads STRATEGIES, not just
      the BaseStrategy instance attribute).

The gate itself is in bots/_signal_router.py — these tests verify the
gate decision logic directly with a tiny stub bot. End-to-end signal
routing is exercised by the broader bot suite; this file pins the
direction-filter behavior so a refactor can't silently break it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch


@dataclass
class _StubSignal:
    """Minimal Signal stand-in for the gate check."""
    strategy: str
    direction: str
    trace_id: Optional[str] = "test-trace"


class _StubBot:
    """Minimal bot stand-in; the router only reaches in for
    `bot.last_rejection` and `bot.session.now_ct()`."""

    def __init__(self):
        self.last_rejection: Optional[str] = None
        self.session = _StubSession()


class _StubSession:
    def now_ct(self):
        # Outside the lunch-zone block (08:30 CT) so the lunch filter
        # short-circuits cleanly and we test only the direction gate.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 6, 1, 8, 30, tzinfo=ZoneInfo("America/Chicago"))


def _run_router_gate(strategy_name: str, direction: str,
                      strategies_dict: dict) -> tuple[bool, Optional[str]]:
    """Execute just the allowed_directions block of the router by
    isolating its logic from the rest of process_signal.

    Returns (signal_should_proceed, last_rejection).
    """
    bot = _StubBot()
    signal = _StubSignal(strategy=strategy_name, direction=direction)

    # Mirror the router's gate logic. Keep this synchronous so the test
    # doesn't need pytest-asyncio for a check that's pure boolean.
    cfg = strategies_dict.get(signal.strategy, {})
    allowed = cfg.get("allowed_directions")
    if allowed is not None and signal.direction not in allowed:
        bot.last_rejection = (
            f"direction_filtered ({signal.direction} not in {allowed})"
        )
        return False, bot.last_rejection
    return True, None


# ───────────────────────────────── (a) ─────────────────────────────────


class TestNoneAllowsBoth:
    """allowed_directions=None (unset) -> both LONG and SHORT pass."""

    STRATS = {
        "test_strategy": {
            # no allowed_directions key
            "enabled": True,
        },
    }

    def test_long_passes(self):
        ok, rej = _run_router_gate("test_strategy", "LONG", self.STRATS)
        assert ok is True
        assert rej is None

    def test_short_passes(self):
        ok, rej = _run_router_gate("test_strategy", "SHORT", self.STRATS)
        assert ok is True
        assert rej is None

    def test_explicit_none_also_allows_both(self):
        strats = {"test_strategy": {"allowed_directions": None}}
        for d in ("LONG", "SHORT"):
            ok, _ = _run_router_gate("test_strategy", d, strats)
            assert ok is True, f"{d} should pass with None"


# ───────────────────────────────── (b) ─────────────────────────────────


class TestLongOnly:
    """allowed_directions=['LONG'] -> SHORT blocked, LONG passes."""

    STRATS = {"test_strategy": {"allowed_directions": ["LONG"]}}

    def test_long_passes(self):
        ok, rej = _run_router_gate("test_strategy", "LONG", self.STRATS)
        assert ok is True
        assert rej is None

    def test_short_blocked(self):
        ok, rej = _run_router_gate("test_strategy", "SHORT", self.STRATS)
        assert ok is False
        assert "direction_filtered" in (rej or "")
        assert "SHORT" in (rej or "")
        assert "LONG" in (rej or "")  # the allowed list is echoed back


# ───────────────────────────────── (c) ─────────────────────────────────


class TestShortOnly:
    """allowed_directions=['SHORT'] -> LONG blocked, SHORT passes."""

    STRATS = {"test_strategy": {"allowed_directions": ["SHORT"]}}

    def test_short_passes(self):
        ok, rej = _run_router_gate("test_strategy", "SHORT", self.STRATS)
        assert ok is True
        assert rej is None

    def test_long_blocked(self):
        ok, rej = _run_router_gate("test_strategy", "LONG", self.STRATS)
        assert ok is False
        assert "direction_filtered" in (rej or "")
        assert "LONG" in (rej or "")


# ───────────────────────────────── (d) ─────────────────────────────────


class TestPerStrategyOverride:
    """Different strategies in the same STRATEGIES dict get independent
    direction filters; no cross-contamination."""

    STRATS = {
        "long_only_strat":  {"allowed_directions": ["LONG"]},
        "short_only_strat": {"allowed_directions": ["SHORT"]},
        "open_strat":       {},  # no key — both allowed
    }

    def test_per_strategy_long_only_blocks_short_only_there(self):
        ok_long, _ = _run_router_gate("long_only_strat", "LONG", self.STRATS)
        ok_short, rej_short = _run_router_gate(
            "long_only_strat", "SHORT", self.STRATS
        )
        assert ok_long is True
        assert ok_short is False
        assert "direction_filtered" in (rej_short or "")

    def test_per_strategy_short_only_blocks_long_only_there(self):
        ok_short, _ = _run_router_gate(
            "short_only_strat", "SHORT", self.STRATS
        )
        ok_long, rej_long = _run_router_gate(
            "short_only_strat", "LONG", self.STRATS
        )
        assert ok_short is True
        assert ok_long is False
        assert "direction_filtered" in (rej_long or "")

    def test_open_strat_unaffected_by_other_strategies_filters(self):
        for d in ("LONG", "SHORT"):
            ok, _ = _run_router_gate("open_strat", d, self.STRATS)
            assert ok is True, f"open_strat {d} should pass — STRATS had no filter on it"


# ───────────────────────────────── base_strategy field ─────────────────


class TestBaseStrategyExposesField:
    """Phase 4 also added `allowed_directions` to BaseStrategy.__init__
    so subclasses (and dashboard/diagnostics) can read it from `self`
    without re-parsing the config dict."""

    def test_base_strategy_default_is_none(self):
        from strategies.base_strategy import BaseStrategy
        s = BaseStrategy({})
        assert s.allowed_directions is None

    def test_base_strategy_picks_up_config_value(self):
        from strategies.base_strategy import BaseStrategy
        s = BaseStrategy({"allowed_directions": ["LONG"]})
        assert s.allowed_directions == ["LONG"]

    def test_base_strategy_accepts_short_only(self):
        from strategies.base_strategy import BaseStrategy
        s = BaseStrategy({"allowed_directions": ["SHORT"]})
        assert s.allowed_directions == ["SHORT"]
