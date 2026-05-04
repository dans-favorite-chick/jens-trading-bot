"""Tests for 2026-05-03 bias_momentum profitability fixes:
   B — SHORT-asymmetric quality requirement
   C — session_block_windows time-of-day filter (helper test)
   D — target_rr lowered 5.0 → 2.5 (config validation)
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from config.strategies import STRATEGIES as STRATEGY_CONFIG
from strategies.bias_momentum import _ct_in_block_window


CT = ZoneInfo("America/Chicago")


# ─── Fix C: session-block helper ──────────────────────────────────────

def test_session_block_inside_window():
    """When current CT time is inside a configured window, blocked=True."""
    windows = [("08:30", "08:59"), ("10:00", "13:29")]
    # 10:30 CT is inside the second window
    now = datetime(2026, 5, 4, 10, 30, tzinfo=CT)
    blocked, rng = _ct_in_block_window(now, windows)
    assert blocked is True
    assert rng == "10:00-13:29"


def test_session_block_outside_windows():
    """09:30 CT is NOT in either window — should not block."""
    windows = [("08:30", "08:59"), ("10:00", "13:29")]
    now = datetime(2026, 5, 4, 9, 30, tzinfo=CT)
    blocked, _ = _ct_in_block_window(now, windows)
    assert blocked is False


def test_session_block_at_exact_boundary_inclusive():
    """10:00 CT is the start of the second window — inclusive, should block."""
    windows = [("10:00", "13:29")]
    now = datetime(2026, 5, 4, 10, 0, tzinfo=CT)
    blocked, _ = _ct_in_block_window(now, windows)
    assert blocked is True


def test_session_block_after_window_ends():
    """13:30 CT is AFTER the second window ends (13:29) — should not block.
    This is the "afternoon momentum re-engages" window per forensic evidence."""
    windows = [("08:30", "08:59"), ("10:00", "13:29")]
    now = datetime(2026, 5, 4, 13, 30, tzinfo=CT)
    blocked, _ = _ct_in_block_window(now, windows)
    assert blocked is False


def test_session_block_empty_list_never_blocks():
    """Empty windows list disables the filter entirely."""
    now = datetime(2026, 5, 4, 10, 30, tzinfo=CT)
    blocked, _ = _ct_in_block_window(now, [])
    assert blocked is False


# ─── Fix C: config validates the documented blocking windows ──────────

def test_bias_momentum_config_has_session_block_windows():
    """Config must declare the session_block_windows key.

    Sprint H (2026-05-04): operator emptied this list to allow
    bias_momentum to trade all hours for prod debug visibility. The
    pre-Sprint-H windows ([08:30-08:59], [10:00-13:29]) are documented
    in config/strategies.py for restoration before go-live.

    This test now asserts the key EXISTS (not what it contains) so
    the runtime filter `_ct_in_block_window` always has something to
    iterate. Operator can restore the windows by editing the config;
    `test_no_session_block_windows_re_added_silently` in the
    Sprint H test file will fail loudly when that happens, prompting
    a deliberate review."""
    cfg = STRATEGY_CONFIG["bias_momentum"]
    assert "session_block_windows" in cfg
    windows = cfg["session_block_windows"]
    assert isinstance(windows, list)  # empty list allowed (Sprint H)


# ─── Fix B: SHORT-asymmetric quality requirement ──────────────────────

def test_bias_momentum_config_has_short_extra_gates():
    """short_extra_gates flag must be present and default to True."""
    cfg = STRATEGY_CONFIG["bias_momentum"]
    assert cfg.get("short_extra_gates") is True


# ─── Fix D: target_rr re-calibrated 5.0 → 2.5 ─────────────────────────

def test_bias_momentum_target_rr_lowered():
    """target_rr was lowered from 5.0 to 2.5 per 2026-05-03 forensic
    research — only 9 of 71 audit trades hit target_hit at 5:1 RR."""
    cfg = STRATEGY_CONFIG["bias_momentum"]
    assert cfg["target_rr"] == 2.5


# ─── Fix A: trend_stall_grace_s knob present ──────────────────────────

def test_bias_momentum_trend_stall_grace_present():
    """Config exposes trend_stall_grace_s for the trend_stall-at-entry bug.
    12 of 46 trades exited at duration_s ≤ 0 — grace period prevents
    instant unwind on entry."""
    cfg = STRATEGY_CONFIG["bias_momentum"]
    assert "trend_stall_grace_s" in cfg
    assert cfg["trend_stall_grace_s"] >= 30  # at least 30s grace


# ─── Bonus: skip_on_stop_clamp + rsi_div_hard_gate flags exposed ──────

def test_bias_momentum_research_backed_flags_present():
    """Both flags from forensic research must be exposed (whether or not
    the runtime path is wired yet — wire-up is a follow-on commit)."""
    cfg = STRATEGY_CONFIG["bias_momentum"]
    assert cfg.get("skip_on_stop_clamp") is True   # 0W/5L on clamped stops
    assert cfg.get("rsi_div_hard_gate") is True    # 0W/6L on RSI div warning
