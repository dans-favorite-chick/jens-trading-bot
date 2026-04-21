"""
Phoenix Bot — NQ-calibrated ATR-anchored stop helper (B14 2026-04-20).

Package-private helper used by vwap_pullback, bias_momentum, dom_pullback to
compute a stop distance + absolute stop price. Mirrors spring_setup's pattern
but with NQ-appropriate defaults:
  - 2.0× ATR_5m from last 5m bar wick
  - clamp [min_stop_ticks, max_stop_ticks] (default 40..120)
  - fallback stop_fallback_ticks when ATR is missing/zero

Returns (stop_ticks, stop_price, atr_stop_override_flag, note).
  stop_ticks      — int, clamped
  stop_price      — float | None (None when caller should derive from entry±ticks)
  override_flag   — True when the ATR path ran (tells base_bot not to re-override)
  note            — short confluence-style string for logging
"""

from __future__ import annotations


def compute_atr_stop(
    direction: str,
    entry_price: float,
    last_5m_bar,
    atr_5m_points: float,
    tick_size: float,
    stop_atr_mult: float = 2.0,
    min_stop_ticks: int = 40,
    max_stop_ticks: int = 120,
    stop_fallback_ticks: int = 64,
):
    """Compute NQ-calibrated ATR-anchored stop.

    atr_5m_points: the 'atr_5m' snapshot field — it's in POINTS (price units),
    not ticks. Caller must not convert. See tick_aggregator.snapshot().

    last_5m_bar: a Bar-like object with .low / .high, used as wick anchor.
        If None or no 5m bar yet, anchors to entry_price (wick anchor = entry).
    """
    # Fallback: ATR missing/zero → fixed stop, caller derives price from entry
    if not atr_5m_points or atr_5m_points <= 0:
        return (stop_fallback_ticks, None, False,
                f"Stop fallback: ATR unavailable, {stop_fallback_ticks}t")

    # Anchor: last 5m bar wick (low for LONG, high for SHORT). If no bar, use entry.
    if last_5m_bar is not None:
        anchor_low = last_5m_bar.low
        anchor_high = last_5m_bar.high
    else:
        anchor_low = anchor_high = entry_price

    if direction == "LONG":
        stop_price = anchor_low - (stop_atr_mult * atr_5m_points)
        stop_distance = entry_price - stop_price
    else:  # SHORT
        stop_price = anchor_high + (stop_atr_mult * atr_5m_points)
        stop_distance = stop_price - entry_price

    if stop_distance <= 0:
        # Price already past the wick — use fallback
        return (stop_fallback_ticks, None, False,
                f"Stop fallback: price past wick, {stop_fallback_ticks}t")

    raw_ticks = int(stop_distance / tick_size)
    stop_ticks = max(min_stop_ticks, min(max_stop_ticks, raw_ticks))

    # Recompute stop_price from the clamped tick count so the caller's bracket matches.
    if direction == "LONG":
        stop_price = entry_price - (stop_ticks * tick_size)
    else:
        stop_price = entry_price + (stop_ticks * tick_size)

    note = (f"ATR stop: {stop_atr_mult}×ATR5m({atr_5m_points:.1f}pt) "
            f"= {stop_ticks}t"
            + (f" [clamped from {raw_ticks}t]" if raw_ticks != stop_ticks else ""))
    return (stop_ticks, stop_price, True, note)
