"""H3 root-cause regression tests for FINDING-2026-06-04-RECON-REPLAY-AS-ENTRY.

The 2026-06-04 18:01:13 incident built Sim101 from FLAT to SHORT 7 in
137 ms because every signal-side `is_flat_for(strategy)` check returned
True even though Sim101 was already holding a reconciled orphan position
labeled `_reconciled_Sim101`.

Root cause (Round 3 Phase 2 analysis):

  - `core/startup_reconciliation.py:249-257` labels reconciled positions
    on MULTI-strategy accounts (Sim101: big_move_signal + es_nq_confluence)
    as `_reconciled_<account>` instead of any one real strategy name.
    The rationale per FINDING-2026-06-04-REDTEAM-1 was to avoid
    misattributing dashboard P&L to a wrong strategy.
  - `core/position_manager.py:472` `is_flat_for(strategy)` does string-
    equality on `pos.strategy == strategy`. `bias_momentum != _reconciled_Sim101`,
    so the check returns True (slot free), and the signal-gate at
    `bots/_ws_dispatcher.py:725, 730` + `bots/sim_bot.py:694` permits the
    fresh entry to fire on top of a reconciled orphan.

The fix (Option A in the master prompt — smaller protected surface area):
extend `is_flat_for` with an optional `account` parameter. When supplied,
the method ALSO returns False if any `_reconciled_<account>` position is
held against the same account. When NOT supplied (legacy callers), the
method falls back to a routing-aware auto-detect via
`config.account_routing.get_account_for_signal(strategy)` so the existing
3 callers automatically benefit. Both lookups are fail-open (any
exception treated as "slot free") to honor Phoenix's fail-open posture
on observability/routing failures.

This file's tests:

  - test_real_strategy_blocked_by_reconciled_orphan_on_same_account
      → fails before the fix; passes after.
  - test_explicit_account_param_blocks_real_strategy
      → new contract: `is_flat_for(strategy, account=...)` signature.
  - test_orphan_on_different_account_does_not_block_strategy
      → the fix MUST NOT over-block (e.g. SimBias Momentum reconciled
        does NOT block a Sim101-routed strategy).
  - test_no_orphan_means_slot_is_free
      → sanity: when no reconciled position exists, is_flat_for(strategy)
        still returns True.
  - test_legacy_callers_get_routing_auto_lookup
      → without the account kwarg, the routing-aware path still kicks in
        and protects existing call sites in _ws_dispatcher.py + sim_bot.py.

Run: pytest tests/test_is_flat_for_reconciled_slot.py -v
"""
from __future__ import annotations

import pytest

from core.position_manager import PositionManager


@pytest.fixture
def positions(tmp_path, monkeypatch):
    from core import position_manager as pm
    monkeypatch.setattr(
        pm, "TRADE_MEMORY_PATH", str(tmp_path / "trade_memory.json")
    )
    return PositionManager(load_history=False)


def _open_reconciled_orphan(positions, *, account, trade_id_suffix="h3test",
                            direction="SHORT", contracts=1):
    """Mirror reconciliation on a MULTI-strategy account — labels as
    `_reconciled_<account>` per startup_reconciliation.py:255."""
    entry = 30368.5
    return positions.open_position(
        trade_id=f"RECONCILED_{account}_{trade_id_suffix}",
        direction=direction,
        entry_price=entry,
        contracts=contracts,
        stop_price=entry + 25.0 if direction == "SHORT" else entry - 25.0,
        target_price=entry - 37.5 if direction == "SHORT" else entry + 37.5,
        strategy=f"_reconciled_{account}",
        reason="reconciled_from_nt8",
        market_snapshot={"reconciled": True, "account": account},
        metadata={"source": "manual_reconciled", "reconciled_from_orphan": True,
                  "strategy_original_attribution": "big_move_signal"},
        account=account,
        reconciled=True,
    )


# ─────────────────────────────────────────────────────────────────
# CORE H3 REGRESSION (these fail before the fix, pass after)
# ─────────────────────────────────────────────────────────────────

