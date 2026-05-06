"""
Price-action HTF reference levels for confluence scoring.

Sprint I (2026-05-05): replaces MenthorQ levels with structure-derived
levels that don't require external data subscriptions. Built from tick
history and session boundaries already maintained by TickAggregator +
SessionLevelsAggregator.

Provides 4 categories of levels:
  1. Volume Profile (HVN, LVN, POC, VAH, VAL) — institutional accumulation zones
  2. Session levels (prior day H/L/C, ON H/L, IB H/L) — well-known reaction points
  3. VWAP bands (session VWAP ± 1/2/3 stdev) — fair value reference
  4. Swing pivots (recent swing H/L on 5m + 15m) — market structure

DESIGN NOTES (Phoenix-specific):
- Aggregator attribute names verified against core/tick_aggregator.py:
  vwap, vwap_std, vwap_upper1/lower1/upper2/lower2, last_price, atr_5m,
  bars_5m (BarBuilder with .completed deque), bars_15m (same).
- Prior-day fields live on aggregator.session_levels, not the aggregator
  itself: session_levels.prior_day_high/low/close/poc.
- Bars in BarBuilder are dataclass instances (Bar) with .open/.high/.low/.close/.volume.
- Module is purely additive — Sprint J wires strategies to consume it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class LevelTier(Enum):
    """Level importance tier — higher = more confluence weight."""
    TIER_1 = "tier_1"   # Prior day H/L, session POC, weekly H/L — institutional
    TIER_2 = "tier_2"   # HVN, VWAP, ON H/L — strong
    TIER_3 = "tier_3"   # LVN, IB H/L, swing pivots — moderate


@dataclass
class PriceLevel:
    """A single HTF level with metadata for confluence scoring."""
    price: float
    label: str           # "PDH" / "POC" / "HVN_27850" / "VWAP" etc
    tier: LevelTier
    side: str = "both"   # "support" / "resistance" / "both"
    age_bars: int = 0    # bars since formed (for decay)


@dataclass
class PriceActionLevels:
    """All HTF levels for current session — replaces GammaLevels role."""
    # Volume profile
    session_poc: Optional[float] = None
    session_vah: Optional[float] = None
    session_val: Optional[float] = None
    hvn_levels: list[float] = field(default_factory=list)  # High volume nodes
    lvn_levels: list[float] = field(default_factory=list)  # Low volume nodes

    # Session boundaries
    prior_day_high: Optional[float] = None
    prior_day_low: Optional[float] = None
    prior_day_close: Optional[float] = None
    overnight_high: Optional[float] = None
    overnight_low: Optional[float] = None
    initial_balance_high: Optional[float] = None  # First 60 min
    initial_balance_low: Optional[float] = None

    # VWAP bands
    session_vwap: Optional[float] = None
    vwap_upper_1sd: Optional[float] = None
    vwap_lower_1sd: Optional[float] = None
    vwap_upper_2sd: Optional[float] = None
    vwap_lower_2sd: Optional[float] = None

    # Swing pivots (most recent)
    swing_high_5m: Optional[float] = None
    swing_low_5m: Optional[float] = None
    swing_high_15m: Optional[float] = None
    swing_low_15m: Optional[float] = None

    # Computed regime (replaces gamma_regime role)
    structure_bias: str = "NEUTRAL"      # "BULLISH" / "BEARISH" / "NEUTRAL"
    volatility_regime: str = "NORMAL"    # "HIGH" / "NORMAL" / "LOW"

    @property
    def is_stale(self) -> bool:
        """True if no levels populated — caller should treat as no data.

        Used by base_bot to decide whether to skip the structure-bias gate
        when warmup hasn't filled in any reference levels yet.
        """
        return (
            self.prior_day_high is None
            and self.session_poc is None
            and self.session_vwap is None
        )


# ──────────────────────────────────────────────────────────────────
# Builder — constructs PriceActionLevels from a TickAggregator
# ──────────────────────────────────────────────────────────────────

def _bars_from(agg: Any, attr: str) -> list:
    """Extract a list of Bar instances from an aggregator's BarBuilder.

    Phoenix's BarBuilder stores completed bars in a deque (.completed).
    Some test fixtures pass a plain list directly — handle both.
    """
    bars = getattr(agg, attr, None)
    if bars is None:
        return []
    if hasattr(bars, "completed"):
        return list(bars.completed)
    if isinstance(bars, list):
        return bars
    return []


def build_levels_from_aggregator(agg: Any) -> PriceActionLevels:
    """Construct PriceActionLevels from a Phoenix TickAggregator instance.

    Pulls:
      - VWAP + bands from agg.vwap, agg.vwap_std (or pre-computed
        agg.vwap_upper1/2 / vwap_lower1/2 if available)
      - Prior day H/L/C from agg.session_levels.prior_day_*
      - Session POC/VAH/VAL from agg.session_levels.prior_day_poc as a
        seed; recomputes from completed 5m bars when more recent data
        is available
      - Swing pivots from last 20 bars of bars_5m + bars_15m
      - HVN/LVN by binning bar volumes into 0.25-tick buckets and
        finding local maxima/minima
      - structure_bias from price vs VWAP + prior-day H/L
      - volatility_regime from atr_5m vs baseline (atr_5m_baseline if
        present, else falls back to NORMAL)
    """
    levels = PriceActionLevels()

    # ── VWAP block ─────────────────────────────────────────────
    vwap = getattr(agg, "vwap", None)
    levels.session_vwap = vwap if vwap and vwap > 0 else None
    vwap_std = getattr(agg, "vwap_std", None)
    if levels.session_vwap and vwap_std and vwap_std > 0:
        # Prefer pre-computed bands if available (live-state path)
        u1 = getattr(agg, "vwap_upper1", None)
        l1 = getattr(agg, "vwap_lower1", None)
        u2 = getattr(agg, "vwap_upper2", None)
        l2 = getattr(agg, "vwap_lower2", None)
        levels.vwap_upper_1sd = u1 if u1 else levels.session_vwap + vwap_std
        levels.vwap_lower_1sd = l1 if l1 else levels.session_vwap - vwap_std
        levels.vwap_upper_2sd = u2 if u2 else levels.session_vwap + 2 * vwap_std
        levels.vwap_lower_2sd = l2 if l2 else levels.session_vwap - 2 * vwap_std

    # ── Prior day block (lives on agg.session_levels) ──────────
    sl = getattr(agg, "session_levels", None)
    if sl is not None:
        pdh = getattr(sl, "prior_day_high", None)
        pdl = getattr(sl, "prior_day_low", None)
        pdc = getattr(sl, "prior_day_close", None)
        pdp = getattr(sl, "prior_day_poc", None)
        levels.prior_day_high = pdh if pdh and pdh > 0 else None
        levels.prior_day_low = pdl if pdl and pdl > 0 else None
        levels.prior_day_close = pdc if pdc and pdc > 0 else None
        # Use prior-day POC as seed; will be overridden below if we
        # have enough live bars to compute a current-session POC.
        levels.session_poc = pdp if pdp and pdp > 0 else None

    # ── Volume profile from completed 5m bars ──────────────────
    bars_5m = _bars_from(agg, "bars_5m")[-78:]  # ~6.5h RTH at 5m
    if bars_5m:
        live_poc = _compute_poc(bars_5m)
        if live_poc is not None:
            levels.session_poc = live_poc
        vah, val = _compute_value_area(bars_5m)
        levels.session_vah = vah
        levels.session_val = val
        levels.hvn_levels = _compute_hvn_levels(bars_5m, n=3)
        levels.lvn_levels = _compute_lvn_levels(bars_5m, n=3)

    # ── Swing pivots ───────────────────────────────────────────
    levels.swing_high_5m, levels.swing_low_5m = _swing_pivots(
        _bars_from(agg, "bars_5m"), lookback=20,
    )
    levels.swing_high_15m, levels.swing_low_15m = _swing_pivots(
        _bars_from(agg, "bars_15m"), lookback=20,
    )

    # ── Computed regime (replaces gamma_regime role) ───────────
    current_price = getattr(agg, "last_price", None)
    levels.structure_bias = _classify_structure_bias(levels, current_price)
    levels.volatility_regime = _classify_volatility(
        getattr(agg, "atr_5m", None),
        getattr(agg, "atr_5m_baseline", None),
    )

    return levels


# ──────────────────────────────────────────────────────────────────
# Helpers — pure functions, easy to unit test
# ──────────────────────────────────────────────────────────────────

# MNQ tick size — matches config.settings.TICK_SIZE
_TICK = 0.25


def _bucket_price(price: float) -> float:
    """Round price to nearest MNQ tick (0.25)."""
    return round(price * 4) / 4


def _compute_poc(bars: list) -> Optional[float]:
    """Most-traded price across the bars (close × volume histogram).

    Approximation: each bar's volume is attributed to its close price.
    More accurate would be to distribute across the bar's range, but
    Phoenix's downstream consumers (find_nearest_htf_level confluence
    scoring) only care about ~tick-level proximity, so close-bucket is
    fine for the use case.
    """
    if not bars:
        return None
    buckets: dict[float, int] = defaultdict(int)
    for bar in bars:
        price = getattr(bar, "close", None)
        vol = getattr(bar, "volume", 0) or 0
        if price is None or vol <= 0:
            continue
        buckets[_bucket_price(float(price))] += int(vol)
    if not buckets:
        return None
    return max(buckets, key=buckets.get)


def _compute_value_area(
    bars: list, value_area_pct: float = 0.70,
) -> tuple[Optional[float], Optional[float]]:
    """Returns (VAH, VAL) — price range containing `value_area_pct` of volume.

    Standard market-profile convention: 70% value area, expanded outward
    from POC by repeatedly adding the higher-volume side until the
    accumulated volume crosses the target.
    """
    if not bars:
        return None, None
    buckets: dict[float, int] = defaultdict(int)
    for bar in bars:
        price = getattr(bar, "close", None)
        vol = getattr(bar, "volume", 0) or 0
        if price is None or vol <= 0:
            continue
        buckets[_bucket_price(float(price))] += int(vol)
    if not buckets:
        return None, None

    total_volume = sum(buckets.values())
    if total_volume <= 0:
        return None, None
    target = total_volume * value_area_pct

    poc = max(buckets, key=buckets.get)
    accumulated = buckets[poc]
    sorted_prices = sorted(buckets.keys())
    poc_idx = sorted_prices.index(poc)
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target and (lo_idx > 0 or hi_idx < len(sorted_prices) - 1):
        next_lo_vol = buckets[sorted_prices[lo_idx - 1]] if lo_idx > 0 else 0
        next_hi_vol = (
            buckets[sorted_prices[hi_idx + 1]]
            if hi_idx < len(sorted_prices) - 1 else 0
        )
        if next_hi_vol == 0 and next_lo_vol == 0:
            break
        if next_hi_vol >= next_lo_vol and hi_idx < len(sorted_prices) - 1:
            hi_idx += 1
            accumulated += next_hi_vol
        elif lo_idx > 0:
            lo_idx -= 1
            accumulated += next_lo_vol
        else:
            break

    return sorted_prices[hi_idx], sorted_prices[lo_idx]


def _compute_hvn_levels(bars: list, n: int = 3) -> list[float]:
    """Top-n high volume nodes — local maxima in volume histogram.

    A bucket is a HVN if its volume is > both neighbors AND >= 1.5x
    average bucket volume. Returns up to `n` HVNs sorted by descending
    volume.
    """
    if not bars:
        return []
    buckets: dict[float, int] = defaultdict(int)
    for bar in bars:
        price = getattr(bar, "close", None)
        vol = getattr(bar, "volume", 0) or 0
        if price is None or vol <= 0:
            continue
        buckets[_bucket_price(float(price))] += int(vol)
    if len(buckets) < 3:
        return []

    sorted_prices = sorted(buckets.keys())
    avg_vol = sum(buckets.values()) / len(buckets)

    candidates: list[tuple[float, int]] = []
    for i in range(1, len(sorted_prices) - 1):
        vol = buckets[sorted_prices[i]]
        prev_vol = buckets[sorted_prices[i - 1]]
        next_vol = buckets[sorted_prices[i + 1]]
        if vol > prev_vol and vol > next_vol and vol >= avg_vol * 1.5:
            candidates.append((sorted_prices[i], vol))

    candidates.sort(key=lambda x: -x[1])
    return [p for p, _ in candidates[:n]]


def _compute_lvn_levels(bars: list, n: int = 3) -> list[float]:
    """Top-n low volume nodes — local minima in volume histogram.

    A bucket is a LVN if its volume is < both neighbors AND <= 0.5x
    average bucket volume. Returns up to `n` LVNs sorted by ascending
    volume (lowest first).
    """
    if not bars:
        return []
    buckets: dict[float, int] = defaultdict(int)
    for bar in bars:
        price = getattr(bar, "close", None)
        vol = getattr(bar, "volume", 0) or 0
        if price is None or vol <= 0:
            continue
        buckets[_bucket_price(float(price))] += int(vol)
    if len(buckets) < 3:
        return []

    sorted_prices = sorted(buckets.keys())
    avg_vol = sum(buckets.values()) / len(buckets)

    candidates: list[tuple[float, int]] = []
    for i in range(1, len(sorted_prices) - 1):
        vol = buckets[sorted_prices[i]]
        prev_vol = buckets[sorted_prices[i - 1]]
        next_vol = buckets[sorted_prices[i + 1]]
        if vol < prev_vol and vol < next_vol and vol <= avg_vol * 0.5:
            candidates.append((sorted_prices[i], vol))

    candidates.sort(key=lambda x: x[1])
    return [p for p, _ in candidates[:n]]


def _swing_pivots(
    bars: list, lookback: int = 20,
) -> tuple[Optional[float], Optional[float]]:
    """Most recent swing high and swing low across the last `lookback` bars.

    Phoenix-style: simple max(high) / min(low) over the window. More
    sophisticated pivot detection (n-bar pivot confirmation) is left
    for a future enhancement; this is sufficient for confluence scoring.
    """
    if not bars or len(bars) < 5:
        return None, None
    recent = bars[-lookback:]
    try:
        swing_high = max(b.high for b in recent if getattr(b, "high", None) is not None)
        swing_low = min(b.low for b in recent if getattr(b, "low", None) is not None)
    except (ValueError, AttributeError):
        return None, None
    return swing_high, swing_low


def _classify_structure_bias(
    levels: PriceActionLevels, current_price: Optional[float],
) -> str:
    """Classify market bias from price vs key structural levels.

    Replaces gamma_regime role.

      BULLISH: price > VWAP AND price > prior_day_high
      BEARISH: price < VWAP AND price < prior_day_low
      NEUTRAL: anything else (including missing data)

    Conservative — only declares bias when price is clearly above/below
    BOTH a fair-value reference (VWAP) AND a structural reference
    (prior-day extreme). The Sprint J base_bot gate uses this to block
    only obvious counter-trend trades.
    """
    if current_price is None or levels.session_vwap is None:
        return "NEUTRAL"

    above_vwap = current_price > levels.session_vwap
    below_vwap = current_price < levels.session_vwap

    if (
        levels.prior_day_high is not None
        and above_vwap
        and current_price > levels.prior_day_high
    ):
        return "BULLISH"
    if (
        levels.prior_day_low is not None
        and below_vwap
        and current_price < levels.prior_day_low
    ):
        return "BEARISH"
    return "NEUTRAL"


def _classify_volatility(
    current_atr: Optional[float], baseline_atr: Optional[float],
) -> str:
    """Classify volatility regime from ATR vs a rolling baseline.

      HIGH:   atr >= 1.5x baseline
      LOW:    atr <= 0.7x baseline
      NORMAL: anything else (including missing data — defaults safe)

    Used by base_bot's stop-multiplier logic (HIGH widens stops 1.3x).
    """
    if current_atr is None or baseline_atr is None or baseline_atr <= 0:
        return "NORMAL"
    ratio = current_atr / baseline_atr
    if ratio >= 1.5:
        return "HIGH"
    if ratio <= 0.7:
        return "LOW"
    return "NORMAL"


# ──────────────────────────────────────────────────────────────────
# Confluence helpers — used by strategies (Sprint J wires these)
# ──────────────────────────────────────────────────────────────────

def find_nearest_htf_level(
    price: float,
    levels: PriceActionLevels,
    max_distance_ticks: int = 12,
    tick_size: float = 0.25,
) -> Optional[PriceLevel]:
    """Find the nearest HTF level to a given price within max distance.

    Returns a PriceLevel object with tier set, or None if no level is
    in range. Replaces the old MenthorQ "is price at HVL/wall" check
    used by footprint_cvd_reversal's IQS scoring.

    Tie-breaking rule: if multiple levels are within max_distance, the
    closest wins; on equidistant ties, lower-numbered tier (more
    important) wins.
    """
    max_distance = max_distance_ticks * tick_size
    candidates: list[PriceLevel] = []

    # ── Tier 1: institutional levels ──────────────────────────
    if (
        levels.prior_day_high is not None
        and abs(price - levels.prior_day_high) <= max_distance
    ):
        candidates.append(
            PriceLevel(levels.prior_day_high, "PDH", LevelTier.TIER_1, "resistance"),
        )
    if (
        levels.prior_day_low is not None
        and abs(price - levels.prior_day_low) <= max_distance
    ):
        candidates.append(
            PriceLevel(levels.prior_day_low, "PDL", LevelTier.TIER_1, "support"),
        )
    if (
        levels.session_poc is not None
        and abs(price - levels.session_poc) <= max_distance
    ):
        candidates.append(
            PriceLevel(levels.session_poc, "POC", LevelTier.TIER_1, "both"),
        )

    # ── Tier 2: strong structural levels ──────────────────────
    for hvn in levels.hvn_levels:
        if abs(price - hvn) <= max_distance:
            candidates.append(
                PriceLevel(hvn, f"HVN_{hvn:.2f}", LevelTier.TIER_2, "both"),
            )
    if (
        levels.session_vwap is not None
        and abs(price - levels.session_vwap) <= max_distance
    ):
        candidates.append(
            PriceLevel(levels.session_vwap, "VWAP", LevelTier.TIER_2, "both"),
        )
    if (
        levels.overnight_high is not None
        and abs(price - levels.overnight_high) <= max_distance
    ):
        candidates.append(
            PriceLevel(
                levels.overnight_high, "ONH", LevelTier.TIER_2, "resistance",
            ),
        )
    if (
        levels.overnight_low is not None
        and abs(price - levels.overnight_low) <= max_distance
    ):
        candidates.append(
            PriceLevel(levels.overnight_low, "ONL", LevelTier.TIER_2, "support"),
        )

    # ── Tier 3: moderate levels ───────────────────────────────
    for lvn in levels.lvn_levels:
        if abs(price - lvn) <= max_distance:
            candidates.append(
                PriceLevel(lvn, f"LVN_{lvn:.2f}", LevelTier.TIER_3, "both"),
            )

    if not candidates:
        return None

    # Closest wins; tier 1 beats tier 2 beats tier 3 on equidistant ties.
    _tier_rank = {LevelTier.TIER_1: 1, LevelTier.TIER_2: 2, LevelTier.TIER_3: 3}
    candidates.sort(key=lambda lv: (abs(price - lv.price), _tier_rank[lv.tier]))
    return candidates[0]
