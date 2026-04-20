"""
Phoenix Bot — Chandelier Exit Manager v2 (timeframe-aware)

CHANGES FROM v1:
  ✅ Explicit timeframe parameter — no more ambiguity about lookback
  ✅ Accepts bars from the strategy's EXECUTION timeframe
  ✅ Computes lookback duration for logging (e.g. "22 × 15m = 5.5 hours")

TIMEFRAME USAGE BY STRATEGY:
  trend_following_pullback  → 5m bars (22 × 5m = 110 min lookback)
  bias_momentum_v2          → 5m bars (22 × 5m = 110 min lookback)
  compression_breakout_15m  → 15m bars (22 × 15m = 5.5 hrs lookback)
  compression_breakout_30m  → 30m bars (22 × 30m = 11 hrs lookback)

WHY MATCH EXECUTION TIMEFRAME:
  The Chandelier trail needs to look back far enough to capture the
  meaningful swing but not so far that it lags price dramatically.
  If a 30m compression breakout holds for 5-8 hours, a 110-min Chandelier
  (from 5m bars) would reset too aggressively as the trail "forgets"
  the pullback lows. Using the execution timeframe keeps the trail
  proportional to the expected hold time.

CLASSIC CHANDELIER (for reference):
  Chuck LeBeau's original on DAILY bars: 22 daily bars = ~1 month.
  Our intraday adaptation preserves the 22-bar proportion while
  scaling the clock to the execution timeframe.

NOT USED BY (intentional):
  vwap_pullback     → mean reversion, target IS VWAP mean
  spring_setup      → spring low IS the stop

RESEARCH BASIS:
  - LeBeau's original Chandelier Exit rules (1992)
  - 22 bars + 3x ATR standard parametrization
  - Matches Brian Shannon's trend-following guidance
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ExitMode(Enum):
    INITIAL = "initial"              # Original ATR stop active
    BREAKEVEN = "breakeven"          # Moved to BE at +1R
    CHANDELIER_STD = "chandelier_std"    # 22-bar, 3x ATR
    CHANDELIER_TIGHT = "chandelier_tight"  # 22-bar, 2.5x ATR (after +2R)


@dataclass
class ChandelierConfig:
    timeframe_minutes: int       # The execution timeframe (5, 15, 30, etc.)
    lookback_bars: int = 22      # Classic LeBeau value
    atr_period: int = 22
    mult_initial: float = 1.5    # Initial stop = 1.5x ATR
    mult_std: float = 3.0        # Chandelier trail = 3x ATR below highest high
    mult_tight: float = 2.5      # Tighter trail = 2.5x ATR after +2R
    activate_at_r: float = 1.5   # Start Chandelier trail at +1.5R
    tighten_at_r: float = 2.0    # Switch to tight trail at +2R
    be_at_r: float = 1.0         # Move to breakeven at +1R

    def lookback_minutes(self) -> int:
        return self.timeframe_minutes * self.lookback_bars


@dataclass
class ChandelierState:
    entry_price: float
    direction: str              # "LONG" or "SHORT"
    initial_stop: float
    current_stop: float
    mode: ExitMode
    r_value: float              # Distance from entry to initial stop
    highest_high: float         # For LONG
    lowest_low: float           # For SHORT
    last_update_bar_count: int = 0


class ChandelierExitManager:
    """
    Manages trade exit via initial ATR stop → breakeven → Chandelier trail.

    Per-strategy instance: create one manager per strategy with its
    own timeframe config. Each open position has its own state.
    """

    def __init__(self, config: ChandelierConfig):
        self.config = config
        self.states: dict = {}  # Keyed by trade_id

    def open_position(
        self,
        trade_id: str,
        direction: str,
        entry_price: float,
        initial_stop: float,
    ) -> ChandelierState:
        """Register a new open position."""
        r_value = abs(entry_price - initial_stop)

        state = ChandelierState(
            entry_price=entry_price,
            direction=direction.upper(),
            initial_stop=initial_stop,
            current_stop=initial_stop,
            mode=ExitMode.INITIAL,
            r_value=r_value,
            highest_high=entry_price if direction.upper() == "LONG" else 0,
            lowest_low=entry_price if direction.upper() == "SHORT" else float("inf"),
        )
        self.states[trade_id] = state
        return state

    def update(
        self,
        trade_id: str,
        bars: List,               # EXECUTION timeframe bars
        current_price: float,
    ) -> Optional[float]:
        """
        Update the Chandelier stop for a position based on new bars.

        Returns the NEW stop price if it moved, else None.
        Returns None if position not found.
        """
        state = self.states.get(trade_id)
        if state is None:
            return None

        if len(bars) < self.config.lookback_bars:
            return None

        # Compute current R progress
        if state.direction == "LONG":
            progress = (current_price - state.entry_price) / state.r_value
        else:
            progress = (state.entry_price - current_price) / state.r_value

        # Update extremes
        lookback_slice = bars[-self.config.lookback_bars:]
        if state.direction == "LONG":
            highest_high = max(b.high for b in lookback_slice)
            state.highest_high = max(state.highest_high, highest_high)
        else:
            lowest_low = min(b.low for b in lookback_slice)
            state.lowest_low = min(state.lowest_low, lowest_low)

        atr = self._calc_atr(bars, self.config.atr_period)
        if atr is None or atr <= 0:
            return None

        # State machine: INITIAL → BREAKEVEN → CHANDELIER_STD → CHANDELIER_TIGHT
        new_stop = state.current_stop

        if state.mode == ExitMode.INITIAL:
            if progress >= self.config.be_at_r:
                new_stop = state.entry_price  # Move to breakeven
                state.mode = ExitMode.BREAKEVEN

        if state.mode == ExitMode.BREAKEVEN:
            if progress >= self.config.activate_at_r:
                # Engage Chandelier trail at 3x ATR
                if state.direction == "LONG":
                    trail = state.highest_high - atr * self.config.mult_std
                else:
                    trail = state.lowest_low + atr * self.config.mult_std
                # Only tighten, never loosen
                if self._is_tighter(trail, state.current_stop, state.direction):
                    new_stop = trail
                    state.mode = ExitMode.CHANDELIER_STD

        if state.mode == ExitMode.CHANDELIER_STD:
            # Standard 3x ATR trail
            if state.direction == "LONG":
                trail = state.highest_high - atr * self.config.mult_std
            else:
                trail = state.lowest_low + atr * self.config.mult_std
            if self._is_tighter(trail, state.current_stop, state.direction):
                new_stop = trail

            # Potentially upgrade to tight trail
            if progress >= self.config.tighten_at_r:
                state.mode = ExitMode.CHANDELIER_TIGHT

        if state.mode == ExitMode.CHANDELIER_TIGHT:
            # Tighter 2.5x ATR trail
            if state.direction == "LONG":
                trail = state.highest_high - atr * self.config.mult_tight
            else:
                trail = state.lowest_low + atr * self.config.mult_tight
            if self._is_tighter(trail, state.current_stop, state.direction):
                new_stop = trail

        # Only update if stop moved tighter
        if self._is_tighter(new_stop, state.current_stop, state.direction):
            state.current_stop = new_stop
            state.last_update_bar_count = len(bars)
            return new_stop

        return None

    def close_position(self, trade_id: str) -> None:
        self.states.pop(trade_id, None)

    def get_state(self, trade_id: str) -> Optional[ChandelierState]:
        return self.states.get(trade_id)

    # ─── HELPERS ───────────────────────────────────────────────────────

    def _is_tighter(self, new_stop: float, current_stop: float, direction: str) -> bool:
        """True if new_stop is tighter (reduces risk) than current_stop."""
        if direction == "LONG":
            return new_stop > current_stop
        return new_stop < current_stop

    def _calc_atr(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        tr = []
        for i in range(1, len(bars)):
            c, p = bars[i], bars[i - 1]
            tr.append(max(
                c.high - c.low,
                abs(c.high - p.close),
                abs(c.low - p.close),
            ))
        return sum(tr[-period:]) / min(len(tr), period) if tr else None

    # ─── FACTORY METHODS (per-strategy convenience) ────────────────────

    @classmethod
    def for_5m_strategy(cls) -> "ChandelierExitManager":
        """Standard 5-min execution strategies (trend_following, bias_momentum)."""
        return cls(ChandelierConfig(timeframe_minutes=5))

    @classmethod
    def for_15m_strategy(cls) -> "ChandelierExitManager":
        """15-min execution strategies (compression_breakout_15m)."""
        return cls(ChandelierConfig(timeframe_minutes=15))

    @classmethod
    def for_30m_strategy(cls) -> "ChandelierExitManager":
        """30-min execution strategies (compression_breakout_30m)."""
        return cls(ChandelierConfig(timeframe_minutes=30))
