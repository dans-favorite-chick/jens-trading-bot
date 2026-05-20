"""
Phoenix Exit Policies — Per-strategy production exit logic
============================================================

After Phase 13 Section U (tick-validated production specs), each strategy
gets a per-strategy exit policy chosen from this module. The choice is
driven by tick-level empirical data (see Agent A's TICK_LEVEL_EXIT_VERIFICATION.md
and Agent B's TICK_LEVEL_ENTRY_VERIFICATION.md).

POLICIES AVAILABLE:
  - fixed_rr:           target = entry + (rr * stop_distance). Simplest, most
                        common. Used by 5 strategies post-tick-validation.
  - chandelier:         rolling-window high - (atr_mult * ATR). Used by 3
                        high-WR breakout strategies.
  - time_exit:          close at market after N minutes. Used by 2 fast-
                        resolving setups.
  - managed_existing:   passthrough — let the strategy manage its own exit
                        (used by opening_session.orb which has internal logic).

ARCHITECTURE:
  Each policy is a class with two methods:
    - compute_initial_target(signal) -> float
        Called when the Signal is emitted, sets the initial target_price.
        Some policies (chandelier) return None — they manage exit dynamically
        instead of a fixed target.
    - should_exit(position_state, current_bar) -> Optional[ExitDecision]
        Called every 1m bar close. Returns an ExitDecision if the position
        should close NOW, else None.

  base_bot reads strategy config:
    config["exit_policy"]:        name of the policy (string)
    config["exit_policy_params"]: dict of policy-specific parameters

  The dispatcher get_policy(name, params) returns the right instance.

INTEGRATION POINTS:
  - bots/base_bot.py — reads config, dispatches to policy at signal emit + per-bar
  - core/position_manager.py — uses ExitDecision to close position via OIF
  - config/strategies.py — per-strategy exit_policy + exit_policy_params fields

PHASE 13 PER-STRATEGY ASSIGNMENTS (Section U.3):
  bias_momentum:                  fixed_rr(rr=2.0)
  spring_setup:                   fixed_rr(rr=3.0)
  vwap_pullback_v2:               fixed_rr(rr=3.0)
  opening_session.orb:            managed_existing
  opening_session.open_drive:     fixed_rr(rr=3.0)        (post Bug B2 fix)
  g_inside_bar_breakout:          chandelier(50, 3.0, 1.0)
  e_multi_day_breakout:           chandelier(50, 3.0, 1.0)
  a_asian_continuation:           time_exit(minutes=30)
  raschke_baseline:               time_exit(minutes=30)
  es_nq_confluence:               chandelier(50, 3.0, 1.0) (dormant pending MES)
  vwap_band_pullback:             fixed_rr(rr=3.0)
  ib_breakout:                    fixed_rr(rr=2.0)        (baseline / minimal)
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

TICK_SIZE = 0.25


# ════════════════════════════════════════════════════════════════════
# Common types
# ════════════════════════════════════════════════════════════════════

@dataclass
class ExitDecision:
    """Returned by a policy's should_exit() when the position should close."""
    exit_price: float            # The price to exit at (typically market)
    exit_reason: str             # Human-readable reason: "target", "trail_stop", "time_exit", etc.
    partial_fraction: float = 1.0  # 1.0 = close full position; <1.0 for scale-out


@dataclass
class PositionState:
    """Per-position state passed to policies on each bar.
    base_bot/position_manager owns this; policies READ it only.
    """
    strategy: str
    direction: str               # "LONG" or "SHORT"
    entry_price: float
    entry_ts: datetime           # When the position opened
    stop_price: float            # Current stop (may have moved from initial)
    initial_stop: float          # Stop at entry (never changes)
    target_price: Optional[float] = None  # Some policies don't use a target
    contracts: int = 1
    # Per-bar updated by base_bot:
    high_water: float = 0.0      # Best favorable price seen since entry
    low_water: float = 0.0       # Worst price seen
    bars_held: int = 0           # 1m bars since entry
    # Per-policy extra state (each policy stores its own data here):
    policy_state: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════
# Base policy
# ════════════════════════════════════════════════════════════════════

