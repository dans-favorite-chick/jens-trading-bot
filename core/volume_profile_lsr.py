"""
Phoenix Bot — Volume Profile Builder (HVN / LVN)
=================================================

PHASE B: Adds volume-at-price intelligence to the LSR strategy.

CORE IDEA
---------
For each 1-min bar in a lookback window, distribute its volume across
its price range (high to low) into 1-point buckets. Sum across all bars
to get a histogram of volume-at-price for the period.

From this histogram extract:
  POC  — bucket with max volume (Point of Control / "fair value")
  VAH  — top of value area (70% volume envelope)
  VAL  — bottom of value area
  HVN  — local peaks in the histogram (price levels with concentrated trade)
  LVN  — local valleys (price levels with thin trade / "air pockets")

USE IN LSR STRATEGY
-------------------
When a sweep fires, classify the swept level relative to HVN/LVN:
  - Swept level near HVN → strong defense, conservative target (T2 → POC)
  - Swept level near LVN → air pocket, extended target (next HVN beyond)
  - Neutral → default target at opposite extreme

INPUTS
------
- bars: list of Bar-like objects with .high, .low, .volume attributes
- price_resolution: bucket size in price units (default 1.0 = 1 NQ point)
- value_area_pct: fraction of volume in value area (default 0.70)

OUTPUTS — VolumeProfile dataclass with:
  poc, vah, val, total_volume, session_count
  hvn_levels: list of HVN prices (sorted by volume desc)
  lvn_levels: list of LVN prices (sorted by inverse-volume desc)
  histogram: dict[price_bucket → volume]

DEPENDENCIES
------------
None. Pure stdlib.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")


# ────────────────────────────────────────────────────────────────────
@dataclass
class VolumeProfile:
    """Computed volume profile over some bar window."""
    poc: float                            # Point of Control (max-volume price)
    vah: float                            # Value Area High
    val: float                            # Value Area Low
    total_volume: float                   # sum of all volume in window
    session_count: int                    # number of trading sessions covered
    hvn_levels: list[float] = field(default_factory=list)
    lvn_levels: list[float] = field(default_factory=list)
    histogram: dict[float, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "total_volume": self.total_volume,
            "session_count": self.session_count,
            "hvn_levels": list(self.hvn_levels),
            "lvn_levels": list(self.lvn_levels),
            # histogram intentionally omitted — large, only useful for plotting
        }


# ────────────────────────────────────────────────────────────────────
class VolumeProfileBuilder:
    """Stateless volume profile builder.

    Call build_from_bars() with a list of 1-min bars. Returns VolumeProfile.
    """

    def __init__(self,
                 price_resolution: float = 1.0,
                 value_area_pct: float = 0.70,
                 hvn_count: int = 5,
                 lvn_count: int = 5,
                 peak_neighborhood: int = 2):
        """
        Args:
            price_resolution: bucket size in price units. 1.0 means each
                1 NQ point gets its own bucket. Smaller = more precision
                but slower and more noise.
            value_area_pct: fraction of volume to include in the value
                area (standard = 0.70).
            hvn_count: number of HVN peaks to return.
            lvn_count: number of LVN valleys to return.
            peak_neighborhood: how many adjacent buckets must be lower
                (HVN) or higher (LVN) for a bucket to qualify as a local
                extreme. 2 = 5-wide window centered on candidate.
        """
        self.price_resolution = price_resolution
        self.value_area_pct = value_area_pct
        self.hvn_count = hvn_count
        self.lvn_count = lvn_count
        self.peak_neighborhood = peak_neighborhood

    # ── Main entry ─────────────────────────────────────────────────
    def build_from_bars(self, bars: list) -> Optional[VolumeProfile]:
        """Build profile from a list of bars.

        Returns None if input is empty or all bars have zero range.
        """
        if not bars:
            return None

        # 1) Build the histogram: dict[bucket_price → cumulative volume]
        histogram: dict[float, float] = {}
        for b in bars:
            high = float(getattr(b, "high"))
            low = float(getattr(b, "low"))
            volume = float(getattr(b, "volume", 0) or 0)
            if volume <= 0:
                continue
            bar_range = high - low
            if bar_range <= 0:
                # Doji or single-tick bar — assign all volume to that price
                bucket = self._bucket(low)
                histogram[bucket] = histogram.get(bucket, 0) + volume
                continue
            # Distribute volume evenly across the buckets the bar covers
            low_bucket = self._bucket(low)
            high_bucket = self._bucket(high)
            n_buckets = int(round((high_bucket - low_bucket) / self.price_resolution)) + 1
            if n_buckets <= 0:
                continue
            vol_per_bucket = volume / n_buckets
            for k in range(n_buckets):
                bp = round(low_bucket + k * self.price_resolution, 4)
                histogram[bp] = histogram.get(bp, 0) + vol_per_bucket

        if not histogram:
            return None

        # 2) POC = bucket with max volume
        poc = max(histogram.items(), key=lambda kv: kv[1])[0]
        total_volume = sum(histogram.values())

        # 3) Value area = expand outward from POC until we've covered N% of volume
        vah, val = self._compute_value_area(histogram, poc, total_volume)

        # 4) Count sessions covered
        session_count = self._count_sessions(bars)

        # 5) HVN / LVN
        hvn_levels = self._find_hvn(histogram)
        lvn_levels = self._find_lvn(histogram, hvn_levels, vah, val)

        return VolumeProfile(
            poc=poc,
            vah=vah,
            val=val,
            total_volume=total_volume,
            session_count=session_count,
            hvn_levels=hvn_levels,
            lvn_levels=lvn_levels,
            histogram=histogram,
        )

    # ── Helpers ────────────────────────────────────────────────────
    def _bucket(self, price: float) -> float:
        """Round price to nearest bucket boundary."""
        return round(round(price / self.price_resolution) * self.price_resolution, 4)

    def _compute_value_area(self, histogram: dict, poc: float, total_volume: float) -> tuple[float, float]:
        """Expand outward from POC until we've enclosed value_area_pct of total volume.

        Algorithm: keep two pointers (above_poc, below_poc). At each step
        compare the volume at the next bucket up vs the next bucket down,
        add whichever is larger. Stop when accumulated >= target.
        """
        target = total_volume * self.value_area_pct
        sorted_buckets = sorted(histogram.keys())
        if not sorted_buckets:
            return poc, poc

        # Find POC index
        try:
            poc_idx = sorted_buckets.index(poc)
        except ValueError:
            # Shouldn't happen but be defensive
            return poc, poc

        above_idx = poc_idx
        below_idx = poc_idx
        accumulated = histogram[poc]

        while accumulated < target:
            next_above = above_idx + 1 if above_idx + 1 < len(sorted_buckets) else None
            next_below = below_idx - 1 if below_idx - 1 >= 0 else None

            vol_above = histogram[sorted_buckets[next_above]] if next_above is not None else 0
            vol_below = histogram[sorted_buckets[next_below]] if next_below is not None else 0

            if vol_above == 0 and vol_below == 0:
                break

            if vol_above >= vol_below:
                if next_above is None:
                    break
                accumulated += vol_above
                above_idx = next_above
            else:
                if next_below is None:
                    break
                accumulated += vol_below
                below_idx = next_below

        vah = sorted_buckets[above_idx]
        val = sorted_buckets[below_idx]
        return vah, val

    def _find_hvn(self, histogram: dict) -> list[float]:
        """Find local-maximum buckets (HVN candidates).

        Algorithm: walk every bucket; if its volume strictly exceeds
        every neighbor within peak_neighborhood (treating "missing"
        neighbors at the edges as zero), it's a peak. Then sort by
        volume desc and apply a minimum-separation filter so we don't
        return two HVNs adjacent to each other.
        """
        sorted_buckets = sorted(histogram.keys())
        n = len(sorted_buckets)
        if n == 0:
            return []
        if n == 1:
            return [sorted_buckets[0]]

        W = self.peak_neighborhood
        candidates = []
        for i, bp in enumerate(sorted_buckets):
            vol_here = histogram[bp]
            if vol_here <= 0:
                continue
            # Compare to neighbors in window — edges count any missing as zero
            is_peak = True
            for j in range(max(0, i - W), min(n, i + W + 1)):
                if j == i:
                    continue
                if histogram[sorted_buckets[j]] >= vol_here:
                    is_peak = False
                    break
            if is_peak:
                candidates.append((bp, vol_here))

        # Sort by volume desc
        candidates.sort(key=lambda kv: kv[1], reverse=True)

        # Apply min-separation filter: don't return two peaks within
        # peak_neighborhood buckets of each other
        min_sep = W * self.price_resolution
        selected = []
        for bp, _ in candidates:
            if all(abs(bp - s) > min_sep for s in selected):
                selected.append(bp)
            if len(selected) >= self.hvn_count:
                break
        return selected

    def _find_lvn(self, histogram: dict, hvn_levels: list[float],
                  vah: float, val: float) -> list[float]:
        """Find low-volume valley buckets.

        Strategy:
          1. If we have ≥2 HVN peaks, find the minimum-volume bucket
             between each consecutive pair (this is the classic "valley
             between two distributions" LVN).
          2. Otherwise, look for buckets whose volume is < 50% of the
             median volume in the value area.

        Plateaus (flat low-volume zones) are handled by returning the
        MIDDLE of the plateau, not all of it.
        """
        sorted_buckets = sorted(histogram.keys())
        if not sorted_buckets:
            return []

        results = []

        # Strategy 1: valleys between consecutive HVN peaks
        if len(hvn_levels) >= 2:
            sorted_hvns = sorted(hvn_levels)
            for k in range(len(sorted_hvns) - 1):
                lo_peak = sorted_hvns[k]
                hi_peak = sorted_hvns[k + 1]
                # Find the bucket with min volume STRICTLY BETWEEN the peaks
                between_buckets = [bp for bp in sorted_buckets if lo_peak < bp < hi_peak]
                if not between_buckets:
                    continue
                # Find min volume in this range
                min_vol = min(histogram[bp] for bp in between_buckets)
                # Get all buckets at min (handle plateaus) — pick the middle one
                at_min = [bp for bp in between_buckets if histogram[bp] == min_vol]
                if at_min:
                    middle_idx = len(at_min) // 2
                    results.append(at_min[middle_idx])

        # Strategy 2: if we don't have HVN-pair gaps, look for low buckets in VA
        if not results:
            # Compute median volume in the value area
            va_volumes = [histogram[bp] for bp in sorted_buckets if val <= bp <= vah]
            if va_volumes:
                med = sorted(va_volumes)[len(va_volumes) // 2]
                threshold = med * 0.50
                low_buckets = [bp for bp in sorted_buckets
                               if val <= bp <= vah and 0 < histogram[bp] < threshold]
                # Sort by volume asc (lowest first)
                low_buckets.sort(key=lambda bp: histogram[bp])
                results = low_buckets[:self.lvn_count]

        return results[:self.lvn_count]

    def _count_sessions(self, bars: list) -> int:
        """Count unique trading dates in the bar list (in CT)."""
        dates = set()
        for b in bars:
            try:
                dt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
                dates.add(dt.date())
            except (AttributeError, ValueError, TypeError, OSError):
                continue
        return len(dates)


# ────────────────────────────────────────────────────────────────────
# Bar source helpers — load historical bars from logs/history JSONL
# ────────────────────────────────────────────────────────────────────
def load_bars_from_history(
    history_dir: str,
    bot_name: str = "lab",
    days: int = 5,
    timeframe: str = "1m",
) -> list:
    """Load completed bars from logs/history/YYYY-MM-DD_{bot}.jsonl files.

    Each JSONL file contains 'bar' events with shape:
      {"event": "bar", "timeframe": "1m", "ts": "...", "high": ..., ...}

    Returns a list of dict-bars (NOT Bar objects) — caller can build
    Bar objects if needed, or VolumeProfileBuilder works on these too
    (it uses getattr which works on dicts via a small adapter — see _BarLike).
    """
    from datetime import date, timedelta
    bars = []
    today = date.today()
    for delta in range(days):
        target = today - timedelta(days=delta + 1)  # +1 to exclude today
        # Skip weekends
        if target.weekday() >= 5:
            continue
        path = os.path.join(history_dir, f"{target.isoformat()}_{bot_name}.jsonl")
        if not os.path.exists(path):
            logger.debug(f"[VP] missing history file: {path}")
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("event") != "bar":
                        continue
                    if e.get("timeframe") != timeframe:
                        continue
                    # Wrap dict as Bar-like
                    bars.append(_DictBar(e))
        except OSError as ex:
            logger.warning(f"[VP] failed to read {path}: {ex}")
            continue
    logger.info(f"[VP] loaded {len(bars)} {timeframe} bars from history ({days}d lookback)")
    return bars


class _DictBar:
    """Adapter making a dict look like a Bar object for the builder."""
    __slots__ = ("_d",)

    def __init__(self, d: dict):
        self._d = d

    @property
    def high(self): return float(self._d.get("high", 0))
    @property
    def low(self): return float(self._d.get("low", 0))
    @property
    def open(self): return float(self._d.get("open", 0))
    @property
    def close(self): return float(self._d.get("close", 0))
    @property
    def volume(self): return float(self._d.get("volume", 0))
    @property
    def end_time(self):
        ts = self._d.get("ts") or self._d.get("end_time")
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts).timestamp()
            except ValueError:
                return 0.0
        return float(ts or 0)
    @property
    def start_time(self): return self.end_time - 60.0  # 1m default
