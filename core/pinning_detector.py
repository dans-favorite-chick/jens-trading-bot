"""
Phoenix Bot — 0DTE Pinning Detector

Detects high pin risk in last 90 min of RTH: price near a major 0DTE
gamma strike where dealer hedging tends to pin price around the strike.

Research basis (2026):
- 0DTE gamma peaks on expiration day; dealer hedging strongest at-the-money
- Last 90 min RTH: call/put walls act as magnets or ceilings/floors
- Entering new trades near a pin level is low-probability (price gets stuck)
- Exception: a sustained BREACH of the pin level with volume = pinning failed,
  real directional move in progress

Rules:
  pin_risk_active = True when:
    - Time is in 13:30-15:00 CDT window (last 90 min of RTH)
    - Price within 10 ticks of 0DTE call_resistance OR put_support OR gamma_wall
    - MenthorQ regime is POSITIVE (pinning requires positive gamma)

When pin_risk_active:
  - New entry signals get a "pin_risk" veto flag (strategies check + skip)
  - Existing open positions: tighten stop TO the pin level (pin will reject)

On pin BREACH (sustained 5m close beyond the level with > 1.5x avg volume):
  - Clear pin_risk flag
  - Allow new entries (market just broke out of the pinning zone)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

logger = logging.getLogger("PinningDetector")

# Config
PIN_WINDOW_START = time(13, 30)  # 13:30 CDT
PIN_WINDOW_END = time(15, 0)     # 15:00 CDT
PIN_PROXIMITY_TICKS = 10
TICK_SIZE = 0.25
BREACH_VOL_MULT = 1.5
BREACH_CONFIRM_CLOSES = 2        # 2 consecutive 5m closes outside the level


@dataclass
class PinningState:
    pin_risk_active: bool
    pinning_level: Optional[float]   # Which level is creating pin risk
    pin_level_name: str              # e.g. "0DTE_CR", "0DTE_PS", "GAMMA_WALL_0DTE"
    distance_ticks: float            # Current distance from level
    reasoning: list[str]
    breach_in_progress: bool = False


class PinningDetector:
    """
    Stateful detector. Call update(ts, price, mq_levels, last_5m_bars, vol_ma_5m).
    """

    def __init__(self):
        self._breach_closes: int = 0
        self._breach_side: str = ""

    def _in_pin_window(self, ts: datetime) -> bool:
        t = ts.time()
        return PIN_WINDOW_START <= t < PIN_WINDOW_END

    def update(self, ts: datetime, price: float, mq_levels: dict,
               last_5m_bar=None, vol_ma_5m: float = 0.0) -> PinningState:
        """
        Args:
          ts: current timestamp
          price: current price
          mq_levels: dict with keys 'call_resistance_0dte', 'put_support_0dte', 'gamma_wall_0dte',
                     'call_resistance_all', 'put_support_all', 'regime' (POSITIVE/NEGATIVE/UNKNOWN)
          last_5m_bar: most recent completed 5m bar (for breach detection)
          vol_ma_5m: rolling volume MA for breach threshold
        """
        # Gate 1: time window
        if not self._in_pin_window(ts):
            return PinningState(False, None, "", 0, ["outside pin window"])

        # Gate 2: positive gamma regime (pinning requires pos gamma)
        regime = mq_levels.get("regime", "UNKNOWN")
        if regime not in ("POSITIVE", "UNKNOWN"):
            return PinningState(False, None, "", 0, [f"regime {regime} != POSITIVE"])

        # Find nearest 0DTE level
        candidates = [
            ("0DTE_CR", mq_levels.get("call_resistance_0dte", 0)),
            ("0DTE_PS", mq_levels.get("put_support_0dte", 0)),
            ("0DTE_WALL", mq_levels.get("gamma_wall_0dte", 0)),
            ("ALL_CR", mq_levels.get("call_resistance_all", 0)),
            ("ALL_PS", mq_levels.get("put_support_all", 0)),
        ]
        candidates = [(n, lvl) for n, lvl in candidates if lvl and lvl > 0]
        if not candidates:
            return PinningState(False, None, "", 0, ["no 0DTE levels available"])

        # Nearest
        candidates.sort(key=lambda x: abs(price - x[1]))
        near_name, near_level = candidates[0]
        distance_price = abs(price - near_level)
        distance_ticks = distance_price / TICK_SIZE

        if distance_ticks > PIN_PROXIMITY_TICKS:
            # Not close to any level — reset breach tracking
            self._breach_closes = 0
            self._breach_side = ""
            return PinningState(False, None, "", distance_ticks,
                                 [f"price {distance_ticks:.1f}t from nearest level (>{PIN_PROXIMITY_TICKS})"])

        # Check for sustained breach
        breach_in_progress = False
        if last_5m_bar is not None and vol_ma_5m > 0:
            close = last_5m_bar.close
            bar_side = "ABOVE" if close > near_level else ("BELOW" if close < near_level else "AT")
            if bar_side in ("ABOVE", "BELOW") and last_5m_bar.volume >= vol_ma_5m * BREACH_VOL_MULT:
                if self._breach_side == bar_side:
                    self._breach_closes += 1
                else:
                    self._breach_side = bar_side
                    self._breach_closes = 1
                if self._breach_closes >= BREACH_CONFIRM_CLOSES:
                    breach_in_progress = True
                    logger.warning(f"[PIN BREACH] {near_name} @ {near_level:.2f} breached "
                                   f"{bar_side} with {self._breach_closes} closes on volume")
                    return PinningState(
                        pin_risk_active=False,   # Pin broken — release veto
                        pinning_level=near_level,
                        pin_level_name=near_name,
                        distance_ticks=distance_ticks,
                        reasoning=[f"BREACH: {self._breach_closes} closes {bar_side} {near_name}"],
                        breach_in_progress=True,
                    )
            else:
                # Bar back inside pin zone
                if self._breach_closes > 0:
                    logger.debug(f"[PIN] Breach failed, resetting counter")
                self._breach_closes = 0
                self._breach_side = ""

        reasoning = [
            f"last 90 min RTH ({ts.time().strftime('%H:%M')})",
            f"price {distance_ticks:.1f}t from {near_name} @ {near_level:.2f}",
            f"regime {regime}",
        ]
        return PinningState(
            pin_risk_active=True,
            pinning_level=near_level,
            pin_level_name=near_name,
            distance_ticks=distance_ticks,
            reasoning=reasoning,
        )
