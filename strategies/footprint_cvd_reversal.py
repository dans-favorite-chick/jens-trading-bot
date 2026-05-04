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
- MenthorQ levels come from market["gamma_levels"] (a GammaLevels
  dataclass instance from core/menthorq_gamma.py). Attribute access
  via getattr — keys are: put_support, put_support_0dte,
  call_resistance, call_resistance_0dte, hvl, hvl_0dte.
- Prior-session VP POC from market["prior_day_poc"].
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
# ──────────────────────────────────────────────────────────────────
def _gl_attr(gamma_levels: Any, name: str) -> Optional[float]:
    """Read a level off a GammaLevels-like object. Returns None for
    missing attribute or zero/None values (Phoenix sometimes uses 0
    as a sentinel for unset levels)."""
    if gamma_levels is None:
        return None
    val = getattr(gamma_levels, name, None)
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _score_htf_level(
    price: float,
    gamma_levels: Any,
    vp_poc_prev: Optional[float],
    buffer_ticks: int,
    tick_size: float,
    direction: str,
) -> tuple[int, str]:
    """Score 0-25 + level name.

    Reads MenthorQ levels off the GammaLevels dataclass attributes
    used elsewhere in Phoenix (NOT a "_all"-suffixed dict). VP POC
    is the lower-quality fallback at 15 pts.
    """
    buf = buffer_ticks * tick_size

    if direction == "long":
        candidates = [
            ("put_support_0dte", 25),
            ("put_support",       25),
            ("hvl_0dte",          25),
            ("hvl",               25),
        ]
    else:
        candidates = [
            ("call_resistance_0dte", 25),
            ("call_resistance",       25),
            ("hvl_0dte",              25),
            ("hvl",                   25),
        ]

    for name, score in candidates:
        level = _gl_attr(gamma_levels, name)
        if level is not None and abs(price - level) <= buf:
            return score, name

    if vp_poc_prev is not None and vp_poc_prev > 0 \
            and abs(price - vp_poc_prev) <= buf:
        return 15, "vp_poc_prev_session"

    return 0, ""


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
# ──────────────────────────────────────────────────────────────────
def _score_footprint(
    latest: dict, direction: str, cfg: FootprintCVDConfig, tick_size: float,
) -> tuple[int, dict]:
    debug = {"stacked": False, "absorption": False, "oversized": False}
    score = 0

    if direction == "long":
        if latest.get("stacked_buy", False):
            debug["stacked"] = True
            score += 15
        if latest.get("delta", 0) < 0:
            range_ticks = abs(latest["high"] - latest["low"]) / tick_size
            if (abs(latest["delta"]) > cfg.absorption_min_delta
                    and range_ticks < cfg.absorption_max_range_ticks):
                debug["absorption"] = True
                score += 15
    else:
        if latest.get("stacked_sell", False):
            debug["stacked"] = True
            score += 15
        if latest.get("delta", 0) > 0:
            range_ticks = abs(latest["high"] - latest["low"]) / tick_size
            if (abs(latest["delta"]) > cfg.absorption_min_delta
                    and range_ticks < cfg.absorption_max_range_ticks):
                debug["absorption"] = True
                score += 15

    if latest.get("max_imbalance_ratio", 0) >= cfg.oversized_imbalance_ratio:
        debug["oversized"] = True
        score += 5

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
# Strategy class (Phoenix BaseStrategy subclass)
# ──────────────────────────────────────────────────────────────────
# Module-level flag: log DATA_NOT_AVAILABLE only once per session per
# bot run, so we don't spam the log every minute while TickStreamer.cs
# is still being implemented operator-side.
_data_unavailable_logged = False


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

    def evaluate(
        self,
        market: dict,
        bars_5m: list,        # unused — volumetric bars come from disk
        bars_1m: list,        # unused
        session_info: dict,
    ) -> Optional[Signal]:
        global _data_unavailable_logged
        cfg = self.cfg
        if not cfg.enabled:
            return None

        now_ct = self._resolve_now_ct(market, session_info)
        if not isinstance(now_ct, datetime):
            return None

        # Hard gates
        if _is_lunch_block(now_ct, cfg):
            return None
        if _is_session_boundary(now_ct, cfg):
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
            return None

        # Freshness check — stale data means TickStreamer disconnected
        try:
            bar_ts = datetime.fromisoformat(latest["ts"]).astimezone(_CT)
        except Exception:
            return None
        age_s = (now_ct - bar_ts).total_seconds()
        if age_s > cfg.data_freshness_sec:
            logger.info(
                f"[FOOTPRINT_CVD] DATA_STALE — last bar {bar_ts} "
                f"({age_s:.0f}s old, max {cfg.data_freshness_sec}s)"
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
        tick_size = market.get("tick_size", 0.25)
        regime = market.get("regime") or market.get("gamma_regime") or "UNKNOWN"
        # Phoenix passes GammaRegime enum sometimes — coerce to string
        regime = getattr(regime, "name", str(regime))
        gamma_levels = market.get("gamma_levels")
        vp_poc_prev = market.get("prior_day_poc")

        # Try LONG side, then SHORT
        for direction in ("long", "short"):
            # Regime gate
            if direction == "long" and cfg.block_negative_strong_long \
                    and "NEGATIVE_STRONG" in regime:
                continue
            if direction == "short" and cfg.block_positive_strong_short \
                    and "POSITIVE_STRONG" in regime:
                continue

            # Confluence 1: HTF level (must score >0 to continue)
            level_score, level_name = _score_htf_level(
                price, gamma_levels, vp_poc_prev,
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

            iqs = min(100, level_score + div_score + fp_score + comp_score)
            tier = _classify_tier(iqs)

            logger.info(
                f"[FOOTPRINT_CVD][{direction}] IQS={iqs} "
                f"(L={level_score} D={div_score} F={fp_score} C={comp_score}) "
                f"level={level_name} tier={tier}"
            )

            if iqs < cfg.entry_threshold_iqs:
                continue

            # Build the Phoenix Signal — structural stop from latest bar
            if direction == "long":
                stop_price = latest["low"] - cfg.stop_buffer_ticks * tick_size
                stop_ticks = max(
                    1,
                    min(cfg.max_stop_ticks,
                        int(round((price - stop_price) / tick_size))),
                )
            else:
                stop_price = latest["high"] + cfg.stop_buffer_ticks * tick_size
                stop_ticks = max(
                    1,
                    min(cfg.max_stop_ticks,
                        int(round((stop_price - price) / tick_size))),
                )

            confluences = [f"htf_level:{level_name}"]
            if div_debug["divergence_present"]:
                confluences.append("cvd_divergence_multi_bar")
            if div_debug["single_bar_div"]:
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

            return Signal(
                direction="LONG" if direction == "long" else "SHORT",
                stop_ticks=stop_ticks,
                target_rr=cfg.target_t2_rr,
                confidence=float(iqs),
                entry_score=float(min(60, iqs * 0.6)),  # 0-60 scale
                strategy=self.name,
                reason=f"4-confluence reversal at {level_name}, tier {tier}",
                confluences=confluences,
                atr_stop_override=True,
                entry_type="MARKET",
                entry_price=price,
                stop_price=stop_price,
                # T2 in price terms (T1 50% scale-out at +1R is base_bot's job)
                target_price=(price + (price - stop_price) * cfg.target_t2_rr
                              if direction == "long"
                              else price - (stop_price - price) * cfg.target_t2_rr),
                scale_out_rr=cfg.target_t1_rr,
                metadata={
                    "sub_strategy": "footprint_cvd_reversal",
                    "tier": tier,
                    "iqs": iqs,
                    "level_score": level_score,
                    "divergence_score": div_score,
                    "footprint_score": fp_score,
                    "compression_score": comp_score,
                    "level_name": level_name,
                    "regime": regime,
                    "bar_ts": latest["ts"],
                    "divergence_debug": div_debug,
                    "footprint_debug": fp_debug,
                    "compression_debug": comp_debug,
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