class TestReconciledSlotInterlock:
    def test_real_strategy_blocked_by_reconciled_orphan_on_same_account(
        self, positions,
    ):
        """The exact 2026-06-04 18:01:13 incident reproduction.

        Sim101 has a reconciled orphan from a manual operator fill
        (labeled `_reconciled_Sim101`). A real bias_momentum signal
        arrives. is_flat_for("bias_momentum") MUST return False because
        bias_momentum routes to a different account today
        (SimBias Momentum) — wait, no, the H3 scenario is for
        strategies that route TO Sim101 specifically. Use
        big_move_signal which is mapped to Sim101.
        """
        ok = _open_reconciled_orphan(positions, account="Sim101")
        assert ok
        # big_move_signal routes to Sim101 per account_routing.py:83
        assert positions.is_flat_for("big_move_signal") is False, (
            "H3 root cause: is_flat_for('big_move_signal') must return "
            "False when a `_reconciled_Sim101` orphan is held — but the "
            "pre-fix path returned True because the strategy-label "
            "string-equality check missed the pseudo-label."
        )

    def test_explicit_account_param_blocks_real_strategy(self, positions):
        """The Master Prompt Phase 2 test verbatim — caller knows the
        account, passes it explicitly via the new `account` kwarg."""
        ok = _open_reconciled_orphan(positions, account="Sim101")
        assert ok
        assert positions.is_flat_for(
            strategy="bias_momentum", account="Sim101",
        ) is False, (
            "When the caller supplies account='Sim101', is_flat_for must "
            "return False for any real strategy if a `_reconciled_Sim101` "
            "orphan is held."
        )

    def test_legacy_callers_get_routing_auto_lookup(self, positions):
        """The 3 existing in-tree callers (sim_bot.py:694,
        _ws_dispatcher.py:725, 730) do NOT pass account. The fix must
        auto-detect via routing so the existing call sites pick up the
        H3 protection automatically."""
        ok = _open_reconciled_orphan(positions, account="Sim101")
        assert ok
        # No account kwarg — must still block big_move_signal which
        # routes to Sim101.
        assert positions.is_flat_for("big_move_signal") is False


# ─────────────────────────────────────────────────────────────────
# OVER-BLOCK SAFETY (the fix must NOT make is_flat_for return False
# for unrelated strategies / different accounts)
# ─────────────────────────────────────────────────────────────────

class TestNoOverBlock:
    def test_orphan_on_different_account_does_not_block_strategy(
        self, positions,
    ):
        """Reconciled orphan on SimBias Momentum must NOT block
        big_move_signal (which routes to Sim101)."""
        _open_reconciled_orphan(positions, account="SimBias Momentum",
                                trade_id_suffix="cross")
        # big_move_signal routes to Sim101 — different account, must
        # still be flat.
        assert positions.is_flat_for("big_move_signal") is True

    def test_no_orphan_means_slot_is_free(self, positions):
        """Empty PositionManager — every strategy must be flat."""
        assert positions.is_flat_for("bias_momentum") is True
        assert positions.is_flat_for("big_move_signal") is True
        assert positions.is_flat_for("bias_momentum",
                                     account="Sim101") is True

    def test_unmapped_strategy_not_overblocked_by_sim101_orphan(
        self, positions,
    ):
        """Round 3 Bug Hunter Area 3 EXPOSED fix: an UNMAPPED strategy
        (not in `config.account_routing.STRATEGY_ACCOUNT_MAP`) must NOT
        be over-blocked by a Sim101 reconciled orphan.

        Pre-refinement, `get_account_for_signal("future_strategy")`
        falls back to `_default = "Sim101"`, and the H3 interlock then
        blocks every unmapped strategy any time a `_reconciled_Sim101`
        orphan is held. The refinement: H3 only fires when the
        strategy is *explicitly* mapped (i.e. present in
        STRATEGY_ACCOUNT_MAP)."""
        _open_reconciled_orphan(positions, account="Sim101",
                                trade_id_suffix="overblock")
        # "future_strategy_not_in_map" is not present in
        # STRATEGY_ACCOUNT_MAP. The legacy strategy-slot check finds
        # no direct collision, and the H3 routing-aware check must
        # skip the lookup because the strategy is unmapped — so the
        # slot stays free.
        assert positions.is_flat_for("future_strategy_not_in_map") is True

    def test_real_strategy_position_still_blocks_same_strategy(
        self, positions,
    ):
        """Sanity: the H3 fix must not break the legacy strategy-slot
        check. A non-reconciled bias_momentum position must still
        block a subsequent bias_momentum signal."""
        ok = positions.open_position(
            trade_id="bias_normal",
            direction="SHORT",
            entry_price=30315.5,
            contracts=1,
            stop_price=30330.5,
            target_price=30285.5,
            strategy="bias_momentum",
            reason="bias_momentum_signal",
            market_snapshot={},
            account="SimBias Momentum",
        )
        assert ok
        assert positions.is_flat_for("bias_momentum") is False


