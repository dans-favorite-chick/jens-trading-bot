"""
Phoenix Bot — Composite Structural Bias Engine

Integrates ALL Saturday/Sunday signal modules into a single bias with
a full reasoning trail. Replaces the naive 2-of-3 tf_bias rule with
structural + order flow + gamma-context composite scoring.

Inputs (from market_snapshot enriched by base_bot):
  - swing_state          (core/swing_detector.py)  HH/HL/LH/LL trend
  - bos_choch            latest BOS or CHoCH event
  - volume_profile       POC/HVN/LVN/VAH/VAL location
  - footprint_signals    stacked imbalance, absorption, exhaustion, delta div
  - climax_warnings      active climax warnings (reversal pending)
  - reversal_signals     confirmed secondary-test entries
  - sweep_events         liquidity sweep reclassifications
  - chart_patterns       bull/bear flag, H&S (context-weighted)
  - candlestick_patterns existing 23-pattern detector output
  - menthorq_context     GEX regime + CR/HVL/PS proximity
  - gamma_flip_state     active flip / cooldown
  - vix_regime           contango/backwardation class
  - pinning_state        pin risk active?
  - opex_status          OpEx day / triple witching afternoon?
  - es_confirmation      NQ vs ES gamma alignment

Output:
  StructuralBias {
    label,           # STRONG_BULLISH | BULLISH | NEUTRAL | BEARISH | STRONG_BEARISH
    score,           # -100 to +100 (negative = bearish)
    confidence,      # 0-100 (how much evidence supports the label)
    reasoning,       # list of (component, points, description) tuples
    vetoes,          # list of active vetoes
    timestamp,
  }

DUAL-WRITE with tf_bias. Strategies continue using tf_bias until WFO approves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("StructuralBias")

PHOENIX_ROOT = Path(__file__).parent.parent


# ─── Component weights ────────────────────────────────────────────────
# Each component can contribute up to max_points (positive OR negative).
# Tuned toward: structure > order flow > patterns > context.

WEIGHTS = {
    "swing_structure":    30,  # HH/HL trend
    "bos_choch":          20,  # Recent break event
    "footprint_signals":  25,  # Stacked imbalance, absorption, exhaustion
    "chart_patterns":     15,  # Bull flag, H&S, etc.
    "candlestick":        10,  # Existing 23-pattern detector
    "volume_profile":     15,  # Near POC/HVN/LVN structure
    "climax_reversal":    20,  # Climax warnings + confirmed signals
    "liquidity_sweep":    20,  # Failed BOS reclassifications
    "menthorq_gamma":     15,  # GEX regime + wall proximity
    "gamma_flip":         15,  # Active flip event
    "vix_regime":          5,  # VIX term structure
    "es_confirmation":     5,  # NQ vs ES alignment
}


@dataclass
class BiasComponent:
    name: str
    points: int       # -max .. +max per WEIGHTS
    max_points: int
    reasoning: str


@dataclass
class StructuralBias:
    label: str        # STRONG_BULLISH | BULLISH | NEUTRAL | BEARISH | STRONG_BEARISH
    score: int        # -100 to +100 normalized
    raw_score: int    # Sum of component points before normalization
    max_possible: int # Sum of all weights
    confidence: int   # 0-100 (abs(score) × evidence coverage)
    components: list[BiasComponent] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    timestamp: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "score": self.score,
            "raw_score": self.raw_score,
            "max_possible": self.max_possible,
            "confidence": self.confidence,
            "components": [
                {"name": c.name, "points": c.points, "max": c.max_points,
                 "reasoning": c.reasoning}
                for c in self.components
            ],
            "vetoes": self.vetoes,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }

    def reasoning_trail(self) -> str:
        """Human-readable component breakdown."""
        lines = [f"STRUCTURAL BIAS: {self.label} (score={self.score:+d}, conf={self.confidence}%)"]
        for c in self.components:
            if c.points != 0:
                sign = "+" if c.points > 0 else ""
                lines.append(f"  {c.name:18} {sign}{c.points:3d} ({c.max_points:2d}) — {c.reasoning}")
        if self.vetoes:
            lines.append("  VETOES:")
            for v in self.vetoes:
                lines.append(f"    • {v}")
        return "\n".join(lines)


def _label_from_score(score: int) -> str:
    """Normalize score to bias label."""
    if score >= 60:
        return "STRONG_BULLISH"
    if score >= 25:
        return "BULLISH"
    if score <= -60:
        return "STRONG_BEARISH"
    if score <= -25:
        return "BEARISH"
    return "NEUTRAL"


# ─── Per-component scoring functions ──────────────────────────────────
# Each returns (points, reasoning_string). points can be negative for bearish.

def score_swing_structure(swing_state_dict: dict) -> tuple[int, str]:
    """HH+HL = bullish, LH+LL = bearish, mixed = 0."""
    if not swing_state_dict:
        return 0, "no swing data"
    trend = swing_state_dict.get("trend", "SIDEWAYS")
    last_high_cls = swing_state_dict.get("last_high_class", "")
    last_low_cls = swing_state_dict.get("last_low_class", "")
    if trend == "UP":
        return WEIGHTS["swing_structure"], f"HH+HL trend ({last_high_cls}, {last_low_cls})"
    if trend == "DOWN":
        return -WEIGHTS["swing_structure"], f"LH+LL trend ({last_high_cls}, {last_low_cls})"
    return 0, f"mixed structure ({last_high_cls}, {last_low_cls})"


def score_bos_choch(swing_state_dict: dict) -> tuple[int, str]:
    """Recent BOS = continuation, CHoCH = reversal warning."""
    if not swing_state_dict:
        return 0, "no data"
    bos_dir = swing_state_dict.get("last_bos_direction", "")
    choch_dir = swing_state_dict.get("last_choch_direction", "")
    bos_ago = swing_state_dict.get("last_bos_ago_s", -1)
    choch_ago = swing_state_dict.get("last_choch_ago_s", -1)

    # Recent CHoCH (< 30 min) takes priority over BOS
    if choch_ago >= 0 and choch_ago < 1800 and choch_dir:
        if "UP" in choch_dir:
            return WEIGHTS["bos_choch"] // 2, f"CHoCH UP pending ({choch_ago:.0f}s ago)"
        if "DOWN" in choch_dir:
            return -WEIGHTS["bos_choch"] // 2, f"CHoCH DOWN pending ({choch_ago:.0f}s ago)"

    if bos_ago >= 0 and bos_ago < 1800 and bos_dir:
        if bos_dir == "UP":
            return WEIGHTS["bos_choch"], f"BOS UP ({bos_ago:.0f}s ago)"
        if bos_dir == "DOWN":
            return -WEIGHTS["bos_choch"], f"BOS DOWN ({bos_ago:.0f}s ago)"

    return 0, "no recent structural break"


def score_footprint_signals(footprint_signals: list) -> tuple[int, str]:
    """Sum contributions from each active footprint pattern."""
    if not footprint_signals:
        return 0, "no footprint signals"
    total = 0
    reasons = []
    for s in footprint_signals:
        pattern = s.get("pattern") if isinstance(s, dict) else getattr(s, "pattern", "")
        direction = s.get("direction") if isinstance(s, dict) else getattr(s, "direction", "")
        severity = s.get("severity", 0.5) if isinstance(s, dict) else getattr(s, "severity", 0.5)
        per_signal_max = WEIGHTS["footprint_signals"] // 3  # up to 3 signals max contribution
        points = int(severity * per_signal_max)
        if direction == "BEARISH":
            points = -points
        total += points
        reasons.append(f"{pattern} ({direction}, sev {severity:.1f})")
    # Clamp
    total = max(-WEIGHTS["footprint_signals"], min(WEIGHTS["footprint_signals"], total))
    return total, "; ".join(reasons[:3])


def score_chart_patterns(enriched_patterns: list) -> tuple[int, str]:
    """Use best pattern's context-weighted confidence."""
    if not enriched_patterns:
        return 0, "no patterns"
    best = enriched_patterns[0]  # Already sorted by confidence
    direction = best.direction if hasattr(best, "direction") else best.get("direction", "")
    confidence = best.confidence if hasattr(best, "confidence") else best.get("confidence", 0)
    pattern_name = best.pattern_name if hasattr(best, "pattern_name") else best.get("pattern_name", "")
    # Map confidence 0-100 to weight points
    points = int((confidence / 100.0) * WEIGHTS["chart_patterns"])
    if direction == "SHORT":
        points = -points
    return points, f"{pattern_name} conf {confidence:.0f}"


