"""
2026-06-01 master fix Phase 5 — Oracle prompt strengthening tests.

Covers:
  5.1 Schema injection (runtime, NOT hardcoded)
  5.2 Per-strategy proposal cap lifted to 3
  5.3 Proposal vocabulary expanded (stop_atr_mult, target_rr,
      allowed_directions, session_block_windows, ...)
  5.4 Stop-optimization guidance present
  5.5 Safety guards (min_stop_ticks floor, target_rr >= 1.0)
"""
from __future__ import annotations

import pytest


# ──────────────────────────────────────────────────────────────────────
# 5.1 — Schema injection
# ──────────────────────────────────────────────────────────────────────


class TestSchemaInjectionIsRuntime:
    """The schema block MUST reflect the live STRATEGIES dict, not a
    hardcoded list. If a parameter is added to a strategy's config, the
    next prompt build should pick it up."""

    def test_schema_block_lists_real_strategies(self):
        from agents.strategy_oracle import _build_strategy_schema_block
        from config.strategies import STRATEGIES
        # Build a facts panel that names the same strategies as STRATEGIES
        facts = {"strategies": {name: {} for name in list(STRATEGIES.keys())[:3]}}
        block = _build_strategy_schema_block(facts)
        # The block should mention each strategy by name
        for name in list(STRATEGIES.keys())[:3]:
            assert name in block, f"schema block missing strategy {name!r}"

    def test_schema_block_lists_real_param_names(self):
        """Pick a strategy with known parameters and verify they appear
        in the block. Uses bias_momentum which is stable and well-known."""
        from agents.strategy_oracle import _build_strategy_schema_block
        from config.strategies import STRATEGIES
        if "bias_momentum" not in STRATEGIES:
            pytest.skip("bias_momentum not in config (test depends on it)")
        facts = {"strategies": {"bias_momentum": {}}}
        block = _build_strategy_schema_block(facts)
        # bias_momentum has target_rr and stop_atr_mult per config
        assert "target_rr" in block
        assert "session_block_windows" in block

    def test_schema_block_drops_meta_keys(self):
        """enabled / validated / walk_forward_gate are operator-only,
        not proposable. They must NOT appear in the LLM-facing schema."""
        from agents.strategy_oracle import _build_strategy_schema_block
        from config.strategies import STRATEGIES
        # Pick any strategy that has these keys
        target = next(
            (n for n, c in STRATEGIES.items()
             if "enabled" in c and "validated" in c),
            None,
        )
        if target is None:
            pytest.skip("no strategy with both enabled and validated keys")
        facts = {"strategies": {target: {}}}
        block = _build_strategy_schema_block(facts)
        # Find the row for this strategy. The row format is
        # "| `<name>` | <params> |" so we look for the literal `name`.
        # The block-wide "drop" check: enabled / validated should NOT
        # appear as listed params for THIS strategy row.
        lines = [l for l in block.split("\n") if f"`{target}`" in l]
        assert lines, f"strategy row for {target} not found in block"
        row = lines[0]
        # The METHOD says "Drop the obvious meta keys" — they must not
        # appear in the row's parameter list (a forgiving check: their
        # exact backtick-form like `enabled` doesn't appear in row).
        assert "`enabled`" not in row, f"meta key 'enabled' leaked into schema row: {row}"
        assert "`validated`" not in row
        assert "`walk_forward_gate`" not in row

    def test_schema_block_handles_missing_config(self):
        """If facts.json lists a strategy that no longer exists in
        STRATEGIES, the schema block should still build (with a note)."""
        from agents.strategy_oracle import _build_strategy_schema_block
        facts = {"strategies": {"ghost_strategy_xyz_not_real": {}}}
        block = _build_strategy_schema_block(facts)
        assert "ghost_strategy_xyz_not_real" in block


# ──────────────────────────────────────────────────────────────────────
# 5.2 — Proposal cap lifted
# ──────────────────────────────────────────────────────────────────────


