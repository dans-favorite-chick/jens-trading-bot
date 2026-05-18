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

# 2026-05-17: Phase 7 CODE PATCH 3 — re-export compute_confirmation_stop
# and snap_to_tick from core.confirmation_stop for backwards-compatible
# imports from strategy files. The canonical implementation lives in
# core.confirmation_stop (added in Phase 1, commit 9a5de35).
from core.confirmation_stop import compute_confirmation_stop, snap_to_tick  # noqa: F401


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
    # 4-tuple kept for backward compat with existing callers; the `raw_ticks`
    # value can be derived by callers that need to detect upper-bound clamping
    # (raw_ticks > max_stop_ticks) without re-doing the math.
    return (stop_ticks, stop_price, True, note)


def was_clamped_from_above(raw_ticks: int, stop_ticks: int, max_stop_ticks: int) -> bool:
    """Fix (2026-05-03): True when natural ATR stop exceeded max_stop_ticks
    and was forcibly clamped down. This is the 'vol regime mismatch' case
    where the strategy is asking for a wider stop than its risk-tier
    allows. Forensic: clamped-from-above stops were 0W/5L in audit data.
    Use with `skip_on_stop_clamp` config flag.

    Returns False when:
      - clamp was upward (raw < min_stop_ticks): low-vol day, fine
      - no clamp at all (raw == stop_ticks): natural stop was in range
    """
    return raw_ticks > max_stop_ticks and stop_ticks == max_stop_ticks


def compute_natural_stop_ticks(direction: str, entry_price: float,
                                last_5m_bar, atr_5m_points: float,
                                tick_size: float, stop_atr_mult: float = 2.0) -> int:
    """Fix (2026-05-03): return the UNCLAMPED ATR-derived tick count, so
    a caller can decide whether to skip on clamp. Returns 0 when ATR
    unavailable. Mirrors compute_atr_stop's anchor logic.
    """
    if not atr_5m_points or atr_5m_points <= 0:
        return 0
    if last_5m_bar is not None:
        anchor_low = last_5m_bar.low
        anchor_high = last_5m_bar.high
    else:
        anchor_low = anchor_high = entry_price
    if direction == "LONG":
        stop_price = anchor_low - (stop_atr_mult * atr_5m_points)
        stop_distance = entry_price - stop_price
    else:
        stop_price = anchor_high + (stop_atr_mult * atr_5m_points)
        stop_distance = stop_price - entry_price
    if stop_distance <= 0:
        return 0
    return int(stop_distance / tick_size)
