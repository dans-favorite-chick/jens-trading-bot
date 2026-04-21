"""
Phoenix Bot — Liquidity Sweep Detector

A liquidity sweep is a failed BOS: price breaks above/below a pivot, fails to
continue, then RECLAIMS the broken level. In SMC terms, smart money triggered
retail stops at the extreme, then reversed.

Research basis (2026):
- A BOS that reverses within 2-5 bars is usually a liquidity sweep, NOT continuation
- Sweep detection prevents taking stop-hunt fakes as continuation signals
- Signature: sharp break (wick) + failure to close body beyond level + reclaim

This module WATCHES recent pivot breaks and reclassifies them as sweeps
when the criteria fire. Works together with swing_detector.py (which provides
the pivots to watch).

API:
  watcher = SweepWatcher()
  # On each new pivot from swing_detector:
  watcher.track_pivot(pivot)
  # On each new bar:
  sweep = watcher.check_sweep(bar, current_price, bar_idx)
  if sweep:
      # Trade IN THE OPPOSITE direction of the failed BOS
      ...
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("LiquiditySweep")


# ─── Config ─────────────────────────────────────────────────────────────
SWEEP_MIN_BARS_AFTER_BREAK = 1    # Minimum bars after break for sweep detection
SWEEP_MAX_BARS_AFTER_BREAK = 5    # Sweep must resolve within this window
WICK_PENETRATION_MIN_TICKS = 2    # Break must be > this to count
RECLAIM_CONFIRMATION_TICKS = 2    # Reclaim bar close must be > this inside level


@dataclass
class PivotBreakWatch:
    """A recent pivot break that might become a sweep."""
    pivot_price: float
    pivot_ts: datetime
    break_direction: str       # "UP" (price broke above pivot high) or "DOWN" (broke below pivot low)
    break_ts: datetime
    break_bar_idx: int
    break_extreme: float       # How far beyond the pivot did price reach
    active: bool = True


@dataclass
class SweepEvent:
    """A confirmed liquidity sweep — the break was a fake."""
    ts: datetime
    bar_idx: int
    pivot_price: float
    original_break_direction: str    # "UP" or "DOWN" — the failed direction
    reversal_direction: str          # "SHORT" or "LONG" — what we'd trade
    sweep_extreme: float             # The peak/trough of the fake break
    confirmation_reasons: list[str]


class SweepWatcher:
    """Tracks recent pivot breaks, watches for failed-continuation sweeps."""

    def __init__(self):
        self.active_watches: list[PivotBreakWatch] = []

    def track_pivot_break(self, pivot_price: float, break_direction: str,
                          break_ts: datetime, break_bar_idx: int,
                          break_extreme: float) -> None:
        """Called when swing_detector observes price breaking a prior pivot."""
        watch = PivotBreakWatch(
            pivot_price=pivot_price,
            pivot_ts=break_ts,
            break_direction=break_direction,
            break_ts=break_ts,
            break_bar_idx=break_bar_idx,
            break_extreme=break_extreme,
        )
        self.active_watches.append(watch)
        # Cap active watch count
        if len(self.active_watches) > 20:
            self.active_watches = self.active_watches[-20:]

    def check_sweep(self, bar, bar_idx: int, tick_size: float = 0.25
                    ) -> Optional[SweepEvent]:
        """
        Check each active watch: did THIS bar's close reclaim the broken level?
        Returns SweepEvent if a watch resolves as a sweep.
        """
        reclaim_distance = RECLAIM_CONFIRMATION_TICKS * tick_size
        for watch in self.active_watches[:]:
            if not watch.active:
                continue
            bars_since = bar_idx - watch.break_bar_idx

            # Expire too-old watches (continuation confirmed by time)
            if bars_since > SWEEP_MAX_BARS_AFTER_BREAK:
                watch.active = False
                continue
            if bars_since < SWEEP_MIN_BARS_AFTER_BREAK:
                continue

            # Sweep logic — depends on break direction
            if watch.break_direction == "UP":
                # Upward break that's now sweeping. Confirmation: bar closes back BELOW pivot by reclaim_distance
                if bar.close < (watch.pivot_price - reclaim_distance):
                    reasons = [
                        f"Break @ {watch.break_extreme:.2f} above pivot {watch.pivot_price:.2f}",
                        f"Reclaim: close {bar.close:.2f} < pivot - {reclaim_distance:.2f}",
                        f"{bars_since} bars after break",
                    ]
                    watch.active = False
                    logger.info(f"[SWEEP] UP-sweep confirmed: fake break @ {watch.break_extreme:.2f}, "
                                f"reclaim close {bar.close:.2f}")
                    return SweepEvent(
                        ts=bar.ts if hasattr(bar, "ts") else datetime.now(),
                        bar_idx=bar_idx,
                        pivot_price=watch.pivot_price,
                        original_break_direction="UP",
                        reversal_direction="SHORT",
                        sweep_extreme=watch.break_extreme,
                        confirmation_reasons=reasons,
                    )
            elif watch.break_direction == "DOWN":
                if bar.close > (watch.pivot_price + reclaim_distance):
                    reasons = [
                        f"Break @ {watch.break_extreme:.2f} below pivot {watch.pivot_price:.2f}",
                        f"Reclaim: close {bar.close:.2f} > pivot + {reclaim_distance:.2f}",
                        f"{bars_since} bars after break",
                    ]
                    watch.active = False
                    logger.info(f"[SWEEP] DOWN-sweep confirmed: fake break @ {watch.break_extreme:.2f}, "
                                f"reclaim close {bar.close:.2f}")
                    return SweepEvent(
                        ts=bar.ts if hasattr(bar, "ts") else datetime.now(),
                        bar_idx=bar_idx,
                        pivot_price=watch.pivot_price,
                        original_break_direction="DOWN",
                        reversal_direction="LONG",
                        sweep_extreme=watch.break_extreme,
                        confirmation_reasons=reasons,
                    )

        # Clean up inactive watches
        self.active_watches = [w for w in self.active_watches if w.active]
        return None

    def get_state(self) -> dict:
        return {
            "active_watches": len([w for w in self.active_watches if w.active]),
            "watches": [
                {
                    "pivot": w.pivot_price,
                    "break_direction": w.break_direction,
                    "break_extreme": w.break_extreme,
                    "break_bar_idx": w.break_bar_idx,
                }
                for w in self.active_watches if w.active
            ],
        }
