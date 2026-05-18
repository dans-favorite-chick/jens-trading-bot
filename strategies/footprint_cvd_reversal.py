"""
Phoenix — Footprint + CVD Reversal Strategy with IQS Scoring

Sprint H v3 — institutional 4-confluence reversal at MenthorQ HTF
levels. Operates on a 1,500-tick volumetric stream from NT8 (Order
Flow+ data emitted by TickStreamer.cs and persisted to disk by
bridge_server.py:_handle_volumetric_bar).

CONFLUENCES (each contributes 0-25 pts to the Institutional Quality
Score, IQS):

  1. HTF LEVEL CONFLUENCE
     - 25 pts: price within buffer of MenthorQ Put Support / 0DTE /
       Call Resistance / 0DTE / HVL / HVL 0DTE
     - 15 pts: VP POC from prior session (lower-quality fallback)
     -  0 pts: outside buffer of any level (no signal possible)

  2. CVD DIVERGENCE (multi-bar + single-bar)
     - 15 pts base on multi-bar regular divergence over lookback
     - + up to 10 pts weighted by divergence magnitude
     - + 5 pts bonus on single-bar delta div in latest bar

  3. FOOTPRINT CONFIRMATION
     - 15 pts: stacked imbalance same direction as intended trade
     - 15 pts: absorption (heavy delta against trade dir, tiny range)
     - + 5 pts bonus: oversized imbalance (max_ratio >= 10)
     - capped at 25 total

  4. CVD COMPRESSION (5 sub-dimensions x 5 pts each)
     - Delta magnitude shrinking (last 3 bars < 0.6x 20-bar baseline)
     - Bar range shrinking (last 3 bars < 0.6x baseline)
     - Volume holding/elevated (last 3 bars >= 0.8x baseline) -- KEY
       check that distinguishes absorption from dead market
     - Effort/result spike (last bar's |delta|/range > 1.5x baseline)
     - Single-bar delta divergence on last bar

INSTITUTIONAL QUALITY SCORE (IQS) = sum, capped at 100.

ENTRY: IQS >= entry_threshold_iqs (default 70).
TIER (metadata['tier']):
  - A++ : IQS >= 90  (all 4 confluences strong)
  - A   : IQS >= 80
  - B   : IQS >= 70
  - C   : IQS >= 60  (logged for tuning visibility, doesn't fire)

EXITS:
  Stop:    bar low/high +/- buffer ticks, capped at max_stop_ticks (60)
  Target:  +2R (T1 50% scale-out at +1R handled by base_bot)
  Time:    20 volumetric bars (managed exit)

GATES (hard, all must pass before scoring runs):
  - Lunch block: 10:00-13:29 CT
  - Session boundary: skip first/last 5 min
  - Data freshness: data/volumetric_latest.json < 90s old
  - Warmup: >= 25 bars in volumetric_history.jsonl
  - Regime gate: block LONG in NEGATIVE_STRONG, SHORT in POSITIVE_STRONG

PHOENIX INTEGRATION
-------------------
- BaseStrategy subclass with name="footprint_cvd_reversal"
- Uses real Signal constructor (8 required fields + atr_stop_override
  since we compute the stop from structural bar levels, not ATR)
- HTF levels: 2026-05-06 Sprint J rewired to use core/price_action_levels
  (Sprint I infrastructure). _build_pa_levels_from_market() reads
  prior_day_high/low/close, prior_day_poc, vwap, hvn_levels, lvn_levels
  from the market dict; find_nearest_htf_level() does tier-weighted
  scoring (TIER_1=25 / TIER_2=18 / TIER_3=12).
- Volumetric bars come from disk (a separate stream from NT8's
  tick aggregator), so bars_5m/bars_1m args from BaseStrategy.evaluate
  are unused.

DEPENDS ON
----------
- bridge_server.py:_handle_volumetric_bar (Phase 2 — shipped)
- TickStreamer.cs volumetric emitter (operator-side, NOT shipped) —
  strategy logs DATA_NOT_AVAILABLE and stays dormant until data flows.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)
_CT = ZoneInfo("America/Chicago")

# Tests monkeypatch _DATA_ROOT via setattr; production callers leave
# it pointing at the project root so the strategy reads the same files
# bridge_server.py writes.
_DATA_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────
@dataclass
class FootprintCVDConfig:
    enabled: bool = True
    validated: bool = False  # SHIPS UNVALIDATED — lab only

    # Level confluence
    level_buffer_ticks: int = 8
    require_menthorq_level: bool = True

    # CVD divergence
    divergence_lookback_bars: int = 10

    # Footprint
    oversized_imbalance_ratio: float = 10.0
    absorption_min_delta: float = 50.0
    absorption_max_range_ticks: float = 10.0

    # Compression (the v2 addition that addresses the
    # operator's "shrinking CVD bars before the shift" pattern)
    compression_lookback_bars: int = 3
    compression_baseline_bars: int = 20
    compression_size_threshold: float = 0.6
    compression_volume_floor: float = 0.8
    compression_effort_threshold: float = 1.5

    # Entry
    entry_threshold_iqs: int = 70

    # Stops / targets
    stop_buffer_ticks: int = 4
    max_stop_ticks: int = 60
    target_t1_rr: float = 1.0
    target_t2_rr: float = 2.0
    scale_out_pct: float = 0.5
    time_stop_bars: int = 20

    # Gates (use lists in JSON config; constructed back to tuples)
    lunch_block_start_ct: tuple = (10, 0)
    lunch_block_end_ct: tuple = (13, 29)
    session_open_ct: tuple = (8, 30)
    session_close_ct: tuple = (15, 0)
    session_open_skip_min: int = 5
    session_close_skip_min: int = 5
    block_negative_strong_long: bool = True
    block_positive_strong_short: bool = True
    data_freshness_sec: int = 90
    min_history_bars: int = 25


def _ensure_tuple(v: Any) -> tuple:
    """JSON config can't hold tuples — convert lists back."""
    if isinstance(v, tuple):
        return v
    if isinstance(v, list):
        return tuple(v)
    return v


# ──────────────────────────────────────────────────────────────────
# Volumetric data loaders
# ──────────────────────────────────────────────────────────────────
def _load_volumetric_latest(root: Path | None = None) -> Optional[dict]:
    """Read the most-recent volumetric bar bridge_server wrote."""
    base = Path(root) if root is not None else _DATA_ROOT
    f = base / "data" / "volumetric_latest.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[FOOTPRINT_CVD] failed to read volumetric_latest: {e}")
        return None


def _load_volumetric_history(n_bars: int, root: Path | None = None) -> list[dict]:
    """Read the last n_bars from volumetric_history.jsonl."""
    base = Path(root) if root is not None else _DATA_ROOT
    f = base / "logs" / "volumetric_history.jsonl"
    if not f.exists():
        return []
    try:
        with f.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        return [json.loads(line) for line in lines[-n_bars:] if line.strip()]
    except Exception as e:
        logger.warning(f"[FOOTPRINT_CVD] failed to read volumetric_history: {e}")
        return []


# ──────────────────────────────────────────────────────────────────
# Hard gates
# ──────────────────────────────────────────────────────────────────
def _is_lunch_block(now_ct: datetime, cfg: FootprintCVDConfig) -> bool:
    sh, sm = _ensure_tuple(cfg.lunch_block_start_ct)
    eh, em = _ensure_tuple(cfg.lunch_block_end_ct)
    start = now_ct.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now_ct.replace(hour=eh, minute=em, second=59, microsecond=999999)
    return start <= now_ct <= end


def _is_session_boundary(now_ct: datetime, cfg: FootprintCVDConfig) -> bool:
    oh, om = _ensure_tuple(cfg.session_open_ct)
    ch, cm = _ensure_tuple(cfg.session_close_ct)
    open_t = now_ct.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_t = now_ct.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return (now_ct < open_t + timedelta(minutes=cfg.session_open_skip_min)
            or now_ct > close_t - timedelta(minutes=cfg.session_close_skip_min))


