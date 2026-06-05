"""Phase 1.5 audit test for FINDING-2026-06-04-RECON-REPLAY-AS-ENTRY round 3.

The Round 2 Bug Hunter raised a concern that `scale_out_partial` leaves
`pos.original_contracts` at the FULL original count after partial exit,
and that this could feed a downstream OCO writer with a stale qty
producing oversize PARTIAL_EXIT OIFs.

Round 3 Phase 1.5 root-cause audit found:

  - Operational readers of `pos.original_contracts`:
      core/position_manager.py:1151  (dashboard serialization — display-only)
      bots/_ws_dispatcher.py:474    (scale-out gate)

  - The scale-out gate at _ws_dispatcher.py:474 is:
        elif SCALE_OUT_ENABLED and not pos.scaled_out and pos.original_contracts >= 2:
    The `not pos.scaled_out` half is False after scale_out_partial sets
    pos.scaled_out=True at core/position_manager.py:990. So the gate
    blocks regardless of original_contracts. No double-scale-out risk.

  - The downstream OCO writer at core/startup_reconciliation.py:333-336
    takes `qty` as a parameter — sourced from NT8's outgoing/ position
    file (parsed at lines 142-160), NOT from pos.original_contracts.
    So the "OCO writes against stale qty" hypothesis is DISPROVEN.

Verdict: scale_out_partial is SAFE today. No code change in Round 3.

These tests pin the safety invariants so a future refactor that introduces
the hypothesized failure mode surfaces immediately. They are explicitly
behavioral (not structural) per FINDING-2026-06-04-STRUCTURAL-TEST-PATTERN.

Run: pytest tests/test_scale_out_partial_preserves_post_exit_contracts.py -v
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


def _open_3lot_short(positions, *, trade_id="trade_3lot_test",
                     strategy="bias_momentum", account="SimBias Momentum"):
    """Open a 3-contract SHORT — large enough to scale out and still leave
    a 1-contract runner."""
    ok = positions.open_position(
        trade_id=trade_id,
        direction="SHORT",
        entry_price=30300.0,
        contracts=3,
        stop_price=30325.0,
        target_price=30262.5,
        strategy=strategy,
        reason=f"{strategy}_signal",
        market_snapshot={},
        account=account,
    )
    assert ok
    return trade_id


class TestScaleOutPreservesOriginalContracts:
    def test_original_contracts_unchanged_after_partial_exit(self, positions):
        """`pos.original_contracts` is the captured-at-open quantity used
        by the scale-out gate (not the live position size). It must
        survive scale_out_partial untouched — the live size lives in
        `pos.contracts`, the scale-out flag in `pos.scaled_out`."""
        tid = _open_3lot_short(positions)
        pos = positions.get_position(tid)
        assert pos.original_contracts == 3
        assert pos.contracts == 3
        partial = positions.scale_out_partial(
            exit_price=30287.5, n_contracts=1,
            exit_reason="scale_out_target", trade_id=tid,
        )
        assert partial is not None
        pos_after = positions.get_position(tid)
        assert pos_after.contracts == 2  # live size reduced
        assert pos_after.original_contracts == 3  # snapshot preserved
        assert pos_after.scaled_out is True

    def test_scaled_out_flag_set_after_partial_exit(self, positions):
        """The scale-out gate at bots/_ws_dispatcher.py:474 reads
        `not pos.scaled_out` to prevent re-scale. Pin the flag set."""
        tid = _open_3lot_short(positions)
        positions.scale_out_partial(
            exit_price=30287.5, n_contracts=1,
            exit_reason="scale_out_target", trade_id=tid,
        )
        assert positions.get_position(tid).scaled_out is True


class TestScaleOutDoubleCallSafe:
    def test_second_scale_out_uses_reduced_contracts(self, positions):
        """If somehow scale_out_partial is called twice (despite the
        gate), the second call must operate on the REDUCED size, not
        the original — otherwise we'd produce a SHORT-grow OIF.

        This is the failure-mode the Bug Hunter hypothesized. The
        invariant test confirms `pos.contracts` is the source of truth."""
        tid = _open_3lot_short(positions)
        positions.scale_out_partial(exit_price=30287.5, n_contracts=1,
                                    exit_reason="scale_out_1",
                                    trade_id=tid)
        # Manually clear the scale_out flag to force the second call to
        # execute (mirrors a hypothetical bug where the gate fails).
        positions.get_position(tid).scaled_out = False
        positions.scale_out_partial(exit_price=30275.0, n_contracts=1,
                                    exit_reason="scale_out_2",
                                    trade_id=tid)
        pos = positions.get_position(tid)
        # 3 → 2 (first scale) → 1 (second scale), NOT 3 → 2 → -1
        assert pos.contracts == 1, (
            f"Second scale-out must reduce by 1 from the live size, "
            f"got pos.contracts={pos.contracts}. If this is < 1 or > 1, "
            f"a regression introduced original-size math somewhere."
        )

    def test_scale_out_full_remaining_delegates_to_close(self, positions):
        """Edge case: scale_out_partial with n >= pos.contracts delegates
        to close_position. Pin so a refactor doesn't change semantics."""
        tid = _open_3lot_short(positions)
        # Try to scale out ALL 3 contracts → must close the position.
        result = positions.scale_out_partial(
            exit_price=30287.5, n_contracts=3,
            exit_reason="close_via_scale", trade_id=tid,
        )
        assert result is not None
        # After full-close, position should be gone from _positions.
        assert positions.get_position(tid) is None
        assert positions.active_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
