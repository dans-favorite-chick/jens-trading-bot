"""ema_dom_exit dynamic min_profit_ticks floor (#1c, 2026-05-13).

The smart-exit gate previously used a static 20-tick (or 40-tick on
TREND days) floor. That punished small-target strategies (which would
fire ema_dom_exit early-cycle and miss the back half of their planned
move) and big-target strategies (where 40 ticks isn't even halfway).

New: floor = max(static, 70% of (target - entry) in ticks). 70% means
"we've captured most of the planned move — willing to bank if micro
flips." Static floor remains as the noise-band lower bound.

These tests exercise the arithmetic via the same expressions the
bot uses in `bots/base_bot.py:~2032-2055`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _compute_min_profit(
    target_price: float,
    entry_price: float,
    day_type: str,
    tick_size: float = 0.25,
) -> int:
    """Mirrors the bot expression. If this drifts from production code,
    update bots/base_bot.py to match (or vice versa)."""
    static_floor = 40 if day_type == "TREND" else 20
    if target_price > 0 and entry_price > 0:
        target_ticks = abs(target_price - entry_price) / tick_size
        dynamic = int(target_ticks * 0.70)
        return max(static_floor, dynamic)
    return static_floor


# ── Trend day ──────────────────────────────────────────────────────────

def test_trend_day_big_target_uses_70pct_floor():
    """Target 200 ticks → 70% = 140 ticks. Static 40 is overridden."""
    mp = _compute_min_profit(
        target_price=20050.0, entry_price=20000.0,  # 200 ticks
        day_type="TREND",
    )
    assert mp == 140


def test_trend_day_small_target_uses_static_floor():
    """Target 40 ticks → 70% = 28 ticks. Static 40 wins."""
    mp = _compute_min_profit(
        target_price=20010.0, entry_price=20000.0,
        day_type="TREND",
    )
    assert mp == 40  # static_floor wins


# ── Non-trend day ──────────────────────────────────────────────────────

def test_chop_day_medium_target_uses_70pct_floor():
    """Target 80 ticks → 70% = 56 ticks. Static 20 is overridden."""
    mp = _compute_min_profit(
        target_price=20020.0, entry_price=20000.0,  # 80 ticks
        day_type="CHOP",
    )
    assert mp == 56


def test_chop_day_tiny_target_uses_static_floor():
    """Target 20 ticks → 70% = 14 ticks. Static 20 wins."""
    mp = _compute_min_profit(
        target_price=20005.0, entry_price=20000.0,  # 20 ticks
        day_type="CHOP",
    )
    assert mp == 20  # static_floor wins


# ── Edge cases ─────────────────────────────────────────────────────────

def test_missing_target_falls_back_to_static_trend():
    """target_price=None / 0 → static_floor only."""
    assert _compute_min_profit(0.0, 20000.0, "TREND") == 40


def test_missing_target_falls_back_to_static_chop():
    assert _compute_min_profit(0.0, 20000.0, "CHOP") == 20


def test_short_direction_uses_abs_target_distance():
    """SHORT: entry=20000, target=19950 → 50pts = 200t → 70% = 140."""
    mp = _compute_min_profit(
        target_price=19950.0, entry_price=20000.0,
        day_type="TREND",
    )
    assert mp == 140


# ── Source pin ─────────────────────────────────────────────────────────

def test_base_bot_uses_dynamic_70pct_floor():
    src = (ROOT / "bots" / "base_bot.py").read_text(encoding="utf-8")
    assert "_target_ticks * 0.70" in src, (
        "ema_dom_exit smart-exit should use the 70% dynamic floor."
    )
    assert "max(_static_floor, _dynamic)" in src, (
        "Should take max of static and dynamic — static is the noise band."
    )
