"""
Phoenix Bot — MenthorQ Q-Score Integration

Loads and applies MenthorQ's proprietary Q-Score to strategy evaluation.

THE Q-SCORE (from MenthorQ's official guide):
  Four factors updated daily at end of trading day:
    - Momentum:    0-5 (0=bearish, 3=neutral, 5=bullish)
    - Options:     0-5 (options flow sentiment, 0=bearish, 5=bullish)
    - Seasonality: -5 to +5 (20-year historical pattern for next 5 days)
    - Volatility:  0-5 (realized vol regime, 0=calm, 5=wild)

USAGE PHILOSOPHY (from MenthorQ):
  "The Q-Score is not a crystal ball. It's about knowing when not to fight
  the conditions... It's a filter, not a forecast."

BOT APPLICATION:
  1. Load Q-Score at bot startup and after each daily refresh
  2. Compute composite directional bias
  3. Use as TRADE FILTER (skip poor-alignment setups)
  4. Use as SIZE MULTIPLIER (scale up high-conviction setups)
  5. Use as STOP/TARGET ADJUSTER (widen on high vol, tighten on low vol)

EXPECTED DATA FORMAT:
  JSON file (MenthorQ manual export or TradingView webhook dump):
  {
    "symbol": "NQ",
    "date": "2026-04-19",
    "momentum": 4,
    "options": 3,
    "seasonality": 2,
    "volatility": 2
  }

USAGE:
    from core.qscore import QScoreManager

    qsc = QScoreManager(data_path="data/qscore/nq_daily.json")
    qsc.reload()  # Call at session start and after midnight

    eval_result = qsc.evaluate_for_trade(direction="LONG")
    if not eval_result.allow_trade:
        return None  # Q-Score says skip this one
    size_mult = eval_result.size_multiplier
    stop_mult = eval_result.stop_distance_multiplier
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from pathlib import Path
from typing import Optional


class QScoreBias(Enum):
    STRONG_BULLISH = "strong_bullish"     # Both M and O >= 4
    BULLISH = "bullish"                   # M and O aligned positive
    NEUTRAL = "neutral"                   # Mixed or middle values
    BEARISH = "bearish"                   # M and O aligned negative
    STRONG_BEARISH = "strong_bearish"     # Both M and O <= 1
    CONFLICTED = "conflicted"             # M and O disagree strongly


class VolatilityRegime(Enum):
    LOW = "low"           # 0-1: calm, potential breakout setup
    NORMAL = "normal"     # 2-3: standard conditions
    HIGH = "high"         # 4-5: wider stops, reduced size


@dataclass
class QScoreSnapshot:
    """Raw Q-Score data from MenthorQ."""
    symbol: str
    date: date
    momentum: int         # 0-5
    options: int          # 0-5
    seasonality: int      # -5 to +5
    volatility: int       # 0-5
    loaded_at: datetime = field(default_factory=datetime.utcnow)

    def is_stale(self, today: date) -> bool:
        """Q-Score is stale if it's older than today."""
        return self.date < today


@dataclass
class QScoreEvaluation:
    """Result of evaluating Q-Score for a proposed trade."""
    allow_trade: bool
    size_multiplier: float          # 0.0 to 1.5
    stop_distance_multiplier: float # 0.8 to 1.3
    target_rr_multiplier: float     # 0.8 to 1.3
    bias: QScoreBias
    vol_regime: VolatilityRegime
    composite_score: float          # -10 to +10
    reason: str


