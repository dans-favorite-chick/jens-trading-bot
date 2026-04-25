"""
Phoenix Bot — Market Advisor

Deterministic guidance producer that synthesizes:
  * MenthorQ dealer-flow data (gamma regime, HVL, DEX, vanna/charm, CTA)
  * FMP cross-venue reference (QQQ → MNQ-equivalent, SPY correlation)
  * Tick-aggregator state (ATR regime, VWAP extension, RSI, CVD)

…into a small advisory packet that both the Council Gate and individual
strategies can consume. Per Jennifer 2026-04-24: "the counsel needs to
determine market sentiment and direction, and volatility. We need to
make sure we wire in all info from MenthorQ as well as any helpful
data from FMP to further advise the counsel. They should be guiding,
not stopping. For example, choppy market = tighter 2:1 ratio for pnl.
Big runs can get 3-1. Big RSI or overextended from VWAP is caution."

Output schema (AdvisorGuidance):
  sentiment         : "BULLISH" | "BEARISH" | "NEUTRAL"
  direction_conf    : 0-100
  volatility_regime : "COMPRESSED" | "NORMAL" | "EXPANDED" | "EXTREME"
  market_regime     : "TRENDING_BULL" | "TRENDING_BEAR" | "CHOPPY" | "OVEREXTENDED"
  suggested_rr_tier : float (typ. 1.5 / 2.0 / 2.5 / 3.0)
  caution_flags     : list[str]  — e.g., ["rsi_overbought", "vwap_ext_high"]
  reasoning         : str — one-paragraph summary

This module is DETERMINISTIC (no AI call) and fast (<5ms). It is called
every eval cycle by base_bot._build_market_snapshot (or analogous) so
the guidance is always fresh. The council then layers AI on top of this
deterministic baseline; strategies can read the suggested_rr_tier and
caution_flags directly to modify RR/position size/entry gates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("MarketAdvisor")


# ─── Tunables ──────────────────────────────────────────────────────

# ATR ratio thresholds (current 5m ATR / avg of last N bars' ATR).
_ATR_RATIO_COMPRESSED = 0.7
_ATR_RATIO_EXPANDED = 1.4
_ATR_RATIO_EXTREME = 2.0

# VWAP extension in σ (stddev from VWAP).
_VWAP_EXT_CAUTION_SIGMA = 2.0
_VWAP_EXT_EXTREME_SIGMA = 3.0

# RSI thresholds.
_RSI_OVERBOUGHT = 75.0
_RSI_OVERSOLD = 25.0
_RSI_EXTREME_OB = 85.0
_RSI_EXTREME_OS = 15.0

# FMP disagreement threshold (% deviation MNQ-local vs QQQ-implied).
_FMP_DISAGREE_SOFT = 0.005    # 0.5% — log only
_FMP_DISAGREE_HARD = 0.015    # 1.5% — add caution flag


# ─── Data schema ──────────────────────────────────────────────────

@dataclass
class AdvisorGuidance:
    sentiment: str = "NEUTRAL"
    direction_conf: float = 0.0
    volatility_regime: str = "NORMAL"
    market_regime: str = "CHOPPY"
    suggested_rr_tier: float = 2.0
    caution_flags: list[str] = field(default_factory=list)
    reasoning: str = ""
    # Debug surface for logs / dashboard
    inputs_snapshot: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def as_log_line(self) -> str:
        return (
            f"sent={self.sentiment}({self.direction_conf:.0f}) "
            f"vol={self.volatility_regime} regime={self.market_regime} "
            f"rr={self.suggested_rr_tier:.1f} "
            f"caution={','.join(self.caution_flags) or '-'}"
        )


# ─── Sub-producers (each returns a small partial update) ───────────

def _classify_volatility(market: dict) -> tuple[str, dict]:
    """Use ATR 5m vs a 60-bar-ish baseline (approximated via ATR 15m)."""
    atr_5m = float(market.get("atr_5m", 0) or 0)
    atr_15m = float(market.get("atr_15m", 0) or 0)
    if atr_5m <= 0 or atr_15m <= 0:
        return "NORMAL", {"atr_5m": atr_5m, "atr_15m": atr_15m, "reason": "insufficient_data"}
    # Normalize: atr_5m is per-5m bar, atr_15m is per-15m bar; scale to comparable units.
    # Rough ratio: atr_5m * 3 ≈ atr_15m in a steady regime.
    ratio = (atr_5m * 3.0) / atr_15m if atr_15m > 0 else 1.0
    if ratio >= _ATR_RATIO_EXTREME:
        return "EXTREME", {"ratio": ratio}
    if ratio >= _ATR_RATIO_EXPANDED:
        return "EXPANDED", {"ratio": ratio}
    if ratio <= _ATR_RATIO_COMPRESSED:
        return "COMPRESSED", {"ratio": ratio}
    return "NORMAL", {"ratio": ratio}


def _classify_sentiment(market: dict) -> tuple[str, float, dict]:
    """Blend MQ direction + tf_bias + council-ish votes from snapshot."""
    mq = market.get("menthorq", {}) or {}
    mq_dir = str(mq.get("direction_bias", "NEUTRAL")).upper()
    tf_bias = market.get("tf_bias", {}) or {}
    bull = sum(1 for v in tf_bias.values() if str(v).upper() == "BULLISH")
    bear = sum(1 for v in tf_bias.values() if str(v).upper() == "BEARISH")

    vanna = str(mq.get("vanna", "NEUTRAL")).upper()
    charm = str(mq.get("charm", "NEUTRAL")).upper()

    # Score: +1 per bullish signal, -1 per bearish.
    score = 0
    score += bull - bear
    if mq_dir == "LONG":
        score += 1
    elif mq_dir == "SHORT":
        score -= 1
    if vanna == "BULLISH":
        score += 0.5
    elif vanna == "BEARISH":
        score -= 0.5
    if charm == "BULLISH":
        score += 0.5
    elif charm == "BEARISH":
        score -= 0.5

    if score >= 2:
        return "BULLISH", min(100.0, 50.0 + score * 10), {"score": score, "mq_dir": mq_dir, "tf_bull": bull, "tf_bear": bear}
    if score <= -2:
        return "BEARISH", min(100.0, 50.0 + abs(score) * 10), {"score": score, "mq_dir": mq_dir, "tf_bull": bull, "tf_bear": bear}
    return "NEUTRAL", 50.0 - abs(score) * 5, {"score": score, "mq_dir": mq_dir, "tf_bull": bull, "tf_bear": bear}


def _classify_market_regime(volatility: str, sentiment: str, market: dict) -> tuple[str, dict]:
    """Combine volatility + sentiment + VWAP extension into a regime label."""
    price = float(market.get("price", 0) or 0)
    vwap = float(market.get("vwap", 0) or 0)
    vwap_std = float(market.get("vwap_std", 0) or 0)
    if price > 0 and vwap > 0 and vwap_std > 0:
        vwap_ext_sigma = (price - vwap) / vwap_std
    else:
        vwap_ext_sigma = 0.0

    # Overextended overrides everything when VWAP σ > 3 OR RSI extreme.
    # RSI may not be in the snapshot by default; look for common keys.
    rsi = float(market.get("rsi", 0) or market.get("rsi_14", 0) or 0)
    if abs(vwap_ext_sigma) >= _VWAP_EXT_EXTREME_SIGMA or (
        rsi >= _RSI_EXTREME_OB or (rsi > 0 and rsi <= _RSI_EXTREME_OS)
    ):
        return "OVEREXTENDED", {"vwap_ext_sigma": vwap_ext_sigma, "rsi": rsi}

    if sentiment == "BULLISH" and volatility in ("NORMAL", "EXPANDED"):
        return "TRENDING_BULL", {"vwap_ext_sigma": vwap_ext_sigma}
    if sentiment == "BEARISH" and volatility in ("NORMAL", "EXPANDED"):
        return "TRENDING_BEAR", {"vwap_ext_sigma": vwap_ext_sigma}
    if volatility in ("COMPRESSED", "NORMAL"):
        return "CHOPPY", {"vwap_ext_sigma": vwap_ext_sigma}
    return "CHOPPY", {"vwap_ext_sigma": vwap_ext_sigma}


def _suggest_rr(market_regime: str, volatility: str) -> float:
    """Per Jennifer: choppy = 2:1, big runs = 3:1, overextended = caution (1.5:1)."""
    if market_regime == "OVEREXTENDED":
        return 1.5
    if market_regime in ("TRENDING_BULL", "TRENDING_BEAR"):
        return 3.0 if volatility in ("NORMAL", "EXPANDED") else 2.5
    # CHOPPY + anything
    return 2.0


def _compute_caution_flags(market: dict, fmp_snap: Optional[dict]) -> list[str]:
    flags: list[str] = []

    # VWAP extension
    price = float(market.get("price", 0) or 0)
    vwap = float(market.get("vwap", 0) or 0)
    vwap_std = float(market.get("vwap_std", 0) or 0)
    if price > 0 and vwap > 0 and vwap_std > 0:
        sigma = (price - vwap) / vwap_std
        if abs(sigma) >= _VWAP_EXT_EXTREME_SIGMA:
            flags.append("vwap_extreme")
        elif abs(sigma) >= _VWAP_EXT_CAUTION_SIGMA:
            flags.append("vwap_extended")

    # RSI
    rsi = float(market.get("rsi", 0) or market.get("rsi_14", 0) or 0)
    if rsi > 0:
        if rsi >= _RSI_EXTREME_OB:
            flags.append("rsi_extreme_ob")
        elif rsi >= _RSI_OVERBOUGHT:
            flags.append("rsi_overbought")
        elif rsi <= _RSI_EXTREME_OS:
            flags.append("rsi_extreme_os")
        elif rsi <= _RSI_OVERSOLD:
            flags.append("rsi_oversold")

    # Gamma regime unknown
    mq = market.get("menthorq", {}) or {}
    if str(mq.get("gex_regime", "")).upper() in ("", "UNKNOWN"):
        flags.append("gamma_regime_unknown")

    # MQ price below HVL + negative gamma → amplified downside
    hvl = float(mq.get("hvl", 0) or 0)
    if hvl > 0 and price > 0 and price < hvl and str(mq.get("gex_regime", "")).upper() == "NEGATIVE":
        flags.append("below_hvl_neg_gamma")

    # FMP disagreement
    if fmp_snap and fmp_snap.get("reference") and fmp_snap.get("local"):
        ref = float(fmp_snap["reference"])
        local = float(fmp_snap["local"])
        if ref > 0:
            dev = abs(local - ref) / ref
            if dev >= _FMP_DISAGREE_HARD:
                flags.append(f"fmp_disagrees_{dev*100:.1f}pct")
            elif dev >= _FMP_DISAGREE_SOFT:
                flags.append(f"fmp_mild_disagree_{dev*100:.1f}pct")

    # VIX extreme
    intel = market.get("intel", {}) or {}
    vix = float(intel.get("vix", 0) or market.get("vix", 0) or 0)
    if vix >= 40:
        flags.append("vix_extreme")
    elif vix >= 30:
        flags.append("vix_elevated")

    # Post-OPEX week — from menthorq notes if set
    notes = str(mq.get("notes", "")).lower()
    if "opex" in notes or "post-opex" in notes:
        flags.append("post_opex_week")

    return flags


# ─── Public API ────────────────────────────────────────────────────

def compute_guidance(market: dict, fmp_snap: Optional[dict] = None) -> AdvisorGuidance:
    """Produce an AdvisorGuidance from a market snapshot dict.

    `fmp_snap` is an optional dict with keys {local, reference, source,
    deviation_pct} — typically returned by core.fmp_sanity.check_mnq_vs_fmp.
    If not provided, this function tries to fetch one on the fly (cheap
    — fmp_sanity caches at 30s).
    """
    if fmp_snap is None:
        try:
            from core import fmp_sanity
            local = float(market.get("price", 0) or 0)
            if local > 0:
                fmp_snap = fmp_sanity.check_mnq_vs_fmp(local, tolerance=0.02)
        except Exception as e:
            logger.debug(f"[MarketAdvisor] FMP snapshot fetch failed: {e!r}")
            fmp_snap = None

    volatility, vol_ctx = _classify_volatility(market)
    sentiment, direction_conf, sent_ctx = _classify_sentiment(market)
    market_regime, mr_ctx = _classify_market_regime(volatility, sentiment, market)
    rr_tier = _suggest_rr(market_regime, volatility)
    caution = _compute_caution_flags(market, fmp_snap)

    # Nudge: if OVEREXTENDED, downshift RR one step.
    if "vwap_extreme" in caution and rr_tier > 1.5:
        rr_tier = 1.5
    # If FMP says we disagree significantly, add caution and drop RR.
    if any(f.startswith("fmp_disagrees_") for f in caution):
        rr_tier = min(rr_tier, 2.0)

    reasoning = (
        f"Volatility={volatility} (atr_ratio~{vol_ctx.get('ratio', '?'):.2f}); "
        f"sentiment={sentiment} (score={sent_ctx.get('score', '?')}); "
        f"regime={market_regime}; rr_tier={rr_tier:.1f}; "
        f"flags={caution or 'none'}."
    )

    return AdvisorGuidance(
        sentiment=sentiment,
        direction_conf=float(direction_conf),
        volatility_regime=volatility,
        market_regime=market_regime,
        suggested_rr_tier=float(rr_tier),
        caution_flags=caution,
        reasoning=reasoning,
        inputs_snapshot={
            "price": market.get("price"),
            "vwap": market.get("vwap"),
            "vwap_std": market.get("vwap_std"),
            "atr_5m": market.get("atr_5m"),
            "atr_15m": market.get("atr_15m"),
            "tf_bias": market.get("tf_bias"),
            "rsi": market.get("rsi") or market.get("rsi_14"),
            "mq_regime": (market.get("menthorq") or {}).get("gex_regime"),
            "mq_dir": (market.get("menthorq") or {}).get("direction_bias"),
            "mq_hvl": (market.get("menthorq") or {}).get("hvl"),
            "vix": ((market.get("intel") or {}).get("vix")) or market.get("vix"),
            "fmp_snap": fmp_snap,
        },
    )


def enrich_market_snapshot(market: dict, fmp_snap: Optional[dict] = None) -> dict:
    """Non-destructively add advisor_guidance to a market snapshot dict.

    Strategies can read `market["advisor_guidance"]["suggested_rr_tier"]`
    to modify their target_price. If an advisor error occurs, we still
    return the original market dict untouched — this function must never
    crash the eval path.
    """
    try:
        guidance = compute_guidance(market, fmp_snap)
        out = dict(market)
        out["advisor_guidance"] = guidance.to_dict()
        return out
    except Exception as e:
        logger.warning(f"[MarketAdvisor] enrichment failed (non-blocking): {e!r}")
        return market
