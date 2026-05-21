"""
tests/test_sim_bot_no_zero_gate.py — sim_bot ↔ production gate parity
======================================================================

2026-05-21 (B-030 / Phase 13 Ship Audit pt4):

`bots/sim_bot.py` historically defined a `SIM_STRATEGY_OVERRIDES` dict
that NEUTERED every gate on every strategy ("ZERO_GATE harness"). This
was a lab-data-collection design from 2026-04-21 (commit 03687ef) that
predates the Phase 13 ship plan.

The cost surfaced on 2026-05-21 when bias_momentum (the strategy's
single best earner per 5y backtest: +$308K, PF 1.45, 6/6 years
positive) fired 12 live trades with WR 8.3% (1W/11L, -$330) — the
exact opposite of the 5y backtest's WR 38.8%. Root cause: the
SIM_STRATEGY_OVERRIDES dict was setting `min_tf_votes=1`,
`min_confluence=0.0`, `min_momentum=0` (effectively every weak signal
made it through).

This file enforces that sim_bot uses production gates going forward,
so the same regression cannot land silently.
"""
from __future__ import annotations


def test_sim_strategy_overrides_is_empty_by_default():
    """The ZERO_GATE harness must not be re-introduced silently. Any
    per-strategy override entry needs an explicit code review since it
    diverges sim_bot from the 5y backtest the operator validates against."""
    from bots.sim_bot import SIM_STRATEGY_OVERRIDES
    assert SIM_STRATEGY_OVERRIDES == {}, (
        f"SIM_STRATEGY_OVERRIDES must be empty by default — the entries "
        f"loosen production gates and make sim trades unrepresentative of "
        f"production. Current entries: {sorted(SIM_STRATEGY_OVERRIDES.keys())}. "
        f"If a temporary override is genuinely needed for data collection on "
        f"a specific strategy, add it with a TODO + removal-criteria comment "
        f"AND update this test to allowlist it."
    )


def test_sim_zero_gate_is_empty_by_default():
    """SIM_ZERO_GATE used to set bot-wide risk_per_trade=$15 and
    max_daily_loss=$10K, which let single strategies run away.
    Production values from config/settings.py should apply instead."""
    from bots.sim_bot import SIM_ZERO_GATE
    assert SIM_ZERO_GATE == {}, (
        f"SIM_ZERO_GATE must be empty — production risk values from "
        f"config/settings.py should drive sim_bot's RiskManager init. "
        f"Current entries: {sorted(SIM_ZERO_GATE.keys())}."
    )


def test_bias_momentum_production_gates_are_intact():
    """Direct check on config/strategies.py — the production gates for
    bias_momentum should remain at their research-validated values.
    If any of these drift, sim_bot will silently fire more (worse)
    signals than the 5y backtest validated."""
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["bias_momentum"]
    # min_tf_votes was 3 originally; loosened to 2 in V2 deployment
    # (2026-05-17). The backtest data validates 2 as the working value.
    # If this ever drops below 2 the strategy will fire on noise.
    assert cfg.get("min_tf_votes", 3) >= 2, (
        f"bias_momentum min_tf_votes={cfg.get('min_tf_votes')}; must be "
        f">= 2. Below this the multi-TF alignment premise of the strategy "
        f"falls apart and live WR drops to single digits."
    )
    # Production must have skip_on_stop_clamp=True (F-012 fix).
    # Without this, clamped natural-stop signals fire and lose 0W/5L
    # per the 2026-05-03 forensic audit.
    assert cfg.get("skip_on_stop_clamp") is True, (
        "bias_momentum skip_on_stop_clamp must be True — F-012 forensic "
        "audit showed clamped natural-stop signals are 0W/5L."
    )
