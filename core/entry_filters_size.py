"""
Size-multiplier entry filters
=============================

Filters that DON'T veto a signal — they instead return a sizing multiplier
the bot can apply to position sizing for a high-confidence signal.

Currently:
    SpringSrSizeBoostFilter — boost spring_setup signals that occur at a
                              S/R zone of CONTROLLED strength (0.50 - 0.70).

DRAFT — NOT WIRED INTO PRODUCTION
---------------------------------
See docs/SR_CONFLUENCE_SPRING_SETUP.md for the supporting analysis
(20,778 spring trades over 5 years). The honest verdict is "weak partial
support" — `strong_sr` band shows +$300 lift over 5y, only 3/6 years
positive. **Do NOT ship until live-shadow paper-tracked for 30+ trades.**

This file is separated from Spawn A's `entry_filters_sr.py` (the
`bias_momentum` S/R veto) to keep concerns separate:
  - entry_filters_sr.py   = vetoes (signal -> killed)
  - entry_filters_size.py = sizing tweaks (signal -> resized)

Both can apply to the same signal independently.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.sr_zones import SRZone, detect_sr_zones, TICK

logger = logging.getLogger(__name__)


# ====================================================================
# Configuration constants — match the bucket boundaries that produced
# the positive-edge result in the SR confluence analysis.
# ====================================================================

# Proximity (ticks) — entry price must be within this many ticks of the
# zone to qualify as "AT the zone".
DEFAULT_PROXIMITY_TICKS = 4

# Strength band — ONLY zones in [STRENGTH_LOW, STRENGTH_HIGH) qualify
# for the boost. ABOVE 0.70 ("very_strong_sr") is an anti-edge in the
# data and must be EXCLUDED.
DEFAULT_STRENGTH_LOW = 0.50
DEFAULT_STRENGTH_HIGH = 0.70

# Multiplier on size when conditions met.
DEFAULT_BOOST = 1.30


class SpringSrSizeBoostFilter:
    """Compute a size multiplier for a spring_setup signal based on whether
    it occurred at a moderate-strength S/R zone.

    Returns DEFAULT_BOOST (1.30) ONLY when the nearest zone of the matching
    direction is within DEFAULT_PROXIMITY_TICKS AND its strength falls in
    the [DEFAULT_STRENGTH_LOW, DEFAULT_STRENGTH_HIGH) band.

    Returns 1.00 in all other cases:
      - no zone within proximity (noise area)
      - zone exists but strength < 0.50 (too weak to matter)
      - zone strength >= 0.70 (this is the anti-edge bucket — explicitly
        EXCLUDED, not just neutralized)

    Wiring example
    --------------
        if signal and signal.strategy == "spring_setup":
            multiplier = SpringSrSizeBoostFilter().size_multiplier(
                signal,
                bars_5m=bars_5m,
                market=market,
            )
            signal.size_multiplier = multiplier
    """

    def __init__(
        self,
        proximity_ticks: int = DEFAULT_PROXIMITY_TICKS,
        strength_low: float = DEFAULT_STRENGTH_LOW,
        strength_high: float = DEFAULT_STRENGTH_HIGH,
        boost: float = DEFAULT_BOOST,
        sr_lookback_bars: int = 300,
    ):
        self.proximity_ticks = proximity_ticks
        self.strength_low = strength_low
        self.strength_high = strength_high
        self.boost = boost
        self.sr_lookback_bars = sr_lookback_bars

    def size_multiplier(
        self,
        signal,
        bars_5m: list,
        market: Optional[dict] = None,
    ) -> float:
        """Return 1.30 if conditions met, else 1.00. Never raises."""
        try:
            return self._compute(signal, bars_5m, market or {})
        except Exception as e:  # pragma: no cover — never crash a live signal
            logger.warning(
                f"[SpringSrSizeBoost] exception during sizing decision: {e} "
                f"-- falling back to multiplier=1.00"
            )
            return 1.0

    # ----------------------------------------------------------------
    # internal
    # ----------------------------------------------------------------

    def _compute(self, signal, bars_5m: list, market: dict) -> float:
        if not bars_5m or len(bars_5m) < 50:
            return 1.0
        entry_price = float(getattr(signal, "entry_price", None)
                             or market.get("price") or 0)
        if entry_price <= 0:
            return 1.0
        direction = getattr(signal, "direction", "").upper()
        if direction not in ("LONG", "SHORT"):
            return 1.0

        zones = detect_sr_zones(
            bars_5m=bars_5m,
            current_price=entry_price,
            lookback_bars=self.sr_lookback_bars,
            prior_day_high=market.get("prior_day_high"),
            prior_day_low=market.get("prior_day_low"),
            prior_day_poc=market.get("prior_day_poc"),
            vwap=market.get("vwap"),
            vwap_std=market.get("vwap_std"),
        )
        target_type = "support" if direction == "LONG" else "resistance"
        z = self._nearest_zone(zones, entry_price, target_type)
        if z is None:
            return 1.0
        # MUST be in the narrow band — EXCLUDE very_strong_sr explicitly
        if self.strength_low <= z.strength < self.strength_high:
            logger.info(
                f"[SpringSrSizeBoost] BOOST x{self.boost:.2f} -- "
                f"zone {z.source} @ {z.price:.2f} strength={z.strength:.2f} "
                f"n_tests={z.n_tests}"
            )
            return self.boost
        # Either too weak (< 0.50) or in anti-edge band (>= 0.70)
        return 1.0

    def _nearest_zone(self, zones: list[SRZone], price: float,
                       zone_type: str) -> Optional[SRZone]:
        proximity = self.proximity_ticks * TICK
        best: Optional[SRZone] = None
        best_dist = float("inf")
        for z in zones:
            if z.type != zone_type:
                continue
            d = abs(z.price - price)
            if d <= proximity and d < best_dist:
                best = z
                best_dist = d
        return best