def score_candlestick(pattern_summary: dict) -> tuple[int, str]:
    """Summary from existing candlestick_patterns.py."""
    if not pattern_summary:
        return 0, "no candle patterns"
    bull = pattern_summary.get("bullish_count", 0)
    bear = pattern_summary.get("bearish_count", 0)
    net = bull - bear
    if net == 0:
        return 0, f"{bull}B / {bear}B balanced"
    # Map net to ±max (cap at 3)
    points = int((net / 3.0) * WEIGHTS["candlestick"])
    points = max(-WEIGHTS["candlestick"], min(WEIGHTS["candlestick"], points))
    return points, f"{bull}B / {bear}B net {net:+d}"


def score_volume_profile(vp_dict: dict, price: float) -> tuple[int, str]:
    """Price location in volume profile affects bias."""
    if not vp_dict:
        return 0, "no VP data"
    poc = vp_dict.get("poc")
    vah = vp_dict.get("vah")
    val = vp_dict.get("val")
    if not poc or not vah or not val:
        return 0, "incomplete VP"
    # Above VAH = bullish (in discovery zone), below VAL = bearish
    if price > vah:
        return WEIGHTS["volume_profile"] // 2, f"price {price:.2f} > VAH {vah:.2f} (discovery)"
    if price < val:
        return -(WEIGHTS["volume_profile"] // 2), f"price {price:.2f} < VAL {val:.2f} (discovery down)"
    # In VA — neutral
    return 0, f"price in VA {val:.2f}-{vah:.2f}"


def score_climax_reversal(climax_state: dict) -> tuple[int, str]:
    """Active climax warnings contribute direction points."""
    if not climax_state:
        return 0, "no climax state"
    warnings = climax_state.get("active_warnings", [])
    if not warnings:
        return 0, "no active climax"
    # Most recent warning
    w = warnings[0]
    direction = w.get("direction", "")
    # Climax warning alone is ~half weight (entry requires secondary test)
    half = WEIGHTS["climax_reversal"] // 2
    if direction == "BULLISH_REVERSAL":
        return half, f"selling climax pending {w.get('bars_ago', 0)} bars ago"
    if direction == "BEARISH_REVERSAL":
        return -half, f"buying climax pending {w.get('bars_ago', 0)} bars ago"
    return 0, "unknown climax direction"


def score_liquidity_sweep(sweep_state: dict) -> tuple[int, str]:
    """Recent sweep reclassifications."""
    if not sweep_state:
        return 0, "no sweep state"
    watches = sweep_state.get("watches", [])
    if not watches:
        return 0, "no active sweeps"
    # Sweep state is just pending watches. Active sweep EVENTS come from callbacks,
    # not state. So this just reports that sweeps may be developing.
    return 0, f"{len(watches)} pivot breaks being monitored"


def score_menthorq_gamma(mq_context: dict, price: float) -> tuple[int, str]:
    """Gamma regime + wall proximity."""
    if not mq_context:
        return 0, "no MQ data"
    regime = mq_context.get("gex_regime", "UNKNOWN")
    cr = mq_context.get("call_resistance_all", 0)
    ps = mq_context.get("put_support_all", 0)
    hvl = mq_context.get("hvl", 0)
    allow_longs = mq_context.get("allow_longs", True)
    allow_shorts = mq_context.get("allow_shorts", True)

    points = 0
    reasons = []

    # Above HVL = positive gamma zone → bullish bias
    if hvl > 0:
        if price > hvl:
            points += 5
            reasons.append(f"above HVL {hvl:.0f}")
        else:
            points -= 5
            reasons.append(f"below HVL {hvl:.0f}")

    # Near CR = bearish (resistance)
    if cr > 0:
        dist_ticks = abs(price - cr) / 0.25
        if dist_ticks < 20:
            points -= 5
            reasons.append(f"near CR {cr:.0f} ({dist_ticks:.0f}t)")

    # Near PS = bullish (support)
    if ps > 0:
        dist_ticks = abs(price - ps) / 0.25
        if dist_ticks < 20:
            points += 5
            reasons.append(f"near PS {ps:.0f} ({dist_ticks:.0f}t)")

    # Regime-specific bias
    if regime == "POSITIVE":
        reasons.append("POS GEX (mean-rev)")
    elif regime == "NEGATIVE":
        reasons.append("NEG GEX (trend-amp)")

    points = max(-WEIGHTS["menthorq_gamma"], min(WEIGHTS["menthorq_gamma"], points))
    return points, "; ".join(reasons) if reasons else "neutral MQ context"


def score_gamma_flip(flip_state: dict) -> tuple[int, str]:
    """Active gamma flip event = strong directional signal."""
    if not flip_state:
        return 0, "no flip data"
    last = flip_state.get("last_flip")
    if not last:
        return 0, "no recent flip"
    direction = last.get("direction", "")
    # Time decay: flip from > 30 min ago contributes less
    ts_str = last.get("ts", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        age_min = (datetime.now() - ts).total_seconds() / 60
    except Exception:
        age_min = 999
    if age_min > 60:
        return 0, "flip stale"
    decay = max(0.3, 1 - age_min / 60)
    full = int(WEIGHTS["gamma_flip"] * decay)
    if direction == "NEG_TO_POS":
        return full, f"gamma flipped POS {age_min:.0f}m ago (bullish regime)"
    if direction == "POS_TO_NEG":
        return -full, f"gamma flipped NEG {age_min:.0f}m ago (bearish regime)"
    return 0, "unknown flip direction"


def score_vix_regime(vix_state: dict) -> tuple[int, str]:
    """VIX term structure contributes small bias adjustment."""
    if not vix_state:
        return 0, "no VIX data"
    regime = vix_state.get("regime", "UNKNOWN")
    if regime == "STEEP_BACKWARDATION":
        # Acute fear = tradeable bottom setups favored → slight bullish tilt to reversal setups
        return WEIGHTS["vix_regime"] // 2, "VIX backwardation (acute fear)"
    if regime == "STEEP_CONTANGO":
        # Complacent = trend risk-on continuation
        return 0, "VIX steep contango (complacent)"
    return 0, f"VIX {regime}"


def score_es_confirmation(es_state: dict) -> tuple[int, str]:
    """ES confluence/divergence."""
    if not es_state:
        return 0, "no ES data"
    adjust = es_state.get("confluence_adjust", 0)
    if adjust == 0:
        return 0, "ES data unavailable"
    reasoning = es_state.get("reasoning", [""])[0] if es_state.get("reasoning") else ""
    return adjust, reasoning


# ─── Main entry: compute composite bias ───────────────────────────────

def compute_structural_bias(market_snapshot: dict) -> StructuralBias:
    """
    Compute composite bias from enriched market snapshot.

    market_snapshot expected keys:
      close                 (price)
      swing_state           dict from swing_detector.SwingState.to_dict()
      footprint_signals     list of dict/FootprintSignal
      chart_patterns_v1     list of EnrichedPattern
      candlestick_summary   dict with bullish_count, bearish_count
      volume_profile        dict from VolumeProfile.to_dict()
      climax_state          dict from ReversalDetector.get_state()
      sweep_state           dict from SweepWatcher.get_state()
      menthorq              dict from _menthorq_to_dict()
      gamma_flip_state      dict from GammaFlipDetector.get_state()
      vix_term_structure    dict from VIXTermStructure.to_dict()
      pinning_state         dict from PinningDetector.update()
      opex_status           dict from get_opex_status()
      es_confirmation       dict from check_confirmation()
    """
    price = float(market_snapshot.get("close", 0))
    components: list[BiasComponent] = []
    vetoes: list[str] = []

    # Score each component
    score_fns = [
        ("swing_structure", WEIGHTS["swing_structure"],
         lambda: score_swing_structure(market_snapshot.get("swing_state", {}))),
        ("bos_choch", WEIGHTS["bos_choch"],
         lambda: score_bos_choch(market_snapshot.get("swing_state", {}))),
        ("footprint_signals", WEIGHTS["footprint_signals"],
         lambda: score_footprint_signals(market_snapshot.get("footprint_signals", []))),
        ("chart_patterns", WEIGHTS["chart_patterns"],
         lambda: score_chart_patterns(market_snapshot.get("chart_patterns_v1", []))),
        ("candlestick", WEIGHTS["candlestick"],
         lambda: score_candlestick(market_snapshot.get("candlestick_summary", {}))),
        ("volume_profile", WEIGHTS["volume_profile"],
         lambda: score_volume_profile(market_snapshot.get("volume_profile", {}), price)),
        ("climax_reversal", WEIGHTS["climax_reversal"],
         lambda: score_climax_reversal(market_snapshot.get("climax_state", {}))),
        ("liquidity_sweep", WEIGHTS["liquidity_sweep"],
         lambda: score_liquidity_sweep(market_snapshot.get("sweep_state", {}))),
        ("menthorq_gamma", WEIGHTS["menthorq_gamma"],
         lambda: score_menthorq_gamma(market_snapshot.get("menthorq", {}), price)),
        ("gamma_flip", WEIGHTS["gamma_flip"],
         lambda: score_gamma_flip(market_snapshot.get("gamma_flip_state", {}))),
        ("vix_regime", WEIGHTS["vix_regime"],
         lambda: score_vix_regime(market_snapshot.get("vix_term_structure", {}))),
        ("es_confirmation", WEIGHTS["es_confirmation"],
         lambda: score_es_confirmation(market_snapshot.get("es_confirmation", {}))),
    ]

    raw_score = 0
    for name, max_pts, fn in score_fns:
        try:
            points, reason = fn()
        except Exception as e:
            logger.debug(f"component {name} failed: {e}")
            points, reason = 0, f"scoring error: {type(e).__name__}"
        components.append(BiasComponent(
            name=name, points=points, max_points=max_pts, reasoning=reason
        ))
        raw_score += points

    # Vetoes (non-scoring conditions that flag the bias)
    pin = market_snapshot.get("pinning_state", {})
    if isinstance(pin, dict) and pin.get("pin_risk_active"):
        vetoes.append(f"PIN_RISK: {pin.get('pin_level_name', '?')} @ {pin.get('pinning_level', 0):.2f}")

    opex = market_snapshot.get("opex_status", {})
    if isinstance(opex, dict) and opex.get("veto_continuation_patterns"):
        vetoes.append("OPEX_LAST_HOUR: continuation patterns vetoed")

    mq = market_snapshot.get("menthorq", {})
    if isinstance(mq, dict):
        if mq.get("age_hours", 0) > 24:
            vetoes.append("MQ_STALE: gamma context unreliable")
        if not mq.get("allow_longs", True) and not mq.get("allow_shorts", True):
            vetoes.append("MQ: both directions disallowed")

    # Normalize score to -100..+100
    max_possible = sum(WEIGHTS.values())
    score = int(100 * raw_score / max_possible) if max_possible else 0
    score = max(-100, min(100, score))

    # Confidence = how much evidence contributed (non-zero components) × magnitude
    active_components = sum(1 for c in components if c.points != 0)
    evidence_coverage = active_components / len(components) if components else 0
    confidence = int(abs(score) * (0.3 + 0.7 * evidence_coverage))

    label = _label_from_score(score)

    return StructuralBias(
        label=label, score=score, raw_score=raw_score,
        max_possible=max_possible, confidence=confidence,
        components=components, vetoes=vetoes,
        timestamp=datetime.now(),
    )
