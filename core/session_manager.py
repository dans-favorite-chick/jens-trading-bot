"""
Phoenix Bot — Session Manager

Manages 8 market regimes with time-based strategy selection.
Port from V3 session_manager.py with enhancements.
"""

import logging
from datetime import datetime, time as dtime

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (SESSION_WINDOWS,
                             PROD_PRIMARY_START, PROD_PRIMARY_END,
                             PROD_SECONDARY_START, PROD_SECONDARY_END,
                             CR_ADAPTIVE_SESSION, CR_EXTENDED_END)

logger = logging.getLogger("SessionManager")


# ─── Market Regimes ─────────────────────────────────────────────────
# ─── PROD Regime Config (conservative, proven windows only) ─────────
REGIME_CONFIG = {
    "OVERNIGHT_RANGE": {
        "min_confluence_override": None,
        "size_multiplier": 0.5,
        "allowed_strategies": ["spring_setup"],
        "notes": "Thin volume, fade extremes only",
    },
    "PREMARKET_DRIFT": {
        "min_confluence_override": None,
        "size_multiplier": 0.3,
        "allowed_strategies": ["bias_momentum"],
        "notes": "Reduced size — lower confidence regime",
    },
    "OPEN_MOMENTUM": {
        "min_confluence_override": None,
        "size_multiplier": 1.0,
        "allowed_strategies": None,
        "notes": "HIGH EDGE window — full size, all strategies, GO mode",
    },
    "MID_MORNING": {
        "min_confluence_override": 2.5,
        "size_multiplier": 1.0,
        "allowed_strategies": None,
        "notes": "GOLD REGIME — backtest 100% WR, maximize signal generation",
    },
    "AFTERNOON_CHOP": {
        "min_confluence_override": 4.0,
        "size_multiplier": 0.5,
        # Only level-based strategies in the death zone.
        # bias_momentum and high_precision_only are momentum chasers — they need price
        # moving with conviction. AFTERNOON_CHOP is mean-reverting lunch chop; the only
        # valid trades here are: spring reversal at an extreme, VWAP reclaim, or DOM
        # absorption pullback to a level. Momentum entries stop out on noise instantly.
        # This applies even on TREND days (session_unrestricted bypasses the window
        # check but NOT the per-regime allowed_strategies check here).
        "allowed_strategies": ["dom_pullback", "spring_setup", "vwap_pullback",
                               "ib_breakout", "compression_breakout"],
        "notes": "DEATH ZONE — level-based entries only (dom_pullback, spring, vwap), no momentum chasing",
    },
    "LATE_AFTERNOON": {
        "min_confluence_override": 3.0,   # Slightly higher bar than open — be selective
        "size_multiplier": 0.8,
        "allowed_strategies": None,        # All strategies — this is the second trend window
        "notes": "Institutional reposition 13:00-15:00. Trend continuation trades. "
                 "Today (4/14): +300pt move ran entirely through this window.",
    },
    "CLOSE_CHOP": {
        "min_confluence_override": 4.0,
        "size_multiplier": 0.3,
        "allowed_strategies": None,
        "notes": "Avoid in prod — directionless",
    },
    "AFTERHOURS": {
        "min_confluence_override": None,
        "size_multiplier": 0.3,
        "allowed_strategies": ["spring_setup"],
        "notes": "Very selective, mean reversion only",
    },
}

# ─── LAB Regime Config (AGGRESSIVE — all strategies, all regimes) ───
# Lab bot is for PRACTICE. It should try everything, everywhere, always.
# Every regime is a learning opportunity. Every tick is training data.
LAB_REGIME_CONFIG = {
    "OVERNIGHT_RANGE": {
        "min_confluence_override": None,
        "size_multiplier": 0.8,
        "allowed_strategies": None,  # ALL strategies — practice everything
        "notes": "LAB: full strategy access, learn overnight patterns",
    },
    "PREMARKET_DRIFT": {
        "min_confluence_override": None,
        "size_multiplier": 0.8,
        "allowed_strategies": None,  # ALL strategies
        "notes": "LAB: practice all strategies, collect premarket data",
    },
    "OPEN_MOMENTUM": {
        "min_confluence_override": None,
        "size_multiplier": 1.0,
        "allowed_strategies": None,
        "notes": "LAB: GO mode — max aggression, all strategies",
    },
    "MID_MORNING": {
        "min_confluence_override": None,
        "size_multiplier": 1.0,
        "allowed_strategies": None,
        "notes": "LAB: GO mode — max aggression, all strategies",
    },
    "AFTERNOON_CHOP": {
        "min_confluence_override": None,
        "size_multiplier": 0.8,
        "allowed_strategies": None,  # ALL — learn the chop patterns
        "notes": "LAB: practice in the death zone, learn what fails here",
    },
    "LATE_AFTERNOON": {
        "min_confluence_override": None,
        "size_multiplier": 1.0,
        "allowed_strategies": None,
        "notes": "LAB: institutional flow window — practice all strategies",
    },
    "CLOSE_CHOP": {
        "min_confluence_override": None,
        "size_multiplier": 0.8,
        "allowed_strategies": None,
        "notes": "LAB: practice everything, learn close behavior",
    },
    "AFTERHOURS": {
        "min_confluence_override": None,
        "size_multiplier": 0.8,
        "allowed_strategies": None,  # ALL — try IB patterns, momentum, everything
        "notes": "LAB: AGGRESSIVE practice — try all strategies after hours",
    },
}