class ExitPolicy:
    """Abstract base. Each policy implements compute_initial_target +
    should_exit. Subclasses MUST be deterministic per-bar and stateless
    except for what's in PositionState.policy_state."""

    name: str = "base"

    def __init__(self, **params):
        self.params = params

    def compute_initial_target(self, direction: str, entry_price: float,
                                initial_stop: float) -> Optional[float]:
        """Compute the initial target_price for the signal. Return None if
        this policy doesn't use a fixed target (e.g., chandelier, time_exit)."""
        raise NotImplementedError

    def should_exit(self, pos: PositionState, bar) -> Optional[ExitDecision]:
        """Called every 1m bar close while position is open. Return an
        ExitDecision if position should close now, else None.

        `bar` is the latest 1m Bar object with .open, .high, .low, .close, .end_time.
        """
        raise NotImplementedError

    def _stop_distance(self, entry_price: float, initial_stop: float) -> float:
        """Helper: absolute distance from entry to initial stop, in price points."""
        return abs(entry_price - initial_stop)


# ════════════════════════════════════════════════════════════════════
# Fixed RR — target at entry + (rr * stop_distance)
# ════════════════════════════════════════════════════════════════════

class FixedRRPolicy(ExitPolicy):
    """Take profit at entry + (rr * stop_distance). Initial stop holds.
    Simplest possible policy. Tick-validated as best for 5 momentum strategies
    (see Phase 13 Section U.3).

    Params:
      rr (float): reward-to-risk ratio (e.g., 2.0 means target at +2R)
    """
    name = "fixed_rr"

    def __init__(self, rr: float = 2.0):
        super().__init__(rr=rr)
        self.rr = float(rr)

    def compute_initial_target(self, direction, entry_price, initial_stop):
        stop_dist = self._stop_distance(entry_price, initial_stop)
        if direction == "LONG":
            return round(entry_price + self.rr * stop_dist, 2)
        else:
            return round(entry_price - self.rr * stop_dist, 2)

    def should_exit(self, pos, bar):
        """Stop/target handled by OIF bracket order in NT8 — this policy
        doesn't need per-bar evaluation. base_bot still calls us for
        sanity; we just return None."""
        return None


# ════════════════════════════════════════════════════════════════════
# Chandelier — rolling-window high - (atr_mult * dynamic ATR)
# ════════════════════════════════════════════════════════════════════

