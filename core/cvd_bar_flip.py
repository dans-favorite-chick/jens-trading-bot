"""Bar Delta Flip detector — mid-trade exit signal.

Per operator's trade-flow methodology: when a CVD bar closes opposite
to the trade's intended direction, the move is losing energy. After N
consecutive flipped bars (or one large-magnitude flip), the energy is
gone — exit before the structural stop catches the full retrace.

Mechanism:
  1. Maintain a rolling deque of recent per-bar deltas (bar_delta =
     buy_volume - sell_volume for that bar — already in Phoenix market
     snapshots).
  2. `check_flip_against(direction, min_consecutive, min_magnitude)`:
       - For LONG: a "flip" is a NEGATIVE bar delta (selling pressure).
       - For SHORT: a "flip" is a POSITIVE bar delta (buying pressure).
       - Count CONSECUTIVE flipped bars from the most recent backwards.
       - Return `flipped=True` if either:
           (a) consecutive_count >= min_consecutive, OR
           (b) the most recent bar's delta has magnitude >= min_magnitude
               and is in the flip direction (single-bar capitulation).
  3. Also compute the dominant trend direction of recent bars (sum-of-
     deltas based) to give the consumer context.

Usage in base_bot exit loop:
    self.cvd_flip.update_bar(market["bar_delta"])
    flip = self.cvd_flip.check_flip_against("LONG", min_consecutive=2)
    if flip["flipped"]:
        # exit signal — energy fading
        ...
"""
from __future__ import annotations

import logging
from collections import deque

logger = logging.getLogger("BarDeltaFlipDetector")


class BarDeltaFlipDetector:
    """Detect when per-bar CVD delta flips against position direction.

    Position-direction-agnostic at update time; consumer asks
    `check_flip_against(direction)` to evaluate against a specific trade.
    """

    def __init__(self, lookback: int = 5):
        """
        Args:
            lookback: how many recent bar_deltas to retain. 5 = trailing
                25 min on 5m bars (or 5 min on 1m bars). Smaller = more
                reactive; larger = more lag.
        """
        self.lookback = lookback
        self.recent_deltas: deque[float] = deque(maxlen=lookback)

    def update_bar(self, bar_delta: float) -> None:
        """Call on each completed bar close.

        bar_delta is the buy_volume - sell_volume for THAT BAR (not
        cumulative). Phoenix passes this as `market["bar_delta"]` from
        the tick aggregator's per-bar accounting.
        """
        try:
            self.recent_deltas.append(float(bar_delta))
        except (TypeError, ValueError):
            pass  # skip bad input

    def check_flip_against(
        self,
        trade_direction: str,
        min_consecutive: int = 1,
        min_magnitude: float = 0.0,
    ) -> dict:
        """Evaluate whether recent bars flipped against the trade direction.

        Args:
            trade_direction: "LONG" or "SHORT".
            min_consecutive: how many in-a-row flipped bars before
                `flipped=True` (default 1 — fire on a single flip).
            min_magnitude: if the MOST RECENT bar's flip magnitude meets
                or exceeds this, fire `flipped=True` regardless of
                consecutive count. 0.0 disables the single-bar override.

        Returns:
            {
              "flipped":           bool,
              "consecutive_count": int,   # consecutive flipped bars from the back
              "last_bar_delta":    float, # most recent bar's delta (or 0 if empty)
              "trend_dir":         "LONG"|"SHORT"|"NEUTRAL",
                                          # net direction of recent bars
              "reason":            str,   # human-readable
            }
        """
        deltas = list(self.recent_deltas)
        if not deltas:
            return {
                "flipped": False,
                "consecutive_count": 0,
                "last_bar_delta": 0.0,
                "trend_dir": "NEUTRAL",
                "reason": "no bars yet",
            }

        dir_up = trade_direction.upper() == "LONG"
        # For LONG, a "flip" is negative delta. For SHORT, positive delta.
        def is_flip(d: float) -> bool:
            return d < 0 if dir_up else d > 0

        last_bar_delta = deltas[-1]
        last_magnitude = abs(last_bar_delta)
        last_is_flip = is_flip(last_bar_delta)

        # Count consecutive flipped bars from the back
        consecutive = 0
        for d in reversed(deltas):
            if is_flip(d):
                consecutive += 1
            else:
                break

        # Net direction of recent bars (sum-of-deltas)
        net = sum(deltas)
        # Threshold for "trend direction": at least half the absolute mean of bars
        if not deltas:
            trend_dir = "NEUTRAL"
        else:
            avg_abs = sum(abs(d) for d in deltas) / len(deltas)
            if avg_abs == 0:
                trend_dir = "NEUTRAL"
            elif net > 0.5 * avg_abs:
                trend_dir = "LONG"
            elif net < -0.5 * avg_abs:
                trend_dir = "SHORT"
            else:
                trend_dir = "NEUTRAL"

        # Flip decision: consecutive threshold OR magnitude override
        flipped = consecutive >= min_consecutive
        magnitude_override = (
            min_magnitude > 0
            and last_is_flip
            and last_magnitude >= min_magnitude
        )
        if magnitude_override:
            flipped = True

        if flipped:
            if magnitude_override and consecutive < min_consecutive:
                reason = (
                    f"single-bar capitulation: last_delta={last_bar_delta:+.0f} "
                    f"(magnitude {last_magnitude:.0f} >= {min_magnitude:.0f})"
                )
            else:
                reason = (
                    f"{consecutive} consecutive bars flipped against "
                    f"{trade_direction} (need >= {min_consecutive})"
                )
        else:
            reason = (
                f"no flip: consecutive={consecutive}/{min_consecutive}, "
                f"last_delta={last_bar_delta:+.0f}"
            )

        return {
            "flipped": flipped,
            "consecutive_count": consecutive,
            "last_bar_delta": round(last_bar_delta, 2),
            "trend_dir": trend_dir,
            "reason": reason,
        }