def _parse_time(t: str) -> dtime:
    parts = t.split(":")
    return dtime(int(parts[0]), int(parts[1]))


class SessionManager:
    def __init__(self, bot_name: str = "prod"):
        self.current_regime = "UNKNOWN"
        self._last_regime = None
        self.bot_name = bot_name
        # Lab bot uses aggressive config, prod uses conservative
        self._config = LAB_REGIME_CONFIG if bot_name == "lab" else REGIME_CONFIG

    def get_current_regime(self, now: datetime = None) -> str:
        """Determine current market regime based on time (CST)."""
        if now is None:
            now = datetime.now()
        current_time = now.time()

        for regime_name, window in SESSION_WINDOWS.items():
            start = _parse_time(window["start"])
            end = _parse_time(window["end"])

            if start <= end:
                if start <= current_time < end:
                    self.current_regime = regime_name
                    self._check_regime_change(regime_name)
                    return regime_name
            else:
                # Overnight wrap (e.g., 22:00 – 07:00)
                if current_time >= start or current_time < end:
                    self.current_regime = regime_name
                    self._check_regime_change(regime_name)
                    return regime_name

        self.current_regime = "UNKNOWN"
        return "UNKNOWN"

    def _check_regime_change(self, new_regime: str):
        if self._last_regime and new_regime != self._last_regime:
            logger.info(f"Regime shift: {self._last_regime} -> {new_regime}")
        self._last_regime = new_regime

    def get_regime_config(self, regime: str = None) -> dict:
        """Get config for the current (or specified) regime."""
        r = regime or self.current_regime
        return self._config.get(r, self._config.get("AFTERHOURS", {}))

    def is_strategy_allowed(self, strategy_name: str, regime: str = None) -> bool:
        """Check if a strategy is allowed in the current regime."""
        config = self.get_regime_config(regime)
        allowed = config["allowed_strategies"]
        if allowed is None:
            return True  # All strategies allowed
        return strategy_name in allowed

    def get_size_multiplier(self, regime: str = None) -> float:
        return self.get_regime_config(regime)["size_multiplier"]

    def get_confluence_override(self, regime: str = None) -> float | None:
        return self.get_regime_config(regime)["min_confluence_override"]

    def is_prod_trading_window(self, now: datetime = None,
                               cr_verdict: str = None, cr_score: int = 0) -> bool:
        """Check if we're in a production bot trading window.

        Windows:
          Primary:   PROD_PRIMARY_START – PROD_PRIMARY_END  (08:30–11:00 CST)
                     Open momentum + mid-morning — highest edge window.

          Secondary: PROD_SECONDARY_START – PROD_SECONDARY_END  (13:00–14:30 CST)
                     Institutional repositioning / late-afternoon trend trades.
                     Today's example: +300pt move ran entirely here.

          CR Extended: PROD_SECONDARY_END – CR_EXTENDED_END  (14:30–15:00 CST)
                     Only active on strong CONTINUATION days (cr_score >= 4).
                     Lets the bot ride institutional flow into the close.

        Args:
            cr_verdict: "CONTINUATION" | "REVERSAL" | "CONTESTED" | None
            cr_score:   Momentum score 0-5 from ContinuationReversalEngine
        """
        if now is None:
            now = datetime.now()
        t = now.time()

        # Primary window: 08:30–11:00 CST
        if _parse_time(PROD_PRIMARY_START) <= t < _parse_time(PROD_PRIMARY_END):
            return True

        # Secondary window: 13:00–14:30 CST
        if _parse_time(PROD_SECONDARY_START) <= t < _parse_time(PROD_SECONDARY_END):
            return True

        # C/R Extension: 14:30–15:00 CST — only on strong continuation days
        if CR_ADAPTIVE_SESSION and cr_verdict == "CONTINUATION" and cr_score >= 4:
            if _parse_time(PROD_SECONDARY_END) <= t < _parse_time(CR_EXTENDED_END):
                logger.info(f"[SESSION] CR Extension active: "
                            f"CONTINUATION score={cr_score} → trading until {CR_EXTENDED_END}")
                return True

        return False

    def to_dict(self) -> dict:
        config = self.get_regime_config()
        t = datetime.now().time()
        # Determine which window label is active (for dashboard display)
        in_primary   = _parse_time(PROD_PRIMARY_START) <= t < _parse_time(PROD_PRIMARY_END)
        in_secondary = _parse_time(PROD_SECONDARY_START) <= t < _parse_time(PROD_SECONDARY_END)
        window_label = ("PRIMARY" if in_primary else
                        "SECONDARY" if in_secondary else
                        "EXTENDED" if self.is_prod_trading_window() else "CLOSED")
        return {
            "regime": self.current_regime,
            "size_multiplier": config["size_multiplier"],
            "confluence_override": config["min_confluence_override"],
            "allowed_strategies": config["allowed_strategies"],
            "notes": config["notes"],
            "is_prod_window": self.is_prod_trading_window(),
            "prod_window_label": window_label,  # PRIMARY / SECONDARY / EXTENDED / CLOSED
        }
