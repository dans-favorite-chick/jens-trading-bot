"""
Phoenix Bot — Volume Profile (per-session)

Builds POC, HVN, LVN, VAH, VAL from tick-by-tick data during the session.
Plus time-at-price accounting (TPO-lite): count of bars each price was traded at.

Research basis (2026):
- POC (Point of Control) = price with highest volume in session → magnet
- HVN (High Volume Nodes) = clusters that act as support/resistance
- LVN (Low Volume Nodes) = price zones with little volume → fast transit
- Value Area (70% of volume) = VAH (top) / VAL (bottom)
- Pullbacks to POC are high-probability reversion setups
- LVNs are where stops should be placed beyond (not inside)

Feed: base_bot calls update_tick(price, volume, ts) on every tick.
Reset: at new RTH session open (08:30 CDT) or manually via reset().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("VolumeProfile")

# ─── Config ────────────────────────────────────────────────────────────
PRICE_BUCKET_SIZE = 0.25   # MNQ tick size — aggregate volume per tick level
MIN_SESSION_VOLUME_FOR_POC = 10_000   # Below this, profile is too sparse to trust


@dataclass
class VolumeProfile:
    """Per-session volume profile with POC/HVN/LVN/VA calculation."""
    bucket_size: float = PRICE_BUCKET_SIZE
    # Map: bucketed_price → dict(volume=float, time_bars=int)
    # "time_bars" counts distinct 1m bars where price touched this bucket → time-at-price proxy
    volume_at_price: dict[float, float] = field(default_factory=dict)
    time_at_price: dict[float, int] = field(default_factory=dict)
    total_volume: float = 0.0
    session_started_at: Optional[datetime] = None
    session_date: Optional[str] = None
    _last_bar_prices: set[float] = field(default_factory=set)  # track per-bar uniqueness

    def _bucket(self, price: float) -> float:
        """Round price to nearest bucket."""
        return round(price / self.bucket_size) * self.bucket_size

    def update_tick(self, price: float, volume: float, ts: datetime) -> None:
        """Aggregate a single tick into the profile."""
        if volume <= 0 or price <= 0:
            return
        b = self._bucket(price)
        self.volume_at_price[b] = self.volume_at_price.get(b, 0.0) + volume
        self.total_volume += volume
        self._last_bar_prices.add(b)
        if self.session_started_at is None:
            self.session_started_at = ts
            self.session_date = ts.strftime("%Y-%m-%d")

    def on_bar_close(self) -> None:
        """Call at the close of each 1m bar to tally time-at-price."""
        for price in self._last_bar_prices:
            self.time_at_price[price] = self.time_at_price.get(price, 0) + 1
        self._last_bar_prices.clear()

    def reset(self, session_date: str = None) -> None:
        """Reset profile for a new session."""
        self.volume_at_price.clear()
        self.time_at_price.clear()
        self._last_bar_prices.clear()
        self.total_volume = 0.0
        self.session_started_at = None
        self.session_date = session_date

    # ─── Profile metrics ───────────────────────────────────────────────

    def poc(self) -> Optional[float]:
        """Point of Control — price with highest traded volume."""
        if not self.volume_at_price:
            return None
        return max(self.volume_at_price, key=self.volume_at_price.get)

    def value_area(self, target_pct: float = 0.7) -> Optional[tuple[float, float]]:
        """
        Return (VAL, VAH) — lower/upper bounds containing `target_pct` of volume.
        Standard: 70% value area.
        """
        if self.total_volume < MIN_SESSION_VOLUME_FOR_POC:
            return None
        poc_price = self.poc()
        if poc_price is None:
            return None
        target_volume = self.total_volume * target_pct
        sorted_prices = sorted(self.volume_at_price.keys())
        # Expand outward from POC
        va_volume = self.volume_at_price[poc_price]
        low_i = sorted_prices.index(poc_price)
        high_i = sorted_prices.index(poc_price)
        while va_volume < target_volume and (low_i > 0 or high_i < len(sorted_prices) - 1):
            # Compare volume at prices one step outward from current bounds
            below_vol = self.volume_at_price[sorted_prices[low_i - 1]] if low_i > 0 else -1
            above_vol = self.volume_at_price[sorted_prices[high_i + 1]] if high_i < len(sorted_prices) - 1 else -1
            if below_vol >= above_vol:
                low_i -= 1
                va_volume += below_vol
            else:
                high_i += 1
                va_volume += above_vol
        return (sorted_prices[low_i], sorted_prices[high_i])

    def high_volume_nodes(self, top_n: int = 3) -> list[tuple[float, float]]:
        """
        Return top N HVN prices (highest volume concentration).
        Excludes POC (which is top already).
        """
        if len(self.volume_at_price) < 5:
            return []
        sorted_by_vol = sorted(self.volume_at_price.items(), key=lambda x: -x[1])
        # Filter out prices adjacent to POC (avoid returning same cluster)
        poc_price = self.poc()
        result = []
        for price, vol in sorted_by_vol:
            if abs(price - poc_price) < self.bucket_size * 3:
                continue
            result.append((price, vol))
            if len(result) >= top_n:
                break
        return result

    def low_volume_nodes(self, threshold_pct: float = 0.15) -> list[float]:
        """
        Return prices where volume < threshold × average bucket volume.
        These are LVNs — price tends to move fast through them.
        """
        if not self.volume_at_price:
            return []
        avg_vol = self.total_volume / len(self.volume_at_price)
        threshold = avg_vol * threshold_pct
        lvns = [p for p, v in self.volume_at_price.items() if v < threshold]
        return sorted(lvns)

    def to_dict(self) -> dict:
        """Serializable snapshot for dashboard + market snapshot enrichment."""
        poc_price = self.poc()
        va = self.value_area()
        hvns = self.high_volume_nodes()
        lvns = self.low_volume_nodes()
        return {
            "session_date": self.session_date,
            "total_volume": self.total_volume,
            "poc": poc_price,
            "vah": va[1] if va else None,
            "val": va[0] if va else None,
            "hvn_list": [{"price": p, "volume": v} for p, v in hvns],
            "lvn_count": len(lvns),
            "lvn_sample": lvns[:5],  # first 5
            "price_bucket_count": len(self.volume_at_price),
        }


# ─── Helper: is price near a structural level? ─────────────────────────

def price_near_level(price: float, level: Optional[float], tolerance_ticks: int = 4) -> bool:
    """Returns True if price is within tolerance_ticks of the level."""
    if level is None or level <= 0:
        return False
    return abs(price - level) <= (tolerance_ticks * PRICE_BUCKET_SIZE)


def classify_price_location(price: float, profile: VolumeProfile) -> str:
    """
    Classify current price relative to volume profile structure.
    Returns: "AT_POC", "IN_VA", "ABOVE_VA", "BELOW_VA", "AT_HVN", "IN_LVN", "UNKNOWN"
    """
    poc = profile.poc()
    va = profile.value_area()
    if poc is None or va is None:
        return "UNKNOWN"
    val, vah = va
    if price_near_level(price, poc, tolerance_ticks=4):
        return "AT_POC"
    hvns = [h[0] for h in profile.high_volume_nodes()]
    for hvn in hvns:
        if price_near_level(price, hvn, tolerance_ticks=4):
            return "AT_HVN"
    lvns = profile.low_volume_nodes()
    # Check if price is currently in an LVN zone (no volume cluster)
    if lvns:
        # Find nearest LVN cluster
        nearest_lvn_dist = min(abs(price - l) for l in lvns)
        if nearest_lvn_dist <= PRICE_BUCKET_SIZE * 2:
            return "IN_LVN"
    if val <= price <= vah:
        return "IN_VA"
    if price > vah:
        return "ABOVE_VA"
    if price < val:
        return "BELOW_VA"
    return "UNKNOWN"
