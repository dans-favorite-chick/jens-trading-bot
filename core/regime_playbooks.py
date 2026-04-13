"""
Phoenix Bot — Regime-Aware Playbooks

Maps detected market regime (from HMM) to strategy parameter adjustments.
Instead of one-size-fits-all settings, the playbook selector modifies
strategy behavior based on what the market is actually doing:

- TRENDING: momentum strategies aggressive, mean-reversion off, trail stops
- RANGING: mean-reversion strategies on, momentum tighter, fade extremes
- VOLATILE: wider stops, smaller size, only A+ setups
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger("Playbooks")


@dataclass
class Playbook:
    name: str
    description: str
    strategy_overrides: dict  # {strategy_name: {param: value}}
    risk_overrides: dict  # Global risk adjustments
    active_strategies: list  # Which strategies to prioritize
    suppress_strategies: list  # Which strategies to suppress


# ── Playbook Definitions ────────────────────────────────────────────

PLAYBOOKS = {
    "TRENDING": Playbook(
        name="TRENDING",
        description="Strong directional move — ride momentum, trail stops",
        strategy_overrides={
            "bias_momentum": {
                "min_tf_votes": 2,
                "min_momentum": 25,
                "min_confluence": 1.5,
                "target_rr": 2.0,  # Let winners run in trends
            },
            "ib_breakout": {
                "target_extension": 2.0,  # Bigger extensions in trends
            },
            "vwap_pullback": {
                "min_tf_votes": 2,
                "target_rr": 2.0,
            },
            "high_precision_only": {
                "min_tf_votes": 3,  # Slightly loosened
                "target_rr": 2.0,
            },
            "spring_setup": {
                "min_wick_ticks": 5,  # Slightly more aggressive
            },
        },
        risk_overrides={
            "size_multiplier": 1.2,  # Slightly bigger in confirmed trends
            "max_daily_loss_mult": 1.0,
        },
        active_strategies=["bias_momentum", "ib_breakout", "vwap_pullback"],
        suppress_strategies=[],  # Don't suppress anything, just prioritize
    ),

    "RANGING": Playbook(
        name="RANGING",
        description="Choppy sideways — fade extremes, mean-revert to VWAP",
        strategy_overrides={
            "bias_momentum": {
                "min_tf_votes": 3,  # Tighter — trends are false in ranges
                "min_momentum": 45,
                "min_confluence": 2.5,
                "target_rr": 1.5,  # Quick scalps
            },
            "spring_setup": {
                "min_wick_ticks": 4,  # More aggressive — springs work in ranges
                "require_vwap_reclaim": False,
                "target_rr": 1.5,
            },
            "vwap_pullback": {
                "min_tf_votes": 2,  # VWAP pullback IS the range play
                "target_rr": 1.5,
            },
            "high_precision_only": {
                "min_tf_votes": 3,
                "target_rr": 1.3,  # Quick exits
            },
        },
        risk_overrides={
            "size_multiplier": 0.8,  # Slightly smaller in chop
            "max_daily_loss_mult": 0.8,
        },
        active_strategies=["spring_setup", "vwap_pullback"],
        suppress_strategies=[],
    ),

    "VOLATILE": Playbook(
        name="VOLATILE",
        description="High volatility — wider stops, smaller size, A+ only",
        strategy_overrides={
            "bias_momentum": {
                "min_tf_votes": 3,
                "min_momentum": 50,  # Only strong signals
                "min_confluence": 3.0,
                "stop_ticks": 12,  # Wider stops
                "target_rr": 2.5,  # Bigger reward for the risk
            },
            "spring_setup": {
                "min_wick_ticks": 8,  # Bigger wicks expected
                "stop_multiplier": 2.0,
                "target_rr": 2.0,
            },
            "vwap_pullback": {
                "min_tf_votes": 3,
                "stop_ticks": 12,
                "target_rr": 2.0,
            },
            "high_precision_only": {
                "min_tf_votes": 4,  # Only perfect setups
                "min_precision": 65,
                "stop_ticks": 12,
            },
        },
        risk_overrides={
            "size_multiplier": 0.5,  # Half size in volatility
            "max_daily_loss_mult": 0.6,  # Tighter daily limit
        },
        active_strategies=["high_precision_only"],
        suppress_strategies=["ib_breakout"],  # IB breakouts whipsaw in volatility
    ),
}

# Default — no modifications
DEFAULT_PLAYBOOK = Playbook(
    name="DEFAULT",
    description="No regime detected — use base strategy settings",
    strategy_overrides={},
    risk_overrides={"size_multiplier": 1.0, "max_daily_loss_mult": 1.0},
    active_strategies=[],
    suppress_strategies=[],
)


class PlaybookManager:
    """Selects and applies the right playbook based on detected regime."""

    def __init__(self):
        self._current_playbook: Playbook = DEFAULT_PLAYBOOK
        self._last_regime: str = "DEFAULT"

    def update_regime(self, hmm_regime: str, confidence: float = 0.0) -> Playbook:
        """Update playbook based on HMM regime detection.

        Only switches playbook if confidence > 60% to avoid whipsawing.
        """
        if confidence < 0.6:
            return self._current_playbook

        regime = hmm_regime.upper()
        if regime not in PLAYBOOKS:
            return self._current_playbook

        if regime != self._last_regime:
            self._current_playbook = PLAYBOOKS[regime]
            self._last_regime = regime
            logger.info(f"[PLAYBOOK] Switched to {regime}: {self._current_playbook.description}")

        return self._current_playbook

    def get_strategy_overrides(self, strategy_name: str) -> dict:
        """Get parameter overrides for a specific strategy."""
        return self._current_playbook.strategy_overrides.get(strategy_name, {})

    def get_risk_overrides(self) -> dict:
        """Get global risk parameter adjustments."""
        return self._current_playbook.risk_overrides

    def is_strategy_suppressed(self, strategy_name: str) -> bool:
        """Check if a strategy is suppressed in the current regime."""
        return strategy_name in self._current_playbook.suppress_strategies

    def get_current(self) -> Playbook:
        return self._current_playbook

    def to_dict(self) -> dict:
        pb = self._current_playbook
        return {
            "regime": pb.name,
            "description": pb.description,
            "risk_overrides": pb.risk_overrides,
            "active_strategies": pb.active_strategies,
            "suppress_strategies": pb.suppress_strategies,
        }
