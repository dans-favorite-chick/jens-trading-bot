"""H2 root-cause regression tests for FINDING-2026-06-04-RECON-REPLAY-AS-ENTRY.

These tests pin the EXISTING (correct) persistence behavior of
`PositionManager.open_position(reconciled=True)` so a future refactor
that breaks idempotency surfaces immediately.

Background (from PHANTOM-NT8 Round 2 verification + Round 3 Phase 1
root-cause analysis):

  - `core/position_manager.py:680` unconditionally appends:
        self._positions[trade_id] = pos
    So a reconciled position IS persisted into `_positions`.
  - `active_positions` (line 463-466) returns `list(self._positions.values())`
    with NO filter — reconciled positions DO surface there.
  - `open_position` guards at line 631 (trade_id collision) and line 634
    (`is_flat_for(strategy)`-based strategy-slot collision) — both reject
    a duplicate adoption attempt.
  - So the runtime reconciliation loop's idempotency check
    (`core/startup_reconciliation.py:195-216`, walking
    `positions.active_positions` to build the
    `already_tracked_accounts` set) DOES detect a previously-adopted
    orphan when the strategy label matches.

The actual H2 bug (re-adoption every cycle) was only ever a symptom of
H3 — when a multi-strategy account (e.g. Sim101) labels its reconciled
positions as `_reconciled_<account>`, the slot-collision guard at
line 634 fires correctly inside `open_position` (the second adopt for
the same trade_id is refused), BUT the bot-side `is_flat_for(real_strategy)`
gate (called in `_ws_dispatcher.py:725, 730` + `sim_bot.py:694`)
does NOT see the `_reconciled_<account>` row as occupying the slot —
so a fresh real-strategy signal still fires on the account that already
has reconciled exposure. That's H3, fixed in
`tests/test_is_flat_for_reconciled_slot.py`.

These tests defend against a separate regression class: someone
introducing a hidden filter on `active_positions` or a conditional
that skips `_positions[trade_id] = pos` for `reconciled=True`.

Run: pytest tests/test_position_manager_active_positions.py -v
"""
from __future__ import annotations

import pytest

from core.position_manager import PositionManager


@pytest.fixture
def positions(tmp_path, monkeypatch):
    """Fresh PositionManager with isolated trade_memory path."""
    from core import position_manager as pm
    monkeypatch.setattr(
        pm, "TRADE_MEMORY_PATH", str(tmp_path / "trade_memory.json")
    )
    return PositionManager(load_history=False)


def _open_reconciled(positions, *, trade_id, strategy, account="Sim101",
                     direction="SHORT", contracts=1, entry_price=30368.5):
    """Mirror the `core.startup_reconciliation.reconcile_positions_from_nt8`
    call that adopts an orphan NT8 fill at boot OR mid-session."""
    return positions.open_position(
        trade_id=trade_id,
        direction=direction,
        entry_price=entry_price,
        contracts=contracts,
        stop_price=entry_price + 25.0 if direction == "SHORT" else entry_price - 25.0,
        target_price=entry_price - 37.5 if direction == "SHORT" else entry_price + 37.5,
        strategy=strategy,
        reason="reconciled_from_nt8",
        market_snapshot={"reconciled": True, "account": account},
        metadata={"source": "manual_reconciled", "reconciled_from_orphan": True,
                  "strategy_original_attribution": "bias_momentum"},
        account=account,
        reconciled=True,
    )


class TestReconciledPositionPersistence:
    """Pins the H2 invariant: reconciled positions MUST persist into
    `_positions` and surface via `active_positions` so the runtime
    reconciliation loop's idempotency check can see them."""

    def test_reconciled_position_appears_in_active_positions(self, positions):
        ok = _open_reconciled(
            positions,
            trade_id="RECONCILED_Sim101_h2test",
            strategy="bias_momentum",
        )
        assert ok, "open_position(reconciled=True) must return True on success"
        assert positions.active_count == 1
        assert len(positions.active_positions) == 1
        # The position must carry the account so reconciliation's
        # `already_tracked_accounts` set is correctly built.
        pos = positions.active_positions[0]
        assert pos.account == "Sim101"
        assert pos.reconciled is True

    def test_is_flat_for_real_strategy_label_returns_false(self, positions):
        """The H2 scenario where reconciliation labels with the real
        strategy name (single-strategy-account branch). is_flat_for
        for that exact strategy MUST return False (slot held)."""
        _open_reconciled(
            positions,
            trade_id="RECONCILED_SimBias_h2test",
            strategy="bias_momentum",
            account="SimBias Momentum",
        )
        assert positions.is_flat_for("bias_momentum") is False, (
            "real-strategy-labeled reconciled position must hold the slot"
        )

    def test_second_open_position_with_same_trade_id_rejected(self, positions):
        """The runtime reconciliation loop's idempotency relies on
        open_position refusing a duplicate trade_id. Pin that."""
        tid = "RECONCILED_Sim101_idem"
        ok1 = _open_reconciled(positions, trade_id=tid, strategy="bias_momentum")
        ok2 = _open_reconciled(positions, trade_id=tid, strategy="bias_momentum")
        assert ok1 is True
        assert ok2 is False, (
            "Second open_position with the same trade_id must be refused"
        )
        assert positions.active_count == 1, (
            "Refused duplicate must NOT increment active_count — H2 phantom-"
            "multiplication regression"
        )

    def test_second_open_position_with_same_strategy_label_rejected(self, positions):
        """Even with a different trade_id, the strategy-slot guard at
        line 634 must reject. Pin so a refactor that drops the slot
        check shows up here, not in production."""
        ok1 = _open_reconciled(
            positions, trade_id="RECONCILED_SimBias_a",
            strategy="bias_momentum", account="SimBias Momentum",
        )
        ok2 = _open_reconciled(
            positions, trade_id="RECONCILED_SimBias_b",
            strategy="bias_momentum", account="SimBias Momentum",
        )
        assert ok1 is True
        assert ok2 is False, (
            "Second open_position with the same strategy must be refused "
            "(strategy-slot collision guard at position_manager.py:634)"
        )
        assert positions.active_count == 1


class TestReconciledPositionSurfaceContract:
    """Pins the property contract callers rely on. If a future
    refactor (e.g. splitting reconciled positions into a sibling
    `_reconciled_positions` dict) breaks `active_positions`, this
    test catches it before the live reconciliation loop does."""

    def test_active_count_includes_reconciled(self, positions):
        _open_reconciled(positions,
                         trade_id="RECONCILED_Sim101_x",
                         strategy="bias_momentum")
        assert positions.active_count == 1

    def test_get_position_by_trade_id_finds_reconciled(self, positions):
        tid = "RECONCILED_Sim101_gpb"
        _open_reconciled(positions, trade_id=tid, strategy="bias_momentum")
        found = positions.get_position(tid)
        assert found is not None
        assert found.trade_id == tid
        assert found.reconciled is True
        assert found.account == "Sim101"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
