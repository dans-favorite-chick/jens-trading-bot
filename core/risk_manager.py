"""
Phoenix Bot — Risk Manager

Consolidated risk rules from all legacy bots:
- V1: Daily/weekly limits, recovery mode, VIX filters
- MNQ v5: Dynamic risk sizing by entry quality (A++/B/C tiers)
- V3: Cooloff after consecutive losses
- V2: Max trades per session

All values are read from config/settings.py and can be overridden
at runtime by dashboard sliders.
"""

import time
import logging
from dataclasses import dataclass, field

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    MAX_LOSS_PER_TRADE, DAILY_LOSS_LIMIT, WEEKLY_LOSS_LIMIT,
    RECOVERY_MODE_TRIGGER, MAX_TRADES_PER_SESSION,
    COOLOFF_AFTER_CONSECUTIVE_LOSSES, COOLOFF_DURATION_MIN,
    VIX_LOW, VIX_NORMAL, VIX_HIGH, VIX_EXTREME,
    RISK_TIER_A_PLUS, RISK_TIER_B, RISK_TIER_C,
    ATR_LOW, ATR_NORMAL, ATR_HIGH, TICK_SIZE,
)

logger = logging.getLogger("RiskManager")


@dataclass
class RiskState:
    """Tracks all risk-related state for a single bot session."""
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    consecutive_losses: int = 0
    recovery_mode: bool = False
    cooloff_until: float = 0.0  # timestamp
    killed: bool = False
    kill_reason: str = ""


