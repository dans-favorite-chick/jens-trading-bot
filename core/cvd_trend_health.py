"""CVD Trend Health detector — pre-entry filter.

Per operator's trade-flow methodology: "price moving up but CVD not"
= unhealthy trend. Used as an ENTRY VETO to skip trades fighting
hidden institutional flow.

Mechanism:
  1. Track per-bar `bar_close` and `cumulative_cvd` over a rolling window
     (default 6 bars = 30 min on 5m bars).
  2. On `assess(intended_direction)`:
       a. Compute least-squares slope of price over the window.
       b. Compute least-squares slope of cumulative CVD over the window.
       c. Normalize both to a [-1, +1] range (sign of slope × magnitude
          relative to recent volatility).
       d. agreement = +1 if both slope signs match the intended direction;
          -1 if they fully oppose; intermediate values blend.
       e. veto = True iff agreement < veto_threshold (default -0.3).
  3. Veto fires when the cumulative CVD trend disagrees with intended
     direction by a meaningful margin — even if price is moving with you.

Why slope-based (vs sign comparison):
  Sign-only check misses degree: price barely up + CVD barely down → no
  veto under sign-check, even though it's a weak setup. Slope captures
  the magnitude of disagreement. Empirically more robust to single-bar
  noise.

Usage in strategies:
    self.cvd_health.update_bar(bar.close, market["cvd"])
    health = self.cvd_health.assess("LONG")
    if health["veto"]:
        return None  # skip the entry
"""
from __future__ import annotations

import math
import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger("CVDTrendHealth")


def _slope(series: list[float]) -> float:
    """Least-squares slope of a series (Δy per unit Δx, x=0..N-1).

    Returns 0.0 if fewer than 2 points or all values identical."""
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    num = sum((xs[i] - mean_x) * (series[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


def _normalize_slope(slope: float, series: list[float]) -> float:
    """Normalize a slope to roughly [-1, +1] by dividing by series stdev.

    Returns sign(slope) when stdev is 0. Clipped to [-1, +1]."""
    n = len(series)
    if n < 2:
        return 0.0 if slope == 0 else math.copysign(1.0, slope)
    mean = sum(series) / n
    var = sum((v - mean) ** 2 for v in series) / n
    stdev = math.sqrt(var)
    if stdev == 0:
        return 0.0 if slope == 0 else math.copysign(1.0, slope)
    # Empirical scaling: a slope equal to one stdev per bar gets ratio 1.0.
    ratio = slope / stdev
    if ratio > 1.0:
        return 1.0
    if ratio < -1.0:
        return -1.0
    return ratio


class CVDTrendHealth:
    """Detect when cumulative CVD direction disagrees with price direction.

    Used as ENTRY FILTER to skip trades fighting hidden flow.
    """

    def __init__(self, lookback_bars: int = 6, veto_threshold: float = -0.3):
        """
        Args:
            lookback_bars: how many recent bar closes to consider (default 6 =
                30 min on 5m bars, ~6 min on 1m bars).
            veto_threshold: agreement value below which `veto=True`. Default
                -0.3 = "moderately opposing flow." Lower = pickier filter.
        """
        self.lookback = lookback_bars
        self.veto_threshold = veto_threshold
        self.price_history: deque[float] = deque(maxlen=lookback_bars)
        self.cvd_history: deque[float] = deque(maxlen=lookback_bars)

    def update_bar(self, bar_close: float, cumulative_cvd: float) -> None:
        """Call on each completed bar close."""
        try:
            self.price_history.append(float(bar_close))
            self.cvd_history.append(float(cumulative_cvd))
        except (TypeError, ValueError):
            # Bad input — skip the bar rather than poison the deque
            pass

    def assess(self, intended_direction: str) -> dict:
        """Compute agreement between price slope and CVD slope, given the
        direction the trade WANTS to go.

        Args:
            intended_direction: "LONG" or "SHORT"

        Returns:
            {
              "agreement":  float in [-1, +1] (+1 = full agreement,
                                              -1 = full opposition)
              "veto":       bool (True iff agreement < veto_threshold)
              "price_slope": float (raw slope, points per bar)
              "cvd_slope":   float (raw slope, CVD units per bar)
              "n_bars":     int (bars in the window — 0 if no data)
              "reason":     str (human-readable rationale)
            }
        """
        prices = list(self.price_history)
        cvds = list(self.cvd_history)
        n = min(len(prices), len(cvds))

        if n < 2:
            return {
                "agreement": 0.0,
                "veto": False,
                "price_slope": 0.0,
                "cvd_slope": 0.0,
                "n_bars": n,
                "reason": f"insufficient history ({n} bars)",
            }

        price_slope = _slope(prices)
        cvd_slope = _slope(cvds)

        price_norm = _normalize_slope(price_slope, prices)
        cvd_norm = _normalize_slope(cvd_slope, cvds)

        # Direction sign: +1 for LONG (want both slopes positive), -1 for SHORT.
        dir_sign = 1.0 if intended_direction.upper() == "LONG" else -1.0

        # Each axis: how much does its slope-sign match the intended direction?
        price_score = price_norm * dir_sign   # +1 = perfectly aligned
        cvd_score = cvd_norm * dir_sign       # +1 = perfectly aligned

        # Agreement = MIN of the two scores (NOT average). The "CVD trend
        # health" check is gated on the weakest axis — if EITHER price
        # OR cvd is opposing the intended direction, the trade is fighting
        # something, regardless of how aligned the other axis is.
        # - Both aligned:   agreement near +1
        # - One aligned, one opposing: agreement = the opposing one (negative)
        # - Both opposing:  agreement near -1
        # This is the correct semantic for "veto on opposing flow" — the
        # original (mean) formula let "price up + CVD down" average to 0,
        # masking the very pattern the detector was built to catch.
        agreement = min(price_score, cvd_score)
        agreement = max(-1.0, min(1.0, agreement))

        veto = agreement < self.veto_threshold

        if veto:
            reason = (
                f"CVD disagrees with {intended_direction}: "
                f"price_slope={price_slope:+.2f} cvd_slope={cvd_slope:+.0f} "
                f"agreement={agreement:+.2f} < threshold {self.veto_threshold:+.2f}"
            )
        else:
            reason = (
                f"CVD health OK for {intended_direction}: "
                f"agreement={agreement:+.2f}"
            )

        return {
            "agreement": round(agreement, 3),
            "veto": veto,
            "price_slope": round(price_slope, 4),
            "cvd_slope": round(cvd_slope, 2),
            "n_bars": n,
            "reason": reason,
        }