# ─────────────────────────────────────────────────────────────────
# FAIL-OPEN POSTURE — routing import / lookup failure must NOT block
# the signal path. (Tests Phoenix's standing fail-open invariant.)
# ─────────────────────────────────────────────────────────────────

class TestFailOpenPosture:
    def test_routing_lookup_exception_treated_as_slot_free(
        self, positions, monkeypatch,
    ):
        """If config.account_routing.get_account_for_signal raises,
        is_flat_for must fail open (return True for an empty PM).

        Match the Round 2 _pipeline_healthy convention."""
        import config.account_routing as ar

        def _boom(*a, **kw):
            raise RuntimeError("routing import blew up")

        monkeypatch.setattr(ar, "get_account_for_signal", _boom)
        # With no positions and a broken routing, the answer must be
        # "slot free" — broken observability MUST NOT block the signal
        # path.
        assert positions.is_flat_for("big_move_signal") is True


class TestObservability:
    """Round 3 red-team fixes — H3 must fail-LOUD when bypassed
    (Bucket 3) and must surface when blocking a real signal
    (Bucket 1). Per Phoenix's fail-loudly invariant
    (memory/feedback_silent_failures.md)."""

    def test_routing_exception_emits_warning(
        self, positions, monkeypatch, caplog,
    ):
        """When the routing lookup raises, the fail-open path must
        emit a WARNING. A silent bypass violates the standing
        fail-loudly principle."""
        import config.account_routing as ar
        import logging

        def _boom(*a, **kw):
            raise RuntimeError("routing module corrupted")

        monkeypatch.setattr(ar, "get_account_for_signal", _boom)
        with caplog.at_level(logging.WARNING):
            result = positions.is_flat_for("big_move_signal")
        assert result is True  # fail-open holds
        # The bypass must produce a discoverable WARNING.
        warnings = [r for r in caplog.records
                    if r.levelno >= logging.WARNING
                    and "[H3]" in r.getMessage()
                    and "BYPASSED" in r.getMessage()]
        assert len(warnings) >= 1, (
            "H3 fail-open MUST emit a WARNING so the operator can see "
            "that the interlock was bypassed. Silent fail-open violates "
            "the feedback_silent_failures invariant."
        )

    def test_block_emits_info_log_with_orphan_trade_id(
        self, positions, caplog,
    ):
        """When H3 blocks a real signal, the block must surface in the
        log so the operator can correlate dead-signal periods with
        held orphans."""
        import logging

        ok = _open_reconciled_orphan(positions, account="Sim101",
                                     trade_id_suffix="logtest")
        assert ok
        with caplog.at_level(logging.INFO):
            blocked = positions.is_flat_for("big_move_signal")
        assert blocked is False
        infos = [r for r in caplog.records
                 if r.levelno >= logging.INFO
                 and "[H3]" in r.getMessage()
                 and "BLOCKED" in r.getMessage()]
        assert len(infos) >= 1, (
            "H3 block MUST surface an INFO log line naming the orphan "
            "trade_id so the operator can correlate suppressed signals "
            "with held orphans."
        )
        # And the line must contain the orphan's trade_id for forensics.
        assert any("logtest" in r.getMessage() for r in infos)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