class RiskManager:
    def __init__(self):
        self.state = RiskState()

        # Runtime overrides (from dashboard sliders)
        self._risk_per_trade = MAX_LOSS_PER_TRADE
        self._daily_limit = DAILY_LOSS_LIMIT
        self._max_trades = MAX_TRADES_PER_SESSION

    # ─── Dashboard Slider Overrides ─────────────────────────────────
    def set_risk_per_trade(self, value: float):
        self._risk_per_trade = min(value, MAX_LOSS_PER_TRADE)

    def set_daily_limit(self, value: float):
        self._daily_limit = value

    def set_max_trades(self, value: int):
        self._max_trades = value

    # ─── Pre-Trade Checks ───────────────────────────────────────────
    def can_trade(self, vix: float = 0.0) -> tuple[bool, str]:
        """
        Check all risk gates before entering a trade.
        Returns (allowed: bool, reason: str).
        """
        if self.state.killed:
            return False, f"Kill switch: {self.state.kill_reason}"

        if self.state.daily_pnl <= -self._daily_limit:
            return False, f"Daily loss limit hit (${self.state.daily_pnl:.2f} / -${self._daily_limit:.2f})"

        if self.state.weekly_pnl <= -WEEKLY_LOSS_LIMIT:
            return False, f"Weekly loss limit hit (${self.state.weekly_pnl:.2f} / -${WEEKLY_LOSS_LIMIT:.2f})"

        if self.state.trades_today >= self._max_trades:
            return False, f"Max trades reached ({self.state.trades_today}/{self._max_trades})"

        if time.time() < self.state.cooloff_until:
            remaining = int(self.state.cooloff_until - time.time())
            return False, f"Cooloff active ({remaining}s remaining after {COOLOFF_AFTER_CONSECUTIVE_LOSSES} losses)"

        if vix >= VIX_EXTREME:
            return False, f"VIX extreme ({vix:.1f} >= {VIX_EXTREME}): NO TRADE"

        return True, "OK"

    # ─── Dynamic Risk Sizing (MNQ v5 Elite Upgrade #1) ──────────────
    def get_risk_for_entry(self, entry_score: float, vix: float = 0.0) -> tuple[float, str]:
        """
        Calculate risk per trade based on entry quality score (0-60).

        Returns (risk_dollars: float, tier: str)
        """
        # Base tier from entry score
        if entry_score >= 50:
            risk = RISK_TIER_A_PLUS
            tier = "A++"
        elif entry_score >= 40:
            risk = RISK_TIER_B
            tier = "B"
        elif entry_score >= 30:
            risk = RISK_TIER_C
            tier = "C"
        else:
            return 0.0, "SKIP"  # Score too low

        # VIX adjustment
        if vix >= VIX_HIGH:
            risk *= 0.5
            tier += " (VIX-reduced)"
        elif vix >= VIX_NORMAL:
            pass  # Standard
        elif vix <= VIX_LOW:
            risk *= 1.2  # Can be slightly aggressive in low-vol
            tier += " (low-VIX boost)"

        # Recovery mode: cut 50%
        if self.state.recovery_mode:
            risk *= 0.5
            tier += " (recovery)"

        # Never exceed slider max
        risk = min(risk, self._risk_per_trade)
        return round(risk, 2), tier

    # ─── Volatility Regime ──────────────────────────────────────────
    def get_volatility_regime(self, atr_5m: float) -> dict:
        """
        Returns target RR, max hold time, and selectivity based on ATR.
        """
        if atr_5m < ATR_LOW:
            return {"regime": "LOW", "target_rr": 1.5, "time_stop_min": 15, "selectivity": "more_trades"}
        elif atr_5m < ATR_NORMAL:
            return {"regime": "NORMAL", "target_rr": 1.5, "time_stop_min": 12, "selectivity": "standard"}
        elif atr_5m < ATR_HIGH:
            return {"regime": "HIGH", "target_rr": 1.75, "time_stop_min": 10, "selectivity": "selective"}
        else:
            return {"regime": "VERY_HIGH", "target_rr": 2.0, "time_stop_min": 8, "selectivity": "a_plus_only"}

    # ─── Position Sizing ────────────────────────────────────────────
    def calculate_stop_ticks(self, stop_ticks: int, atr_5m: float) -> int:
        """Adjust stop distance based on volatility regime."""
        regime = self.get_volatility_regime(atr_5m)
        if regime["regime"] == "HIGH":
            return int(stop_ticks * 1.2)
        elif regime["regime"] == "VERY_HIGH":
            return int(stop_ticks * 1.5)
        return stop_ticks

    def calculate_contracts(self, risk_dollars: float, stop_ticks: int) -> int:
        """Calculate number of contracts based on risk and stop distance."""
        dollar_per_tick = TICK_SIZE * 2  # MNQ = $0.50 per tick (0.25 tick size * $2 multiplier)
        risk_per_contract = stop_ticks * dollar_per_tick
        if risk_per_contract <= 0:
            return 0
        return max(1, int(risk_dollars / risk_per_contract))

    # ─── Post-Trade Updates ─────────────────────────────────────────
    def record_trade(self, pnl: float):
        """Update risk state after a trade completes."""
        self.state.daily_pnl += pnl
        self.state.weekly_pnl += pnl
        self.state.trades_today += 1

        if pnl >= 0:
            self.state.wins_today += 1
            self.state.consecutive_losses = 0
        else:
            self.state.losses_today += 1
            self.state.consecutive_losses += 1

        # Recovery mode check
        if self.state.daily_pnl <= -RECOVERY_MODE_TRIGGER and not self.state.recovery_mode:
            self.state.recovery_mode = True
            logger.warning(f"RECOVERY MODE activated: daily P&L = ${self.state.daily_pnl:.2f}")

        # Cooloff check
        if self.state.consecutive_losses >= COOLOFF_AFTER_CONSECUTIVE_LOSSES:
            self.state.cooloff_until = time.time() + COOLOFF_DURATION_MIN * 60
            logger.warning(f"COOLOFF: {COOLOFF_AFTER_CONSECUTIVE_LOSSES} consecutive losses, "
                           f"pausing {COOLOFF_DURATION_MIN} minutes")

        logger.info(f"[TRADE] P&L=${pnl:.2f} daily=${self.state.daily_pnl:.2f} "
                     f"trades={self.state.trades_today} W/L={self.state.wins_today}/{self.state.losses_today}")

    def kill(self, reason: str = "Manual kill"):
        self.state.killed = True
        self.state.kill_reason = reason
        logger.warning(f"KILL SWITCH: {reason}")

    def reset_daily(self):
        """Call at start of new trading day."""
        self.state.daily_pnl = 0.0
        self.state.trades_today = 0
        self.state.wins_today = 0
        self.state.losses_today = 0
        self.state.consecutive_losses = 0
        self.state.recovery_mode = False
        self.state.cooloff_until = 0.0
        self.state.killed = False
        self.state.kill_reason = ""
        logger.info("Daily risk state reset")

    def to_dict(self) -> dict:
        """Serialize state for dashboard."""
        return {
            "daily_pnl": round(self.state.daily_pnl, 2),
            "weekly_pnl": round(self.state.weekly_pnl, 2),
            "trades_today": self.state.trades_today,
            "wins_today": self.state.wins_today,
            "losses_today": self.state.losses_today,
            "win_rate": round(self.state.wins_today / max(1, self.state.trades_today) * 100, 1),
            "consecutive_losses": self.state.consecutive_losses,
            "recovery_mode": self.state.recovery_mode,
            "cooloff_active": time.time() < self.state.cooloff_until,
            "killed": self.state.killed,
            "kill_reason": self.state.kill_reason,
            "daily_limit": self._daily_limit,
            "daily_used_pct": round(abs(self.state.daily_pnl) / self._daily_limit * 100, 1),
        }
