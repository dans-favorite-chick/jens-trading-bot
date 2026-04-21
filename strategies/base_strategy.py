"""
Phoenix Bot — Strategy Base Class

All strategies inherit from this. Dashboard toggles `enabled`.
Prod bot only runs strategies where `validated=True`.
"""

import uuid
from dataclasses import dataclass, field
from typing import Optional


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
    atr_stop_override: bool = False

    # ─── Per-signal order type matrix (roadmap v4 Part C) ──────────────
    entry_type: str = "LIMIT"          # "LIMIT" | "STOPMARKET" | "MARKET"
    stop_type: str = "STOPMARKET"      # Universal rule: all stops = STOPMARKET
    target_type: str = "LIMIT"         # Universal rule: all targets = LIMIT

    # ─── Explicit price overrides (used when strategy computes prices directly) ──
    # If None, base_bot computes from current price + stop_ticks + target_rr.
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None

    # ─── Managed exit (Noise Area, trend riders with dynamic exits) ─────
    # When set, base_bot calls strategy.check_exit() each bar instead of
    # relying solely on bracketed stop/target.
    exit_trigger: Optional[str] = None   # e.g. "price_returns_inside_noise_area"
                                         #      "chandelier_trail_3atr" (ORB)
    eod_flat_time_et: Optional[str] = None  # e.g. "15:55" or "10:55"

    # ─── Per-signal scale-out override ─────────────────────────────────
    # Strategies with a research-backed partial-exit multiple that
    # differs from the global config.SCALE_OUT_RR. None = use global.
    scale_out_rr: Optional[float] = None   # e.g. 1.0 for Zarattini ORB

    # ─── Chandelier trail parameters (when exit_trigger starts with "chandelier_trail") ──
    # {"atr_mult": 3.0, "atr_period": 14, "atr_timeframe": "5m"}
    trail_config: Optional[dict] = None

    # ─── Strategy-specific diagnostics (UB/LB/vwap/sigma_open for Noise Area, OR hi/lo for ORB) ──
    metadata: dict = field(default_factory=dict)

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
            "entry_type": self.entry_type,
            "stop_type": self.stop_type,
            "target_type": self.target_type,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "exit_trigger": self.exit_trigger,
            "eod_flat_time_et": self.eod_flat_time_et,
            "scale_out_rr": self.scale_out_rr,
            "trail_config": self.trail_config,
            "metadata": self.metadata,
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

    def check_exit(self, position, market: dict, bars_1m: list,
                   session_info: dict) -> tuple[bool, str]:
        """
        Optional managed-exit hook. Called every bar for positions where
        Signal.exit_trigger was set. Default: never exits (bracket handles it).

        Returns (should_exit, reason).
        """
        return (False, "")

    @property
    def params(self) -> dict:
        """Return current tunable parameters (for dashboard display)."""
        return {k: v for k, v in self.config.items() if k not in ("enabled", "validated")}

    def update_params(self, updates: dict):
        """Update parameters from dashboard slider changes."""
        for k, v in updates.items():
            if k in self.config:
                self.config[k] = v
