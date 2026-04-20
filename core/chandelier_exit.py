"""
Chandelier trailing stop — Le Beau (1990s) / standard implementation.

For LONG positions:  trail = highest_high_since_entry - (atr_mult * ATR)
For SHORT positions: trail = lowest_low_since_entry  + (atr_mult * ATR)

The trail only ratchets in the favorable direction (never loosens).

Phoenix usage: ORB strategy per Zarattini, Barbon, Aziz 2024 (SSRN 4729284).
ATR basis = 14-period ATR on 5-minute bars, atr_mult = 3.0.

Note: there is a legacy, more elaborate ChandelierExitManager under
strategies/chandelier_exit.py that conflates stop migration with partial
exits. This module is intentionally minimal — a pure trail — because ORB
treats partial-exit-at-1R and Chandelier-trail-from-entry as independent
mechanisms (partial is a size change, trail is the exit-price mechanism).
"""

from dataclasses import dataclass, field


@dataclass
class ChandelierTrailState:
    """Per-position trail state. Owned by the position, mutated each bar."""
    direction: str                       # "LONG" or "SHORT"
    entry_price: float
    atr_mult: float = 3.0
    highest_high: float = 0.0
    lowest_low: float = field(default=float("inf"))
    current_trail: float = 0.0
    initialized: bool = False

    def update(self, bar_high: float, bar_low: float, atr: float) -> float:
        """
        Update trail state on each bar close. Returns current trail price.

        Called from base_bot's per-bar exit-check loop for positions whose
        Signal set an `exit_trigger` starting with "chandelier_trail".
        """
        if atr <= 0:
            # Can't compute trail without ATR; leave state unchanged
            return self.current_trail if self.initialized else 0.0

        if not self.initialized:
            self.highest_high = bar_high
            self.lowest_low = bar_low
            self.initialized = True

        if self.direction == "LONG":
            self.highest_high = max(self.highest_high, bar_high)
            candidate_trail = self.highest_high - (self.atr_mult * atr)
            # Ratchet: only raise, never lower
            self.current_trail = max(self.current_trail, candidate_trail)
        elif self.direction == "SHORT":
            self.lowest_low = min(self.lowest_low, bar_low)
            candidate_trail = self.lowest_low + (self.atr_mult * atr)
            # Ratchet: only lower, never raise (first update seeds the trail)
            if self.current_trail == 0.0:
                self.current_trail = candidate_trail
            else:
                self.current_trail = min(self.current_trail, candidate_trail)

        return self.current_trail

    def should_exit(self, current_price: float) -> bool:
        """True if price has violated the trail."""
        if not self.initialized or self.current_trail <= 0.0:
            return False
        if self.direction == "LONG":
            return current_price <= self.current_trail
        elif self.direction == "SHORT":
            return current_price >= self.current_trail
        return False
