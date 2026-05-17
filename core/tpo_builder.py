"""
Phoenix Bot — TPO / Market Profile Builder
===========================================

PHASE D: Time-at-price analysis (vs Volume Profile's volume-at-price).

CORE IDEA
---------
TPO (Time Price Opportunity) was developed by Peter Steidlmayer at the CBOT
in the 1980s. Divide the session into 30-min periods labeled A-Z. For
every price touched during a period, place that letter at that price level.
After the session you have a horizontal histogram of letters showing how
many 30-min periods visited each price.

What emerges:
  POC  — price with most letters (most time spent = fair value)
  VAH  — top of Value Area (70% of letters)
  VAL  — bottom of Value Area
  Single prints — prices touched by ONLY ONE period (rejected inventory)
  Day type — overall shape of the profile (D, P, b, B, trend)

WHY TPO MATTERS (beyond Volume Profile)
---------------------------------------
Volume Profile asks: where did volume happen?
TPO asks: where did the market SPEND TIME?

These are different questions. A price level can have high volume from a
single fast institutional sweep (low TPO count) or low volume from
prolonged but thin chop (high TPO count). The combination tells you
about market structure.

Specifically TPO is best for:
- Day-type classification (D-day, P-day, b-day, B-day, trend day)
- Single-print identification (price levels that often get revisited)
- Initial Balance vs Range Extension (was the IB held or broken?)

USE IN LSR STRATEGY
-------------------
- D-day (balanced): mean-reversion sweeps work — fade extremes back to POC
- P-day (trend up from acceptance): only take LONG sweeps with trend
- b-day (trend down from acceptance): only take SHORT sweeps with trend
- B-day (double distribution): tricky — only trade rotations between the two value areas
- Trend day: SKIP sweep strategy entirely (price keeps running)
- Single print near entry: target it (price wants to revisit)

INPUT
-----
Stream of (price, ts) tuples — every tick of the session, in time order.

OUTPUT — TPOProfile dataclass with day-type, POC, VA, single prints, IB.

DEPENDENCIES
------------
None. Pure stdlib.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

# NQ cash session — 8:30 CT to 15:00 CT = 6.5 hours = 13 periods @ 30min
RTH_OPEN_CT = dtime(8, 30)
RTH_CLOSE_CT = dtime(15, 0)
PERIOD_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ────────────────────────────────────────────────────────────────────
@dataclass
class TPOProfile:
    """Snapshot of the current TPO state."""
    poc: float                                 # price with max TPO count
    vah: float                                 # value area high
    val: float                                 # value area low
    poc_letter_count: int                      # how many letters at POC
    total_letter_count: int                    # total letters in profile
    day_type: str                              # "D", "P", "b", "B", "trend", "neutral"
    single_prints: list[float] = field(default_factory=list)  # prices touched by only 1 letter
    ib_high: Optional[float] = None            # Initial Balance (first 2 periods, A+B)
    ib_low: Optional[float] = None
    ib_extended_high: bool = False             # has B-onwards exceeded A's high?
    ib_extended_low: bool = False
    periods_completed: int = 0
    histogram: dict[float, int] = field(default_factory=dict)  # price → TPO count
    letters_at_price: dict[float, str] = field(default_factory=dict)  # price → letter string

    def to_dict(self) -> dict:
        return {
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "poc_letter_count": self.poc_letter_count,
            "total_letter_count": self.total_letter_count,
            "day_type": self.day_type,
            "single_prints": list(self.single_prints),
            "ib_high": self.ib_high,
            "ib_low": self.ib_low,
            "ib_extended_high": self.ib_extended_high,
            "ib_extended_low": self.ib_extended_low,
            "periods_completed": self.periods_completed,
        }


# ────────────────────────────────────────────────────────────────────
class TPOBuilder:
    """Stateful TPO/Market Profile builder.

    Call add_tick(price, ts) on every tick. Call get_profile() to retrieve
    the current state. Call reset_for_new_session() at session boundaries.

    The builder maintains:
      - which letter (period) is currently active based on tick timestamp
      - for each letter, the set of price buckets it visited
      - aggregated TPO histogram (price → letter count)
    """

    def __init__(self,
                 period_minutes: int = 30,
                 price_tick: float = 0.25,
                 value_area_pct: float = 0.70,
                 session_open_ct: dtime = RTH_OPEN_CT,
                 session_close_ct: dtime = RTH_CLOSE_CT):
        self.period_minutes = period_minutes
        self.price_tick = price_tick
        self.value_area_pct = value_area_pct
        self.session_open_ct = session_open_ct
        self.session_close_ct = session_close_ct

        # Per-period: dict[letter -> set[price_bucket]]
        self._period_prices: dict[str, set[float]] = {}
        self._session_date = None
        self._last_letter: Optional[str] = None
        self._cached_profile: Optional[TPOProfile] = None
        self._cache_dirty = True

    # ── Public API ─────────────────────────────────────────────────
    def add_tick(self, price: float, ts: float) -> None:
        """Process one tick. Drops it silently if outside RTH window."""
        try:
            dt = datetime.fromtimestamp(float(ts), tz=_CT)
        except (OSError, ValueError, TypeError):
            return

        # Auto-reset if date changed
        if self._session_date is None:
            self._session_date = dt.date()
        elif dt.date() != self._session_date:
            self.reset_for_new_session()
            self._session_date = dt.date()

        # Only collect ticks during RTH
        if dt.time() < self.session_open_ct or dt.time() >= self.session_close_ct:
            return

        letter = self._letter_for_time(dt.time())
        if letter is None:
            return

        bucket = self._bucket(price)
        if letter not in self._period_prices:
            self._period_prices[letter] = set()
        if bucket not in self._period_prices[letter]:
            self._period_prices[letter].add(bucket)
            self._cache_dirty = True

        self._last_letter = letter

    def add_bar(self, bar) -> None:
        """Alternative entry — distribute a bar's price range to its period.

        Used when full tick data isn't available — approximates TPO by
        treating each completed bar as having visited every price in its
        range during the period that contains its end_time.
        """
        try:
            ts = float(getattr(bar, "end_time"))
            dt = datetime.fromtimestamp(ts, tz=_CT)
        except (OSError, ValueError, TypeError, AttributeError):
            return

        # Auto-reset on date change
        if self._session_date is None:
            self._session_date = dt.date()
        elif dt.date() != self._session_date:
            self.reset_for_new_session()
            self._session_date = dt.date()

        if dt.time() < self.session_open_ct or dt.time() >= self.session_close_ct:
            return
        letter = self._letter_for_time(dt.time())
        if letter is None:
            return

        high = float(getattr(bar, "high"))
        low = float(getattr(bar, "low"))
        if high < low:
            return
        if letter not in self._period_prices:
            self._period_prices[letter] = set()

        low_bucket = self._bucket(low)
        high_bucket = self._bucket(high)
        bucket = low_bucket
        while bucket <= high_bucket + 1e-9:
            if bucket not in self._period_prices[letter]:
                self._period_prices[letter].add(bucket)
                self._cache_dirty = True
            bucket = round(bucket + self.price_tick, 4)

    def get_profile(self) -> Optional[TPOProfile]:
        """Compute and cache the current profile."""
        if not self._cache_dirty and self._cached_profile is not None:
            return self._cached_profile
        if not self._period_prices:
            self._cached_profile = None
            self._cache_dirty = False
            return None

        # Build histogram: price → letter count
        histogram: dict[float, int] = {}
        letters_at_price: dict[float, str] = {}
        for letter, prices in self._period_prices.items():
            for p in prices:
                histogram[p] = histogram.get(p, 0) + 1
                letters_at_price[p] = letters_at_price.get(p, "") + letter

        total_letters = sum(histogram.values())
        if not histogram or total_letters == 0:
            self._cached_profile = None
            self._cache_dirty = False
            return None

        # POC = max-count bucket
        poc = max(histogram.items(), key=lambda kv: kv[1])[0]
        poc_count = histogram[poc]

        # Value Area
        vah, val = self._compute_value_area(histogram, poc, total_letters)

        # Single prints
        single_prints = sorted([p for p, count in histogram.items() if count == 1])

        # Initial Balance = first two periods (A + B)
        ib_high: Optional[float] = None
        ib_low: Optional[float] = None
        ib_prices: set[float] = set()
        if "A" in self._period_prices:
            ib_prices.update(self._period_prices["A"])
        if "B" in self._period_prices:
            ib_prices.update(self._period_prices["B"])
        if ib_prices:
            ib_high = max(ib_prices)
            ib_low = min(ib_prices)

        # Range extension: has any later letter gone above/below IB?
        ib_extended_high = False
        ib_extended_low = False
        if ib_high is not None and ib_low is not None:
            for letter, prices in self._period_prices.items():
                if letter in ("A", "B"):
                    continue
                if any(p > ib_high for p in prices):
                    ib_extended_high = True
                if any(p < ib_low for p in prices):
                    ib_extended_low = True

        # Day type classification
        day_type = self._classify_day_type(
            histogram, poc, vah, val, total_letters,
            ib_high, ib_low, ib_extended_high, ib_extended_low,
        )

        prof = TPOProfile(
            poc=poc,
            vah=vah,
            val=val,
            poc_letter_count=poc_count,
            total_letter_count=total_letters,
            day_type=day_type,
            single_prints=single_prints,
            ib_high=ib_high,
            ib_low=ib_low,
            ib_extended_high=ib_extended_high,
            ib_extended_low=ib_extended_low,
            periods_completed=len(self._period_prices),
            histogram=histogram,
            letters_at_price=letters_at_price,
        )
        self._cached_profile = prof
        self._cache_dirty = False
        return prof

    def reset_for_new_session(self) -> None:
        self._period_prices.clear()
        self._last_letter = None
        self._cached_profile = None
        self._cache_dirty = True

    # ── Helpers ────────────────────────────────────────────────────
    def _letter_for_time(self, t: dtime) -> Optional[str]:
        """Return the 30-min period letter (A, B, C...) for a time in CT.

        A = 08:30-09:00, B = 09:00-09:30, C = 09:30-10:00, etc.
        Returns None if t is outside the RTH window.
        """
        if t < self.session_open_ct or t >= self.session_close_ct:
            return None
        # Minutes since session open
        open_minutes = self.session_open_ct.hour * 60 + self.session_open_ct.minute
        now_minutes = t.hour * 60 + t.minute
        offset = now_minutes - open_minutes
        period_idx = offset // self.period_minutes
        if 0 <= period_idx < len(PERIOD_LETTERS):
            return PERIOD_LETTERS[period_idx]
        return None

    def _bucket(self, price: float) -> float:
        return round(round(price / self.price_tick) * self.price_tick, 4)

    def _compute_value_area(self, histogram: dict, poc: float, total: int) -> tuple[float, float]:
        target = total * self.value_area_pct
        sorted_buckets = sorted(histogram.keys())
        try:
            poc_idx = sorted_buckets.index(poc)
        except ValueError:
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

        return sorted_buckets[above_idx], sorted_buckets[below_idx]

    def _classify_day_type(self,
                            histogram: dict,
                            poc: float, vah: float, val: float,
                            total_letters: int,
                            ib_high: Optional[float], ib_low: Optional[float],
                            ib_ext_h: bool, ib_ext_l: bool) -> str:
        """Classify the profile shape.

        Heuristics (simplified from classic Market Profile theory):
          - trend: range extension on BOTH sides of IB suppressed; one side
            dominates AND total range > 2× IB range
          - P-day: trend up from acceptance — POC in lower third of range,
            range extends UP only
          - b-day: mirror — POC in upper third, range extends DOWN only
          - B-day: two distinct value areas (bimodal distribution)
          - D-day: balanced bell curve (default for rotational days)
        """
        sorted_buckets = sorted(histogram.keys())
        if not sorted_buckets:
            return "neutral"

        range_high = max(sorted_buckets)
        range_low = min(sorted_buckets)
        full_range = range_high - range_low
        if full_range <= 0:
            return "neutral"

        ib_range = (ib_high - ib_low) if (ib_high is not None and ib_low is not None) else 0
        poc_relative = (poc - range_low) / full_range if full_range > 0 else 0.5

        # TREND day: large extension above OR below IB, not both
        if ib_range > 0:
            range_to_ib_ratio = full_range / max(ib_range, 1e-9)
            if range_to_ib_ratio > 2.0 and (ib_ext_h ^ ib_ext_l):
                return "trend"

        # P-day: range extends UP only, POC in lower portion
        if ib_ext_h and not ib_ext_l and poc_relative <= 0.45:
            return "P"
        # b-day: range extends DOWN only, POC in upper portion
        if ib_ext_l and not ib_ext_h and poc_relative >= 0.55:
            return "b"

        # B-day: bimodal — two peaks with valley between them
        if self._is_bimodal(histogram, sorted_buckets):
            return "B"

        # Default: balanced D-day
        return "D"

    def _is_bimodal(self, histogram: dict, sorted_buckets: list) -> bool:
        """Crude bimodal detector — looks for two peaks with a valley between.

        Returns True if there are two local maxima separated by a valley
        where the valley count is <= 60% of the smaller peak.
        """
        if len(sorted_buckets) < 7:
            return False
        # Find peaks
        peaks = []
        for i in range(2, len(sorted_buckets) - 2):
            bp = sorted_buckets[i]
            c = histogram[bp]
            if (
                c > histogram[sorted_buckets[i - 1]]
                and c > histogram[sorted_buckets[i - 2]]
                and c > histogram[sorted_buckets[i + 1]]
                and c > histogram[sorted_buckets[i + 2]]
            ):
                peaks.append((bp, c))
        if len(peaks) < 2:
            return False
        # Take top two peaks
        peaks.sort(key=lambda p: p[1], reverse=True)
        p1_price, p1_count = peaks[0]
        p2_price, p2_count = peaks[1]
        # Check that there's a valley between them
        lo = min(p1_price, p2_price)
        hi = max(p1_price, p2_price)
        between = [histogram[bp] for bp in sorted_buckets if lo < bp < hi]
        if not between:
            return False
        valley_min = min(between)
        smaller_peak = min(p1_count, p2_count)
        if smaller_peak == 0:
            return False
        return (valley_min / smaller_peak) <= 0.60
