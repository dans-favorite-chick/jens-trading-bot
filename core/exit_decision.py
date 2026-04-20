"""
P4b — Exit-collision priority function (SHADOW MODE).

Purpose: provide a structural decide_exit(position, candidates) that
priority-orders competing exit triggers into a single decision. The
current tick loop (bots/base_bot.py lines ~595-770) has 9 sequential
if-blocks each potentially calling `await self._exit_trade(...)`;
the implicit priority is order-of-execution + `self.positions.position`
guards preventing double-exits. That works, but it's scattered —
a new exit condition added in slot 5 can't know how its priority
compares to slot 3 without reading the entire tick loop.

THIS SHIPS IN SHADOW MODE: base_bot calls decide_exit() to LOG what
it would have chosen, but the actual exit is still driven by the
sequential if-blocks. The shadow log lets us verify that decide_exit
agrees with the production behavior before flipping to active mode.

DO NOT wire decide_exit() as the authoritative decision point until:
(1) at least 100 real exit events have been cross-checked in shadow
    log, with zero mismatches vs production sequential exits
(2) a deliberate code change with its own tests converts the
    sequential if-blocks to populate a candidates list + consume
    decide_exit()'s result

Priority table (highest priority wins — lowest rank number):
  Rank | Reason                             | Why first
  -----+------------------------------------+--------------------
    0  | pending_exit (market close)        | Pre-armed by operator/watchdog
    1  | hard_stop (bracket stop)           | Capital preservation
    2  | eod_flat_universal                 | Session-close safety
    3  | chandelier_trail_hit               | Trend reversal (ORB runner)
    4  | signal_flip / managed exit         | Strategy-specific invalidation
    5  | trend_stall (rider)                | Momentum exhaustion
    6  | ema_dom_exit (smart)               | Microstructure reversal
    7  | target_hit (bracket target)        | Planned profit-take
    8  | time_stop / max_hold               | Hold budget expired
    9  | scale_out_partial                  | Non-exit; partial size down
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════
# Priority table
# ═══════════════════════════════════════════════════════════════════════
EXIT_PRIORITY: dict[str, int] = {
    # Lowest number = highest priority.
    "pending_exit":           0,
    "market_close":           0,
    "hard_stop":              1,
    "stop_hit":               1,
    "bracket_stop":           1,
    "eod_flat_universal":     2,
    "eod_flat":               2,
    "chandelier_trail_hit":   3,
    "signal_flip":            4,
    "managed_exit":           4,
    "price_returns_inside_noise_area": 4,
    "trend_stall":            5,
    "ema_dom_exit":           6,
    "target_hit":             7,
    "bracket_target":         7,
    "time_stop":              8,
    "max_hold":               8,
    "scale_out_partial":      9,   # Not an exit; partial size reduction
}

# Reasons we treat as actual exits (scale_out_partial is NOT an exit).
_NON_EXIT_REASONS = {"scale_out_partial"}

# Default priority for unknown reasons — just above scale_out_partial.
_UNKNOWN_PRIORITY = 99


@dataclass
class ExitCandidate:
    """A single exit-trigger observation seen during one tick."""
    reason: str                        # Matches EXIT_PRIORITY keys when possible
    detail: str = ""                   # Free-form for logs
    source: str = ""                   # "bracket" | "rider" | "managed" | "universal"
    price: Optional[float] = None      # Price at which the exit fires (for logs)


@dataclass
class ExitDecision:
    """Result of decide_exit()."""
    should_exit: bool
    reason: str                        # Winning candidate's reason (or "" if none)
    priority: int                      # Winning candidate's priority rank
    candidates_considered: list[str] = field(default_factory=list)
    # Free-form explanation for shadow-mode logging:
    explain: str = ""

    def __bool__(self) -> bool:
        return self.should_exit


def priority_of(reason: str) -> int:
    """Lookup priority rank; unknown reasons default to a very-low priority."""
    if not reason:
        return _UNKNOWN_PRIORITY
    return EXIT_PRIORITY.get(reason, _UNKNOWN_PRIORITY)


def decide_exit(candidates: list[ExitCandidate]) -> ExitDecision:
    """
    Given one or more competing exit-trigger observations from a single
    tick, pick ONE winning exit reason using the priority table.

    Invariants:
      - Empty list  → should_exit=False
      - One candidate → that candidate wins (unless it's a non-exit reason)
      - Ties  → first candidate in the list wins (stable ordering)
      - scale_out_partial is NOT an exit — it's filtered out of the
        exit decision (a caller may still act on it for partial sizing)

    SHADOW MODE: callers log the decision alongside the actual sequential
    exit that fired, to verify agreement over live sessions before we
    make this the authoritative decision point.
    """
    if not candidates:
        return ExitDecision(
            should_exit=False, reason="", priority=_UNKNOWN_PRIORITY,
            explain="no candidates",
        )

    # Filter out non-exit reasons for the exit decision; they remain in the
    # candidate list for the explain string but don't drive should_exit.
    considered = [c.reason for c in candidates]
    exit_candidates = [c for c in candidates if c.reason not in _NON_EXIT_REASONS]
    if not exit_candidates:
        return ExitDecision(
            should_exit=False, reason="", priority=_UNKNOWN_PRIORITY,
            candidates_considered=considered,
            explain=f"only non-exit reasons: {considered}",
        )

    # Stable sort by priority rank. Python's sort is stable so ties preserve
    # insertion order — first-seen wins.
    sorted_exits = sorted(exit_candidates, key=lambda c: priority_of(c.reason))
    winner = sorted_exits[0]

    # Explain string lists all seen + the winner
    explain_parts = [f"{c.reason}(p{priority_of(c.reason)})" for c in candidates]
    explain = f"{len(candidates)} candidates: " + ", ".join(explain_parts)
    if len(exit_candidates) > 1:
        explain += f" → {winner.reason} wins"

    return ExitDecision(
        should_exit=True,
        reason=winner.reason,
        priority=priority_of(winner.reason),
        candidates_considered=considered,
        explain=explain,
    )


def would_override(new_reason: str, current_reason: str) -> bool:
    """
    Helper: given a currently-selected exit reason and a new one seen
    on the same tick, does the new one take over? True if new has
    strictly higher priority (lower rank).

    Useful for callers that want to accumulate candidates across a
    non-trivial chain of if-blocks without building a full list first.
    """
    return priority_of(new_reason) < priority_of(current_reason)