class ChandelierPolicy(ExitPolicy):
    """Chuck LeBeau Chandelier Exit with rolling N-bar window + dynamic ATR.

    Stop "hangs" from the rolling N-bar highest high (LONG) or lowest low (SHORT):
      LONG stop  = rolling_high(N) - atr_mult * ATR(N)
      SHORT stop = rolling_low(N)  + atr_mult * ATR(N)

    Both the high/low reference AND the ATR are computed from the same rolling
    N-bar window. Stop only ratchets in the trade's favor.

    Activated after `activate_r` favorable to give the trade room.

    Tick-validated for 3 high-WR breakout strategies (Section U.3):
      g_inside_bar_breakout, e_multi_day_breakout, es_nq_confluence

    Params:
      lookback_bars (int): rolling window size for high/ATR (default 50)
      atr_mult (float): ATR multiplier for trail distance (default 3.0)
      activate_r (float): R-multiple of favorable movement before activation (default 1.0)
    """
    name = "chandelier"

    def __init__(self, lookback_bars: int = 50, atr_mult: float = 3.0,
                  activate_r: float = 1.0):
        super().__init__(lookback_bars=lookback_bars, atr_mult=atr_mult,
                          activate_r=activate_r)
        self.lookback_bars = int(lookback_bars)
        self.atr_mult = float(atr_mult)
        self.activate_r = float(activate_r)

    def compute_initial_target(self, direction, entry_price, initial_stop):
        # Chandelier doesn't use a fixed target — exit is driven by the trail.
        # Return a very wide target so OIF bracket doesn't fire it.
        stop_dist = self._stop_distance(entry_price, initial_stop)
        if direction == "LONG":
            return round(entry_price + 10.0 * stop_dist, 2)  # 10R = effectively no target
        else:
            return round(entry_price - 10.0 * stop_dist, 2)

    def should_exit(self, pos, bar):
        # Initialize policy state on first call
        if "bar_highs" not in pos.policy_state:
            pos.policy_state["bar_highs"] = deque(maxlen=self.lookback_bars)
            pos.policy_state["bar_lows"] = deque(maxlen=self.lookback_bars)
            pos.policy_state["bar_closes"] = deque(maxlen=self.lookback_bars)
            pos.policy_state["activated"] = False
            pos.policy_state["current_trail"] = pos.initial_stop

        st = pos.policy_state
        st["bar_highs"].append(float(bar.high))
        st["bar_lows"].append(float(bar.low))
        st["bar_closes"].append(float(bar.close))

        # Stop distance for activation check
        stop_dist = self._stop_distance(pos.entry_price, pos.initial_stop)
        activation_threshold = self.activate_r * stop_dist

        # Activate trail if favorable movement reached activation_r
        if not st["activated"]:
            if pos.direction == "LONG":
                if float(bar.high) >= pos.entry_price + activation_threshold:
                    st["activated"] = True
            else:
                if float(bar.low) <= pos.entry_price - activation_threshold:
                    st["activated"] = True

        # Compute new trail if activated AND we have enough lookback
        if st["activated"] and len(st["bar_highs"]) >= min(10, self.lookback_bars):
            atr = self._compute_atr(st["bar_highs"], st["bar_lows"], st["bar_closes"])
            if atr > 0:
                trail_buffer = self.atr_mult * atr
                if pos.direction == "LONG":
                    rolling_high = max(st["bar_highs"])
                    new_trail = rolling_high - trail_buffer
                    if new_trail > st["current_trail"]:
                        st["current_trail"] = new_trail
                else:
                    rolling_low = min(st["bar_lows"])
                    new_trail = rolling_low + trail_buffer
                    if new_trail < st["current_trail"]:
                        st["current_trail"] = new_trail

        # Check if stop hit on this bar
        if pos.direction == "LONG":
            if float(bar.low) <= st["current_trail"]:
                return ExitDecision(
                    exit_price=st["current_trail"],
                    exit_reason="chandelier_trail" if st["activated"] else "initial_stop",
                )
        else:
            if float(bar.high) >= st["current_trail"]:
                return ExitDecision(
                    exit_price=st["current_trail"],
                    exit_reason="chandelier_trail" if st["activated"] else "initial_stop",
                )

        return None

    @staticmethod
    def _compute_atr(highs: deque, lows: deque, closes: deque) -> float:
        """Wilder-approximated ATR over the rolling window."""
        if len(highs) < 2:
            return 0.0
        trs = []
        prev_close = closes[0]
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
            trs.append(tr)
            prev_close = closes[i]
        return sum(trs) / max(1, len(trs))


# ════════════════════════════════════════════════════════════════════
# Time Exit — close at market after N minutes
# ════════════════════════════════════════════════════════════════════

class TimeExitPolicy(ExitPolicy):
    """Close at market after N minutes. Original stop still applies if hit
    before time elapses.

    Tick-validated for 2 fast-resolving setups (Section U.3):
      a_asian_continuation, raschke_baseline

    Params:
      minutes (int): hold duration before time-exit (default 30)
    """
    name = "time_exit"

    def __init__(self, minutes: int = 30):
        super().__init__(minutes=minutes)
        self.minutes = int(minutes)

    def compute_initial_target(self, direction, entry_price, initial_stop):
        # Time exit uses target only as a "satisfy bracket order" placeholder.
        # Most exits will be time-based or stop-based.
        stop_dist = self._stop_distance(entry_price, initial_stop)
        if direction == "LONG":
            return round(entry_price + 5.0 * stop_dist, 2)
        else:
            return round(entry_price - 5.0 * stop_dist, 2)

    def should_exit(self, pos, bar):
        # Initial stop is handled by OIF bracket. We just check time.
        elapsed = bar.end_time - pos.entry_ts.timestamp() if hasattr(pos.entry_ts, 'timestamp') else (bar.end_time - pos.entry_ts)
        if isinstance(elapsed, timedelta):
            elapsed_min = elapsed.total_seconds() / 60
        elif isinstance(elapsed, (int, float)):
            elapsed_min = elapsed / 60
        else:
            return None

        if elapsed_min >= self.minutes:
            return ExitDecision(
                exit_price=float(bar.close),
                exit_reason="time_exit",
            )
        return None


