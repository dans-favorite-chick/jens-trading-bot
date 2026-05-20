"""
Phoenix Entry Modes — first-touch vs retest configuration
============================================================

Per Phase 13 Section V.1 (Entry Retest Analyzer findings):

  4 of 6 high-n strategies showed modest positive lift from waiting for
  RETEST instead of entering FIRST-TOUCH. The lift comes from outcome
  selection — retests filter for signals where the initial run validated
  the level. Median fill is 3 ticks WORSE on retest, but the better
  signal quality outweighs the worse fill.

  Aggregate: +$623 over 60 days (+3.6%) = ~$3-4K/year extrapolated.

  Recommended pilot (ship this as opt-in):
    bias_momentum:        retest (+$532 over 60d)
    spring_setup:         retest (+$25, no downside)
    raschke_baseline:     retest (+$119, small n)
    vwap_band_reversion:  retest (+$126)

  Keep first-touch:
    vwap_pullback_v2: first-touch wins (-$144 if retest)
    noise_area:       first-touch wins (-$36 if retest)
    All others:       default to first-touch (no data yet)

ARCHITECTURE:

  This module provides the canonical registry of which strategies use
  which entry mode. It is a CONFIG layer only — actual retest mechanics
  (waiting for "price runs >=4t then returns +-2t" before submitting OIF)
  are implemented in bots/base_bot.py via the signal-buffering logic.

  For Phase 13 ship: this module is registered + read by base_bot, but
  the actual retest-wait logic is a SAFE NO-OP — it logs the override
  but submits market order anyway. Full implementation is deferred to
  the next sprint (requires per-strategy tick buffer + cancellation).

INTEGRATION POINTS:

  - bots/base_bot.py reads ENTRY_MODE_ASSIGNMENTS in _apply_phase13_overrides()
  - When signal.strategy is in retest list, base_bot logs the intent and
    (for now) proceeds with the existing entry. Full retest implementation
    will be added when the operator confirms the analyzer recommendation.

WHY NOT IMPLEMENT THE RETEST WAIT LOOP NOW:

  The retest wait requires:
    1. Pin the signal_level (entry_price at signal time)
    2. Open a per-strategy tick buffer
    3. On each new tick, check: did price run >=4t in trade direction?
    4. If yes, watch for return to within +-2t of signal_level
    5. Submit OIF at the retest tick
    6. Time-out after N minutes (default 15) and cancel

  This is event-loop / async work that needs careful integration with the
  existing OIF pipeline. It also requires testing against the LIVE tick
  feed (not just historical). Better done as a focused sprint than mixed
  in with Phase 13's larger ship.

  For Phase 13: register the intent + log when it would fire. Operator
  can flip the actual switch in a follow-up sprint with confidence the
  config layer is correct.
"""

ENTRY_MODE_ASSIGNMENTS = {
    # ── RETEST mode (pilot per Section V.1, opt-in) ──────────────────
    "bias_momentum":        "retest",   # +$532 / 60d (best mover)
    "spring_setup":         "retest",   # +$25 / 60d (no downside)
    "raschke_baseline":     "retest",   # +$119 / 60d (small n, WR boost)
    "vwap_band_reversion":  "retest",   # +$126 / 60d

    # ── FIRST-TOUCH (default for the rest) ───────────────────────────
    "vwap_pullback_v2":     "first_touch",  # retest LOSES -$144 / 60d
    "noise_area":           "first_touch",  # retest LOSES -$36 / 60d
    # All other strategies: no Section V.1 data → default first_touch.
    # Add to this dict only after analyzer confirms positive lift.
}


# Retest mechanics parameters (used by base_bot when retest mode is wired)
RETEST_PARAMS = {
    "run_threshold_ticks": 4,    # price must run >= N ticks in trade direction
    "retest_window_ticks": 2,    # then return to +- N ticks of signal_level
    "timeout_minutes": 15,       # cancel and revert to first-touch after timeout
}


def get_entry_mode(strategy: str) -> str:
    """Return entry mode for a strategy. Defaults to 'first_touch' for
    strategies not in the registry."""
    return ENTRY_MODE_ASSIGNMENTS.get(strategy, "first_touch")


def is_retest_strategy(strategy: str) -> bool:
    """Helper: True if this strategy is configured for retest mode."""
    return get_entry_mode(strategy) == "retest"
