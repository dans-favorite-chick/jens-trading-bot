"""
Phoenix Bot — Intermarket Regime Filter

Adds VIX/VXN volatility regime detection AND ES (S&P 500) confirmation
filter to MNQ trade evaluation.

WHY THIS MATTERS:
  - VIX/VXN: research-validated regime filter (MindMath, Volatility Box,
    International Trading Institute). High VIX → reduce size / stand down.
  - ES correlation: NQ:ES correlation is 0.85-0.90. Intraday divergence
    is a strong tradeable signal — confirm broad-market alignment before
    trusting an MNQ signal.

WHAT THIS DOESN'T DO (intentionally):
  - DXY, BTC, Gold, Crude: too noisy or too distant for intraday MNQ.
    These are better-suited to swing/position trading frameworks.
  - Doesn't replace the Q-Score Volatility factor (Q-Score is daily;
    this is intraday, complementary).

DATA REQUIREMENTS:
  - VIX or VXN ticks (VXN preferred for NQ-specific reading)
  - ES tick stream (continuous front-month)
  - Both should be added as additional NinjaTrader data subscriptions
    and routed through the bridge alongside MNQ.

USAGE:
    from core.intermarket import IntermarketFilter

    im = IntermarketFilter()

    # On every tick from the bridge:
    if tick.symbol == "VIX" or tick.symbol == "VXN":
        im.update_vix(tick.price, tick.timestamp)
    elif tick.symbol == "ES":
        im.update_es(tick.price, tick.timestamp)

    # Before each strategy evaluation:
    eval = im.evaluate_for_trade(direction="LONG", current_nq_price=22150.0)
    if not eval.allow_trade:
        return None  # Skip — intermarket says no

    final_size = base_size * eval.size_multiplier
"""

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VolatilityRegime(Enum):
    CALM = "calm"            # VIX < 15
    NORMAL = "normal"        # VIX 15-20
    ELEVATED = "elevated"    # VIX 20-25
    HIGH = "high"            # VIX 25-30
    EXTREME = "extreme"      # VIX > 30


class ESAlignment(Enum):
    STRONG_BULL = "strong_bull"      # ES strongly up, supports longs
    BULL = "bull"                    # ES up
    NEUTRAL = "neutral"              # ES flat
    BEAR = "bear"                    # ES down
    STRONG_BEAR = "strong_bear"      # ES strongly down, supports shorts
    DIVERGING_BULL = "diverging_bull"  # NQ up, ES flat/down
    DIVERGING_BEAR = "diverging_bear"  # NQ down, ES flat/up


@dataclass
class IntermarketEvaluation:
    allow_trade: bool
    size_multiplier: float
    stop_distance_multiplier: float
    vix_regime: VolatilityRegime
    es_alignment: ESAlignment
    vix_value: float
    vix_intraday_change_pct: float
    in_vix_spike_pause: bool  # True for 15 min after big VIX spike
    reason: str


@dataclass
class _VIXState:
    current: float = 0.0
    last_update: float = 0.0
    session_open: float = 0.0      # First reading of the day
    history: deque = field(default_factory=lambda: deque(maxlen=120))  # Last 30min @ 15s
    spike_pause_until: float = 0.0  # Timestamp


@dataclass
class _ESState:
    current: float = 0.0
    last_update: float = 0.0
    session_open: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=120))


