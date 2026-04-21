"""
Phoenix Bot -- Setup Crowding Detector

Detects when a setup is at an 'obvious' level everyone is watching.
Obvious setups have decayed edge because too many traders are positioned there.

OBSERVATION ONLY -- returns a crowding score for the AI to consider.
"""

import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger("CrowdingDetector")

TICK_SIZE = 0.25
# NQ round numbers every 100 points (e.g., 18500, 18600)
ROUND_NUMBER_INTERVAL = 100


class CrowdingDetector:
    """
    Detects when a setup is at an 'obvious' level everyone is watching.
    Obvious setups have decayed edge because too many traders are positioned there.
    """

    def __init__(self):
        self._prior_day_levels: dict = {}   # {date_str: {high, low, close}}
        self._session_levels: dict = {}      # {vwap, open, high, low}
        self._touch_counts: dict = {}        # {rounded_price: touch_count}
        self._session_date: str | None = None
        self._session_open: float = 0.0
        self._session_high: float = 0.0
        self._session_low: float = float("inf")
        self._overnight_high: float = 0.0
        self._overnight_low: float = float("inf")
        self._last_bar_range: float = 0.0    # For compressed-range detection

    def update_levels(self, market: dict, bars_1m: list):
        """
        Update tracked levels from market data and completed bars.

        Args:
            market: tick_aggregator.snapshot() dict
            bars_1m: list of completed 1m Bar objects
        """
        price = market.get("price", 0)
        if price <= 0:
            return

        today = datetime.now().strftime("%Y-%m-%d")

        # New session day: roll prior day levels
        if self._session_date and today != self._session_date:
            self._prior_day_levels[self._session_date] = {
                "high": self._session_high,
                "low": self._session_low if self._session_low < float("inf") else 0,
                "close": price,  # Approximate close as first price of new day
            }
            # Keep only last 5 days
            dates = sorted(self._prior_day_levels.keys())
            while len(dates) > 5:
                del self._prior_day_levels[dates.pop(0)]

            # Reset session tracking
            self._session_open = price
            self._session_high = price
            self._session_low = price
            self._touch_counts.clear()

        if not self._session_date or today != self._session_date:
            self._session_date = today
            if self._session_open == 0:
                self._session_open = price

        # Update session extremes
        self._session_high = max(self._session_high, price)
        if self._session_low == float("inf"):
            self._session_low = price
        else:
            self._session_low = min(self._session_low, price)

        # Track touches at rounded levels (1-point granularity)
        rounded = round(price)
        self._touch_counts[rounded] = self._touch_counts.get(rounded, 0) + 1

        # Track recent bar range for compression detection
        if bars_1m and len(bars_1m) >= 5:
            recent_5 = bars_1m[-5:]
            highs = [b.high for b in recent_5]
            lows = [b.low for b in recent_5]
            self._last_bar_range = max(highs) - min(lows)

        # Update session levels dict
        self._session_levels = {
            "vwap": market.get("vwap", 0),
            "open": self._session_open,
            "high": self._session_high,
            "low": self._session_low,
        }

    def get_crowding_score(self, entry_price: float, direction: str,
                           market: dict) -> dict:
        """
        Score how 'crowded' this entry level is.

        Args:
            entry_price: Proposed entry price
            direction: "LONG" or "SHORT"
            market: tick_aggregator.snapshot() dict

        Returns: {
            score: 0-100 (0 = unique level, 100 = maximum crowding),
            factors: [{name, distance_ticks, crowded: bool}],
            recommendation: "UNIQUE_EDGE" | "MODERATE" | "CROWDED_AVOID",
        }
        """
        score = 0
        factors = []

        # ── 1. Distance to prior day high/low ────────────────────────
        for date_str, levels in self._prior_day_levels.items():
            pd_high = levels.get("high", 0)
            pd_low = levels.get("low", 0)

            if pd_high > 0:
                dist_high = abs(entry_price - pd_high) / TICK_SIZE
                crowded = dist_high <= 5
                factors.append({
                    "name": f"Prior day high ({date_str})",
                    "distance_ticks": round(dist_high, 1),
                    "crowded": crowded,
                })
                if crowded:
                    score += 15

            if pd_low > 0:
                dist_low = abs(entry_price - pd_low) / TICK_SIZE
                crowded = dist_low <= 5
                factors.append({
                    "name": f"Prior day low ({date_str})",
                    "distance_ticks": round(dist_low, 1),
                    "crowded": crowded,
                })
                if crowded:
                    score += 15

        # ── 2. Distance to overnight high/low ────────────────────────
        # Use session high/low before open momentum as overnight proxy
        if self._session_high > 0:
            dist = abs(entry_price - self._session_high) / TICK_SIZE
            crowded = dist <= 5
            factors.append({
                "name": "Session high",
                "distance_ticks": round(dist, 1),
                "crowded": crowded,
            })
            if crowded:
                score += 10

        if self._session_low < float("inf"):
            dist = abs(entry_price - self._session_low) / TICK_SIZE
            crowded = dist <= 5
            factors.append({
                "name": "Session low",
                "distance_ticks": round(dist, 1),
                "crowded": crowded,
            })
            if crowded:
                score += 10

        # ── 3. Distance to session VWAP ──────────────────────────────
        vwap = market.get("vwap", 0)
        if vwap > 0:
            dist_vwap = abs(entry_price - vwap) / TICK_SIZE
            crowded = dist_vwap <= 3
            factors.append({
                "name": "Session VWAP",
                "distance_ticks": round(dist_vwap, 1),
                "crowded": crowded,
            })
            if crowded:
                score += 20  # VWAP = most watched level

        # ── 4. Touch count at this level ─────────────────────────────
        rounded_price = round(entry_price)
        touches = self._touch_counts.get(rounded_price, 0)
        well_defended = touches > 3
        factors.append({
            "name": f"Touch count at {rounded_price}",
            "distance_ticks": 0,
            "touches": touches,
            "crowded": well_defended,
        })
        if well_defended:
            score += 15

        # ── 5. Distance to round number ──────────────────────────────
        nearest_round = round(entry_price / ROUND_NUMBER_INTERVAL) * ROUND_NUMBER_INTERVAL
        dist_round = abs(entry_price - nearest_round) / TICK_SIZE
        crowded = dist_round <= 5
        factors.append({
            "name": f"Round number ({nearest_round:.0f})",
            "distance_ticks": round(dist_round, 1),
            "crowded": crowded,
        })
        if crowded:
            score += 10

        # ── 6. Compressed range before signal ────────────────────────
        atr_5m = market.get("atr_5m", 0)
        if atr_5m > 0 and self._last_bar_range > 0:
            compression_ratio = self._last_bar_range / atr_5m
            compressed = compression_ratio < 0.5  # Range < 50% of ATR
            factors.append({
                "name": "Range compression",
                "distance_ticks": 0,
                "compression_ratio": round(compression_ratio, 2),
                "crowded": compressed,
            })
            if compressed:
                score += 15  # Tight range = breakout everyone sees

        # ── Clamp and classify ───────────────────────────────────────
        score = max(0, min(100, score))

        if score <= 25:
            recommendation = "UNIQUE_EDGE"
        elif score <= 55:
            recommendation = "MODERATE"
        else:
            recommendation = "CROWDED_AVOID"

        result = {
            "score": score,
            "factors": factors,
            "recommendation": recommendation,
        }

        crowded_names = [f["name"] for f in factors if f.get("crowded")]
        logger.info(
            f"[CROWDING] score={score} rec={recommendation} "
            f"crowded_at={crowded_names[:3] if crowded_names else 'none'}"
        )

        return result

    def to_dict(self) -> dict:
        """For dashboard display."""
        return {
            "session_date": self._session_date,
            "session_high": self._session_high,
            "session_low": self._session_low if self._session_low < float("inf") else 0,
            "session_open": self._session_open,
            "prior_days_tracked": len(self._prior_day_levels),
            "touch_levels_tracked": len(self._touch_counts),
            "last_bar_range": round(self._last_bar_range, 2),
        }
