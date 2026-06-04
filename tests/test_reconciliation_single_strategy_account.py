"""Regression test for REDTEAM-1-FIX-3A (remediation round 2).

When an orphan position is adopted on a SINGLE-STRATEGY account
(e.g. SimBias Momentum routes only to bias_momentum per
`config/account_routing.py`), the strategy label must be the REAL
strategy name (not the `_reconciled_<account>` pseudo-label from
commit 257df2f). This restores the `is_flat_for` strategy-slot
interlock — a real bias_momentum signal arriving while the orphan
is open will collide in the strategy slot and be blocked, preventing
a double-fill on the live canary account.

The provenance fields (`source='manual_reconciled'`,
`reconciled_from_orphan=True`) are still stamped so the dashboard
aggregator continues to exclude the orphan from strategy win-rate
math. Clean attribution + safety interlock both preserved.

See also `tests/test_reconciliation_multi_strategy_account.py` for
the complementary Sim101 case (pseudo-label retained).
"""
from __future__ import annotations

import os

import pytest

from core.position_manager import PositionManager
from core.startup_reconciliation import reconcile_positions_from_nt8


INSTRUMENT = "MNQM6 06-26"


def _write_pos_file(outgoing_dir: str, account: str, body: str) -> None:
    safe = account.replace(" ", " ")  # NT8 writes literal names with spaces
    path = os.path.join(outgoing_dir, f"{INSTRUMENT}_{safe}_position.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


@pytest.fixture
def outgoing_dir(tmp_path):
    d = tmp_path / "outgoing"
    d.mkdir()
    return str(d)


def test_single_strategy_account_orphan_gets_real_strategy_name(outgoing_dir):
    """SimBias Momentum routes only to bias_momentum in the production
    account_routing map. An orphan position adopted there must be labeled
    `bias_momentum` so the slot collision check blocks a parallel real
    signal."""
    _write_pos_file(outgoing_dir, "SimBias Momentum", "LONG;1;30000.00")
    pm = PositionManager()
    adopted = reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["SimBias Momentum"],
        oco_writer=lambda **kw: ["ok1", "ok2"],
    )
    assert len(adopted) == 1
    pos = pm.active_positions[0]

    # The CRITICAL fix: strategy is the REAL routed strategy, not the
    # `_reconciled_<account>` pseudo-label.
    assert pos.strategy == "bias_momentum", (
        f"single-strategy account orphan must take the real strategy name "
        f"to preserve the is_flat_for slot interlock; got {pos.strategy!r}"
    )

    # Provenance fields still set so the dashboard aggregator excludes this
    # row from per-strategy win-rate / PnL math.
    assert pos.metadata.get("source") == "manual_reconciled"
    assert pos.metadata.get("reconciled_from_orphan") is True
    # Original attribution mirrors the label since inference is unambiguous
    # on a single-strategy account.
    assert pos.metadata.get("strategy_original_attribution") == "bias_momentum"


def test_single_strategy_account_blocks_parallel_signal_in_same_strategy(outgoing_dir):
    """The real point of Phase 3a: on a single-strategy account, after the
    orphan is adopted, the PositionManager.is_flat_for() check for the same
    strategy returns False, so a real signal would be rejected before it
    can double-fill the account.
    """
    _write_pos_file(outgoing_dir, "SimBias Momentum", "SHORT;2;30500.00")
    pm = PositionManager()
    reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["SimBias Momentum"],
        oco_writer=lambda **kw: ["ok1", "ok2"],
    )
    # Slot is now claimed by the orphan under the real strategy name.
    assert pm.is_flat_for("bias_momentum") is False, (
        "Orphan adoption MUST claim the strategy slot to block double-fill"
    )
    # And open_position for a real bias_momentum signal will refuse.
    ok = pm.open_position(
        trade_id="trade_real_bias_momentum_signal_attempt",
        direction="SHORT",
        entry_price=30490.0,
        contracts=1,
        stop_price=30505.0,
        target_price=30460.0,
        strategy="bias_momentum",
        reason="bias_momentum_short_signal",
        market_snapshot={},
        account="SimBias Momentum",
    )
    assert ok is False, (
        "A real bias_momentum signal MUST be refused while an orphan-adopted "
        "position is open on the same single-strategy account."
    )


def test_sub_strategy_single_strategy_account_gets_colon_form(outgoing_dir):
    """opening_session is split per-sub-strategy via the nested-dict mapping
    in STRATEGY_ACCOUNT_MAP. SimOpenDrive routes to opening_session:open_drive
    only — the strategy label should be the colon-joined form, mirroring the
    convention `_infer_strategy_from_account` already uses for nested
    strategies."""
    _write_pos_file(outgoing_dir, "SimOpenDrive", "LONG;1;30200.00")
    pm = PositionManager()
    reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["SimOpenDrive"],
        oco_writer=lambda **kw: ["ok1", "ok2"],
    )
    pos = pm.active_positions[0]
    assert pos.strategy == "opening_session:open_drive"
    assert pos.metadata.get("source") == "manual_reconciled"
    assert pos.metadata.get("strategy_original_attribution") == "opening_session:open_drive"