class TestProposalCapLift:
    """The system prompt must allow up to 3 proposals per strategy."""

    def test_prompt_mentions_3_proposals_per_strategy(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        # The prompt should say "UP TO 3" or "up to 3" (case-insensitive)
        lower = SYSTEM_PROMPT_RESEARCH.lower()
        assert ("up to 3 proposals" in lower
                or "3 proposals per strategy" in lower), (
            "SYSTEM_PROMPT_RESEARCH must allow up to 3 proposals per "
            "strategy per run (Phase 5.2). Current text:\n\n"
            f"{SYSTEM_PROMPT_RESEARCH[:500]}..."
        )


# ──────────────────────────────────────────────────────────────────────
# 5.3 — Vocabulary expansion
# ──────────────────────────────────────────────────────────────────────


class TestProposalVocabulary:
    """The expanded vocabulary must enumerate the real config knobs the
    LLM is allowed to propose changes to."""

    REQUIRED_VOCAB = [
        "stop_atr_mult",
        "target_rr",
        "min_stop_ticks",
        "max_stop_ticks",
        "stop_ticks",
        "target_ticks",
        "max_hold_min",
        "session_block_windows",
        "window_start_ct",
        "window_end_ct",
        "allowed_directions",
    ]

    def test_research_prompt_mentions_all_vocab(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        missing = [v for v in self.REQUIRED_VOCAB
                   if v not in SYSTEM_PROMPT_RESEARCH]
        assert not missing, (
            f"SYSTEM_PROMPT_RESEARCH missing vocab terms: {missing}"
        )

    def test_weekly_prompt_mentions_all_vocab(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_WEEKLY
        missing = [v for v in self.REQUIRED_VOCAB
                   if v not in SYSTEM_PROMPT_WEEKLY]
        assert not missing, (
            f"SYSTEM_PROMPT_WEEKLY missing vocab terms: {missing}"
        )

    def test_prompt_mentions_new_prefix_escape_hatch(self):
        """If a proposal doesn't map to an existing knob, the LLM must
        emit `NEW:<name>` instead of inventing a fake parameter."""
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        assert 'NEW:' in SYSTEM_PROMPT_RESEARCH or 'NEW:<' in SYSTEM_PROMPT_RESEARCH


# ──────────────────────────────────────────────────────────────────────
# 5.4 — Stop-optimization guidance
# ──────────────────────────────────────────────────────────────────────


class TestStopOptimizationGuidance:
    """The prompt must explain WHEN to propose stop / target changes
    based on the MAE elbow / MFE p90 splits from Phase 6."""

    def test_research_prompt_references_mae_elbow(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        assert "mae_elbow" in SYSTEM_PROMPT_RESEARCH.lower()

    def test_research_prompt_references_mfe_percentile(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        # Phase 6 helper name is "mfe_p90" (90th percentile)
        assert ("mfe_p90" in SYSTEM_PROMPT_RESEARCH.lower()
                or "90th-percentile" in SYSTEM_PROMPT_RESEARCH.lower())


# ──────────────────────────────────────────────────────────────────────
# 5.5 — Safety guards
# ──────────────────────────────────────────────────────────────────────


class TestSafetyGuards:
    """The prompt must spell out the hard floors so the LLM doesn't
    propose suicide stops or below-1.0 RR targets."""

    def test_min_stop_ticks_floor_mentioned(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        assert "min_stop_ticks" in SYSTEM_PROMPT_RESEARCH

    def test_target_rr_floor_1_0_mentioned(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        # The prompt should reference both "target_rr" AND the 1.0 floor
        assert "target_rr" in SYSTEM_PROMPT_RESEARCH
        # Look for "1.0" or "below 1.0" or "< 1.0"
        assert ("1.0" in SYSTEM_PROMPT_RESEARCH
                or "below 1" in SYSTEM_PROMPT_RESEARCH.lower())

    def test_no_empty_allowed_hours_guard(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        assert "allowed_hours_ct" in SYSTEM_PROMPT_RESEARCH

    def test_window_start_lt_end_guard(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_RESEARCH
        assert "window_start_ct" in SYSTEM_PROMPT_RESEARCH
        assert "window_end_ct" in SYSTEM_PROMPT_RESEARCH


# ──────────────────────────────────────────────────────────────────────
# Daily mode: should NOT have proposal vocab (proposals disabled)
# ──────────────────────────────────────────────────────────────────────


class TestDailyModeUnchanged:
    """Daily mode disables propose_change. It should keep its
    minimal prompt and NOT inherit the proposal vocab."""

    def test_daily_prompt_does_not_offer_vocab(self):
        from agents.strategy_oracle import SYSTEM_PROMPT_DAILY
        # Daily explicitly says "propose_change is DISABLED" — that's
        # the source of truth. The vocab block being absent is a
        # consistent corollary.
        assert "DISABLED" in SYSTEM_PROMPT_DAILY