class QScoreManager:
    """
    Manages MenthorQ Q-Score loading, interpretation, and application.

    Thread-safety note: reload() should be called from the main bot loop
    only, not from strategy evaluation threads.
    """

    # Thresholds (configurable)
    SKIP_COMPOSITE_THRESHOLD = -3.0   # Skip trade if composite < this
    CONFLICTED_MO_GAP = 3             # |momentum - options| >= this = conflicted

    # Size/stop multipliers
    STRONG_ALIGNMENT_SIZE_BOOST = 1.25
    WEAK_ALIGNMENT_SIZE_REDUCTION = 0.7
    HIGH_VOL_SIZE_REDUCTION = 0.7
    HIGH_VOL_STOP_WIDEN = 1.3
    LOW_VOL_STOP_TIGHTEN = 0.9

    def __init__(self, data_path: str):
        self.data_path = Path(data_path)
        self.snapshot: Optional[QScoreSnapshot] = None

    # ─── DATA LOADING ──────────────────────────────────────────────────

    def reload(self) -> bool:
        """
        Reload Q-Score from disk. Returns True if loaded successfully.

        The expected format is JSON, but if MenthorQ provides a different
        format (CSV, TradingView alert payload), extend this method.
        """
        if not self.data_path.exists():
            return False

        try:
            with open(self.data_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return False

        try:
            # Parse date string
            date_str = data.get("date", "")
            parsed_date = datetime.fromisoformat(date_str).date() if date_str else None

            if parsed_date is None:
                return False

            # Validate and clamp scores
            momentum = self._clamp_int(data.get("momentum"), 0, 5)
            options = self._clamp_int(data.get("options"), 0, 5)
            seasonality = self._clamp_int(data.get("seasonality"), -5, 5)
            volatility = self._clamp_int(data.get("volatility"), 0, 5)

            self.snapshot = QScoreSnapshot(
                symbol=data.get("symbol", "UNKNOWN"),
                date=parsed_date,
                momentum=momentum,
                options=options,
                seasonality=seasonality,
                volatility=volatility,
            )
            return True
        except (ValueError, TypeError):
            return False

    def _clamp_int(self, value, min_val: int, max_val: int) -> int:
        """Clamp and convert value to int in range."""
        if value is None:
            raise ValueError("Missing score value")
        v = int(value)
        return max(min_val, min(max_val, v))

    # ─── EVALUATION ────────────────────────────────────────────────────

    def evaluate_for_trade(
        self,
        direction: str,           # "LONG" or "SHORT"
        today: Optional[date] = None,
    ) -> QScoreEvaluation:
        """
        Evaluate Q-Score for a proposed trade direction.

        Returns a QScoreEvaluation with all the multipliers and filters
        the strategy should apply.
        """
        today = today or date.today()
        direction = direction.upper()

        # If no Q-Score loaded, return neutral pass-through
        if self.snapshot is None:
            return QScoreEvaluation(
                allow_trade=True,
                size_multiplier=1.0,
                stop_distance_multiplier=1.0,
                target_rr_multiplier=1.0,
                bias=QScoreBias.NEUTRAL,
                vol_regime=VolatilityRegime.NORMAL,
                composite_score=0.0,
                reason="No Q-Score loaded; neutral pass-through",
            )

        # If Q-Score is stale (we didn't reload today), warn but still use
        stale_warning = ""
        if self.snapshot.is_stale(today):
            stale_warning = f" [STALE: Q-Score from {self.snapshot.date}]"

        # Classify bias
        bias = self._classify_bias(self.snapshot.momentum, self.snapshot.options)

        # Classify volatility regime
        vol_regime = self._classify_vol(self.snapshot.volatility)

        # Compute composite score (directional: positive = bullish)
        composite = self._compute_composite(self.snapshot)

        # Determine direction alignment
        aligned_with_direction = self._check_alignment(direction, composite, bias)

        # Apply rules to derive multipliers
        return self._apply_rules(
            direction=direction,
            bias=bias,
            vol_regime=vol_regime,
            composite=composite,
            aligned=aligned_with_direction,
            stale_warning=stale_warning,
        )

    # ─── CLASSIFICATION ────────────────────────────────────────────────

    def _classify_bias(self, momentum: int, options: int) -> QScoreBias:
        """Classify directional bias from Momentum + Options scores."""
        gap = abs(momentum - options)

        # Conflicted if M and O disagree strongly
        if gap >= self.CONFLICTED_MO_GAP:
            if momentum <= 1 or options <= 1:
                if momentum >= 4 or options >= 4:
                    return QScoreBias.CONFLICTED

        # Strong alignment
        if momentum >= 4 and options >= 4:
            return QScoreBias.STRONG_BULLISH
        if momentum <= 1 and options <= 1:
            return QScoreBias.STRONG_BEARISH

        # Regular alignment
        if momentum >= 3 and options >= 3:
            return QScoreBias.BULLISH
        if momentum <= 2 and options <= 2:
            return QScoreBias.BEARISH

        return QScoreBias.NEUTRAL

    def _classify_vol(self, volatility: int) -> VolatilityRegime:
        if volatility <= 1:
            return VolatilityRegime.LOW
        if volatility >= 4:
            return VolatilityRegime.HIGH
        return VolatilityRegime.NORMAL

    def _compute_composite(self, snap: QScoreSnapshot) -> float:
        """
        Compute composite directional score (-10 to +10, positive = bullish).

        Weights:
          - Momentum:    center on 3, multiply by 1.5
          - Options:     center on 3, multiply by 1.5
          - Seasonality: already centered on 0, multiply by 0.5
          - Volatility:  not directional, excluded

        Example: M=5, O=5, S=3 → (5-3)*1.5 + (5-3)*1.5 + 3*0.5 = 3 + 3 + 1.5 = 7.5
        """
        momentum_contribution = (snap.momentum - 3) * 1.5
        options_contribution = (snap.options - 3) * 1.5
        seasonality_contribution = snap.seasonality * 0.5
        return momentum_contribution + options_contribution + seasonality_contribution

    def _check_alignment(
        self,
        direction: str,
        composite: float,
        bias: QScoreBias,
    ) -> bool:
        """Is the proposed trade direction aligned with Q-Score bias?"""
        if direction == "LONG":
            return composite > 0 and bias != QScoreBias.BEARISH and bias != QScoreBias.STRONG_BEARISH
        if direction == "SHORT":
            return composite < 0 and bias != QScoreBias.BULLISH and bias != QScoreBias.STRONG_BULLISH
        return False

    # ─── RULES APPLICATION ─────────────────────────────────────────────

    def _apply_rules(
        self,
        direction: str,
        bias: QScoreBias,
        vol_regime: VolatilityRegime,
        composite: float,
        aligned: bool,
        stale_warning: str,
    ) -> QScoreEvaluation:
        """Apply all rules to produce final evaluation."""

        allow_trade = True
        size_mult = 1.0
        stop_mult = 1.0
        target_rr_mult = 1.0
        reasons = []

        # RULE 1: Conflicted bias blocks trades entirely
        if bias == QScoreBias.CONFLICTED:
            allow_trade = False
            reasons.append("Conflicted Q-Score (M/O disagree): SKIP")

        # RULE 2: Counter-trend trades get reduced size or skipped
        if not aligned:
            if direction == "LONG" and bias in (QScoreBias.STRONG_BEARISH,):
                allow_trade = False
                reasons.append("LONG rejected: Q-Score strong bearish")
            elif direction == "SHORT" and bias in (QScoreBias.STRONG_BULLISH,):
                allow_trade = False
                reasons.append("SHORT rejected: Q-Score strong bullish")
            else:
                size_mult *= self.WEAK_ALIGNMENT_SIZE_REDUCTION
                reasons.append(f"Counter-Q-Score trade: size -{int((1 - self.WEAK_ALIGNMENT_SIZE_REDUCTION) * 100)}%")

        # RULE 3: Strong alignment → size boost
        if aligned:
            if direction == "LONG" and bias == QScoreBias.STRONG_BULLISH:
                size_mult *= self.STRONG_ALIGNMENT_SIZE_BOOST
                reasons.append(f"Strong bullish alignment: size +{int((self.STRONG_ALIGNMENT_SIZE_BOOST - 1) * 100)}%")
            elif direction == "SHORT" and bias == QScoreBias.STRONG_BEARISH:
                size_mult *= self.STRONG_ALIGNMENT_SIZE_BOOST
                reasons.append(f"Strong bearish alignment: size +{int((self.STRONG_ALIGNMENT_SIZE_BOOST - 1) * 100)}%")
            else:
                reasons.append(f"Aligned with {bias.value}: normal size")

        # RULE 4: Volatility regime adjustments (always applies)
        if vol_regime == VolatilityRegime.HIGH:
            size_mult *= self.HIGH_VOL_SIZE_REDUCTION
            stop_mult *= self.HIGH_VOL_STOP_WIDEN
            target_rr_mult *= self.HIGH_VOL_STOP_WIDEN  # Wider target to match wider stop
            reasons.append(
                f"High vol regime: size -{int((1 - self.HIGH_VOL_SIZE_REDUCTION) * 100)}%, "
                f"stops +{int((self.HIGH_VOL_STOP_WIDEN - 1) * 100)}%"
            )
        elif vol_regime == VolatilityRegime.LOW:
            stop_mult *= self.LOW_VOL_STOP_TIGHTEN
            reasons.append(f"Low vol regime: stops -{int((1 - self.LOW_VOL_STOP_TIGHTEN) * 100)}%")

        # RULE 5: Composite threshold
        if composite < self.SKIP_COMPOSITE_THRESHOLD and direction == "LONG":
            allow_trade = False
            reasons.append(f"Composite {composite:.1f} too bearish for LONG")
        if composite > -self.SKIP_COMPOSITE_THRESHOLD and direction == "SHORT":
            allow_trade = False
            reasons.append(f"Composite {composite:.1f} too bullish for SHORT")

        # Add stale warning if applicable
        if stale_warning:
            reasons.append(stale_warning)

        reason_str = " | ".join(reasons) if reasons else "Q-Score neutral"

        return QScoreEvaluation(
            allow_trade=allow_trade,
            size_multiplier=round(size_mult, 2),
            stop_distance_multiplier=round(stop_mult, 2),
            target_rr_multiplier=round(target_rr_mult, 2),
            bias=bias,
            vol_regime=vol_regime,
            composite_score=round(composite, 2),
            reason=reason_str,
        )

    # ─── DASHBOARD SUPPORT ─────────────────────────────────────────────

    def snapshot_dict(self) -> dict:
        """Return state for dashboard display."""
        if self.snapshot is None:
            return {"loaded": False, "message": "No Q-Score data loaded"}

        return {
            "loaded": True,
            "symbol": self.snapshot.symbol,
            "date": self.snapshot.date.isoformat(),
            "momentum": self.snapshot.momentum,
            "options": self.snapshot.options,
            "seasonality": self.snapshot.seasonality,
            "volatility": self.snapshot.volatility,
            "composite": round(self._compute_composite(self.snapshot), 2),
            "bias": self._classify_bias(
                self.snapshot.momentum, self.snapshot.options
            ).value,
            "vol_regime": self._classify_vol(self.snapshot.volatility).value,
            "loaded_at": self.snapshot.loaded_at.isoformat(),
        }