# ──────────────────────────────────────────────────────────────────
# Confluence 1: HTF level
#
# Sprint J (2026-05-05): rewired from MenthorQ GammaLevels attributes
# to Sprint I's price_action_levels infrastructure.
#   TIER_1 (PDH/PDL/POC):    25 pts  (institutional)
#   TIER_2 (VWAP/HVN/ON H/L): 18 pts  (strong structural)
#   TIER_3 (LVN):            12 pts  (moderate)
# ──────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> Optional[float]:
    """Coerce to positive float or return None (treats 0/None/junk as 'unset')."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _build_pa_levels_from_market(market: dict):
    """Adapter — construct a PriceActionLevels from a Phoenix market
    snapshot dict so find_nearest_htf_level can score confluence.

    Reads the structural fields Phoenix already populates in the market
    dict during evaluation (vwap, prior_day_*, etc). HVN/LVN are read
    if upstream populated them; otherwise empty (graceful).
    """
    from core.price_action_levels import PriceActionLevels
    return PriceActionLevels(
        prior_day_high=_safe_float(market.get("prior_day_high")),
        prior_day_low=_safe_float(market.get("prior_day_low")),
        prior_day_close=_safe_float(market.get("prior_day_close")),
        session_poc=(_safe_float(market.get("session_poc"))
                     or _safe_float(market.get("prior_day_poc"))),
        session_vwap=_safe_float(market.get("vwap")),
        hvn_levels=[float(v) for v in (market.get("hvn_levels") or [])
                    if isinstance(v, (int, float)) and v > 0],
        lvn_levels=[float(v) for v in (market.get("lvn_levels") or [])
                    if isinstance(v, (int, float)) and v > 0],
    )


def _count_extra_htf_confluences(
    price: float,
    market: dict,
    buffer_ticks: int,
    tick_size: float,
) -> tuple[int, list[str]]:
    """Count HTF levels within buffer of price, beyond the nearest one.

    The research consensus: a 50-quality pattern at a 3-level confluence
    beats a 75-quality pattern at no confluence. Pure additive scoring
    can't capture this because levels are categorical multipliers, not
    continuous additions. _score_htf_level returns the score of the
    NEAREST level only; this function reports how many ADDITIONAL
    levels also cluster nearby — used to multiply the non-level score
    components.

    Returns (extra_count, label_list). Deduplicates levels within ~1
    tick (tagged the "same node") so PDH and PDC at the same price
    don't count as 2.

    For MNQ 0.25 tick size and buffer_ticks=8, this captures all
    levels within 2 points of price.
    """
    levels = _build_pa_levels_from_market(market)
    threshold = buffer_ticks * tick_size
    candidates: list[tuple[float, str]] = []

    for attr, label in (
        ("prior_day_high", "PDH"),
        ("prior_day_low", "PDL"),
        ("prior_day_close", "PDC"),
        ("session_poc", "POC"),
        ("session_vwap", "VWAP"),
    ):
        v = getattr(levels, attr, None)
        if v is not None and abs(v - price) <= threshold:
            candidates.append((float(v), label))

    for v in (levels.hvn_levels or []):
        if abs(v - price) <= threshold:
            candidates.append((float(v), "HVN"))
    for v in (levels.lvn_levels or []):
        if abs(v - price) <= threshold:
            candidates.append((float(v), "LVN"))

    # Dedup: levels within 1 tick of each other are "the same node"
    unique: list[tuple[float, str]] = []
    for v, lbl in candidates:
        if not any(abs(v - u_v) < tick_size for u_v, _ in unique):
            unique.append((v, lbl))

    # _score_htf_level uses the NEAREST level — so "extra" = total - 1
    extra = max(0, len(unique) - 1)
    labels = [lbl for _, lbl in unique]
    return extra, labels


def _score_htf_level(
    price: float,
    market: dict,
    buffer_ticks: int,
    tick_size: float,
    direction: str,
) -> tuple[int, str]:
    """Score 0-25 + level name.

    Sprint J: pulls from Phoenix's market snapshot via
    `_build_pa_levels_from_market` and scores via Sprint I's
    `find_nearest_htf_level` with tier-weighted points.

    Direction is currently unused — the score uses tier rank only.
    Future enhancement could weight LONG higher when nearest level is
    on the support side, etc., but the current Phoenix scoring logic
    treats all tier-1 levels equivalently.
    """
    from core.price_action_levels import LevelTier, find_nearest_htf_level

    levels = _build_pa_levels_from_market(market)
    nearest = find_nearest_htf_level(
        price, levels,
        max_distance_ticks=buffer_ticks,
        tick_size=tick_size,
    )
    if nearest is None:
        return 0, ""

    tier_points = {
        LevelTier.TIER_1: 25,
        LevelTier.TIER_2: 18,
        LevelTier.TIER_3: 12,
    }
    return tier_points.get(nearest.tier, 0), nearest.label


# ──────────────────────────────────────────────────────────────────
# Confluence 2: CVD divergence
# ──────────────────────────────────────────────────────────────────
def _score_cvd_divergence(
    bars: list[dict], lookback: int, direction: str, latest_bar: dict,
) -> tuple[int, dict]:
    """Multi-bar regular divergence + single-bar delta divergence."""
    debug = {"divergence_present": False, "single_bar_div": False,
             "magnitude": 0.0}
    if len(bars) < lookback * 2:
        return 0, debug

    recent = bars[-lookback:]
    prior = bars[-(lookback * 2):-lookback]

    if direction == "long":
        recent_low_p = min(b["low"] for b in recent)
        prior_low_p = min(b["low"] for b in prior)
        recent_low_cvd = min(b["cvd_session"] for b in recent)
        prior_low_cvd = min(b["cvd_session"] for b in prior)
        regular_div = (recent_low_p < prior_low_p
                       and recent_low_cvd > prior_low_cvd)
        if regular_div:
            cvd_range = max(abs(b["cvd_session"]) for b in bars[-lookback*2:])
            magnitude = (recent_low_cvd - prior_low_cvd) / max(cvd_range, 1)
            debug["divergence_present"] = True
            debug["magnitude"] = magnitude
    else:
        recent_high_p = max(b["high"] for b in recent)
        prior_high_p = max(b["high"] for b in prior)
        recent_high_cvd = max(b["cvd_session"] for b in recent)
        prior_high_cvd = max(b["cvd_session"] for b in prior)
        regular_div = (recent_high_p > prior_high_p
                       and recent_high_cvd < prior_high_cvd)
        if regular_div:
            cvd_range = max(abs(b["cvd_session"]) for b in bars[-lookback*2:])
            magnitude = (prior_high_cvd - recent_high_cvd) / max(cvd_range, 1)
            debug["divergence_present"] = True
            debug["magnitude"] = magnitude

    base_score = 0
    if debug["divergence_present"]:
        base_score = 15 + min(10, int(debug["magnitude"] * 50))

    bar_close = latest_bar.get("close", 0)
    bar_open = latest_bar.get("open", 0)
    bar_delta = latest_bar.get("delta", 0)
    if direction == "long":
        if bar_close < bar_open and bar_delta > 0:
            debug["single_bar_div"] = True
            base_score = min(25, base_score + 5)
    else:
        if bar_close > bar_open and bar_delta < 0:
            debug["single_bar_div"] = True
            base_score = min(25, base_score + 5)

    return base_score, debug


# ──────────────────────────────────────────────────────────────────
# Confluence 3: Footprint absorption / stacked / oversized
#
# 2026-05-08 v2 (Sprint L): graduated scoring using the per-level
# `imbalances[]` array TickStreamer emits but the v1 scoring ignored.
# v1 produced a sparse {0, 5, 15, 20, 25} distribution that combined
# with mid-tier scores from other confluences produced IQS clustering
# at 40-55 (well below the 70 threshold). Audit of 7 days of logs showed
# only ONE evaluation reaching IQS=75 (5 pts above threshold) — the
# scoring was the binding constraint, not the gating threshold.
#
# v2 walks the imbalances array, weights by stacked count, location at
# bar extreme, and ratio strength. Replaces the binary stacked AND
# binary absorption (each 0/15) with a continuous 0-25 distribution.
# Also relaxes absorption from `delta AND range` to a weighted score
# so a bar with extreme delta + tight range scores partial credit even
# if one threshold is borderline.
# ──────────────────────────────────────────────────────────────────
def _score_imbalances(
    latest: dict, direction: str, cfg: FootprintCVDConfig, tick_size: float,
) -> tuple[int, dict]:
    """Walk the per-level imbalances array.

    The volumetric_bar message from TickStreamer.cs includes:
      imbalances: [{price, bid_vol, ask_vol, ratio, side}, ...]

    Sides: "buy" = ask_vol/bid_vol_below ≥ ratio_threshold (institutional
    buying pressure stacked on the offer); "sell" = inverse.

    Scoring (capped at 18 for the imbalance sub-component):
      base 0
      + 4 per same-side imbalance, max 12 (so 1=4, 2=8, 3+=12)
      + 4 if any imbalance is at the bar extreme (within 1 tick of high/low)
      + 4 if max_ratio >= oversized_imbalance_ratio (10x default)
      + 2 if 4+ same-side imbalances stacked (very rare, deserves bump)
    Cap 18 — the absorption sub-component contributes the remaining 7.
    """
    debug = {
        "imbalance_count": 0,
        "at_extreme": False,
        "oversized": False,
        "stacked_4plus": False,
    }
    score = 0

    imbalances = latest.get("imbalances") or []
    target_side = "buy" if direction == "long" else "sell"

    same_side = [
        ib for ib in imbalances
        if isinstance(ib, dict) and str(ib.get("side", "")).lower() == target_side
    ]
    debug["imbalance_count"] = len(same_side)

    # Tier 1: count of same-side imbalances (0/4/8/12)
    if same_side:
        score += min(12, 4 * len(same_side))

    # Tier 2: at bar extreme (the institutional "trap zone")
    if same_side:
        bar_low = latest.get("low")
        bar_high = latest.get("high")
        if bar_low is not None and bar_high is not None:
            extreme_threshold = tick_size * 1.0  # within 1 tick of extreme
            for ib in same_side:
                price = ib.get("price")
                if price is None:
                    continue
                # LONG reversal: imbalance at LOW = bid stacking absorption
                # SHORT reversal: imbalance at HIGH = offer stacking
                target_extreme = bar_low if direction == "long" else bar_high
                if abs(price - target_extreme) <= extreme_threshold:
                    debug["at_extreme"] = True
                    score += 4
                    break

    # Tier 3: oversized ratio (a single huge imbalance regardless of stack)
    if latest.get("max_imbalance_ratio", 0) >= cfg.oversized_imbalance_ratio:
        debug["oversized"] = True
        score += 4

    # Tier 4: deep stacked (4+ on same side)
    if len(same_side) >= 4:
        debug["stacked_4plus"] = True
        score += 2

    return min(18, score), debug


def _score_absorption_graduated(
    latest: dict, direction: str, cfg: FootprintCVDConfig, tick_size: float,
) -> tuple[int, dict]:
    """Graduated absorption score (0-7).

    v1 was binary 0/15 with hard AND on `abs(delta) > 50 AND range < 10t`.
    v2 awards partial credit so a bar with extreme delta but borderline
    range (or vice versa) still scores. Absorption is "high effort,
    little progress" — so the right metric is delta-per-tick-of-range.

    For LONG (we want bid absorption — sellers swinging, price holds):
      - Need delta < 0 (sellers active)
      - Score by |delta| / max(range_ticks, 1) ratio

    For SHORT (offer absorption — buyers swinging, price holds):
      - Need delta > 0 (buyers active)
      - Same metric

    Score:
        |delta| / range_ticks ratio:
          < 5:    0 pts (no absorption signal)
          5-10:   3 pts
          10-20:  5 pts
          >= 20:  7 pts (textbook absorption — extreme effort, tight range)
    """
    debug = {"absorption_ratio": 0.0, "absorption_tier": 0}

    delta = latest.get("delta", 0)
    high = latest.get("high")
    low = latest.get("low")
    if high is None or low is None:
        return 0, debug

    # Direction filter — only count absorption when delta opposes the trade
    if direction == "long" and delta >= 0:
        return 0, debug
    if direction == "short" and delta <= 0:
        return 0, debug

    range_ticks = max(1.0, (high - low) / tick_size)
    abs_ratio = abs(delta) / range_ticks
    debug["absorption_ratio"] = round(abs_ratio, 2)

    # Also require a minimum |delta| floor to avoid scoring tiny-delta /
    # tiny-range bars as "absorption" (they're just no-volume bars).
    if abs(delta) < cfg.absorption_min_delta * 0.5:  # half the v1 floor
        return 0, debug

    if abs_ratio >= 20:
        debug["absorption_tier"] = 3
        return 7, debug
    if abs_ratio >= 10:
        debug["absorption_tier"] = 2
        return 5, debug
    if abs_ratio >= 5:
        debug["absorption_tier"] = 1
        return 3, debug
    return 0, debug


def _score_footprint(
    latest: dict, direction: str, cfg: FootprintCVDConfig, tick_size: float,
) -> tuple[int, dict]:
    """Combined footprint score: imbalances (0-18) + absorption (0-7), cap 25.

    v2 uses the per-level imbalances array (was unused in v1) and a
    graduated absorption metric (was binary AND-gate in v1). Total
    distribution moves from sparse {0, 5, 15, 20, 25} to continuous 0-25.
    Backwards-compatibility: when imbalances array is absent (legacy
    bars or older TickStreamer), falls back to v1 stacked_buy/sell flags.
    """
    debug = {"stacked": False, "absorption": False, "oversized": False}
    score = 0

    # New per-level imbalance scoring (consumes imbalances[] array)
    imb_score, imb_debug = _score_imbalances(latest, direction, cfg, tick_size)
    debug.update(imb_debug)

    # Fallback: if imbalances array missing, use v1 binary flags as a
    # bridge while older bars/replays don't yet carry the array. The
    # imb_debug.oversized check still adds +4 since max_imbalance_ratio
    # is independent of the array — so this just ADDS the stacked points.
    if not latest.get("imbalances"):
        if direction == "long" and latest.get("stacked_buy", False):
            debug["stacked"] = True
            imb_score += 12  # equivalent to 3 same-side imbalances in v2
        elif direction == "short" and latest.get("stacked_sell", False):
            debug["stacked"] = True
            imb_score += 12
        imb_score = min(18, imb_score)  # respect imbalance sub-cap

    # Mark legacy "stacked" debug flag on for back-compat with downstream
    # confluences list / dashboards that key off it.
    if imb_debug.get("imbalance_count", 0) >= 3:
        debug["stacked"] = True
    if imb_debug.get("oversized"):
        debug["oversized"] = True
    score += imb_score

    # Graduated absorption (replaces the binary AND-conditioned v1 check)
    abs_score, abs_debug = _score_absorption_graduated(
        latest, direction, cfg, tick_size,
    )
    debug.update(abs_debug)
    if abs_score > 0:
        debug["absorption"] = True
    score += abs_score

    return min(25, score), debug


# ──────────────────────────────────────────────────────────────────
# Confluence 4: CVD compression (the v2 addition)
# ──────────────────────────────────────────────────────────────────
def _score_compression(
    bars: list[dict], cfg: FootprintCVDConfig, tick_size: float,
) -> tuple[int, dict]:
    """5 sub-dimensions x 5 pts. The volume-holding check is the key
    discriminator between absorption (low delta + low range + normal
    volume) and dead market (low everything)."""
    debug = {
        "delta_compression": False, "range_compression": False,
        "volume_holding": False, "effort_spike": False,
        "single_bar_div": False,
        "delta_compression_ratio": 0.0, "range_compression_ratio": 0.0,
        "volume_ratio": 0.0, "effort_ratio": 0.0,
    }
    score = 0

    needed = cfg.compression_baseline_bars + cfg.compression_lookback_bars
    if len(bars) < needed:
        return 0, debug

    recent = bars[-cfg.compression_lookback_bars:]
    baseline = bars[-needed:-cfg.compression_lookback_bars]

    # a) Delta magnitude shrinking
    recent_delta_mag = sum(abs(b["delta"]) for b in recent) / len(recent)
    baseline_delta_mag = sum(abs(b["delta"]) for b in baseline) / max(len(baseline), 1)
    if baseline_delta_mag > 0:
        ratio_a = recent_delta_mag / baseline_delta_mag
        debug["delta_compression_ratio"] = round(ratio_a, 2)
        if ratio_a < cfg.compression_size_threshold:
            debug["delta_compression"] = True
            score += 5

    # b) Bar range shrinking
    recent_range = sum((b["high"] - b["low"]) for b in recent) / len(recent)
    baseline_range = sum((b["high"] - b["low"]) for b in baseline) / max(len(baseline), 1)
    if baseline_range > 0:
        ratio_b = recent_range / baseline_range
        debug["range_compression_ratio"] = round(ratio_b, 2)
        if ratio_b < cfg.compression_size_threshold:
            debug["range_compression"] = True
            score += 5

    # c) Volume holding (KEY check)
    recent_vol = sum(b["total_volume"] for b in recent) / len(recent)
    baseline_vol = sum(b["total_volume"] for b in baseline) / max(len(baseline), 1)
    if baseline_vol > 0:
        ratio_c = recent_vol / baseline_vol
        debug["volume_ratio"] = round(ratio_c, 2)
        if ratio_c >= cfg.compression_volume_floor:
            debug["volume_holding"] = True
            score += 5

    # d) Effort/result spike on last bar
    last = recent[-1]
    last_range_ticks = max((last["high"] - last["low"]) / tick_size, 0.5)
    last_effort = abs(last["delta"]) / last_range_ticks
    baseline_effort = sum(
        abs(b["delta"]) / max((b["high"] - b["low"]) / tick_size, 0.5)
        for b in baseline
    ) / max(len(baseline), 1)
    if baseline_effort > 0:
        ratio_d = last_effort / baseline_effort
        debug["effort_ratio"] = round(ratio_d, 2)
        if ratio_d > cfg.compression_effort_threshold:
            debug["effort_spike"] = True
            score += 5

    # e) Single-bar delta divergence on last bar
    last_close = last.get("close", 0)
    last_open = last.get("open", 0)
    last_delta = last.get("delta", 0)
    if (last_close > last_open and last_delta < 0) or \
       (last_close < last_open and last_delta > 0):
        debug["single_bar_div"] = True
        score += 5

    return score, debug


# ──────────────────────────────────────────────────────────────────
# Tier classification
# ──────────────────────────────────────────────────────────────────
def _classify_tier(iqs: int) -> str:
    if iqs >= 90: return "A++"
    if iqs >= 80: return "A"
    if iqs >= 70: return "B"
    if iqs >= 60: return "C"
    return "REJECTED"


# ──────────────────────────────────────────────────────────────────
# Sprint K1 — TAPE-READING PRO PATTERNS
#
# Layered onto the base 4-confluence IQS as bonus points (cap +20).
# These are professional tape-reading patterns that help marginal
# setups cross the 70 threshold when institutional context is strong:
#   - Finished auction at extreme: +10
#   - Trapped traders confirmed: +10
# Bonuses cap at +20 total; final IQS cap stays at 100.
# ──────────────────────────────────────────────────────────────────

def _detect_finished_auction(
    latest: dict,
    bars_history: list[dict],
    direction: str,
    lookback: int = 5,
) -> tuple[bool, str]:
    """Finished auction = extreme tested, volume diminishing, no new high/low.

    For a LONG (buying exhaustion at HIGH extreme — we'd want to SHORT):
      - Recent bars tested or exceeded prior extreme
      - Latest bar's high is at or below recent peak (no new high)
      - Latest bar's volume is < recent average (auction "winding down")

    For a SHORT (selling exhaustion at LOW extreme — we'd want to LONG):
      - Recent bars tested or breached prior low
      - Latest bar's low is at or above recent trough (no new low)
      - Latest bar's volume is < recent average

    Note: direction here = the trade direction we're considering.
    A LONG signal benefits from a finished-auction at the LOW (selling
    exhausted), and vice versa for SHORT.

    Returns (is_finished, reason).
    """
    if len(bars_history) < lookback:
        return False, ""
    recent = bars_history[-lookback:]
    last = latest

    last_volume = last.get("total_volume", 0)
    avg_recent_volume = sum(b.get("total_volume", 0) for b in recent) / lookback
    if avg_recent_volume <= 0:
        return False, ""

    volume_diminished = last_volume < avg_recent_volume * 0.7

    if direction == "long":
        # Selling exhausted at lows — look for "no new low" on the latest
        recent_low = min(b.get("low", float("inf")) for b in recent)
        last_low = last.get("low", float("inf"))
        no_new_low = last_low >= recent_low - 0.01
        if no_new_low and volume_diminished:
            return True, (
                f"finished_auction_low: last_low {last_low:.2f} >= "
                f"recent_low {recent_low:.2f}, vol {last_volume:.0f} < "
                f"0.7x avg {avg_recent_volume:.0f}"
            )
    else:  # short
        recent_high = max(b.get("high", float("-inf")) for b in recent)
        last_high = last.get("high", float("-inf"))
        no_new_high = last_high <= recent_high + 0.01
        if no_new_high and volume_diminished:
            return True, (
                f"finished_auction_high: last_high {last_high:.2f} <= "
                f"recent_high {recent_high:.2f}, vol {last_volume:.0f} < "
                f"0.7x avg {avg_recent_volume:.0f}"
            )

    return False, ""


def _detect_trapped_traders(
    latest: dict,
    bars_history: list[dict],
    direction: str,
) -> tuple[bool, str]:
    """Trapped traders = breakout that immediately reverses with absorption.

    Long-trapped (we go SHORT — direction=short):
      - Prior bar (bars_history[-1]) broke above resistance with stacked_buy
      - Current latest bar reverses with stacked_sell + negative delta
      - Cumulative delta turns sharply negative (latest < prior)

    Short-trapped (we go LONG — direction=long):
      - Prior bar broke below support with stacked_sell
      - Current latest bar reverses with stacked_buy + positive delta
      - Cumulative delta turns sharply positive

    Returns (is_trapped, reason).
    """
    if not bars_history:
        return False, ""
    prior = bars_history[-1]

    last_delta = latest.get("delta", 0)
    last_stacked_buy = latest.get("stacked_buy", False)
    last_stacked_sell = latest.get("stacked_sell", False)
    last_cvd = latest.get("cvd_session", 0)

    prior_stacked_buy = prior.get("stacked_buy", False)
    prior_stacked_sell = prior.get("stacked_sell", False)
    prior_cvd = prior.get("cvd_session", 0)

    if direction == "long":
        # Short-trapped: prior bar broke down with stacked_sell, current
        # reverses with stacked_buy + positive delta + rising CVD
        if (prior_stacked_sell and last_stacked_buy
                and last_delta > 0
                and last_cvd > prior_cvd):
            return True, (
                f"shorts_trapped: prior stacked_sell, latest stacked_buy "
                f"with delta {last_delta:+d}, CVD {prior_cvd:+d}->{last_cvd:+d}"
            )
    else:  # short
        if (prior_stacked_buy and last_stacked_sell
                and last_delta < 0
                and last_cvd < prior_cvd):
            return True, (
                f"longs_trapped: prior stacked_buy, latest stacked_sell "
                f"with delta {last_delta:+d}, CVD {prior_cvd:+d}->{last_cvd:+d}"
            )

    return False, ""


def _score_context_bonuses(
    market: dict,
    latest: dict,
    bars_history: list[dict],
    direction: str,
) -> tuple[int, dict]:
    """Sprint M Tier 1: context-alignment bonuses, capped at +20 total.

    Four 5-point sub-bonuses, each fires when external market context
    agrees with the proposed reversal direction. These are *confirmation*
    signals — they don't generate a trade alone but boost marginal IQS
    setups across the entry threshold when institutional / multi-timeframe
    flow confirms.

    Sub-bonuses (each 0 or 5):

      a. **structural_bias_aligned** — `market["structural_bias"]["label"]`
         agrees with the trade direction (BULLISH/STRONG_BULLISH on LONG,
         BEARISH/STRONG_BEARISH on SHORT). Source: composite bias scorer
         that aggregates ~15 components (VWAP side, EMA stack, swing
         structure, etc.). When the bias confirms, the reversal entry
         is *with* the larger structure, not against it.

      b. **sweep_aligned** — there's an active liquidity sweep in the
         opposite direction of our trade (LONG entry wants a recent
         `break_direction="down"` sweep = stops below price taken out,
         likely trapping shorts; SHORT entry wants `break_direction="up"`).
         Source: `core/liquidity_sweep.SweepWatcher`.

      c. **multi_tf_cvd_aligned** — short-window CVD (sum of last 3 bar
         deltas), medium-window CVD (sum of last 10 bar deltas), and
         session CVD (latest bar's cvd_session) all point the same way
         and agree with the trade direction. Single-TF CVD divergence
         is already in the base IQS (D-score); this bonus rewards
         setups where ALL timeframes confirm — much more robust.

      d. **poc_migration_aligned** — the bar-by-bar POC has been migrating
         in the trade direction over the last `_POC_MIGRATION_WINDOW`
         bars (POC up for LONG, POC down for SHORT). Migrating POC
         indicates the value-area centroid is shifting in our direction —
         a leading signal for trend continuation. Static POC = no bonus.

    Returns (bonus_score, debug_dict).
    """
    debug = {
        "structural_bias_aligned": False, "structural_bias_label": "",
        "sweep_aligned": False, "sweep_pivot": None,
        "multi_tf_cvd_aligned": False,
        "cvd_short": 0.0, "cvd_medium": 0.0, "cvd_session": 0.0,
        "poc_migration_aligned": False, "poc_migration_ticks": 0.0,
    }
    bonus = 0

    # ── (a) Structural bias alignment ───────────────────────────────
    bias_info = market.get("structural_bias") or {}
    bias_label = str(bias_info.get("label") or "")
    debug["structural_bias_label"] = bias_label
    bullish_labels = ("BULLISH", "STRONG_BULLISH")
    bearish_labels = ("BEARISH", "STRONG_BEARISH")
    if direction == "long" and bias_label in bullish_labels:
        debug["structural_bias_aligned"] = True
        bonus += 5
    elif direction == "short" and bias_label in bearish_labels:
        debug["structural_bias_aligned"] = True
        bonus += 5

    # ── (b) Liquidity-sweep alignment ───────────────────────────────
    # An active "down sweep" (stops swept below pivot) supports a LONG
    # reversal — shorts are trapped, rebound is on. An active "up sweep"
    # supports a SHORT reversal.
    sweep_state = market.get("sweep_state") or {}
    watches = sweep_state.get("watches") or []
    for w in watches:
        bd = str(w.get("break_direction") or "")
        if direction == "long" and bd == "down":
            debug["sweep_aligned"] = True
            debug["sweep_pivot"] = w.get("pivot")
            bonus += 5
            break
        if direction == "short" and bd == "up":
            debug["sweep_aligned"] = True
            debug["sweep_pivot"] = w.get("pivot")
            bonus += 5
            break

    # ── (c) Multi-timeframe CVD alignment ───────────────────────────
    # Three windows over the volumetric-bar history:
    #   short  = last 3 bars  (~30s-90s of action at 1500-tick bars)
    #   medium = last 10 bars (~5-15 min)
    #   session = cvd_session from the latest bar
    # All three same sign + agree with direction = +5.
    if len(bars_history) >= 10:
        recent3 = bars_history[-3:]
        recent10 = bars_history[-10:]
        cvd_short = sum(float(b.get("delta", 0) or 0) for b in recent3)
        cvd_medium = sum(float(b.get("delta", 0) or 0) for b in recent10)
        cvd_session = float(latest.get("cvd_session", 0) or 0)
        debug["cvd_short"] = round(cvd_short, 1)
        debug["cvd_medium"] = round(cvd_medium, 1)
        debug["cvd_session"] = round(cvd_session, 1)

        want_positive = (direction == "long")
        all_aligned = (
            (cvd_short > 0) == want_positive
            and (cvd_medium > 0) == want_positive
            and (cvd_session > 0) == want_positive
        )
        # Require ALL three to have meaningful magnitude (avoid +0/-0
        # noise on quiet bars). Threshold = at least 1 contract net per
        # bar averaged.
        meaningful = (
            abs(cvd_short) >= 3
            and abs(cvd_medium) >= 10
            and abs(cvd_session) >= 10
        )
        if all_aligned and meaningful:
            debug["multi_tf_cvd_aligned"] = True
            bonus += 5

    # ── (d) POC migration alignment ─────────────────────────────────
    # Trailing-3 POC slope as a directional confirmation. Static or
    # contradictory POC = no bonus.
    _POC_MIGRATION_WINDOW = 3
    if len(bars_history) >= _POC_MIGRATION_WINDOW:
        recent_pocs = [
            float(b.get("poc", 0) or 0)
            for b in bars_history[-_POC_MIGRATION_WINDOW:]
        ]
        # Only count if every bar in window has a valid POC.
        if all(p > 0 for p in recent_pocs):
            poc_delta = recent_pocs[-1] - recent_pocs[0]
            # In POINTS (price units). Scale-free comparison across
            # MNQ vs ES vs other instruments.
            debug["poc_migration_ticks"] = round(poc_delta * 4, 1)  # 4 ticks/pt MNQ
            # Threshold: 2+ ticks of migration in the right direction.
            if direction == "long" and poc_delta * 4 >= 2:
                debug["poc_migration_aligned"] = True
                bonus += 5
            elif direction == "short" and poc_delta * 4 <= -2:
                debug["poc_migration_aligned"] = True
                bonus += 5

    return min(20, bonus), debug


def _score_tape_bonuses(
    latest: dict,
    bars_history: list[dict],
    direction: str,
) -> tuple[int, dict]:
    """Compute Sprint K1 pattern bonuses, capped at +20 total.

    Returns (bonus_score, debug_dict).
    """
    debug = {
        "finished_auction": False, "finished_auction_reason": "",
        "trapped_traders": False, "trapped_traders_reason": "",
    }
    bonus = 0

    fa, fa_reason = _detect_finished_auction(latest, bars_history, direction)
    if fa:
        debug["finished_auction"] = True
        debug["finished_auction_reason"] = fa_reason
        bonus += 10

    tt, tt_reason = _detect_trapped_traders(latest, bars_history, direction)
    if tt:
        debug["trapped_traders"] = True
        debug["trapped_traders_reason"] = tt_reason
        bonus += 10

    return min(20, bonus), debug


# ──────────────────────────────────────────────────────────────────
# Sprint K1 — TAPE-READ EVENT EMISSION (for dashboard live panel)
# ──────────────────────────────────────────────────────────────────

def _emit_tape_read_event(eval_data: dict, root: Path | None = None) -> None:
    """Write data/tape_read_latest.json for the dashboard tape-reader panel.

    Called from evaluate() on each evaluation (not just signals) so the
    dashboard can show "what the bot is seeing right now" even when no
    signal fires. Best-effort — never breaks the strategy on write failure.
    """
    try:
        base = Path(root) if root is not None else _DATA_ROOT
        out = base / "data" / "tape_read_latest.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(eval_data, indent=2), encoding="utf-8")
        tmp.replace(out)
    except Exception as e:
        logger.debug(f"[FOOTPRINT_CVD] tape_read emit failed: {e!r}")


# ──────────────────────────────────────────────────────────────────
# Strategy class (Phoenix BaseStrategy subclass)
# ──────────────────────────────────────────────────────────────────
# Module-level flag: log DATA_NOT_AVAILABLE only once per session per
# bot run, so we don't spam the log every minute while TickStreamer.cs
# is still being implemented operator-side.
_data_unavailable_logged = False


# ──────────────────────────────────────────────────────────────────
# Sprint L (2026-05-08) — N+1 confirmation gate
#
# Research finding: discretionary footprint reversal traders WAIT for
# the next bar to confirm the trigger bar's wick held. Coders fire on
# the bar that triggers the condition. This single rule kills most
# discretionary→code translation losses.
#
# Implementation:
#   On bar N: IQS >= threshold → store pending state (trigger_ts,
#   trigger_low/high, full signal context). Return None instead of
#   firing. Subsequent evaluations on the SAME bar (multiple ticks
#   between bar closes) refresh the pending state.
#
#   On bar N+1 (when latest["ts"] != pending.trigger_ts): check that
#   the new bar didn't violate the trigger bar's wick. If confirmed,
#   fire the signal NOW with the trigger bar's wick anchoring the stop.
#   If violated (trigger bar's low broken for LONG, or high broken for
#   SHORT), discard the pending — the absorption thesis failed.
#
#   Stale pendings (trigger > 3 bars old) are discarded so the strategy
#   doesn't fire on stale state if the bot is paused/restarted.
#
# Keyed by direction so a LONG pending doesn't clash with a SHORT
# pending (they're independent reversal opportunities at different levels).
# ──────────────────────────────────────────────────────────────────
_pending_signals: dict[str, dict] = {}
_PENDING_MAX_AGE_BARS = 3


def _check_pending_confirmation(
    direction: str, latest: dict, bars_history: list[dict],
) -> tuple[Optional[dict], str]:
    """Check if a pending signal should fire, be discarded, or stay pending.

    Returns (action, reason):
      - (pending_data, "FIRE")     — fire now, with trigger-bar context
      - (None, "DISCARD_VIOLATED") — pending invalidated by new bar's price action
      - (None, "DISCARD_STALE")    — pending too old (>3 bars), drop it
      - (None, "WAIT")             — same bar still, keep waiting
      - (None, "NONE")             — no pending exists for this direction
    """
    pending = _pending_signals.get(direction)
    if pending is None:
        return None, "NONE"

    # Same bar still — keep waiting (state will refresh in caller)
    if pending["trigger_ts"] == latest.get("ts"):
        return None, "WAIT"

    # New bar has arrived — check confirmation
    if direction == "long":
        # Confirmed if latest bar's low stayed at or above trigger's low.
        # tick_size buffer absorbs floating-point noise.
        if latest.get("low", 0) >= pending["trigger_low"] - 1e-9:
            return pending, "FIRE"
        return None, "DISCARD_VIOLATED"
    else:
        if latest.get("high", float("inf")) <= pending["trigger_high"] + 1e-9:
            return pending, "FIRE"
        return None, "DISCARD_VIOLATED"


def _is_pending_stale(direction: str, bars_history: list[dict]) -> bool:
    """Drop pendings whose trigger bar is no longer in recent history."""
    pending = _pending_signals.get(direction)
    if pending is None or not bars_history:
        return False
    trigger_ts = pending["trigger_ts"]
    # If we can't find the trigger ts in the last N bars of history,
    # it's old enough to discard.
    recent_ts = {b.get("ts") for b in bars_history[-_PENDING_MAX_AGE_BARS:]}
    return trigger_ts not in recent_ts


def _compute_atr_from_history(
    bars_history: list[dict], tick_size: float, period: int = 14,
) -> float:
    """ATR of last `period` volumetric bars, in ticks.

    Volumetric bars are activity-driven, so this is a per-tick-bar ATR
    (different scale than time-bar ATR). For 1,500-tick MNQ bars, expect
    roughly 8-25 ticks ATR depending on regime.
    """
    if not bars_history:
        return 0.0
    recent = bars_history[-period:]
    if not recent:
        return 0.0
    total_range = sum(
        max(0.0, b.get("high", 0) - b.get("low", 0)) / tick_size
        for b in recent
    )
    return total_range / len(recent)


class FootprintCVDReversal(BaseStrategy):
    """Phoenix BaseStrategy subclass.

    Volumetric bars come from disk (a separate stream from NT8's tick
    aggregator) — the bars_5m / bars_1m args from BaseStrategy.evaluate
    are unused by this strategy.
    """

    name = "footprint_cvd_reversal"

    # We compute the stop from the latest volumetric bar's low/high
    # (structural), not from ATR. Tell base_bot to skip its ATR override
    # so our computed stop_ticks is honored verbatim.
    atr_stop_override = True

    # B21: Strategies with managed exits (time stop, scale-out at +1R)
    # use this flag so risk_manager doesn't size against the structural
    # stop but uses a risk-reference instead.
    uses_managed_exit = True

    # WS-A audit: this strategy emits explicit stop/target prices on
    # the Signal, computed from market structure not config rr.
    computes_own_target = True
    computes_own_stop = True

    def __init__(self, config: dict):
        super().__init__(config)
        # Filter config dict to only fields FootprintCVDConfig accepts
        # (config/strategies.py may include 'enabled'/'validated' which
        # are also dataclass fields, plus extras like comments).
        import dataclasses as _dc
        valid_fields = {f.name for f in _dc.fields(FootprintCVDConfig)}
        clean_cfg = {k: v for k, v in (config or {}).items()
                     if k in valid_fields}
        self.cfg = FootprintCVDConfig(**clean_cfg)
        # 2026-05-17: Phase 7 FCD-3 — validate target_rr config.
        # Negative or non-finite target_rr produces wrong-side targets
        # (LONG with rr=-2 puts target BELOW entry). Sanitize once at
        # init so the rest of evaluate() can trust cfg.target_t1_rr/t2_rr.
        import math as _math
        if (self.cfg.target_t1_rr <= 0
                or not _math.isfinite(self.cfg.target_t1_rr)):
            logger.warning(
                f"[FOOTPRINT_CVD] bad target_t1_rr={self.cfg.target_t1_rr}, "
                f"using 1.0"
            )
            self.cfg.target_t1_rr = 1.0
        if (self.cfg.target_t2_rr <= 0
                or not _math.isfinite(self.cfg.target_t2_rr)):
            logger.warning(
                f"[FOOTPRINT_CVD] bad target_t2_rr={self.cfg.target_t2_rr}, "
                f"using 2.0"
            )
            self.cfg.target_t2_rr = 2.0
        if self.cfg.target_t2_rr <= self.cfg.target_t1_rr:
            logger.warning(
                f"[FOOTPRINT_CVD] target_t2_rr ({self.cfg.target_t2_rr}) "
                f"<= target_t1_rr ({self.cfg.target_t1_rr}); swapping"
            )
            self.cfg.target_t1_rr, self.cfg.target_t2_rr = (
                self.cfg.target_t2_rr, self.cfg.target_t1_rr,
            )

    def evaluate(
        self,
        market: dict,
        bars_5m: list,        # unused — volumetric bars come from disk
        bars_1m: list,        # unused
        session_info: dict,
    ) -> Optional[Signal]:
        global _data_unavailable_logged
        cfg = self.cfg
        # 2026-05-17 Phase 9.5 Item E: per-evaluate observability.
        # Single entry log so eval-count grep works reliably, plus SKIP
        # reason logs on every early-return path. The existing
        # DATA_NOT_AVAILABLE log throttles to once-per-process via the
        # _data_unavailable_logged flag, which made this strategy invisible
        # in Phase 9 per-strategy eval-count breakdown after the first hit.
        logger.debug(f"[EVAL] {self.name}: entered evaluate()")
        if not cfg.enabled:
            logger.debug(f"[EVAL] {self.name}: SKIP disabled")
            return None

        now_ct = self._resolve_now_ct(market, session_info)
        if not isinstance(now_ct, datetime):
            logger.debug(f"[EVAL] {self.name}: SKIP no_now_ct")
            return None

        # Hard gates
        if _is_lunch_block(now_ct, cfg):
            logger.debug(f"[EVAL] {self.name}: SKIP lunch_block")
            return None
        if _is_session_boundary(now_ct, cfg):
            logger.debug(f"[EVAL] {self.name}: SKIP session_boundary")
            return None

        latest = _load_volumetric_latest()
        if latest is None:
            if not _data_unavailable_logged:
                logger.info(
                    "[FOOTPRINT_CVD] DATA_NOT_AVAILABLE — "
                    "volumetric_latest.json absent. Strategy dormant "
                    "until TickStreamer.cs volumetric emitter ships."
                )
                _data_unavailable_logged = True
            # 2026-05-17 Phase 9.5 Item E: per-evaluate visibility (this fires
            # every cycle the data is missing; complements the throttled
            # INFO log above which only fires once-per-process).
            logger.debug(f"[EVAL] {self.name}: SKIP data_not_available")
            return None

        # Freshness check — stale data means TickStreamer disconnected
        try:
            bar_ts = datetime.fromisoformat(latest["ts"]).astimezone(_CT)
        except Exception:
            logger.debug(f"[EVAL] {self.name}: SKIP bar_ts_parse_fail")
            return None
        age_s = (now_ct - bar_ts).total_seconds()
        if age_s > cfg.data_freshness_sec:
            logger.info(
                f"[FOOTPRINT_CVD] DATA_STALE — last bar {bar_ts} "
                f"({age_s:.0f}s old, max {cfg.data_freshness_sec}s)"
            )
            # 2026-05-17 Phase 9.5 Item E: per-evaluate visibility.
            logger.debug(
                f"[EVAL] {self.name}: SKIP data_stale ({age_s:.0f}s)"
            )
            return None

        # 2026-05-17: Phase 7 FCD-2 — NaN guard on volumetric bar fields.
        # delta_total / OHLC NaN values silently pass downstream comparisons.
        # Reject corrupt bars (partial / tick-stream glitch) explicitly.
        import math as _math
        _critical_fields = ("delta_total", "high", "low", "open", "close")
        for _field in _critical_fields:
            _val = latest.get(_field)
            if _val is None:
                continue
            try:
                if not _math.isfinite(float(_val)):
                    logger.warning(
                        f"[FOOTPRINT_CVD] SKIP corrupt_volumetric_bar "
                        f"{_field}={_val} not finite"
                    )
                    return None
            except (TypeError, ValueError):
                logger.warning(
                    f"[FOOTPRINT_CVD] SKIP corrupt_volumetric_bar "
                    f"{_field}={_val} not numeric"
                )
                return None

        # Load history for divergence + compression baselines. Must load
        # at least min_history_bars so the warmup gate below isn't tripped
        # purely by under-loading (rather than genuinely insufficient data).
        n_history = max(
            cfg.compression_baseline_bars + cfg.compression_lookback_bars,
            cfg.divergence_lookback_bars * 2,
            cfg.min_history_bars,
        )
        bars_history = _load_volumetric_history(n_history)
        if len(bars_history) < cfg.min_history_bars:
            return None  # warmup — insufficient history on disk yet

        price = market.get("price")
        if price is None:
            return None
        # 2026-05-17: Phase 7 FCD-1 — finite-value guards.
        # NaN/Inf comparisons silently evaluate False, letting garbage
        # values pass downstream gates. Reject explicitly here.
        import math as _math
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None
        if not _math.isfinite(price) or price <= 0:
            logger.warning(f"[FOOTPRINT_CVD] SKIP non_finite_price={price}")
            return None
        tick_size = market.get("tick_size", 0.25)
        if not _math.isfinite(tick_size) or tick_size <= 0:
            tick_size = 0.25
        # Sprint J (2026-05-05): structure_bias replaces gamma_regime.
        # Falls back to legacy fields for compatibility with old market
        # dicts; default UNKNOWN never trips a regime gate.
        regime = (
            market.get("structure_bias")
            or market.get("regime")
            or market.get("gamma_regime")
            or "UNKNOWN"
        )
        regime = getattr(regime, "name", str(regime))

        # Try LONG side, then SHORT
        for direction in ("long", "short"):
            # Regime gate — accepts both:
            #   - new Sprint I structure_bias: "BULLISH" / "BEARISH" / "NEUTRAL"
            #   - legacy gamma_regime: "POSITIVE_STRONG" / "NEGATIVE_STRONG"
            # NEGATIVE_NORMAL / POSITIVE_NORMAL no longer trip the gate
            # (matches pre-Sprint-J behavior that only blocked on _STRONG).
            if direction == "long" and cfg.block_negative_strong_long \
                    and regime in ("NEGATIVE_STRONG", "BEARISH"):
                continue
            if direction == "short" and cfg.block_positive_strong_short \
                    and regime in ("POSITIVE_STRONG", "BULLISH"):
                continue

            # Confluence 1: HTF level (must score >0 to continue)
            # Sprint J: rewired to read price-action levels from market
            # dict via Sprint I's find_nearest_htf_level.
            level_score, level_name = _score_htf_level(
                price, market,
                cfg.level_buffer_ticks, tick_size, direction,
            )
            if level_score == 0:
                continue

            # Confluences 2/3/4
            div_score, div_debug = _score_cvd_divergence(
                bars_history, cfg.divergence_lookback_bars, direction, latest,
            )
            fp_score, fp_debug = _score_footprint(
                latest, direction, cfg, tick_size,
            )
            comp_score, comp_debug = _score_compression(
                bars_history, cfg, tick_size,
            )

            # Sprint K1: tape-reading pro pattern bonuses (cap +20).
            # Bonuses are ADDITIVE on top of the base 4-confluence IQS;
            # final IQS still capped at 100. Helps marginal setups cross
            # the threshold when institutional context is strong.
            tape_bonus, tape_debug = _score_tape_bonuses(
                latest, bars_history, direction,
            )

            # Sprint M Tier 1: context-alignment bonuses (cap +20).
            # Four 5-pt sub-bonuses for structural-bias / sweep / multi-TF
            # CVD / POC migration alignment. See _score_context_bonuses
            # docstring for details. Additive to tape_bonus; final IQS
            # cap is still 100.
            ctx_bonus, ctx_debug = _score_context_bonuses(
                market, latest, bars_history, direction,
            )

            # Sprint L: pattern × level-confluence multiplier.
            # Research finding: a 50-IQS pattern at a 3-level confluence
            # beats a 75-IQS pattern at a 1-level confluence. Levels are
            # categorical (something to be trapped against) — pure
            # additive scoring can't capture that. Multiply the
            # non-level score components by 1 + 0.25*extra_levels. Keeps
            # level_score additive (no double-count); caps at 100.
            extra_levels, confluence_labels = _count_extra_htf_confluences(
                price, market, cfg.level_buffer_ticks, tick_size,
            )
            confluence_multiplier = 1.0 + 0.25 * extra_levels  # 0:1.00, 1:1.25, 2:1.50, 3:1.75
            non_level_raw = div_score + fp_score + comp_score + tape_bonus + ctx_bonus
            non_level_adjusted = int(round(non_level_raw * confluence_multiplier))

            base_iqs = level_score + div_score + fp_score + comp_score
            iqs = min(100, level_score + non_level_adjusted)
            tier = _classify_tier(iqs)

            logger.info(
                f"[FOOTPRINT_CVD][{direction}] IQS={iqs} "
                f"(L={level_score} D={div_score} F={fp_score} "
                f"C={comp_score} +T={tape_bonus} +X={ctx_bonus} "
                f"x{confluence_multiplier:.2f}@{extra_levels}lvl) "
                f"level={level_name} tier={tier}"
            )

            # Sprint K1: emit tape-read event for dashboard live panel
            # — every evaluation, not just signal-fire ones.
            structure_bias_for_event = (
                "BULLISH" if regime in ("BULLISH", "POSITIVE_STRONG")
                else "BEARISH" if regime in ("BEARISH", "NEGATIVE_STRONG")
                else "NEUTRAL"
            )
            _emit_tape_read_event({
                "ts": now_ct.isoformat(),
                "direction_evaluated": direction,
                "structure_bias": structure_bias_for_event,
                "iqs_score": iqs,
                "iqs_breakdown": {
                    "L": level_score, "D": div_score,
                    "F": fp_score, "C": comp_score,
                    "T": tape_bonus, "X": ctx_bonus,
                    "bonus": tape_bonus,  # legacy key kept for older dashboard
                },
                "nearest_htf_level": level_name,
                "absorption_detected": fp_debug.get("absorption", False),
                "stacked_buy": latest.get("stacked_buy", False),
                "stacked_sell": latest.get("stacked_sell", False),
                "cvd_divergence": (
                    "BULLISH_DIV" if (direction == "long"
                                      and div_debug.get("divergence_present"))
                    else "BEARISH_DIV" if (direction == "short"
                                           and div_debug.get("divergence_present"))
                    else ""
                ),
                "finished_auction": tape_debug["finished_auction"],
                "trapped_traders": (
                    "shorts_trapped" if (direction == "long"
                                         and tape_debug["trapped_traders"])
                    else "longs_trapped" if (direction == "short"
                                             and tape_debug["trapped_traders"])
                    else ""
                ),
                "would_fire": iqs >= cfg.entry_threshold_iqs,
                "fire_direction": direction.upper() if iqs >= cfg.entry_threshold_iqs else "",
                "tier": tier,
                "bar_ts": latest.get("ts", ""),
            })

            # Sprint L: N+1 confirmation gate.
            # Before firing, check pending state from the prior bar:
            # - If a pending exists for this direction and the new bar's
            #   wick held the trigger bar's extreme: FIRE NOW with the
            #   trigger bar (not current bar) anchoring the stop.
            # - If the wick was violated: discard pending (absorption failed).
            # - If still on the same bar: refresh pending and don't fire yet.
            # - If no pending and current IQS qualifies: store pending and
            #   wait for next bar's confirmation.
            #
            # Stale pendings (trigger no longer in recent history) get
            # cleared so a paused/restarted bot doesn't fire on stale state.
            if _is_pending_stale(direction, bars_history):
                _pending_signals.pop(direction, None)

            confirm_data, confirm_action = _check_pending_confirmation(
                direction, latest, bars_history,
            )

            if confirm_action == "DISCARD_VIOLATED":
                logger.info(
                    f"[FOOTPRINT_CVD][{direction}] N+1 FAILED — "
                    f"trigger wick violated by current bar. Discarding pending."
                )
                _pending_signals.pop(direction, None)
                continue

            if confirm_action == "FIRE":
                # Fire on confirmed pending. Anchor stop to TRIGGER bar's
                # wick, not current bar's wick — that's the level the
                # absorption defended.
                _pending_signals.pop(direction, None)
                trigger_low = confirm_data["trigger_low"]
                trigger_high = confirm_data["trigger_high"]
                logger.info(
                    f"[FOOTPRINT_CVD][{direction}] N+1 CONFIRMED — firing "
                    f"signal anchored to trigger bar at {confirm_data['trigger_ts']}"
                )
            else:
                # confirm_action in {"NONE", "WAIT"} — IQS qualifies but
                # we need to defer one bar. Store/refresh pending and continue.
                if iqs < cfg.entry_threshold_iqs:
                    continue
                _pending_signals[direction] = {
                    "trigger_ts": latest.get("ts"),
                    "trigger_low": float(latest.get("low", 0)),
                    "trigger_high": float(latest.get("high", 0)),
                    "trigger_iqs": iqs,
                    "trigger_tier": tier,
                    "level_name": level_name,
                    "extra_levels": extra_levels,
                    "confluence_labels": confluence_labels,
                    "confluence_multiplier": confluence_multiplier,
                }
                if confirm_action == "WAIT":
                    logger.debug(
                        f"[FOOTPRINT_CVD][{direction}] PENDING refresh — same bar"
                    )
                else:
                    logger.info(
                        f"[FOOTPRINT_CVD][{direction}] PENDING — "
                        f"awaiting N+1 confirmation (IQS={iqs}, "
                        f"trigger_low={latest.get('low')}, trigger_high={latest.get('high')})"
                    )
                continue

            # If we got here, confirm_action == "FIRE" — build the Signal.
            # Sprint L: wick + ATR stop anchoring. Buffer is the larger
            # of stop_buffer_ticks (categorical floor) and 0.3 × ATR(14)
            # (volatility-aware). Keeps the wick anchor regardless of
            # regime: in low-vol the categorical floor dominates; in
            # high-vol the ATR buffer dominates.
            atr_ticks = _compute_atr_from_history(bars_history, tick_size, period=14)
            atr_buffer_ticks = max(
                float(cfg.stop_buffer_ticks),
                0.3 * atr_ticks,
            )
            buffer_price = atr_buffer_ticks * tick_size

            # 2026-05-17: Phase 7 FCD-4 — min_stop_ticks floor (was 1, allows
            # micro-stops eaten by NQ noise; 8t = 2pt is structurally sound)
            # + tick-grid snap on stop_price (off-grid prices rejected by
            # PhoenixOIFGuard / NT8 router).
            _min_stop_ticks = int(self.config.get("min_stop_ticks", 8))
            if direction == "long":
                stop_price = trigger_low - buffer_price
                stop_ticks = max(
                    _min_stop_ticks,
                    min(cfg.max_stop_ticks,
                        int(round((price - stop_price) / tick_size))),
                )
                # Recompute stop_price from CLAMPED stop_ticks so it stays
                # consistent if min/max floor was hit
                stop_price = price - stop_ticks * tick_size
            else:
                stop_price = trigger_high + buffer_price
                stop_ticks = max(
                    _min_stop_ticks,
                    min(cfg.max_stop_ticks,
                        int(round((stop_price - price) / tick_size))),
                )
                stop_price = price + stop_ticks * tick_size
            # Snap stop_price to tick grid (PhoenixOIFGuard rejects off-grid).
            stop_price = round(stop_price / tick_size) * tick_size

            # 2026-05-13 (#14): explicit CVD div-type instrumentation so
            # post-hoc analysis can answer "do multi-bar divs outperform
            # single-bar divs, or vice versa? are both-types entries the
            # highest-quality?" Currently we know A divergence fired but
            # not WHICH one — instrumenting both confluences AND a discrete
            # cvd_div_type metadata key.
            multi_div = bool(div_debug["divergence_present"])
            single_div = bool(div_debug["single_bar_div"])
            if multi_div and single_div:
                cvd_div_type = "both"
            elif multi_div:
                cvd_div_type = "multi_bar"
            elif single_div:
                cvd_div_type = "single_bar"
            else:
                cvd_div_type = "none"
            cvd_div_magnitude = float(div_debug.get("magnitude", 0.0) or 0.0)

            confluences = [f"htf_level:{level_name}"]
            if multi_div:
                confluences.append(
                    f"cvd_divergence_multi_bar(mag={cvd_div_magnitude:+.2f})"
                )
            if single_div:
                confluences.append("cvd_divergence_single_bar")
            if fp_debug["stacked"]:
                confluences.append("stacked_imbalance")
            if fp_debug["absorption"]:
                confluences.append("absorption")
            if fp_debug["oversized"]:
                confluences.append("oversized_imbalance")
            if comp_debug["delta_compression"]:
                confluences.append("delta_compression")
            if comp_debug["range_compression"]:
                confluences.append("range_compression")
            if comp_debug["volume_holding"]:
                confluences.append("volume_holding")
            if comp_debug["effort_spike"]:
                confluences.append("effort_spike")
            # Sprint K1 tape-read pattern confluences
            if tape_debug["finished_auction"]:
                confluences.append("finished_auction")
            if tape_debug["trapped_traders"]:
                confluences.append("trapped_traders")
            # Sprint M Tier 1 context-alignment confluences
            if ctx_debug["structural_bias_aligned"]:
                confluences.append("bias_aligned")
            if ctx_debug["sweep_aligned"]:
                confluences.append("sweep_aligned")
            if ctx_debug["multi_tf_cvd_aligned"]:
                confluences.append("multi_tf_cvd_aligned")
            if ctx_debug["poc_migration_aligned"]:
                confluences.append("poc_migration_aligned")

            # 2026-05-17: Phase 7 FCD-5 — pre-compute and snap target_price
            # to the tick grid. Off-grid target prices are rejected by
            # PhoenixOIFGuard / NT8 router.
            if direction == "long":
                _target_price = price + (price - stop_price) * cfg.target_t2_rr
            else:
                _target_price = price - (stop_price - price) * cfg.target_t2_rr
            _target_price = round(_target_price / tick_size) * tick_size

            return Signal(
                direction="LONG" if direction == "long" else "SHORT",
                stop_ticks=stop_ticks,
                target_rr=cfg.target_t2_rr,
                confidence=float(iqs),
                entry_score=float(min(60, iqs * 0.6)),  # 0-60 scale
                strategy=self.name,
                reason=(
                    f"4-confluence reversal at {level_name}, tier {tier} "
                    f"[cvd_div={cvd_div_type}]"
                ),
                confluences=confluences,
                atr_stop_override=True,
                entry_type="MARKET",
                entry_price=price,
                stop_price=stop_price,
                # T2 in price terms (T1 50% scale-out at +1R is base_bot's job)
                # Snapped to tick grid via FCD-5 (above) — pass the variable.
                target_price=_target_price,
                scale_out_rr=cfg.target_t1_rr,
                metadata={
                    "sub_strategy": "footprint_cvd_reversal",
                    "tier": tier,
                    "iqs": iqs,
                    "base_iqs": base_iqs,            # Sprint K1: pre-bonus
                    "tape_bonus": tape_bonus,        # Sprint K1: pattern bonuses
                    "tape_debug": tape_debug,        # Sprint K1
                    "ctx_bonus": ctx_bonus,          # Sprint M Tier 1: context bonuses
                    "ctx_debug": ctx_debug,          # Sprint M Tier 1
                    "level_score": level_score,
                    "divergence_score": div_score,
                    # 2026-05-13 (#14): structured fields so a post-hoc
                    # group-by can answer "which div type wins?" Compact
                    # enum + magnitude, separate from the verbose debug dict.
                    "cvd_div_type": cvd_div_type,
                    "cvd_div_magnitude": cvd_div_magnitude,
                    "footprint_score": fp_score,
                    "compression_score": comp_score,
                    "level_name": level_name,
                    "regime": regime,
                    "bar_ts": latest["ts"],
                    "divergence_debug": div_debug,
                    "footprint_debug": fp_debug,
                    "compression_debug": comp_debug,
                    # Sprint L (2026-05-08) additions:
                    "extra_htf_levels": extra_levels,
                    "confluence_labels": confluence_labels,
                    "confluence_multiplier": confluence_multiplier,
                    "trigger_bar_ts": confirm_data["trigger_ts"],
                    "trigger_low": trigger_low,
                    "trigger_high": trigger_high,
                    "atr_buffer_ticks": round(atr_buffer_ticks, 2),
                    "atr_ticks": round(atr_ticks, 2),
                },
            )

        return None

    # ─── helpers ─────────────────────────────────────────────────
    @staticmethod
    def _resolve_now_ct(market: dict, session_info: dict) -> Optional[datetime]:
        """Pull current CT time from session_info (preferred) or market."""
        for src in (session_info, market):
            if not isinstance(src, dict):
                continue
            v = src.get("now_ct")
            if isinstance(v, datetime):
                return v.astimezone(_CT) if v.tzinfo else v.replace(tzinfo=_CT)
        # Fallback: real wall clock (only used when caller didn't supply)
        return datetime.now(tz=_CT)
