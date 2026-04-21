"""
Phoenix Bot -- Regime Transition Playbook

Detects regime transitions and provides specific entry rules for the
first setup after a shift. The first clean setup after a regime change
is often the highest-conviction trade of the session.

Feeds into:
  - Strategy evaluation (score bonus for first post-transition signal)
  - Risk sizing (size boost on high-value transitions)
  - Dashboard (transition timeline display)
"""

import time
import logging

logger = logging.getLogger("RegimeTransitions")

# Transition bonus window: how many seconds after a transition
# the first-signal bonus remains active.
TRANSITION_WINDOW_S = 300  # 5 minutes


class RegimeTransitionDetector:
    """
    Detects and tracks regime transitions.
    The first clean setup after a shift is often the highest-conviction trade.
    """

    # Known high-value transitions with specific playbook entries.
    # Key: (from_regime, to_regime)
    HIGH_VALUE_TRANSITIONS = {
        ("PREMARKET_DRIFT", "OPEN_MOMENTUM"): {
            "description": "Session open -- highest edge transition",
            "bias": "AGGRESSIVE",
            "first_signal_bonus": 25,   # Add to momentum/confidence score
            "size_boost": 1.5,          # 1.5x normal size
        },
        ("OPEN_MOMENTUM", "MID_MORNING"): {
            "description": "First pullback window opening",
            "bias": "NORMAL",
            "first_signal_bonus": 15,
            "size_boost": 1.2,
        },
        ("MID_MORNING", "AFTERNOON_CHOP"): {
            "description": "Entering death zone -- reduce exposure",
            "bias": "DEFENSIVE",
            "first_signal_bonus": 0,
            "size_boost": 0.5,          # Half size entering chop
        },
        ("AFTERNOON_CHOP", "LATE_AFTERNOON"): {
            "description": "Institutional repositioning begins",
            "bias": "CAUTIOUS",
            "first_signal_bonus": 10,
            "size_boost": 1.0,
        },
        ("LATE_AFTERNOON", "CLOSE_CHOP"): {
            "description": "Close approaching -- wind down",
            "bias": "DEFENSIVE",
            "first_signal_bonus": 0,
            "size_boost": 0.3,
        },
    }

    def __init__(self):
        self._last_regime = None
        self._transition_time = None
        self._first_signal_used = False
        self._current_transition = None     # Active transition info (if any)
        self._transition_history = []       # Log of all transitions today

    # ─── Detection ─────────────────────────────────────────────────

    def check_transition(self, current_regime: str) -> dict | None:
        """
        Check if a regime transition just happened.
        Returns transition info dict if this is a new transition, None otherwise.
        """
        if self._last_regime is None:
            # First call -- initialize without triggering a transition
            self._last_regime = current_regime
            return None

        if current_regime == self._last_regime:
            return None

        # Transition detected
        from_regime = self._last_regime
        to_regime = current_regime
        self._last_regime = current_regime

        transition_key = (from_regime, to_regime)
        playbook = self.HIGH_VALUE_TRANSITIONS.get(transition_key)

        transition = {
            "from": from_regime,
            "to": to_regime,
            "time": time.time(),
            "is_high_value": playbook is not None,
            "playbook": playbook,
        }

        if playbook:
            logger.info(f"[TRANSITION] HIGH VALUE: {from_regime} -> {to_regime} "
                         f"-- {playbook['description']} (bias={playbook['bias']})")
        else:
            logger.info(f"[TRANSITION] {from_regime} -> {to_regime}")

        # Set up first-signal tracking
        self._transition_time = time.time()
        self._first_signal_used = False
        self._current_transition = transition

        # Record in history
        self._transition_history.append({
            "from": from_regime,
            "to": to_regime,
            "time": time.time(),
            "is_high_value": transition["is_high_value"],
            "description": playbook["description"] if playbook else f"{from_regime} -> {to_regime}",
        })
        # Keep last 20 transitions
        self._transition_history = self._transition_history[-20:]

        return transition

    # ─── Bonus Scoring ─────────────────────────────────────────────

    def get_transition_bonus(self, current_regime: str) -> dict:
        """
        If we're within TRANSITION_WINDOW_S after a high-value transition
        and haven't used the first-signal bonus yet, return it.

        Returns: {
            active: bool,
            bonus_score: int,
            size_boost: float,
            description: str,
            bias: str,
            seconds_remaining: float,
        }
        """
        default = {
            "active": False,
            "bonus_score": 0,
            "size_boost": 1.0,
            "description": "",
            "bias": "NORMAL",
            "seconds_remaining": 0,
        }

        if (not self._current_transition
                or not self._current_transition.get("is_high_value")
                or self._first_signal_used
                or self._transition_time is None):
            return default

        elapsed = time.time() - self._transition_time
        if elapsed > TRANSITION_WINDOW_S:
            # Window expired
            return default

        playbook = self._current_transition["playbook"]
        if not playbook:
            return default

        return {
            "active": True,
            "bonus_score": playbook["first_signal_bonus"],
            "size_boost": playbook["size_boost"],
            "description": playbook["description"],
            "bias": playbook["bias"],
            "seconds_remaining": round(TRANSITION_WINDOW_S - elapsed, 0),
        }

    def mark_signal_used(self):
        """Called when the first post-transition signal is taken."""
        if not self._first_signal_used:
            self._first_signal_used = True
            logger.info("[TRANSITION] First post-transition signal consumed")

    # ─── Daily Reset ───────────────────────────────────────────────

    def reset_daily(self):
        """Reset transition state for a new trading day."""
        self._last_regime = None
        self._transition_time = None
        self._first_signal_used = False
        self._current_transition = None
        self._transition_history = []

    # ─── Dashboard ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """For dashboard display."""
        bonus = self.get_transition_bonus(self._last_regime or "UNKNOWN")
        return {
            "current_regime": self._last_regime,
            "transition_bonus": bonus,
            "transitions_today": len(self._transition_history),
            "recent_transitions": self._transition_history[-5:],
            "high_value_transitions_seen": sum(
                1 for t in self._transition_history if t.get("is_high_value")
            ),
        }
