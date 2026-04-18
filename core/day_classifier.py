"""
Phoenix Bot — Day Type Classifier

Classifies each trading day as TREND, RANGE, or VOLATILE based on:
  - C/R verdict + momentum score (CONTINUATION | REVERSAL | CONTESTED)
  - ATR regime (measures volatility vs normal)
  - VIX level

Day type drives the entire strategy posture for the session:

  TREND    — Low-to-normal volatility, strong directional conviction.
             "Today's April 14 chart." Ride it, don't fade it.
             Spacing: 5 min | Target: 2.5:1 | Rider: ON | Size: 100%

  RANGE    — Contested market. Price oscillates around VWAP.
             Mean-revert the extremes. Quick exits. No runners.
             Spacing: 12 min | Target: 1.5:1 | Rider: OFF | Size: 80%

  VOLATILE — ATR spiking or VIX extreme. Chop city.
             Survive. Very selective. Small size. Stop early.
             Spacing: 20 min | Target: 1.5:1 | Rider: OFF | Size: 50%

  UNKNOWN  — Not enough data yet (first 15 min of session).
             Use conservative RANGE defaults until classified.

Used by base_bot to dynamically adjust:
  - Trade spacing (risk_manager)
  - Strategy target_rr
  - Scale-out / trend rider on/off
  - Size multiplier (feeds into position scaler)
  - Preferred strategy set
"""

import logging
from dataclasses import dataclass

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    VIX_NORMAL, VIX_HIGH,
    MIN_TRADE_SPACING_MIN,
)

# ATR thresholds in POINTS (atr_5m from snapshot() is in price points, not ticks).
# MNQ typical ATR_5m: 4-8pt quiet, 8-15pt normal open, 15-25pt elevated, 25pt+ extreme.
# These are separate from config.settings ATR_* which are in ticks (for stop sizing).
_ATR_PT_QUIET    =  8   # Below: very calm, tight ranges
_ATR_PT_NORMAL   = 15   # 8-15pt: standard open-hour volatility
_ATR_PT_HIGH     = 25   # 15-25pt: elevated — still tradeable with care (was 22, bug fix)
_ATR_PT_EXTREME  = 30   # Above: choppy/dangerous — VOLATILE day (was 22, matched HIGH — bug)

logger = logging.getLogger("DayClassifier")

# ── Day types ──────────────────────────────────────────────────────────────────
TREND    = "TREND"
RANGE    = "RANGE"
VOLATILE = "VOLATILE"
UNKNOWN  = "UNKNOWN"


# ── Parameter sets per day type ────────────────────────────────────────────────
DAY_PARAMS = {
    TREND: {
        # Strong directional day — maximize capture
        "trade_spacing_min":  5,      # Catch pullback re-entries quickly
        "default_target_rr":  2.5,    # Bigger targets — let it run
        "scale_out_enabled":  True,   # Always scale and ride the runner
        "trend_rider_enabled": True,
        "size_multiplier":    1.0,    # Full size
        "suppressed_strategies": [],  # All strategies allowed
        "session_unrestricted": True, # Trade all day — not just primary/secondary windows
        "description": "Strong trend — ride it, wide targets, frequent re-entries",
    },
    RANGE: {
        # Contested, mean-reverting day
        "trade_spacing_min":  12,     # Moderate spacing — not range-trading every tick
        "default_target_rr":  1.5,    # Quick targets — don't expect big moves
        "scale_out_enabled":  False,  # No runner on choppy days
        "trend_rider_enabled": False,
        "size_multiplier":    0.8,
        "suppressed_strategies": ["bias_momentum"],  # Trend strat underperforms in range
        "description": "Contested/range — fade extremes, quick exits, no runners",
    },
    VOLATILE: {
        # High ATR, choppy, dangerous — survivability mode
        "trade_spacing_min":  20,     # Very selective entries only
        "default_target_rr":  1.5,   # Quick exits — don't get caught in whips
        "scale_out_enabled":  False,
        "trend_rider_enabled": False,
        "size_multiplier":    0.5,    # Half size — capital preservation
        "suppressed_strategies": ["ib_breakout", "compression_breakout"],
                                      # Breakout strategies fail in chop
        "description": "Volatile/choppy — 50% size, selective only, survive",
    },
    UNKNOWN: {
        # Default until classified — conservative RANGE-like behavior
        "trade_spacing_min":  MIN_TRADE_SPACING_MIN,  # Use config default
        "default_target_rr":  1.5,
        "scale_out_enabled":  True,   # Keep on — will be overridden once classified
        "trend_rider_enabled": True,
        "size_multiplier":    0.9,
        "suppressed_strategies": [],
        "description": "Unclassified — using conservative defaults",
    },
}


@dataclass
class DayAssessment:
    day_type: str          # TREND | RANGE | VOLATILE | UNKNOWN
    params: dict           # Parameter overrides for this day type
    reason: str            # Human-readable classification reason
    cr_verdict: str = ""   # Underlying C/R verdict
    cr_score: int = 0      # Underlying momentum score
    atr_regime: str = ""   # LOW | NORMAL | HIGH | VERY_HIGH


