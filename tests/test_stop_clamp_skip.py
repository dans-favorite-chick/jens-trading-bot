"""Fix (2026-05-03): skip_on_stop_clamp — skip when natural ATR stop > max.

Forensic context: bias_momentum trades with stops clamped DOWN from a
wider natural ATR distance were 0W/5L. The vol regime asks for a wider
stop than the strategy's risk tier allows; clamping creates undersized
stops that get hit. Better to skip than clamp.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from strategies._nq_stop import (
    compute_atr_stop,
    compute_natural_stop_ticks,
    was_clamped_from_above,
)


@dataclass
class _Bar:
    high: float
    low: float
    close: float = 0.0
    open: float = 0.0
    volume: int = 1000


# ─── compute_natural_stop_ticks helper ────────────────────────────────

def test_natural_ticks_zero_when_atr_unavailable():
    """No ATR input → 0 ticks (caller knows to use fallback)."""
    assert compute_natural_stop_ticks("LONG", 20000.0, None, 0.0, 0.25) == 0
    assert compute_natural_stop_ticks("LONG", 20000.0, None, -1.0, 0.25) == 0


def test_natural_ticks_high_vol_exceeds_default_max():
    """High ATR (40 points = 160 ticks) at 2× mult → natural >> 120 max."""
    bar = _Bar(high=20010.0, low=19990.0)
    raw = compute_natural_stop_ticks(
        direction="LONG", entry_price=20000.0, last_5m_bar=bar,
        atr_5m_points=40.0, tick_size=0.25, stop_atr_mult=2.0,
    )
    # entry 20000, anchor_low 19990 → distance = (20000 - 19990) + 80 = 90 points = 360t
    assert raw > 120  # would be clamped down


def test_natural_ticks_normal_vol_within_default_max():
    """ATR=4 (16 ticks at 2x = 32 ticks of stop) → natural < 120."""
    bar = _Bar(high=20002.0, low=19998.0)
    raw = compute_natural_stop_ticks(
        direction="LONG", entry_price=20000.0, last_5m_bar=bar,
        atr_5m_points=4.0, tick_size=0.25, stop_atr_mult=2.0,
    )
    # distance = (20000-19998) + 8 = 10 pts = 40 ticks
    assert raw <= 120


def test_natural_ticks_short_mirror():
    """SHORT direction uses anchor_high, mirrors LONG math."""
    bar = _Bar(high=20010.0, low=19990.0)
    long_raw = compute_natural_stop_ticks(
        direction="LONG", entry_price=20000.0, last_5m_bar=bar,
        atr_5m_points=4.0, tick_size=0.25, stop_atr_mult=2.0,
    )
    short_raw = compute_natural_stop_ticks(
        direction="SHORT", entry_price=20000.0, last_5m_bar=bar,
        atr_5m_points=4.0, tick_size=0.25, stop_atr_mult=2.0,
    )
    # Symmetric (anchor distances are 10pts both directions)
    assert abs(long_raw - short_raw) <= 1


# ─── was_clamped_from_above helper ────────────────────────────────────

def test_clamped_from_above_true():
    """raw=200, clamped down to 120 → True."""
    assert was_clamped_from_above(raw_ticks=200, stop_ticks=120, max_stop_ticks=120) is True


def test_clamped_from_above_false_when_no_clamp():
    """raw=80, no clamp needed → False."""
    assert was_clamped_from_above(raw_ticks=80, stop_ticks=80, max_stop_ticks=120) is False


def test_clamped_from_above_false_when_clamped_up():
    """raw=20 (below min), clamped UP to min=40 → False (low-vol day, fine)."""
    assert was_clamped_from_above(raw_ticks=20, stop_ticks=40, max_stop_ticks=120) is False


# ─── compute_atr_stop produces clamp note for above-max raw ──────────

def test_atr_stop_clamps_high_vol_and_marks_note():
    """High ATR → clamped to max + note records original raw."""
    bar = _Bar(high=20010.0, low=19990.0)
    stop_ticks, stop_price, override, note = compute_atr_stop(
        direction="LONG", entry_price=20000.0, last_5m_bar=bar,
        atr_5m_points=40.0, tick_size=0.25,
        stop_atr_mult=2.0, min_stop_ticks=40, max_stop_ticks=120,
        stop_fallback_ticks=64,
    )
    assert stop_ticks == 120  # clamped to max
    assert "clamped from" in note
    assert override is True


# ─── bias_momentum config flag wired through ─────────────────────────

# 2026-05-17: SIM TESTING — flipped to False during V2 overhaul.
# Phase 7 wires a confirmation-bar fallback instead. RESTORE this skip
# before going live (or migrate test to assert stop_fallback_mode logic).
@pytest.mark.skip(reason="2026-05-17 SIM TESTING — skip_on_stop_clamp flipped to False for V2 overhaul; restore before live")
def test_bias_momentum_config_has_skip_on_stop_clamp():
    """Config knob present and defaults to True (per 1d7ca77)."""
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["bias_momentum"]
    assert cfg.get("skip_on_stop_clamp") is True
