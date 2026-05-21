"""
tests/test_playbooks_no_silent_override.py — PLAYBOOK gate guardrail
=====================================================================

2026-05-21 (S-001 / Phase 13 Ship Audit pt4):

`core/regime_playbooks.py` PLAYBOOKS dict can override production
strategy gates based on HMM regime. This is the EXACT same pattern as
the B-030 sim_bot ZERO_GATE bug:

  for strat in self.strategies:
      pb_overrides = self.playbook_mgr.get_strategy_overrides(strat.name)
      for k, v in pb_overrides.items():
          strat.config[k] = v   # ← silent production-config mutation

The PLAYBOOKS values were never validated against the 5y backtest that
production gates passed. Loosening them silently lands a B-030 clone.

This test enforces that either:
  - PLAYBOOK_ENABLED is False (safe default), OR
  - The dispatcher is gated behind a future per-strategy validation
    check that hasn't been built yet.

If you flip PLAYBOOK_ENABLED to True, add a new assertion here:
  every PLAYBOOKS[regime][strategy][gate_key] >= STRATEGIES[strategy][gate_key]
i.e., playbooks can only TIGHTEN gates, never loosen them.
"""
from __future__ import annotations


def test_playbook_enabled_is_false_by_default():
    """The PLAYBOOKS dispatcher silently overrides production gates the
    same way SIM_STRATEGY_OVERRIDES did before B-030. Default must be
    OFF until each playbook entry is research-validated."""
    from config.settings import PLAYBOOK_ENABLED
    assert PLAYBOOK_ENABLED is False, (
        "PLAYBOOK_ENABLED is True — the dispatcher at bots/base_bot.py:3633 "
        "will silently override every production gate based on HMM regime. "
        "Before flipping True, audit core/regime_playbooks.py PLAYBOOKS dict: "
        "for bias_momentum TRENDING the playbook loosens min_confluence "
        "5.5 → 1.5 (73% looser) — same hidden-loosening pattern that "
        "caused B-030. Either tighten PLAYBOOKS values to match or exceed "
        "production STRATEGIES values, or add a regression test that asserts "
        "loosening direction is impossible."
    )
