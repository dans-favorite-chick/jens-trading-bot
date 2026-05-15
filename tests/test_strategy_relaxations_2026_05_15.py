"""Strategy-relaxation pins (2026-05-15 deep-dive).

The deep-dive identified the same kind of root cause across multiple
"never fires" strategies: gates calibrated for an equity-ETF research
paper applied to MNQ futures, where the conditions almost never all
align simultaneously. Two architectural changes shipped:

1. compression_breakout: `all 4 stage-1 conditions` → `N of 4` (config
   default 3). Carver's "Systematic Trading" — scaled forecasts beat
   binary AND gates. The 4 conditions overlap (TTM, ATR, range all
   measure volatility from different angles), so requiring all 4
   double-counts the same signal.

2. classify_opening_type: OPEN_DRIVE's `close_at_extreme` check was a
   fixed 8-tick proximity (2pt). On a typical MNQ 5-min range of 90pt,
   that's the top/bottom 2% of range. Steidlmayer's original Market
   Profile work defines Open Drive as "close in the top/bottom THIRD."
   Switched to `rng_5m * 0.33` with the 8-tick fallback as a floor.
   Volume mult also relaxed 1.4 → 1.2 to match the entry trigger.

These tests pin the new behavior + keep the regression-protection
edges (don't accept a degenerate or contradictory open drive).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── compression_breakout: 3-of-4 logic ─────────────────────────────────

def test_compression_config_min_conditions_is_3():
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["compression_breakout"]
    assert cfg["min_compression_conditions"] == 3, (
        "If you raise this to 4, the strategy returns to the previous "
        "all-conditions-required behavior — verify firing rate stays "
        "non-zero on MNQ before doing so."
    )


def test_compression_source_uses_n_of_4_logic():
    """Pin the structural change so a future refactor doesn't silently
    return to the all-AND pattern."""
    src = (ROOT / "strategies" / "compression_breakout.py").read_text(encoding="utf-8")
    assert "compressed_count = sum(1 for c in conditions_pass if c)" in src, (
        "compression_breakout must use the N-of-4 voting logic, not "
        "an all-AND boolean."
    )
    assert "min_compression_conditions" in src
    # Make sure the legacy all-AND line is gone
    assert "all_compressed = in_ttm_squeeze and atr_compressed" not in src, (
        "Legacy `all_compressed = ... and ... and ...` reintroduced. "
        "Use the N-of-4 count instead."
    )


# ── OPEN_DRIVE classifier: relaxed close-at-extreme ───────────────────

def test_open_drive_fires_on_typical_mnq_session():
    """Realistic MNQ open: 5-min range = 90pt, displacement = 50pt LONG,
    close 25pt below high. Pre-fix the 2pt close-proximity bound rejected
    this (top-third bound would be 30pt). Post-fix it should classify
    as OPEN_DRIVE."""
    from core.session_levels import classify_opening_type
    snapshot = {
        # 5-min range 90pt: low=29400, high=29490
        "rth_open_price": 29410.0,
        "rth_5min_high":  29490.0,
        "rth_5min_low":   29400.0,
        # Close 25pt below high (within top third)
        "rth_5min_close": 29465.0,
        # Volume 1.5× avg → clears the new 1.2× threshold
        "rth_5min_volume": 15000.0,
        "avg_5min_volume": 10000.0,
        # Prior-day levels (any values that don't conflict)
        "prior_day_vah": 29300.0,
        "prior_day_val": 29200.0,
        "prior_day_high": 29350.0,
        "prior_day_low":  29150.0,
    }
    result = classify_opening_type(snapshot)
    assert result == "OPEN_DRIVE", (
        f"Realistic OPEN_DRIVE setup (range=90pt, close 25pt below "
        f"high = top third) should classify as OPEN_DRIVE post-2026-05-15 "
        f"relaxation. Got {result}."
    )


def test_open_drive_rejected_when_close_in_middle_of_range():
    """Even with the relaxed proximity bound, a close in the MIDDLE of
    the range still rejects OPEN_DRIVE. We're widening to top/bottom
    third, not 'anywhere in range'."""
    from core.session_levels import classify_opening_type
    snapshot = {
        "rth_open_price": 29410.0,
        "rth_5min_high":  29490.0,
        "rth_5min_low":   29400.0,
        # Close in the MIDDLE (45pt below high — bottom of top half)
        "rth_5min_close": 29445.0,
        "rth_5min_volume": 15000.0,
        "avg_5min_volume": 10000.0,
        "prior_day_vah": 29300.0,
        "prior_day_val": 29200.0,
        "prior_day_high": 29350.0,
        "prior_day_low":  29150.0,
    }
    result = classify_opening_type(snapshot)
    assert result != "OPEN_DRIVE", (
        "Close in MIDDLE of 5m range must NOT classify as OPEN_DRIVE — "
        "Steidlmayer's spec is top/bottom THIRD, not anywhere."
    )


def test_open_drive_volume_threshold_relaxed_to_1_2x():
    """The volume mult relaxed 1.4 → 1.2. A session with vol=1.3× avg
    should now classify (previously rejected)."""
    from core.session_levels import classify_opening_type
    snapshot = {
        "rth_open_price": 29410.0,
        "rth_5min_high":  29490.0,
        "rth_5min_low":   29400.0,
        "rth_5min_close": 29470.0,   # top fifth — clears top-third
        "rth_5min_volume": 13000.0,  # 1.3× avg
        "avg_5min_volume": 10000.0,
        "prior_day_vah": 29300.0,
        "prior_day_val": 29200.0,
        "prior_day_high": 29350.0,
        "prior_day_low":  29150.0,
    }
    result = classify_opening_type(snapshot)
    assert result == "OPEN_DRIVE", (
        f"Volume 1.3× avg should now pass (was 1.4× threshold). Got {result}."
    )


def test_open_drive_volume_still_rejected_below_threshold():
    """Volume below the new 1.2× threshold still rejects."""
    from core.session_levels import classify_opening_type
    snapshot = {
        "rth_open_price": 29410.0,
        "rth_5min_high":  29490.0,
        "rth_5min_low":   29400.0,
        "rth_5min_close": 29485.0,
        "rth_5min_volume": 11000.0,  # 1.1× — below 1.2× threshold
        "avg_5min_volume": 10000.0,
        "prior_day_vah": 29300.0,
        "prior_day_val": 29200.0,
        "prior_day_high": 29350.0,
        "prior_day_low":  29150.0,
    }
    result = classify_opening_type(snapshot)
    assert result != "OPEN_DRIVE", (
        "Volume 1.1× avg still below the relaxed 1.2× threshold — "
        "shouldn't classify as OPEN_DRIVE."
    )


def test_open_drive_short_direction_with_relaxed_bound():
    """SHORT side: close in BOTTOM third of range qualifies."""
    from core.session_levels import classify_opening_type
    snapshot = {
        "rth_open_price": 29490.0,
        "rth_5min_high":  29495.0,
        "rth_5min_low":   29400.0,   # 95pt range
        "rth_5min_close": 29425.0,   # 25pt above low (bottom 26% = top of bottom third)
        "rth_5min_volume": 15000.0,
        "avg_5min_volume": 10000.0,
        "prior_day_vah": 29550.0,
        "prior_day_val": 29500.0,
        "prior_day_high": 29560.0,
        "prior_day_low":  29470.0,
    }
    result = classify_opening_type(snapshot)
    assert result == "OPEN_DRIVE", (
        f"SHORT OPEN_DRIVE with close in bottom-third should classify. "
        f"Got {result}."
    )


# ── ib_breakout: session-anchor + width cap relaxation ────────────────

def test_ib_breakout_config_has_session_open():
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["ib_breakout"]
    assert cfg.get("session_open_et") == "09:30", (
        "ib_breakout must anchor to 09:30 ET cash open (mirrors ORB fix). "
        "Pre-fix the ET-midnight anchor produced 3,472 ib_too_wide "
        "rejections from overnight-bar IBs."
    )


def test_ib_breakout_width_cap_relaxed_to_4x_atr():
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["ib_breakout"]
    assert cfg.get("max_ib_width_atr_mult") == 4.0, (
        "max_ib_width_atr_mult was 1.5 (tuned for SPY). MNQ 10-min IB "
        "typically runs 50-80pt = 2-3x 5m ATR. 4.0x matches ORB's "
        "working cap."
    )


# ── day_classifier VOLATILE suppression removed ──────────────────────

def test_volatile_day_no_longer_suppresses_breakout_strategies():
    """2026-05-15 fix: removed `ib_breakout` + `compression_breakout`
    from the VOLATILE day suppression list. The original suppression
    rationale ("breakout strategies fail in chop") conflated VOLATILE
    (high ATR) with CHOP (range-bound). Volatile-trending days are
    exactly when breakouts work. The 50% size multiplier on VOLATILE
    days already caps risk; gathering trade data tells us if the
    chop-vs-trend sub-mode shifts the expected value."""
    from core.day_classifier import DAY_PARAMS, VOLATILE
    suppressed = DAY_PARAMS[VOLATILE]["suppressed_strategies"]
    assert "ib_breakout" not in suppressed, (
        "ib_breakout must NOT be auto-suppressed on VOLATILE days. "
        "Pre-fix, every VOLATILE day silently blocked it at the "
        "bot-level gate before strategy.evaluate() even ran."
    )
    assert "compression_breakout" not in suppressed, (
        "compression_breakout must NOT be auto-suppressed on VOLATILE "
        "days. Same rationale — let the strategy fire with the 50% "
        "size multiplier already in place."
    )
    # Size multiplier still capped — risk management unchanged
    assert DAY_PARAMS[VOLATILE]["size_multiplier"] == 0.5, (
        "The 50% size multiplier on VOLATILE days is the risk cap that "
        "lets us safely remove the breakout-strategy suppression. "
        "Don't raise this without re-examining the suppression list."
    )


def test_ib_breakout_source_uses_session_anchor():
    src = (ROOT / "strategies" / "ib_breakout.py").read_text(encoding="utf-8")
    assert "session_open_ts" in src, (
        "ib_breakout must compute session_open_ts and filter bars to "
        "the session window. See ORB session-anchor fix."
    )
    assert "session_open_et" in src
    # Must NOT use the old `today = bar_dt.strftime` pattern as the
    # session-day key (that was the ET-midnight anchor bug).
    assert "today = session_open_et.strftime" in src, (
        "today must derive from session_open_et, not bar_dt directly — "
        "regression of the 2026-05-15 ib_breakout anchor fix."
    )


def test_open_drive_displacement_threshold_unchanged():
    """We did NOT relax the 15pt displacement threshold — data shows 78%
    of MNQ sessions clear it. Verify it still rejects sub-threshold."""
    from core.session_levels import classify_opening_type
    snapshot = {
        "rth_open_price": 29410.0,
        "rth_5min_high":  29420.0,
        "rth_5min_low":   29400.0,
        # Displacement only 8pt — below 15pt threshold
        "rth_5min_close": 29418.0,
        "rth_5min_volume": 15000.0,
        "avg_5min_volume": 10000.0,
        "prior_day_vah": 29300.0,
        "prior_day_val": 29200.0,
        "prior_day_high": 29350.0,
        "prior_day_low":  29150.0,
    }
    result = classify_opening_type(snapshot)
    assert result != "OPEN_DRIVE", (
        "8pt displacement is below the 15pt threshold — must reject."
    )