class DayClassifier:
    """
    Classifies the current trading day and returns strategy parameter overrides.

    Call classify() on every bar or whenever C/R/ATR updates.
    The classification is sticky — once TREND is established it takes
    multiple conflicting signals to flip it (prevents thrashing mid-day).
    """

    def __init__(self):
        self._current: DayAssessment = DayAssessment(
            day_type=UNKNOWN,
            params=DAY_PARAMS[UNKNOWN],
            reason="Initializing",
        )
        self._classified_at: float = 0.0   # timestamp of first classification
        self._flip_count: int = 0           # how many times type has changed today

    def classify(self, cr_verdict: str, cr_score: int,
                 atr_5m: float, vix: float = 0.0) -> DayAssessment:
        """
        Classify the day and return the current DayAssessment.

        Args:
            cr_verdict: "CONTINUATION" | "REVERSAL" | "CONTESTED" | "UNKNOWN"
            cr_score:   Momentum score 0-5 from ContinuationReversalEngine
            atr_5m:     Current 5-minute ATR in points
            vix:        Current VIX level (0 = unavailable)
        """
        import time

        # ── ATR Regime (in points — atr_5m from snapshot() is price-based) ──
        if atr_5m <= 0:
            atr_regime = "UNKNOWN"
        elif atr_5m < _ATR_PT_QUIET:
            atr_regime = "QUIET"           # <8pt: very calm
        elif atr_5m < _ATR_PT_NORMAL:
            atr_regime = "NORMAL"          # 8-15pt: standard open volatility
        elif atr_5m < _ATR_PT_HIGH:
            atr_regime = "HIGH"            # 15-22pt: elevated — trade carefully
        else:
            atr_regime = "EXTREME"         # >22pt: choppy/dangerous

        # ── VIX classification ────────────────────────────────────────
        vix_extreme  = vix >= VIX_HIGH   if vix > 0 else False  # VIX >= 30 = danger
        vix_elevated = vix >= VIX_NORMAL if vix > 0 else False  # VIX >= 25

        # ── Day type logic (priority order matters) ───────────────────
        #
        # TREND OVERRIDE: Strong CONTINUATION + ATR EXTREME (not VIX).
        # High ATR on a CONTINUATION day = large directional moves, NOT chop.
        # Example: April 15 — 23pt ATR, VIX=18, Q-Score 5 BULLISH. The market is
        # trending hard, not volatile. Only override if VIX is NOT extreme (VIX=0
        # means unavailable — don't penalize for missing data).
        if (cr_verdict == "CONTINUATION" and cr_score >= 4
                and atr_regime == "EXTREME" and not vix_extreme):
            day_type = TREND
            reason = (f"TREND (high ATR override): CONTINUATION score={cr_score}/5, "
                      f"ATR={atr_5m:.1f}pt EXTREME but VIX not extreme — large trend moves")

        # VOLATILE: ATR extreme OR VIX extreme — safety first, top priority
        elif atr_regime == "EXTREME" or vix_extreme:
            day_type = VOLATILE
            reason = (f"VOLATILE: ATR={atr_5m:.1f}pt ({atr_regime})"
                      + (f", VIX={vix:.1f} (extreme)" if vix_extreme else ""))

        # VOLATILE: High ATR + elevated VIX — compounding risk
        elif atr_regime == "HIGH" and vix_elevated:
            day_type = VOLATILE
            reason = f"VOLATILE: ATR={atr_5m:.1f}pt (HIGH) + VIX={vix:.1f} elevated"

        # TREND: Strong CONTINUATION with non-extreme ATR
        elif (cr_verdict == "CONTINUATION" and cr_score >= 4
              and atr_regime in ("QUIET", "NORMAL", "HIGH")):
            day_type = TREND
            reason = (f"TREND: CONTINUATION score={cr_score}/5, "
                      f"ATR={atr_5m:.1f}pt ({atr_regime})"
                      + (f", VIX={vix:.1f} elevated" if vix_elevated else ""))

        # TREND (developing): Score 3 + quiet-to-normal ATR — still trend-able
        elif (cr_verdict == "CONTINUATION" and cr_score == 3
              and atr_regime in ("QUIET", "NORMAL")):
            day_type = TREND
            reason = f"TREND (developing): CONTINUATION score={cr_score}/5, ATR={atr_5m:.1f}pt"

        # VOLATILE: Strong reversal day — fade swings but very selective
        elif cr_verdict == "REVERSAL" and cr_score >= 4:
            day_type = VOLATILE
            reason = f"VOLATILE: REVERSAL score={cr_score}/5 — strong counter-trend pressure"

        # RANGE: Contested, weak continuation, or moderate reversal — fade extremes
        elif cr_verdict in ("CONTESTED", "UNKNOWN", "REVERSAL") or cr_score <= 2:
            day_type = RANGE
            reason = (f"RANGE: verdict={cr_verdict} score={cr_score}/5, "
                      f"ATR={atr_5m:.1f}pt ({atr_regime})")

        else:
            day_type = UNKNOWN
            reason = f"UNKNOWN: verdict={cr_verdict} score={cr_score} ATR={atr_5m:.1f}pt"

        # ── Log type changes ──────────────────────────────────────────
        if day_type != self._current.day_type:
            logger.info(
                f"[DAY TYPE] {self._current.day_type} -> {day_type}  |  {reason}"
            )
            self._flip_count += 1
            if self._classified_at == 0:
                self._classified_at = time.time()

        self._current = DayAssessment(
            day_type=day_type,
            params=DAY_PARAMS[day_type],
            reason=reason,
            cr_verdict=cr_verdict,
            cr_score=cr_score,
            atr_regime=atr_regime,
        )
        return self._current

    @property
    def day_type(self) -> str:
        return self._current.day_type

    @property
    def params(self) -> dict:
        return self._current.params

    def get_state(self) -> dict:
        """Return current classification for dashboard / logging."""
        return {
            "day_type":   self._current.day_type,
            "reason":     self._current.reason,
            "cr_verdict": self._current.cr_verdict,
            "cr_score":   self._current.cr_score,
            "atr_regime": self._current.atr_regime,
            "trade_spacing_min": self._current.params["trade_spacing_min"],
            "target_rr":  self._current.params["default_target_rr"],
            "size_mult":  self._current.params["size_multiplier"],
            "rider_on":   self._current.params["trend_rider_enabled"],
            "flip_count": self._flip_count,
        }
