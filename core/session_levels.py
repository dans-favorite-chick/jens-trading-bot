"""
Phoenix Bot — Session Levels Helpers

Pure functions supporting the Opening Session strategy family:
  - Standard floor-trader pivot points
  - Opening type classifier (Drive / Test Drive / Auction In / Auction Out)
  - Time window check (CT)
  - Premarket range accessor
  - News blackout check (+/- 5 min of high-impact events)

No bot state, no I/O, no imports from bots/ or strategies/.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Optional, Tuple

# MNQ tick size (points per tick).
_TICK = 0.25

# Opening-type classifier thresholds (MNQ points).
_DRIVE_DISPLACEMENT_POINTS = 15.0          # 60 ticks
_DRIVE_PULLBACK_MAX_FRAC = 0.30            # 30% of displacement
_DRIVE_VOLUME_MULT = 1.4                   # 1.4x avg 5-min volume
_DRIVE_CLOSE_PROXIMITY_TICKS = 8
_DRIVE_CLOSE_PROXIMITY_POINTS = _DRIVE_CLOSE_PROXIMITY_TICKS * _TICK

# News blackout window on either side of a high-impact release.
_NEWS_BLACKOUT_MINUTES = 5


# ─── Pivot points ───────────────────────────────────────────────────
def calc_pivot_points(prior_high: float, prior_low: float, prior_close: float) -> dict:
    """
    Standard floor-trader pivot points from prior session H/L/C.

    Returns dict with keys: pp, r1, r2, s1, s2.
    """
    pp = (prior_high + prior_low + prior_close) / 3.0
    rng = prior_high - prior_low
    r1 = 2 * pp - prior_low
    s1 = 2 * pp - prior_high
    r2 = pp + rng
    s2 = pp - rng
    return {"pp": pp, "r1": r1, "r2": r2, "s1": s1, "s2": s2}


# ─── Opening-type classifier ────────────────────────────────────────
def classify_opening_type(snapshot: dict) -> str:
    """
    Classify the first 5 minutes of RTH into one of:
      OPEN_DRIVE, OPEN_TEST_DRIVE, OPEN_AUCTION_IN, OPEN_AUCTION_OUT, INDETERMINATE

    Rules applied in order — first match wins. Missing required fields
    yield INDETERMINATE rather than raising, so callers upstream (which
    may feed partial snapshots pre-9:00 CT) don't crash.
    """
    required = (
        "rth_open_price", "rth_5min_high", "rth_5min_low", "rth_5min_close",
        "rth_5min_volume", "avg_5min_volume",
        "prior_day_vah", "prior_day_val",
        "prior_day_high", "prior_day_low",
    )
    if any(snapshot.get(k) is None for k in required):
        return "INDETERMINATE"

    rth_open = float(snapshot["rth_open_price"])
    h5 = float(snapshot["rth_5min_high"])
    l5 = float(snapshot["rth_5min_low"])
    c5 = float(snapshot["rth_5min_close"])
    v5 = float(snapshot["rth_5min_volume"])
    avg_v5 = float(snapshot["avg_5min_volume"])
    pd_vah = float(snapshot["prior_day_vah"])
    pd_val = float(snapshot["prior_day_val"])
    pd_high = float(snapshot["prior_day_high"])
    pd_low = float(snapshot["prior_day_low"])

    displacement = abs(c5 - rth_open)

    # --- 1. OPEN_DRIVE ------------------------------------------------
    if displacement > _DRIVE_DISPLACEMENT_POINTS:
        max_pullback = _DRIVE_PULLBACK_MAX_FRAC * displacement
        if c5 > rth_open:
            same_direction = l5 >= (rth_open - max_pullback)
            close_at_extreme = (h5 - c5) <= _DRIVE_CLOSE_PROXIMITY_POINTS
        else:
            same_direction = h5 <= (rth_open + max_pullback)
            close_at_extreme = (c5 - l5) <= _DRIVE_CLOSE_PROXIMITY_POINTS

        volume_ok = avg_v5 > 0 and v5 > _DRIVE_VOLUME_MULT * avg_v5

        if same_direction and volume_ok and close_at_extreme:
            return "OPEN_DRIVE"

    # --- 2. OPEN_TEST_DRIVE ------------------------------------------
    # Poke above prior-day high (or below prior-day low), then close
    # back inside the prior-day range on the opposite side of the open
    # from the tested extreme.
    tested_high = h5 > pd_high
    tested_low = l5 < pd_low
    closed_inside = (pd_low <= c5 <= pd_high)

    if tested_high and closed_inside and c5 < rth_open:
        return "OPEN_TEST_DRIVE"
    if tested_low and closed_inside and c5 > rth_open:
        return "OPEN_TEST_DRIVE"

    # --- 3. OPEN_AUCTION_OUT -----------------------------------------
    if rth_open > pd_high or rth_open < pd_low:
        return "OPEN_AUCTION_OUT"

    # --- 4. OPEN_AUCTION_IN ------------------------------------------
    if pd_val <= rth_open <= pd_vah:
        return "OPEN_AUCTION_IN"

    return "INDETERMINATE"


# ─── Time window check ──────────────────────────────────────────────
def is_in_window(now_ct: datetime, start_str: str, end_str: str) -> bool:
    """
    True if now_ct's time-of-day is within [start_str, end_str] inclusive.
    Strings are 'HH:MM' in CT. Same-day windows only (no wrap past midnight).
    """
    start_t = _parse_hhmm(start_str)
    end_t = _parse_hhmm(end_str)
    t = now_ct.time()
    return start_t <= t <= end_t


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


# ─── Premarket range accessor ───────────────────────────────────────
def get_premarket_range(snapshot: dict) -> Tuple[Optional[float], Optional[float]]:
    """
    Return (pmh, pml) from snapshot. Both None if either is missing.
    """
    pmh = snapshot.get("pmh")
    pml = snapshot.get("pml")
    if pmh is None or pml is None:
        return (None, None)
    return (float(pmh), float(pml))


# ─── News blackout ──────────────────────────────────────────────────
def is_news_blackout(now_ct: datetime, news_calendar: Optional[list] = None) -> bool:
    """
    True if now_ct falls within +/- 5 minutes of any high-impact news event.

    news_calendar: iterable of {"time_ct": datetime, "impact": str}. Only
    entries whose impact is 'high' (case-insensitive) are considered.
    Default (None or empty) = no blackout.
    """
    if not news_calendar:
        return False

    window = timedelta(minutes=_NEWS_BLACKOUT_MINUTES)
    for entry in news_calendar:
        impact = str(entry.get("impact", "")).lower()
        if impact != "high":
            continue
        event_time = entry.get("time_ct")
        if event_time is None:
            continue
        if abs(now_ct - event_time) <= window:
            return True
    return False
