"""Swing Divergence detector — classic bear/bull divergence at swing points.

This is the hardest of the three CVD detectors to implement well — it
requires swing-point detection (a swing high is a local maximum confirmed
by N bars after) and enough bar-spacing between swings to be meaningful.

Mechanism:
  1. Maintain rolling price + cumulative_cvd history.
  2. On each new bar, scan for newly-confirmed pivots:
       - A pivot HIGH at bar index i requires bars i-N..i to have lower
         highs AND bars i+1..i+N to also have lower highs (3-bars-each-side
         by default). This means the pivot is "confirmed" with N bars of lag.
       - A pivot LOW is the mirror.
  3. When a new swing pivot is confirmed:
       - Compare its CVD value to the PREVIOUS pivot of the same type
         (high-to-high for bearish; low-to-low for bullish).
       - BEARISH DIVERGENCE: new price high > prior price high BUT
         new CVD < prior CVD ("price made new high without volume conviction").
       - BULLISH DIVERGENCE: new price low < prior price low BUT
         new CVD > prior CVD ("price made new low without sell pressure").
       - Only fire if bars-between is within [min_bars_between,
         max_bars_between] — too close = noise, too far = stale.
  4. Returns a fresh DivergenceSignal once per newly-formed swing, then
     None until the NEXT swing forms.

Usage in base_bot exit loop:
    self.cvd_div.update_bar(bar_high, bar_low, market["cvd"])
    sig = self.cvd_div.check_divergence(trade_direction="LONG")
    if sig and sig.kind == "BEARISH":
        # bearish divergence at the recent high while we're LONG → exit
        ...
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("SwingDivergenceDetector")


@dataclass
class Pivot:
    """A confirmed swing point."""
    bar_idx: int       # bar index from session start (monotonic)
    price: float       # the high (for swing high) or low (for swing low)
    cvd: float         # cumulative CVD at this bar
    kind: str          # "HIGH" or "LOW"


@dataclass
class DivergenceSignal:
    """A fresh divergence signal."""
    kind: str          # "BEARISH" (at swing highs) or "BULLISH" (at swing lows)
    bar_idx: int       # the new pivot's bar index
    new_price: float
    prior_price: float
    new_cvd: float
    prior_cvd: float
    bars_between: int


class SwingDivergenceDetector:
    """Detect classic bearish/bullish divergence at confirmed swing points."""

    def __init__(
        self,
        swing_strength: int = 3,
        min_bars_between: int = 10,
        max_bars_between: int = 40,
    ):
        """
        Args:
            swing_strength: number of bars on EACH side of a candidate that
                must have lower highs (or higher lows) to confirm the pivot.
                Default 3 = a moderate filter; higher = stricter.
            min_bars_between: minimum bars between consecutive same-type
                pivots for divergence to be considered meaningful.
            max_bars_between: max bars; staler than this and the prior
                pivot is no longer comparable.
        """
        self.swing_strength = swing_strength
        self.min_bars_between = min_bars_between
        self.max_bars_between = max_bars_between

        # Keep enough bars for the lookback + look-ahead window
        keep_n = max(50, swing_strength * 10)
        self.high_history: deque[float] = deque(maxlen=keep_n)
        self.low_history: deque[float] = deque(maxlen=keep_n)
        self.cvd_history: deque[float] = deque(maxlen=keep_n)

        # Monotonically increasing bar index since detector start
        self._bar_idx = 0

        # Tracked confirmed pivots
        self._last_high_pivot: Optional[Pivot] = None
        self._prior_high_pivot: Optional[Pivot] = None
        self._last_low_pivot: Optional[Pivot] = None
        self._prior_low_pivot: Optional[Pivot] = None

        # Most recent fresh signal — yielded once, then cleared
        self._pending_signal: Optional[DivergenceSignal] = None

    def update_bar(self, bar_high: float, bar_low: float,
                   cumulative_cvd: float) -> None:
        """Call on each completed bar close.

        Records the bar's high/low/cvd, then attempts to confirm a pivot
        N bars back (where N = swing_strength). If a new pivot forms,
        compares to the prior pivot of the same type for divergence.
        """
        try:
            self.high_history.append(float(bar_high))
            self.low_history.append(float(bar_low))
            self.cvd_history.append(float(cumulative_cvd))
        except (TypeError, ValueError):
            return
        self._bar_idx += 1

        # We can confirm a pivot at position `i` only after seeing
        # `swing_strength` bars AFTER it. So the candidate is at index
        # -1 - swing_strength from the right (counting from -1).
        cand_pos = -1 - self.swing_strength
        need_len = self.swing_strength * 2 + 1
        if len(self.high_history) < need_len:
            return

        # Convert deques to lists for index slicing
        highs = list(self.high_history)
        lows = list(self.low_history)
        cvds = list(self.cvd_history)

        cand_high = highs[cand_pos]
        cand_low = lows[cand_pos]
        cand_cvd = cvds[cand_pos]

        left_window_h = highs[cand_pos - self.swing_strength : cand_pos]
        right_window_h = highs[cand_pos + 1 : cand_pos + 1 + self.swing_strength]
        if not right_window_h:  # may happen on the very last bar of the deque
            right_window_h = highs[cand_pos + 1:]
        left_window_l = lows[cand_pos - self.swing_strength : cand_pos]
        right_window_l = lows[cand_pos + 1 : cand_pos + 1 + self.swing_strength]
        if not right_window_l:
            right_window_l = lows[cand_pos + 1:]

        # The actual bar index of the candidate (1-based from session start)
        cand_bar_idx = self._bar_idx - self.swing_strength

        # Check for confirmed swing HIGH
        if (left_window_h and right_window_h
                and all(h < cand_high for h in left_window_h)
                and all(h < cand_high for h in right_window_h)):
            self._on_new_pivot(Pivot(
                bar_idx=cand_bar_idx, price=cand_high,
                cvd=cand_cvd, kind="HIGH",
            ))

        # Check for confirmed swing LOW
        if (left_window_l and right_window_l
                and all(l > cand_low for l in left_window_l)
                and all(l > cand_low for l in right_window_l)):
            self._on_new_pivot(Pivot(
                bar_idx=cand_bar_idx, price=cand_low,
                cvd=cand_cvd, kind="LOW",
            ))

    def _on_new_pivot(self, pivot: Pivot) -> None:
        """Compare a fresh pivot to the prior same-type pivot for divergence."""
        if pivot.kind == "HIGH":
            prior = self._last_high_pivot
            self._prior_high_pivot = prior
            self._last_high_pivot = pivot
        else:
            prior = self._last_low_pivot
            self._prior_low_pivot = prior
            self._last_low_pivot = pivot

        if prior is None:
            return  # need at least 2 same-type pivots for divergence

        bars_between = pivot.bar_idx - prior.bar_idx
        if (bars_between < self.min_bars_between
                or bars_between > self.max_bars_between):
            return  # too close or too stale

        if pivot.kind == "HIGH":
            # Bearish divergence: new price high > prior, but new CVD < prior CVD.
            if pivot.price > prior.price and pivot.cvd < prior.cvd:
                self._pending_signal = DivergenceSignal(
                    kind="BEARISH",
                    bar_idx=pivot.bar_idx,
                    new_price=pivot.price,
                    prior_price=prior.price,
                    new_cvd=pivot.cvd,
                    prior_cvd=prior.cvd,
                    bars_between=bars_between,
                )
                logger.info(
                    f"[CVD_DIV] BEARISH at bar {pivot.bar_idx}: "
                    f"price {prior.price:.2f} -> {pivot.price:.2f} (HH) "
                    f"BUT cvd {prior.cvd:.0f} -> {pivot.cvd:.0f} (LH)"
                )
        else:  # LOW
            # Bullish divergence: new price low < prior, but new CVD > prior CVD.
            if pivot.price < prior.price and pivot.cvd > prior.cvd:
                self._pending_signal = DivergenceSignal(
                    kind="BULLISH",
                    bar_idx=pivot.bar_idx,
                    new_price=pivot.price,
                    prior_price=prior.price,
                    new_cvd=pivot.cvd,
                    prior_cvd=prior.cvd,
                    bars_between=bars_between,
                )
                logger.info(
                    f"[CVD_DIV] BULLISH at bar {pivot.bar_idx}: "
                    f"price {prior.price:.2f} -> {pivot.price:.2f} (LL) "
                    f"BUT cvd {prior.cvd:.0f} -> {pivot.cvd:.0f} (HL)"
                )

    def check_divergence(
        self, trade_direction: Optional[str] = None
    ) -> Optional[DivergenceSignal]:
        """Return a fresh divergence signal if one just formed; else None.

        Signals are consumed once — the next call returns None until the
        NEXT divergence forms.

        Args:
            trade_direction: if provided ("LONG"/"SHORT"), filter to only
                return signals relevant to that direction (BEARISH for LONG,
                BULLISH for SHORT). If None, return any pending signal.
        """
        sig = self._pending_signal
        if sig is None:
            return None
        # Pop the signal — caller gets it once
        self._pending_signal = None

        if trade_direction is None:
            return sig
        d = trade_direction.upper()
        # BEARISH divergence at a swing high warns LONG holders
        if d == "LONG" and sig.kind == "BEARISH":
            return sig
        # BULLISH divergence at a swing low warns SHORT holders
        if d == "SHORT" and sig.kind == "BULLISH":
            return sig
        # Doesn't match the trade — drop it
        return None
