"""
Phoenix Bot — Gamma Flip Detector (skeleton)

Detects intraday regime flip: price crossing HVL (gamma flip line) with
volume expansion confirming a transition from one gamma regime to the other.

Research basis (2026):
- HVL (High Volatility Level) = where net dealer gamma crosses sign
- Above HVL → positive gamma → mean-reversion dominant → dampened vol
- Below HVL → negative gamma → trend-amplifying → expanding vol
- Gamma flip is THE single most important intraday level per SpotGamma
- False flips: wick through HVL without commitment vs real sustained breach

Detection criteria (two consecutive 5m closes):
  1. Close crosses HVL (up or down)
  2. Volume on breach bar ≥ 1.5× 20-bar MA
  3. Next bar close stays on new side (confirmation)
  → trigger regime_matrix re-evaluation

Cooldown: 15 min after flip detected (don't thrash on HVL retests).

News blackout: no flip signal within 15 min of calendar events.

SKELETON: Sunday will complete integration with regime_matrix reload + dashboard
alert. This file provides the detection primitive.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("GammaFlip")


VOLUME_MULTIPLIER = 1.5      # Breach bar must have vol ≥ 1.5× MA
CONFIRMATION_BARS = 1        # N additional 5m bars must close on new side
COOLDOWN_MINUTES = 15
NEWS_BLACKOUT_MINUTES = 15


@dataclass
class FlipEvent:
    ts: datetime
    direction: str           # "POS_TO_NEG" (price broke below HVL) or "NEG_TO_POS"
    hvl_level: float
    breach_price: float      # 5m close that triggered
    breach_volume: float
    volume_ma: float
    confirmation_bars: int   # How many confirming bars observed
    reasons: list[str]


class GammaFlipDetector:
    """
    Stateful detector. Call update(bar_5m, hvl, session_cvd) on each 5m close.
    Returns FlipEvent if confirmed, else None.
    """

    def __init__(self):
        self.recent_volumes: deque[float] = deque(maxlen=20)  # 5m bars
        self._pending_breach: Optional[FlipEvent] = None  # Awaiting confirmation
        self._cooldown_until: Optional[datetime] = None
        self._last_flip: Optional[FlipEvent] = None
        # Track price-vs-HVL state to detect crossings
        self._last_close: Optional[float] = None
        self._last_side: Optional[str] = None  # "ABOVE" | "BELOW"

    def _in_cooldown(self, now: datetime) -> bool:
        if self._cooldown_until is None:
            return False
        if now >= self._cooldown_until:
            self._cooldown_until = None
            return False
        return True

    def _side(self, price: float, hvl: float) -> str:
        return "ABOVE" if price > hvl else "BELOW"

    def update(self, bar, hvl: float, news_event_recent: bool = False
               ) -> Optional[FlipEvent]:
        """
        Feed one 5m completed bar.
        Args:
          bar: has .close, .volume, .ts attributes
          hvl: current HVL level from MenthorQ
          news_event_recent: True if a major calendar event is within +/- 15 min
        Returns:
          FlipEvent if a flip is confirmed this bar, else None.
        """
        if hvl <= 0:
            return None  # No HVL data
        ts = bar.ts if hasattr(bar, "ts") else datetime.now()

        # Track volume MA
        self.recent_volumes.append(bar.volume)

        if self._in_cooldown(ts):
            return None

        if news_event_recent:
            logger.debug(f"[GAMMA FLIP] News blackout active, skipping detection")
            return None

        current_side = self._side(bar.close, hvl)

        # First bar seen
        if self._last_side is None:
            self._last_side = current_side
            self._last_close = bar.close
            return None

        # Check if a PENDING breach just got its confirmation
        if self._pending_breach is not None:
            expected_side = "BELOW" if self._pending_breach.direction == "POS_TO_NEG" else "ABOVE"
            if current_side == expected_side:
                # Confirmed!
                self._pending_breach.confirmation_bars = 1
                self._pending_breach.reasons.append(
                    f"confirmation close {bar.close:.2f} stays {current_side} HVL"
                )
                confirmed = self._pending_breach
                self._pending_breach = None
                self._cooldown_until = ts + timedelta(minutes=COOLDOWN_MINUTES)
                self._last_flip = confirmed
                logger.warning(f"[GAMMA FLIP] CONFIRMED {confirmed.direction} at HVL {hvl:.2f} - "
                               f"{', '.join(confirmed.reasons)}")
                self._last_side = current_side
                self._last_close = bar.close
                return confirmed
            else:
                # False breach — failed confirmation
                logger.info(f"[GAMMA FLIP] Breach failed confirmation (close {bar.close:.2f} back to {current_side})")
                self._pending_breach = None

        # Check for NEW breach this bar
        if current_side != self._last_side:
            # Volume check
            if len(self.recent_volumes) < 5:
                # Not enough MA baseline
                self._last_side = current_side
                self._last_close = bar.close
                return None
            vol_ma = sum(self.recent_volumes) / len(self.recent_volumes)
            if bar.volume < vol_ma * VOLUME_MULTIPLIER:
                logger.debug(f"[GAMMA FLIP] Breach without volume: bar vol {bar.volume:.0f} < "
                             f"{vol_ma * VOLUME_MULTIPLIER:.0f} required")
                self._last_side = current_side
                self._last_close = bar.close
                return None

            direction = "POS_TO_NEG" if current_side == "BELOW" else "NEG_TO_POS"
            reasons = [
                f"{self._last_side}→{current_side} crossing",
                f"vol {bar.volume / vol_ma:.1f}× MA",
                f"HVL {hvl:.2f}",
            ]
            self._pending_breach = FlipEvent(
                ts=ts,
                direction=direction,
                hvl_level=hvl,
                breach_price=bar.close,
                breach_volume=bar.volume,
                volume_ma=vol_ma,
                confirmation_bars=0,
                reasons=reasons,
            )
            logger.info(f"[GAMMA FLIP] Breach pending: {direction} at {bar.close:.2f}, "
                        f"awaiting confirmation...")

        self._last_side = current_side
        self._last_close = bar.close
        return None

    def get_state(self) -> dict:
        """Dashboard snapshot."""
        return {
            "cooldown_active": self._cooldown_until is not None and datetime.now() < self._cooldown_until,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "pending_breach": self._pending_breach.direction if self._pending_breach else None,
            "last_flip": {
                "ts": self._last_flip.ts.isoformat(),
                "direction": self._last_flip.direction,
                "breach_price": self._last_flip.breach_price,
                "hvl": self._last_flip.hvl_level,
            } if self._last_flip else None,
        }
