"""
Phoenix Bot -- Trend Stall Detector

Determines when a trending move is running out of steam. Used by the trend
rider to decide when to exit the remaining (runner) contract after scale-out.

Approach:
    Three independent signals, each contributing to a stall score (0-6):
      1. TF alignment fading  (0-2 pts)
      2. CVD divergence       (0-2 pts)
      3. Price not making new highs/lows  (0-2 pts)

Severity thresholds:
    0-1 pts  -> NONE   (trend intact)
    2   pts  -> WEAK   (caution, hold)
    3-4 pts  -> MODERATE (consider tightening stop)
    5-6 pts  -> STRONG  (exit signal)

Design principle:
    Levels alone don't stall a trend (Quinn / user feedback). Stall = momentum
    losing internal energy, not just hitting a number.
"""

from __future__ import annotations
import logging
from collections import deque
from typing import Deque

logger = logging.getLogger("TrendStall")

# Minimum bar samples before emitting MODERATE+ signal
MIN_BARS_FOR_STALL = 3


class TrendStallDetector:
    """
    Stateful detector — call update_bar() on every completed bar,
    then check() to get current stall assessment.

    Keep one instance per trade (or reset between trades via reset()).
    """

    def __init__(self, lookback: int = 5):
        """
        Args:
            lookback: Number of bars to evaluate for price-not-advancing signal.
        """
        self.lookback = lookback
        self._price_highs: Deque[float] = deque(maxlen=lookback)
        self._price_lows: Deque[float] = deque(maxlen=lookback)
        self._bar_count = 0
        self._last_result: dict = _empty_result()
        # Intra-bar extremes — reset each bar, used for wick detection
        self._intrabar_high: float = 0.0
        self._intrabar_low: float = float("inf")

    def reset(self):
        """Call at trade entry to clear any previous state."""
        self._price_highs.clear()
        self._price_lows.clear()
        self._bar_count = 0
        self._last_result = _empty_result()
        self._intrabar_high = 0.0
        self._intrabar_low = float("inf")

    def update_tick_price(self, price: float):
        """
        Call on every tick to track the intra-bar price extreme.
        Used by check_ema_dom_exit() to detect wicks forming in real time.
        """
        if price <= 0:
            return
        if self._intrabar_high == 0.0:
            self._intrabar_high = price
            self._intrabar_low = price
        else:
            if price > self._intrabar_high:
                self._intrabar_high = price
            if price < self._intrabar_low:
                self._intrabar_low = price

    def update_bar(self, bar_high: float, bar_low: float, bar_close: float):
        """Feed each completed bar's high, low, close."""
        self._price_highs.append(bar_high)
        self._price_lows.append(bar_low)
        self._bar_count += 1
        # Reset intra-bar tracking for the new bar
        self._intrabar_high = bar_close
        self._intrabar_low = bar_close

    def check(self, market: dict, trade_direction: str) -> dict:
        """
        Evaluate current stall status.

        Args:
            market: aggregator.snapshot() dict — needs price, tf_bias, cvd, bar_delta
            trade_direction: "LONG" or "SHORT"

        Returns:
            {
              "stalling": bool,
              "severity": "NONE"|"WEAK"|"MODERATE"|"STRONG",
              "exit_signal": bool,       # True = close the runner
              "tighten_stop": bool,      # True = trail stop tighter
              "score": int,             # Raw score 0-6
              "reasons": [str, ...]
            }
        """
        direction = trade_direction.upper()
        price     = float(market.get("price", 0) or 0)
        tf_bias   = market.get("tf_bias", {})
        cvd       = float(market.get("cvd", 0) or 0)

        reasons = []
        score   = 0

        # ── Factor 1: TF Alignment Fading ─────────────────────────
        tf_bull = sum(1 for v in tf_bias.values() if v == "BULLISH")
        tf_bear = sum(1 for v in tf_bias.values() if v == "BEARISH")
        total   = len(tf_bias) or 4

        if direction == "LONG":
            aligned = tf_bull
        else:
            aligned = tf_bear

        if aligned <= 1:
            score += 2
            reasons.append(f"TF alignment collapsed ({aligned}/{total} for {direction})")
        elif aligned <= 2 and total >= 4:
            score += 1
            reasons.append(f"TF alignment fading ({aligned}/{total} for {direction})")

        # ── Factor 2: CVD Divergence ───────────────────────────────
        # Price making highs but sellers absorbing (CVD dropping) = distribution
        if direction == "LONG":
            if cvd < -500_000:
                score += 2
                reasons.append(f"CVD strongly negative ({cvd/1e6:.1f}M) vs LONG — heavy selling")
            elif cvd < 0:
                score += 1
                reasons.append(f"CVD negative ({cvd/1e6:.1f}M) while price bullish — divergence")
        else:  # SHORT
            if cvd > 500_000:
                score += 2
                reasons.append(f"CVD strongly positive (+{cvd/1e6:.1f}M) vs SHORT — heavy buying")
            elif cvd > 0:
                score += 1
                reasons.append(f"CVD positive (+{cvd/1e6:.1f}M) while price bearish — divergence")

        # ── Factor 3: Price Not Advancing ──────────────────────────
        if self._bar_count >= MIN_BARS_FOR_STALL and price > 0:
            if direction == "LONG" and len(self._price_highs) >= 2:
                recent_max  = max(list(self._price_highs)[-2:])
                earlier_max = max(list(self._price_highs)[:-2]) if len(self._price_highs) > 2 else recent_max
                if recent_max <= earlier_max:
                    score += 2
                    reasons.append("Price not making new highs (upside momentum stalled)")
                elif recent_max <= earlier_max * 1.0005:  # < 0.05% new highs = marginal
                    score += 1
                    reasons.append("Price barely making new highs (momentum slowing)")

            elif direction == "SHORT" and len(self._price_lows) >= 2:
                recent_min  = min(list(self._price_lows)[-2:])
                earlier_min = min(list(self._price_lows)[:-2]) if len(self._price_lows) > 2 else recent_min
                if recent_min >= earlier_min:
                    score += 2
                    reasons.append("Price not making new lows (downside momentum stalled)")
                elif recent_min >= earlier_min * 0.9995:
                    score += 1
                    reasons.append("Price barely making new lows (momentum slowing)")

        # ── Severity ───────────────────────────────────────────────
        if score == 0:
            severity = "NONE"
        elif score == 1:
            severity = "WEAK"
        elif score <= 3:
            severity = "MODERATE"
        else:
            severity = "STRONG"

        exit_signal   = severity == "STRONG"
        tighten_stop  = severity == "MODERATE"

        result = {
            "stalling":     score > 0,
            "severity":     severity,
            "exit_signal":  exit_signal,
            "tighten_stop": tighten_stop,
            "score":        score,
            "reasons":      reasons,
            "bars_tracked": self._bar_count,
        }
        self._last_result = result

        if severity in ("MODERATE", "STRONG"):
            logger.info(f"[STALL:{direction}] {severity} (score={score}) — {'; '.join(reasons)}")

        return result

    @property
    def last_result(self) -> dict:
        return self._last_result

    def check_ema_dom_exit(self, market: dict, trade_direction: str,
                           tick_size: float = 0.25,
                           entry_price: float = 0.0,
                           entry_time: float = 0.0,
                           min_hold_seconds: int = 120,
                           min_profit_ticks: int = 40,
                           ema_ext_ticks: int = 40,
                           wick_ticks: int = 10) -> dict:
        """
        Reversal exit detector — fires when a move is genuinely reversing, not just
        pausing. Designed for 20-50+ point targets, not 3-point scalps.

        Replicates the user's manual exit:
          "As SOON as I saw sell orders stacking on the DOM, AND my candlesticks
           starting to wick, I closed the trade."
        Both signals must confirm — DOM alone or wick alone is noise in a trend.

        Gate conditions (ALL must pass):
          1. Min hold 120s — give the trade 2 minutes minimum before any exit.
             Most 20-point moves take 2-8 minutes to develop.
          2. Min profit 40t (10 pts) — only protect gains, never scalp exits.
             If we haven't moved 10 points, we haven't earned the right to exit.
          3. EMA extension 40t (10 pts) — price must be extended before reversal
             matters. A pullback at 5 pts of extension is just noise.

        Reversal signals (BOTH required — this is the key change from v1):
          a. DOM reversal: raw imbalance ratio flipped against position
          b. Candle wick: intra-bar high/low has pulled back >= wick_ticks
          (Either alone = noise in a real trend. Both together = genuine reversal.)

        Returns:
            {
              "exit_signal": bool,
              "reason": str,
              "ema_dist_ticks": float,
              "profit_ticks": float,
              "hold_seconds": float,
              "dom_reversal": bool,
              "wick_rejection": bool,
            }
        """
        import time as _time
        direction  = trade_direction.upper()
        price      = float(market.get("price", 0) or 0)
        ema9       = float(market.get("ema9",  0) or 0)
        hold_secs  = _time.time() - entry_time if entry_time > 0 else 9999

        _no_signal = {"exit_signal": False, "reason": "", "ema_dist_ticks": 0,
                      "profit_ticks": 0, "hold_seconds": hold_secs,
                      "dom_reversal": False, "wick_rejection": False}

        if price <= 0 or tick_size <= 0:
            return _no_signal

        # ── Gate 1: Minimum hold time ──────────────────────────────────
        # Prevents firing instantly on momentum entries that are already above EMA9.
        # The user's pattern: enter at pullback → ride up → exit at top.
        # If we entered at EMA9, we need time for price to rally away first.
        if hold_secs < min_hold_seconds:
            return {**_no_signal, "reason": f"hold_wait ({hold_secs:.0f}s < {min_hold_seconds}s)"}

        # ── Gate 2: Minimum profit from entry ─────────────────────────
        # Must be in profit before we can smart-exit. Prevents exiting a losing
        # trade via smart exit (let stop loss handle those).
        if entry_price > 0:
            if direction == "LONG":
                profit_ticks = (price - entry_price) / tick_size
            else:
                profit_ticks = (entry_price - price) / tick_size
        else:
            profit_ticks = 0.0

        if profit_ticks < min_profit_ticks:
            return {**_no_signal, "profit_ticks": profit_ticks,
                    "reason": f"not_in_profit ({profit_ticks:.0f}t < {min_profit_ticks}t needed)"}

        # ── Gate 3: EMA Extension ─────────────────────────────────────
        # Price must be overextended above the 9 EMA (user: "1-2 inches from EMA9")
        ema_dist_ticks = 0.0
        if ema9 > 0:
            if direction == "LONG":
                ema_dist_ticks = (price - ema9) / tick_size
            else:
                ema_dist_ticks = (ema9 - price) / tick_size

            if ema_dist_ticks < ema_ext_ticks:
                return {**_no_signal, "profit_ticks": profit_ticks,
                        "ema_dist_ticks": ema_dist_ticks, "reason": "not_extended"}

        reasons = [f"+{profit_ticks:.0f}t from entry, {ema_dist_ticks:.0f}t from EMA9"]

        # ── Reversal 1: DOM imbalance shifted against position ─────────
        # Use RAW ratio only (not the boolean flag — that uses a different threshold
        # and can be stale). A clear shift: LONG needs imbal < 0.40 (ask-heavy).
        dom_reversal = False
        dom_imbal    = float(market.get("dom_imbalance", 0.5) or 0.5)
        dom_signal   = market.get("dom_signal", {}) or {}
        dom_dir      = dom_signal.get("direction") if isinstance(dom_signal, dict) else None
        dom_str      = float(dom_signal.get("strength", 0)) if isinstance(dom_signal, dict) else 0

        if direction == "LONG":
            if dom_imbal < 0.40:   # Clearly ask-heavy (sellers stacking)
                dom_reversal = True
                reasons.append(f"DOM ask-heavy ({dom_imbal:.2f}) — sell orders stacking")
            if dom_dir == "SHORT" and dom_str >= 50:
                dom_reversal = True
                reasons.append(f"DOM absorption SHORT (str={dom_str:.0f})")
        else:  # SHORT
            if dom_imbal > 0.60:   # Clearly bid-heavy (buyers stacking)
                dom_reversal = True
                reasons.append(f"DOM bid-heavy ({dom_imbal:.2f}) — buy orders stacking")
            if dom_dir == "LONG" and dom_str >= 50:
                dom_reversal = True
                reasons.append(f"DOM absorption LONG (str={dom_str:.0f})")

        # ── Reversal 2: Candle wicking (real-time intra-bar) ──────────
        # price pulled back from this bar's high = wick forming at the top
        wick_rejection = False
        if direction == "LONG" and self._intrabar_high > 0:
            intrabar_wick = (self._intrabar_high - price) / tick_size
            if intrabar_wick >= wick_ticks:
                wick_rejection = True
                reasons.append(f"Upper wick {intrabar_wick:.0f}t (high={self._intrabar_high:.2f})")
        elif direction == "SHORT" and self._intrabar_low < float("inf"):
            intrabar_wick = (price - self._intrabar_low) / tick_size
            if intrabar_wick >= wick_ticks:
                wick_rejection = True
                reasons.append(f"Lower wick {intrabar_wick:.0f}t (low={self._intrabar_low:.2f})")

        # ── Decision ──────────────────────────────────────────────────
        # BOTH required — DOM alone or wick alone is trend noise.
        # When both fire simultaneously = the user's described pattern:
        # "sell orders stacking AND candlesticks starting to wick."
        exit_signal = dom_reversal and wick_rejection

        if exit_signal:
            logger.info(f"[SMART EXIT:{direction}] Triggered — {' | '.join(reasons)}")

        return {
            "exit_signal":    exit_signal,
            "reason":         f"ema_dom_exit [{' | '.join(reasons)}]" if exit_signal else "",
            "ema_dist_ticks": ema_dist_ticks,
            "profit_ticks":   profit_ticks,
            "hold_seconds":   hold_secs,
            "dom_reversal":   dom_reversal,
            "wick_rejection": wick_rejection,
        }


# ── Stateless helper for one-shot checks ─────────────────────────────

def detect_stall(market: dict, trade_direction: str,
                 recent_bar_highs: list[float] | None = None,
                 recent_bar_lows: list[float] | None = None) -> dict:
    """
    Stateless convenience function. Pass recent bar highs/lows explicitly.

    Args:
        market: aggregator snapshot
        trade_direction: "LONG" or "SHORT"
        recent_bar_highs: List of recent bar highs (newest last)
        recent_bar_lows: List of recent bar lows (newest last)

    Returns same dict as TrendStallDetector.check()
    """
    det = TrendStallDetector(lookback=5)
    if recent_bar_highs and recent_bar_lows:
        n = min(len(recent_bar_highs), len(recent_bar_lows))
        for i in range(n):
            h = recent_bar_highs[i]
            l = recent_bar_lows[i]
            det.update_bar(h, l, (h + l) / 2)
    return det.check(market, trade_direction)


def _empty_result() -> dict:
    return {
        "stalling": False, "severity": "NONE", "exit_signal": False,
        "tighten_stop": False, "score": 0, "reasons": [], "bars_tracked": 0,
    }
