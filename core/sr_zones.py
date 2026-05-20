"""
Support/Resistance Zone Detection
==================================

Pure-Python S/R zone detector that operates on a rolling window of 5m bars.
Composes multiple methodologies into a single ranked zone list:

  1. SWING PIVOTS    — local maxima/minima with N-bar lookback each side
  2. ROUND NUMBERS   — psychological levels at 100/500/1000 point intervals
                       (MNQ trades $24-29K so round-100 levels are reactive)
  3. PRIOR DAY H/L/POC — already-known reaction points
  4. VWAP STD-DEV BANDS — extreme deviations (2.1 sigma+) often reverse

After candidate levels are collected, nearby candidates are CLUSTERED into
zones (within `cluster_ticks` of each other), TOUCHES are counted (how many
times has price come within X ticks then reversed Y ticks?), and a
COMPOSITE STRENGTH score is computed.

API
---
    from core.sr_zones import detect_sr_zones, SRZone

    zones = detect_sr_zones(
        bars_5m=list_of_bars,            # most recent last
        current_price=24500.0,
        lookback_bars=300,               # ~25 hours of 5m bars
        prior_day_high=None,             # optional context
        prior_day_low=None,
        prior_day_poc=None,
        vwap=None, vwap_std=None,
    )
    # → list[SRZone] ranked by strength desc

Zones include all categories. Use SRZone.source to filter.

Tick conventions: MNQ = 0.25 tick / $0.50 per tick / $2 per point.

This module is a pure read-only utility — does not call any Phoenix
production code. Safe to use in backtests + live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


TICK = 0.25
DEFAULT_CLUSTER_TICKS = 8        # 2 points — typical MNQ S/R width
DEFAULT_TOUCH_PROXIMITY = 6      # 1.5 points — counts as "tested"
DEFAULT_REVERSAL_TICKS = 6       # 1.5 points reversal = a real touch
DEFAULT_SWING_LOOKBACK = 10      # N bars each side for pivot detection
DEFAULT_MIN_TOUCHES = 2          # below this = noise


@dataclass
class SRZone:
    """A support or resistance zone."""
    price: float                  # zone midpoint
    type: str                     # "support" or "resistance"
    strength: float               # 0.0-1.0 composite score
    age_bars: int                 # bars since first established
    n_tests: int                  # count of touches (>= 1)
    source: str                   # "swing" | "round" | "pdh" | "pdl" | "poc" |
                                  #   "vwap_band_upper" | "vwap_band_lower"
    width_ticks: int = 0          # zone span (cluster spread)
    last_touch_bars: int = 0      # bars since last test


# ════════════════════════════════════════════════════════════════════
# Sub-detectors
# ════════════════════════════════════════════════════════════════════

def _detect_swing_pivots(bars_5m: list, lookback: int = DEFAULT_SWING_LOOKBACK
                          ) -> list[tuple[float, str, int]]:
    """Return list of (price, "high"|"low", bar_idx) swing pivots.

    A bar at index i is a swing high if its .high is >= every .high in the
    `lookback` bars before AND after it. Symmetric for swing low.

    Note: the last `lookback` bars cannot be confirmed pivots (need future
    bars). They are excluded.
    """
    n = len(bars_5m)
    pivots: list[tuple[float, str, int]] = []
    if n < 2 * lookback + 1:
        return pivots
    for i in range(lookback, n - lookback):
        bar = bars_5m[i]
        h = float(bar.high)
        l = float(bar.low)
        # Check swing high
        is_high = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if float(bars_5m[j].high) > h:
                is_high = False
                break
        if is_high:
            pivots.append((h, "high", i))
            continue
        # Check swing low
        is_low = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if float(bars_5m[j].low) < l:
                is_low = False
                break
        if is_low:
            pivots.append((l, "low", i))
    return pivots


def _round_number_levels(current_price: float, range_points: float = 600.0
                          ) -> list[float]:
    """Return round-100 and round-500 levels within ±range_points of price.

    MNQ trades at $24-29K range; round-100 (e.g. 24500, 24600) are reactive,
    round-500 (24500, 25000, 25500) are stronger psychological anchors.
    """
    levels: list[float] = []
    lo = current_price - range_points
    hi = current_price + range_points
    # Round-100 levels
    start = int(lo // 100) * 100
    end = int(hi // 100) * 100 + 100
    for v in range(start, end + 1, 100):
        if lo <= v <= hi:
            levels.append(float(v))
    return levels


def _cluster_levels(prices: list[float], cluster_ticks: int = DEFAULT_CLUSTER_TICKS
                     ) -> list[tuple[float, int, float, float]]:
    """Cluster nearby prices into zones.

    Returns: list of (centroid, count, lo, hi) where lo/hi are zone bounds.
    """
    if not prices:
        return []
    threshold = cluster_ticks * TICK
    sorted_prices = sorted(prices)
    clusters: list[list[float]] = [[sorted_prices[0]]]
    for p in sorted_prices[1:]:
        if p - clusters[-1][-1] <= threshold:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    out: list[tuple[float, int, float, float]] = []
    for c in clusters:
        centroid = sum(c) / len(c)
        out.append((centroid, len(c), min(c), max(c)))
    return out


def _count_touches(bars_5m: list, level_price: float,
                    proximity_ticks: int = DEFAULT_TOUCH_PROXIMITY,
                    reversal_ticks: int = DEFAULT_REVERSAL_TICKS,
                    side: str = "both") -> tuple[int, int]:
    """Count how many times price came within `proximity_ticks` of `level_price`
    then REVERSED by `reversal_ticks` (a meaningful rejection).

    side="both" counts both touches from above (resistance) and below (support).
    side="support" only counts touches from above (price came down to level then bounced up).
    side="resistance" only counts touches from below.

    Returns: (n_touches, last_touch_bars_ago)
            last_touch_bars_ago = 9999 if no touches
    """
    proximity = proximity_ticks * TICK
    reversal = reversal_ticks * TICK
    n = len(bars_5m)
    touches = 0
    last_touch_idx = -1
    i = 0
    while i < n:
        bar = bars_5m[i]
        # Did this bar come within proximity?
        bar_low = float(bar.low)
        bar_high = float(bar.high)
        # Touched from above (support test): bar dipped to within proximity
        # and closed/recovered higher
        if side in ("support", "both") and bar_low <= level_price + proximity \
                and bar_high >= level_price - proximity:
            # Look forward up to 5 bars for a reversal of `reversal` magnitude
            recovered = False
            highest_after = bar_high
            for j in range(i + 1, min(n, i + 6)):
                highest_after = max(highest_after, float(bars_5m[j].high))
                if highest_after >= level_price + reversal:
                    recovered = True
                    break
            if recovered:
                touches += 1
                last_touch_idx = i
                i += 5  # skip ahead so the same touch isn't double-counted
                continue
        # Touched from below (resistance test)
        if side in ("resistance", "both") and bar_high >= level_price - proximity \
                and bar_low <= level_price + proximity:
            rejected = False
            lowest_after = bar_low
            for j in range(i + 1, min(n, i + 6)):
                lowest_after = min(lowest_after, float(bars_5m[j].low))
                if lowest_after <= level_price - reversal:
                    rejected = True
                    break
            if rejected:
                touches += 1
                last_touch_idx = i
                i += 5
                continue
        i += 1
    last_touch_bars = (n - 1 - last_touch_idx) if last_touch_idx >= 0 else 9999
    return touches, last_touch_bars


def _classify_zone_side(level: float, current_price: float) -> str:
    """Above current price → resistance. Below → support."""
    return "resistance" if level > current_price else "support"


# ════════════════════════════════════════════════════════════════════
# Main detector
# ════════════════════════════════════════════════════════════════════

def detect_sr_zones(
    bars_5m: list,
    current_price: float,
    lookback_bars: int = 300,
    prior_day_high: Optional[float] = None,
    prior_day_low: Optional[float] = None,
    prior_day_poc: Optional[float] = None,
    vwap: Optional[float] = None,
    vwap_std: Optional[float] = None,
    cluster_ticks: int = DEFAULT_CLUSTER_TICKS,
    min_touches: int = DEFAULT_MIN_TOUCHES,
    swing_lookback: int = DEFAULT_SWING_LOOKBACK,
) -> list[SRZone]:
    """Detect S/R zones from bars + context. Returns ranked list by strength.

    Methodology:
      1. Collect candidate levels from: swing pivots (last `lookback_bars`
         of bars_5m), round numbers near current price, prior day H/L/POC,
         VWAP +/- 2.1 sigma bands.
      2. Cluster swing pivots into zones (within `cluster_ticks`).
      3. Count touches for each cluster + each session level.
      4. Compute strength = weighted blend of:
         - touches (more = stronger; capped at 6)
         - recency (recent touches > old touches)
         - source weight (PDH/POC > swing > round)
         - tightness (cluster width inverse)
      5. Filter clusters with < min_touches; session-level zones always
         pass (they have known significance).

    Returns: list[SRZone] sorted by strength desc.
    """
    if not bars_5m:
        return []
    # Trim to lookback window
    if len(bars_5m) > lookback_bars:
        window = bars_5m[-lookback_bars:]
    else:
        window = list(bars_5m)
    n = len(window)

    zones: list[SRZone] = []

    # ── 1. Swing pivots ──────────────────────────────────────────
    swing_pivots = _detect_swing_pivots(window, lookback=swing_lookback)
    if swing_pivots:
        # Group by high vs low separately (don't cluster a high and a low
        # at the same price into one zone — they're different micro-structures)
        high_prices = [p for p, k, _ in swing_pivots if k == "high"]
        low_prices = [p for p, k, _ in swing_pivots if k == "low"]
        for prices, kind in ((high_prices, "high"), (low_prices, "low")):
            clusters = _cluster_levels(prices, cluster_ticks=cluster_ticks)
            for centroid, count, lo, hi in clusters:
                # Count touches against the cluster centroid
                n_touches, last_touch = _count_touches(
                    window, centroid,
                    side=("resistance" if kind == "high" else "support"),
                )
                if n_touches < min_touches:
                    continue
                # Estimate age from earliest pivot in this cluster
                first_idx = min(
                    [idx for p, k, idx in swing_pivots
                     if k == kind and lo <= p <= hi],
                    default=0,
                )
                age = n - 1 - first_idx
                zones.append(SRZone(
                    price=centroid,
                    type=_classify_zone_side(centroid, current_price),
                    strength=0.0,  # filled below
                    age_bars=age,
                    n_tests=n_touches,
                    source="swing",
                    width_ticks=int(round((hi - lo) / TICK)),
                    last_touch_bars=last_touch,
                ))

    # ── 2. Round numbers ────────────────────────────────────────
    for rn in _round_number_levels(current_price, range_points=600.0):
        n_touches, last_touch = _count_touches(window, rn)
        # Round numbers always emitted (psychological); n_tests=0 ok
        zones.append(SRZone(
            price=rn,
            type=_classify_zone_side(rn, current_price),
            strength=0.0,
            age_bars=n,
            n_tests=n_touches,
            source="round",
            width_ticks=4,
            last_touch_bars=last_touch,
        ))

    # ── 3. Prior day levels ─────────────────────────────────────
    for level, source in (
        (prior_day_high, "pdh"),
        (prior_day_low, "pdl"),
        (prior_day_poc, "poc"),
    ):
        if level is None or level <= 0:
            continue
        n_touches, last_touch = _count_touches(window, float(level))
        zones.append(SRZone(
            price=float(level),
            type=_classify_zone_side(float(level), current_price),
            strength=0.0,
            age_bars=n,
            n_tests=n_touches,
            source=source,
            width_ticks=4,
            last_touch_bars=last_touch,
        ))

    # ── 4. VWAP std-dev bands (2.1 sigma) ───────────────────────
    if vwap is not None and vwap_std is not None and vwap_std > 0:
        for mult, name in ((2.1, "vwap_band_upper"), (-2.1, "vwap_band_lower")):
            level = vwap + mult * vwap_std
            n_touches, last_touch = _count_touches(window, level)
            zones.append(SRZone(
                price=level,
                type=_classify_zone_side(level, current_price),
                strength=0.0,
                age_bars=n,
                n_tests=n_touches,
                source=name,
                width_ticks=4,
                last_touch_bars=last_touch,
            ))

    # ── 5. Composite strength scoring ───────────────────────────
    SOURCE_WEIGHTS = {
        "swing": 0.30,
        "round": 0.20,
        "pdh": 0.35,
        "pdl": 0.35,
        "poc": 0.35,
        "vwap_band_upper": 0.25,
        "vwap_band_lower": 0.25,
    }
    for z in zones:
        # touches component (saturates at 6 touches)
        touch_score = min(z.n_tests / 6.0, 1.0)
        # recency: 0 if last touch > 200 bars, 1 if recent (within 20)
        if z.last_touch_bars >= 9999:
            recency_score = 0.0
        else:
            recency_score = max(0.0, 1.0 - (z.last_touch_bars / 200.0))
        # tightness: width_ticks <= 4 → 1.0, > 20 → 0.0
        tightness_score = max(0.0, 1.0 - (z.width_ticks / 20.0))
        source_w = SOURCE_WEIGHTS.get(z.source, 0.2)
        # Composite: weighted blend
        z.strength = round(
            0.40 * touch_score +
            0.25 * recency_score +
            0.15 * tightness_score +
            0.20 * source_w,
            3,
        )

    # Deduplicate: if two zones overlap within cluster_ticks, keep the stronger
    zones.sort(key=lambda z: -z.strength)
    deduped: list[SRZone] = []
    threshold = cluster_ticks * TICK
    for z in zones:
        is_dup = False
        for kept in deduped:
            if abs(z.price - kept.price) <= threshold and z.type == kept.type:
                is_dup = True
                break
        if not is_dup:
            deduped.append(z)

    return deduped


# ════════════════════════════════════════════════════════════════════
# Helper for strategy code: get nearest zone of a given type
# ════════════════════════════════════════════════════════════════════

def nearest_zone(
    zones: list[SRZone],
    price: float,
    zone_type: str,
    max_distance_ticks: int = 12,
) -> Optional[SRZone]:
    """Return the nearest zone of `zone_type` ("support" / "resistance")
    within `max_distance_ticks` of price. None if none qualify."""
    max_dist = max_distance_ticks * TICK
    best: Optional[SRZone] = None
    best_dist = float("inf")
    for z in zones:
        if z.type != zone_type:
            continue
        d = abs(z.price - price)
        if d <= max_dist and d < best_dist:
            best = z
            best_dist = d
    return best
