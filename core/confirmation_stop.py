"""
Phoenix Bot — Confirmation-Bar Stop Helper
============================================

PROBLEM
-------
NQ in the 2026 volatility regime has ATR_5m typically 30-50pt (was 15-25pt
in 2022). A "natural ATR stop" anchored to last 5m bar ± 2.0×ATR can be
80-100 points wide. That's 320-400 ticks on MNQ — well over any reasonable
budget cap and far wider than what the original strategies (calibrated
against 2022-2023 data) expected.

The existing code rejects every signal where natural stop > max (the
"stop_clamp skip" pattern in eval logs). That's the wrong fix.

SOLUTION
--------
When natural stop is too wide, compute a CONFIRMATION-BAR stop instead:
place the stop just beyond the recent N-bar swing extreme + buffer.
This is what professional NQ scalpers actually do — the stop hugs the
most recent structural reversal point, which is usually 8-40 ticks
away regardless of ATR.

Mathematically:
  LONG  stop = min(bars_1m[-N:].low)  - buffer_ticks * TICK_SIZE
  SHORT stop = max(bars_1m[-N:].high) + buffer_ticks * TICK_SIZE

Typical results on NQ:
  - 3-bar lookback: 12-25 ticks (very tight, scalp-style)
  - 5-bar lookback: 18-40 ticks (balanced)
  - 7-bar lookback: 25-55 ticks (more conservative)

USE
---
    from core.confirmation_stop import compute_confirmation_stop

    stop_ticks, stop_price, note = compute_confirmation_stop(
        direction="LONG",
        entry_price=22000.50,
        bars_1m=bars_1m,
        lookback_bars=5,
        buffer_ticks=2,
        tick_size=0.25,
    )

DEPENDENCIES
------------
None. Pure stdlib.
"""
from __future__ import annotations

from typing import Optional


def snap_to_tick(price: float, tick_size: float = 0.25) -> float:
    """Round a price to the nearest tick boundary.

    Stop prices and target prices MUST be on the tick grid or NinjaTrader
    will reject the order. E.g., NQ price 21998.63 is invalid; it must be
    one of 21998.50, 21998.75. This function rounds to nearest.
    """
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 4)


def compute_confirmation_stop(
    direction: str,
    entry_price: float,
    bars_1m: list,
    *,
    lookback_bars: int = 5,
    buffer_ticks: int = 2,
    tick_size: float = 0.25,
    min_ticks: int = 8,
    max_ticks: int = 60,
) -> tuple[int, float, str]:
    """Compute a confirmation-bar stop from recent swing extremes.

    Args:
        direction: "LONG" or "SHORT".
        entry_price: signal entry price.
        bars_1m: list of completed 1-min Bar objects with .high/.low.
        lookback_bars: how many bars to scan for the swing extreme (default 5).
        buffer_ticks: ticks beyond the swing extreme (default 2).
        tick_size: instrument tick size (NQ/MNQ = 0.25).
        min_ticks: lower bound on stop distance (default 8 ticks).
        max_ticks: upper bound on stop distance (default 60 ticks).

    Returns:
        Tuple of (stop_ticks, stop_price, note_string).
        stop_price is guaranteed to be on the tick grid.

    Behavior:
        - If insufficient bars, falls back to a fixed 20-tick stop.
        - If swing extreme is wrong side of entry (rare), falls back.
        - Result is always within [min_ticks, max_ticks].
        - Returned stop_price is always snapped to tick_size grid.
    """
    # Insufficient data fallback
    if not bars_1m or len(bars_1m) < lookback_bars:
        ticks = 20
        if direction == "LONG":
            sp = entry_price - ticks * tick_size
        else:
            sp = entry_price + ticks * tick_size
        return ticks, snap_to_tick(sp, tick_size), f"conf_stop: insufficient_bars (fallback {ticks}t)"

    recent = bars_1m[-lookback_bars:]

    if direction == "LONG":
        swing_low = min(float(getattr(b, "low", getattr(b, "close", 0)))
                        for b in recent)
        stop_price = swing_low - buffer_ticks * tick_size
        raw_ticks = (entry_price - stop_price) / tick_size

    elif direction == "SHORT":
        swing_high = max(float(getattr(b, "high", getattr(b, "close", 0)))
                         for b in recent)
        stop_price = swing_high + buffer_ticks * tick_size
        raw_ticks = (stop_price - entry_price) / tick_size

    else:
        # Unknown direction — fall back
        ticks = 20
        sp = entry_price - ticks * tick_size if direction != "SHORT" else entry_price + ticks * tick_size
        return ticks, snap_to_tick(sp, tick_size), f"conf_stop: bad_direction={direction} fallback"

    # Wrong-side guard
    if raw_ticks <= 0:
        ticks = 20
        if direction == "LONG":
            sp = entry_price - ticks * tick_size
        else:
            sp = entry_price + ticks * tick_size
        return ticks, snap_to_tick(sp, tick_size), f"conf_stop: wrong_side_swing (fallback {ticks}t)"

    # Clamp to sane range
    ticks = int(round(raw_ticks))
    if ticks < min_ticks:
        ticks = min_ticks
        if direction == "LONG":
            stop_price = entry_price - ticks * tick_size
        else:
            stop_price = entry_price + ticks * tick_size
        note = f"conf_stop: {ticks}t (clamped UP from {raw_ticks:.0f}t swing-{lookback_bars}b)"
    elif ticks > max_ticks:
        ticks = max_ticks
        if direction == "LONG":
            stop_price = entry_price - ticks * tick_size
        else:
            stop_price = entry_price + ticks * tick_size
        note = f"conf_stop: {ticks}t (clamped DOWN from {raw_ticks:.0f}t swing-{lookback_bars}b)"
    else:
        note = f"conf_stop: {ticks}t (swing-{lookback_bars}b + {buffer_ticks}t buffer)"

    # CRITICAL: snap to tick grid — NT8 rejects off-grid orders
    stop_price = snap_to_tick(stop_price, tick_size)
    return ticks, stop_price, note
