"""
Phoenix Bot — Strategy Base Class

All strategies inherit from this. Dashboard toggles `enabled`.
Prod bot only runs strategies where `validated=True`.
"""

import uuid
from dataclasses import dataclass, field


@dataclass
class Signal:
    """A trade signal produced by a strategy."""
    direction: str         # "LONG" or "SHORT"
    stop_ticks: int        # Stop distance in ticks
    target_rr: float       # Risk:reward ratio (target = stop * rr)
    confidence: float      # 0-100 confidence score
    entry_score: float     # 0-60 entry precision score (for risk sizing)
    strategy: str          # Strategy name
    reason: str            # Human-readable entry reason
    confluences: list[str] # List of confluences met
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    # Set True when the strategy has already computed an ATR-based stop internally.
    # base_bot will skip its own ATR override so the strategy's calculation is used.
    # Use this for patterns that need stop anchored to a specific price (wick extreme)
    # rather than from entry price.
    atr_stop_override: bool = False

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "direction": self.direction,
            "strategy": self.strategy,
            "reason": self.reason,
            "confluences": self.confluences,
            "confidence": self.confidence,
            "entry_score": self.entry_score,
            "stop_ticks": self.stop_ticks,
            "target_rr": self.target_rr,
        }


class BaseStrategy:
    """
    Abstract strategy interface.

    Subclasses must implement `evaluate()` and set `name`.
    Strategy parameters come from config/strategies.py STRATEGIES dict.
    """

    name: str = "unnamed"
    enabled: bool = True
    validated: bool = False

    def __init__(self, config: dict):
        """
        Args:
            config: Strategy-specific params from config/strategies.py
        """
        self.config = config
        self.enabled = config.get("enabled", True)
        self.validated = config.get("validated", False)

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:
        """
        Evaluate current market conditions for a trade signal.

        Args:
            market: Current tick aggregator snapshot (price, ATR, VWAP, CVD, etc.)
            bars_5m: List of recent completed 5-min Bar objects
            bars_1m: List of recent completed 1-min Bar objects
            session_info: Session manager state (regime, allowed strategies, etc.)

        Returns:
            Signal if entry conditions met, None otherwise.
        """
        raise NotImplementedError

    @property
    def params(self) -> dict:
        """Return current tunable parameters (for dashboard display)."""
        return {k: v for k, v in self.config.items() if k not in ("enabled", "validated")}

    def update_params(self, updates: dict):
        """Update parameters from dashboard slider changes."""
        for k, v in updates.items():
            if k in self.config:
                self.config[k] = v