class IntermarketFilter:
    """
    Tracks VIX/VXN and ES state, evaluates intermarket alignment for trades.
    """

    # VIX regime thresholds
    VIX_CALM_MAX = 15.0
    VIX_NORMAL_MAX = 20.0
    VIX_ELEVATED_MAX = 25.0
    VIX_HIGH_MAX = 30.0

    # VIX spike detection
    VIX_SPIKE_PCT_30MIN = 10.0          # +10% in 30min = spike
    VIX_SPIKE_PAUSE_MINUTES = 15        # Pause new entries for 15 min after spike

    # ES alignment thresholds (intraday % move from session open)
    ES_STRONG_THRESHOLD = 0.5           # > 0.5% move = strong direction

    # Size/stop multipliers per VIX regime
    VIX_RULES = {
        VolatilityRegime.CALM:     {"size": 1.10, "stop": 0.90, "allow": True},
        VolatilityRegime.NORMAL:   {"size": 1.00, "stop": 1.00, "allow": True},
        VolatilityRegime.ELEVATED: {"size": 0.70, "stop": 1.20, "allow": True},
        VolatilityRegime.HIGH:     {"size": 0.50, "stop": 1.30, "allow": True},
        VolatilityRegime.EXTREME:  {"size": 0.00, "stop": 1.50, "allow": False},
    }

    def __init__(self):
        self.vix = _VIXState()
        self.es = _ESState()
        self._current_session_date = None

    # ─── DATA UPDATES (called from bridge) ─────────────────────────────

    def update_vix(self, price: float, timestamp: float = None) -> None:
        """Update VIX/VXN reading."""
        if price <= 0:
            return

        ts = timestamp or time.time()
        self._reset_session_if_needed(ts)

        # Capture session open
        if self.vix.session_open <= 0:
            self.vix.session_open = price

        # Detect intraday spike before updating
        self._detect_vix_spike(price, ts)

        self.vix.current = price
        self.vix.last_update = ts
        self.vix.history.append((ts, price))

    def update_es(self, price: float, timestamp: float = None) -> None:
        """Update ES (S&P 500 e-mini) reading."""
        if price <= 0:
            return

        ts = timestamp or time.time()
        self._reset_session_if_needed(ts)

        if self.es.session_open <= 0:
            self.es.session_open = price

        self.es.current = price
        self.es.last_update = ts
        self.es.history.append((ts, price))

    def _reset_session_if_needed(self, ts: float) -> None:
        """Reset session open at start of new trading day."""
        from datetime import datetime, timezone
        try:
            current_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (ValueError, OSError):
            return
        if self._current_session_date != current_date:
            self._current_session_date = current_date
            self.vix.session_open = 0.0
            self.es.session_open = 0.0
            self.vix.history.clear()
            self.es.history.clear()

    # ─── VIX SPIKE DETECTION ──────────────────────────────────────────

    def _detect_vix_spike(self, new_price: float, ts: float) -> None:
        """Detect a sudden VIX spike (>10% in 30 min) and start pause."""
        if not self.vix.history:
            return

        # Look for the oldest reading within the last 30 min
        cutoff = ts - (30 * 60)
        old_readings = [p for (t, p) in self.vix.history if t >= cutoff]
        if not old_readings:
            return

        old_min = min(old_readings)
        if old_min <= 0:
            return

        pct_change = (new_price - old_min) / old_min * 100

        if pct_change >= self.VIX_SPIKE_PCT_30MIN:
            self.vix.spike_pause_until = ts + (self.VIX_SPIKE_PAUSE_MINUTES * 60)

    # ─── REGIME CLASSIFICATION ─────────────────────────────────────────

    def classify_vix_regime(self) -> VolatilityRegime:
        v = self.vix.current
        if v <= 0:
            return VolatilityRegime.NORMAL  # No data → assume normal
        if v < self.VIX_CALM_MAX:
            return VolatilityRegime.CALM
        if v < self.VIX_NORMAL_MAX:
            return VolatilityRegime.NORMAL
        if v < self.VIX_ELEVATED_MAX:
            return VolatilityRegime.ELEVATED
        if v < self.VIX_HIGH_MAX:
            return VolatilityRegime.HIGH
        return VolatilityRegime.EXTREME

    def classify_es_alignment(
        self,
        nq_price: float,
        nq_session_open: Optional[float] = None,
    ) -> ESAlignment:
        """
        Classify ES alignment relative to NQ.
        If nq_session_open provided, can detect divergence.
        """
        if self.es.current <= 0 or self.es.session_open <= 0:
            return ESAlignment.NEUTRAL

        es_pct = (self.es.current - self.es.session_open) / self.es.session_open * 100

        # Pure ES direction
        if es_pct > self.ES_STRONG_THRESHOLD:
            base = ESAlignment.STRONG_BULL
        elif es_pct > 0.1:
            base = ESAlignment.BULL
        elif es_pct < -self.ES_STRONG_THRESHOLD:
            base = ESAlignment.STRONG_BEAR
        elif es_pct < -0.1:
            base = ESAlignment.BEAR
        else:
            base = ESAlignment.NEUTRAL

        # Divergence detection (requires NQ session open)
        if nq_session_open is not None and nq_session_open > 0:
            nq_pct = (nq_price - nq_session_open) / nq_session_open * 100
            # NQ up but ES flat/down = bull divergence (often fades)
            if nq_pct > 0.3 and es_pct < 0.1:
                return ESAlignment.DIVERGING_BULL
            # NQ down but ES flat/up = bear divergence (often fades)
            if nq_pct < -0.3 and es_pct > -0.1:
                return ESAlignment.DIVERGING_BEAR

        return base

    # ─── EVALUATION ────────────────────────────────────────────────────

    def evaluate_for_trade(
        self,
        direction: str,
        current_nq_price: float,
        nq_session_open: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> IntermarketEvaluation:
        """Evaluate intermarket conditions for a proposed trade."""
        ts = timestamp or time.time()
        direction = direction.upper()

        # 1. Classify VIX regime
        vix_regime = self.classify_vix_regime()
        vix_rules = self.VIX_RULES[vix_regime]

        # 2. Compute VIX intraday change
        vix_change_pct = 0.0
        if self.vix.session_open > 0 and self.vix.current > 0:
            vix_change_pct = (
                (self.vix.current - self.vix.session_open)
                / self.vix.session_open * 100
            )

        # 3. Check VIX spike pause
        in_spike_pause = ts < self.vix.spike_pause_until
        spike_remaining = max(0, self.vix.spike_pause_until - ts) / 60

        # 4. Classify ES alignment
        es_alignment = self.classify_es_alignment(current_nq_price, nq_session_open)

        # 5. Apply rules
        allow_trade = True
        size_mult = vix_rules["size"]
        stop_mult = vix_rules["stop"]
        reasons = [f"VIX={self.vix.current:.1f} ({vix_regime.value})"]

        # RULE: VIX regime allow
        if not vix_rules["allow"]:
            allow_trade = False
            reasons.append(f"VIX EXTREME: stand down")

        # RULE: VIX spike pause
        if in_spike_pause:
            allow_trade = False
            reasons.append(f"VIX spike pause active ({spike_remaining:.0f} min remaining)")

        # RULE: ES alignment
        if direction == "LONG":
            if es_alignment in (ESAlignment.STRONG_BEAR, ESAlignment.BEAR):
                size_mult *= 0.7
                reasons.append(f"ES bearish ({es_alignment.value}): -30% size for LONG")
            elif es_alignment == ESAlignment.DIVERGING_BULL:
                size_mult *= 0.6
                reasons.append("ES diverging from NQ (often fades): -40% size")
            elif es_alignment in (ESAlignment.STRONG_BULL, ESAlignment.BULL):
                reasons.append(f"ES aligned bullish ({es_alignment.value})")

        elif direction == "SHORT":
            if es_alignment in (ESAlignment.STRONG_BULL, ESAlignment.BULL):
                size_mult *= 0.7
                reasons.append(f"ES bullish ({es_alignment.value}): -30% size for SHORT")
            elif es_alignment == ESAlignment.DIVERGING_BEAR:
                size_mult *= 0.6
                reasons.append("NQ diverging from ES (often fades): -40% size")
            elif es_alignment in (ESAlignment.STRONG_BEAR, ESAlignment.BEAR):
                reasons.append(f"ES aligned bearish ({es_alignment.value})")

        # RULE: VIX direction confirmation
        # Rising VIX during a long = bearish wind, reduce
        if direction == "LONG" and vix_change_pct > 5:
            size_mult *= 0.85
            reasons.append(f"VIX +{vix_change_pct:.1f}% on day: -15% size for LONG")
        if direction == "SHORT" and vix_change_pct < -5:
            size_mult *= 0.85
            reasons.append(f"VIX {vix_change_pct:.1f}% on day: -15% size for SHORT")

        return IntermarketEvaluation(
            allow_trade=allow_trade,
            size_multiplier=round(size_mult, 2),
            stop_distance_multiplier=round(stop_mult, 2),
            vix_regime=vix_regime,
            es_alignment=es_alignment,
            vix_value=round(self.vix.current, 2),
            vix_intraday_change_pct=round(vix_change_pct, 2),
            in_vix_spike_pause=in_spike_pause,
            reason=" | ".join(reasons),
        )

    # ─── DASHBOARD SUPPORT ─────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return state for dashboard display."""
        regime = self.classify_vix_regime()
        es_align = self.classify_es_alignment(0, 0) if self.es.current > 0 else ESAlignment.NEUTRAL

        es_pct = 0.0
        if self.es.session_open > 0 and self.es.current > 0:
            es_pct = (self.es.current - self.es.session_open) / self.es.session_open * 100

        vix_pct = 0.0
        if self.vix.session_open > 0 and self.vix.current > 0:
            vix_pct = (self.vix.current - self.vix.session_open) / self.vix.session_open * 100

        return {
            "vix_value": round(self.vix.current, 2),
            "vix_regime": regime.value,
            "vix_change_pct": round(vix_pct, 2),
            "vix_spike_pause_active": time.time() < self.vix.spike_pause_until,
            "es_value": round(self.es.current, 2),
            "es_change_pct": round(es_pct, 2),
            "es_alignment": es_align.value,
            "size_multiplier": self.VIX_RULES[regime]["size"],
            "stop_multiplier": self.VIX_RULES[regime]["stop"],
            "allow_trades": self.VIX_RULES[regime]["allow"],
        }