# ════════════════════════════════════════════════════════════════════
# Managed Existing — passthrough (strategy handles its own exit)
# ════════════════════════════════════════════════════════════════════

class ManagedExistingPolicy(ExitPolicy):
    """No-op policy. The strategy is responsible for its own exit logic
    via Signal.exit_trigger or other mechanism.

    Used by opening_session.orb which has built-in managed exit logic
    that's already optimized (per Section T baseline preserves it).
    """
    name = "managed_existing"

    def __init__(self):
        super().__init__()

    def compute_initial_target(self, direction, entry_price, initial_stop):
        # Let the strategy's Signal set its own target
        return None  # Caller should fall back to strategy-provided target

    def should_exit(self, pos, bar):
        return None  # Strategy manages exit


# ════════════════════════════════════════════════════════════════════
# Dispatcher
# ════════════════════════════════════════════════════════════════════

POLICY_REGISTRY = {
    "fixed_rr":         FixedRRPolicy,
    "chandelier":       ChandelierPolicy,
    "time_exit":        TimeExitPolicy,
    "managed_existing": ManagedExistingPolicy,
}


def get_policy(name: str, params: Optional[dict] = None) -> ExitPolicy:
    """Factory: return the right policy instance for the given name + params.

    Args:
        name: policy name from POLICY_REGISTRY (e.g., "fixed_rr", "chandelier")
        params: kwargs to pass to the policy's __init__

    Returns:
        ExitPolicy instance

    Raises:
        ValueError if name is not recognized
    """
    if name not in POLICY_REGISTRY:
        raise ValueError(
            f"Unknown exit policy: {name!r}. "
            f"Available: {sorted(POLICY_REGISTRY.keys())}"
        )
    cls = POLICY_REGISTRY[name]
    return cls(**(params or {}))


# ════════════════════════════════════════════════════════════════════
# Reference: Phase 13 Section U.3 per-strategy assignments
# ════════════════════════════════════════════════════════════════════

PHASE_13_EXIT_ASSIGNMENTS = {
    "bias_momentum":              ("fixed_rr",   {"rr": 2.0}),
    "spring_setup":               ("fixed_rr",   {"rr": 3.0}),
    "vwap_pullback_v2":           ("fixed_rr",   {"rr": 3.0}),
    "opening_session.orb":        ("managed_existing", {}),
    "opening_session.open_drive": ("fixed_rr",   {"rr": 3.0}),  # post Bug B2 fix
    "g_inside_bar_breakout":      ("chandelier", {"lookback_bars": 50, "atr_mult": 3.0, "activate_r": 1.0}),
    "e_multi_day_breakout":       ("chandelier", {"lookback_bars": 50, "atr_mult": 3.0, "activate_r": 1.0}),
    "a_asian_continuation":       ("time_exit",  {"minutes": 30}),
    "raschke_baseline":           ("time_exit",  {"minutes": 30}),
    "es_nq_confluence":           ("chandelier", {"lookback_bars": 50, "atr_mult": 3.0, "activate_r": 1.0}),
    "vwap_band_pullback":         ("fixed_rr",   {"rr": 3.0}),
    "ib_breakout":                ("fixed_rr",   {"rr": 2.0}),
}


# ════════════════════════════════════════════════════════════════════
# Tick-validated entry order types (Section U.3)
# ════════════════════════════════════════════════════════════════════

PHASE_13_ORDER_TYPES = {
    # Most strategies: market orders are fine (favorable or neutral slippage)
    "bias_momentum":              "market",
    "spring_setup":                "market",
    "vwap_pullback_v2":            "market",
    "opening_session.orb":         "market",
    "opening_session.open_drive":  "market",
    "a_asian_continuation":        "market",
    "raschke_baseline":            "market",
    "es_nq_confluence":            "market",
    "vwap_band_pullback":          "market",
    "ib_breakout":                 "market",

    # RTH-open breakouts get systematic adverse slippage with market orders.
    # 5-second limit at signal price (then market if unfilled) saves $0.6-1.5k/yr.
    "g_inside_bar_breakout":       "limit_5s",
    "e_multi_day_breakout":        "limit_5s",
}
