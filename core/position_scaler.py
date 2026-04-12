"""
Phoenix Bot -- Multi-Contract Position Scaler

Scales position size based on account equity, entry quality, and regime.
During golden windows the bot can scale up; otherwise stays conservative.
"""

import logging

logger = logging.getLogger("PositionScaler")

# Hard cap -- never exceed this regardless of account size or conditions
MAX_CONTRACTS = 3

# Golden regimes where scaling up is allowed
_SCALABLE_REGIMES = {"OPEN_MOMENTUM", "MID_MORNING"}


class PositionScaler:
    """Scale position size based on account equity and entry quality."""

    def __init__(self, base_account: float = 1000.0):
        self.base_account = base_account  # 1 contract per $base_account

    def get_max_contracts(self, account_equity: float, entry_score: float,
                          regime: str) -> int:
        """
        Calculate max contracts based on:
        - Account size (1 contract per $base_account)
        - Entry quality (A++ score >= 50 can go 2x, C stays at 1x)
        - Regime (OPEN_MOMENTUM / MID_MORNING allow 2x, others 1x)
        - Never more than MAX_CONTRACTS (hard cap for safety)

        Args:
            account_equity: current account equity in dollars
            entry_score: entry quality score (0-60 scale)
            regime: current session regime string

        Returns:
            int: max contracts to trade (1 to MAX_CONTRACTS)
        """
        if account_equity <= 0 or self.base_account <= 0:
            return 1

        # Base: 1 contract per $base_account of equity
        base_contracts = max(1, int(account_equity / self.base_account))

        # Entry quality multiplier
        if entry_score >= 50:
            # A++ entry -- allow 2x scaling
            quality_mult = 2.0
        elif entry_score >= 40:
            # B entry -- allow 1.5x
            quality_mult = 1.5
        else:
            # C entry or below -- no scaling
            quality_mult = 1.0

        # Regime multiplier
        if regime in _SCALABLE_REGIMES:
            regime_mult = 2.0  # Golden windows allow full scaling
        else:
            regime_mult = 1.0  # Other regimes stay at base

        # Combined: take the MINIMUM of quality and regime multipliers
        # (both conditions must be favorable to scale up)
        effective_mult = min(quality_mult, regime_mult)

        scaled = int(base_contracts * effective_mult)

        # Clamp to [1, MAX_CONTRACTS]
        result = max(1, min(scaled, MAX_CONTRACTS))

        logger.debug(f"[SCALER] equity=${account_equity:.0f} score={entry_score:.0f} "
                     f"regime={regime} -> base={base_contracts} "
                     f"qual_mult={quality_mult} regime_mult={regime_mult} "
                     f"-> {result} contracts")

        return result
