"""
Phoenix Bot — Session Manager

Manages 8 market regimes with time-based strategy selection.
Port from V3 session_manager.py with enhancements.
"""

import logging
from datetime import datetime, time as dtime

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import SESSION_WINDOWS, PROD_PRIMARY_START, PROD_PRIMARY_END

logger = logging.getLogger("SessionManager")


# ─── Market Regimes ─────────────────────────────────────────────────
REGIME_CONFIG = {
    "OVERNIGHT_RANGE": {
        "min_confluence_override": None,  # Use strategy default
        "size_multiplier": 0.5,
        "allowed_strategies": ["spring_setup"],
        "notes": "Thin volume, fade extremes only",
    },
    "PREMARKET_DRIFT": {
        "min_confluence_override": None,
        "size_multiplier": 0.5,
        "allowed_strategies": ["bias_momentum"],
        "notes": "Light drift, avoid large size",
    },
    "OPEN_MOMENTUM": {
        "min_confluence_override": None,
        "size_multiplier": 1.0,
        "allowed_strategies": None,  # All strategies allowed
        "notes": "BEST window — highest edge, full size",
    },
    "MID_MORNING": {
        "min_confluence_override": 3.8,
        "size_multiplier": 1.0,
        "allowed_strategies": ["vwap_pullback", "bias_momentum", "spring_setup"],
        "notes": "First pullback territory",
    },
    "AFTERNOON_CHOP": {
        "min_confluence_override": 5.5,
        "size_multiplier": 0.5,
        "allowed_strategies": [],  # Skip most trades
        "notes": "DEATH ZONE — lunch lull, very selective",
    },
    "LATE_AFTERNOON": {
        "min_confluence_override": None,
        "size_multiplier": 0.8,
        "allowed_strategies": ["bias_momentum", "spring_setup"],
        "notes": "Institutional reposition window",
    },
    "CLOSE_CHOP": {
        "min_confluence_override": 5.0,
        "size_multiplier": 0.3,
        "allowed_strategies": [],
        "notes": "Avoid — directionless, tight stops",
    },
    "AFTERHOURS": {
        "min_confluence_override": None,
        "size_multiplier": 0.3,
        "allowed_strategies": ["spring_setup"],
        "notes": "Very selective, mean reversion only",
    },
}


def _parse_time(t: str) -> dtime:
    parts = t.split(":")
    return dtime(int(parts[0]), int(parts[1]))


class SessionManager:
    def __init__(self):
        self.current_regime = "UNKNOWN"
        self._last_regime = None

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
        return REGIME_CONFIG.get(r, REGIME_CONFIG["AFTERHOURS"])

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

    def is_prod_trading_window(self, now: datetime = None) -> bool:
        """Check if we're in the production bot's primary trading window."""
        if now is None:
            now = datetime.now()
        start = _parse_time(PROD_PRIMARY_START)
        end = _parse_time(PROD_PRIMARY_END)
        return start <= now.time() < end

    def to_dict(self) -> dict:
        config = self.get_regime_config()
        return {
            "regime": self.current_regime,
            "size_multiplier": config["size_multiplier"],
            "confluence_override": config["min_confluence_override"],
            "allowed_strategies": config["allowed_strategies"],
            "notes": config["notes"],
            "is_prod_window": self.is_prod_trading_window(),
        }
