"""Regression test for REDTEAM-1-FIX-3A (remediation round 2) —
multi-strategy account branch.

When an orphan position is adopted on a MULTI-STRATEGY account
(currently only Sim101, which hosts `big_move_signal`,
`es_nq_confluence`, and `_default` fallback), the strategy label
must remain the `_reconciled_<account>` pseudo-label — multi-strategy
concurrency is by design on Sim101, and labeling the orphan with a
real strategy name would either inflate that strategy's stats
(pre-257df2f bug) or fire the slot collision against a strategy
that has legitimate concurrent-fill rights.

The single-strategy branch is tested in
`tests/test_reconciliation_single_strategy_account.py`.
"""
from __future__ import annotations

import os

import pytest

from core.position_manager import PositionManager
from core.startup_reconciliation import reconcile_positions_from_nt8


INSTRUMENT = "MNQM6 06-26"


def _write_pos_file(outgoing_dir: str, account: str, body: str) -> None:
    path = os.path.join(outgoing_dir, f"{INSTRUMENT}_{account}_position.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


@pytest.fixture
def outgoing_dir(tmp_path):
    d = tmp_path / "outgoing"
    d.mkdir()
    return str(d)


def test_multi_strategy_account_orphan_keeps_pseudo_label(outgoing_dir):
    """Sim101 routes big_move_signal AND es_nq_confluence (plus the _default
    fallback). An orphan adopted there must NOT take the name of either real
    strategy (pre-257df2f bug — inflated big_move_signal stats with operator
    manual fills). The pseudo-label is correct here.
    """
    _write_pos_file(outgoing_dir, "Sim101", "SHORT;1;30100.00")
    pm = PositionManager()
    adopted = reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["Sim101"],
        oco_writer=lambda **kw: ["ok1", "ok2"],
    )
    assert len(adopted) == 1
    pos = pm.active_positions[0]
    assert pos.strategy == "_reconciled_Sim101"
    assert pos.metadata.get("source") == "manual_reconciled"
    assert pos.metadata.get("reconciled_from_orphan") is True
    # The pre-fix-era inference is preserved for forensic audit; for Sim101
    # this is whichever strategy dict-iteration finds first
    # (`big_move_signal` per the current map).
    assert pos.metadata.get("strategy_original_attribution") == "big_move_signal"


def test_multi_strategy_account_DOES_block_real_signal_on_other_strategies_H3(
    outgoing_dir,
):
    """**2026-06-05 H3 deliberate design reversal** of REDTEAM-1-FIX-3A.

    Prior behavior (REDTEAM-1-FIX-3A, 257df2f): a reconciled orphan on
    Sim101 labeled `_reconciled_Sim101` did NOT block real strategies
    routed to Sim101 — the rationale was that Sim101 is "multi-strategy
    by design" so legitimate concurrent fills should still work.

    New behavior (FINDING-2026-06-04-RECON-REPLAY-AS-ENTRY Round 3, this
    sprint): the 2026-06-04 18:01:13 CDT incident built Sim101 from
    FLAT to SHORT 7 in 137 ms — exactly because the prior design let
    fresh entries pile on top of a reconciled orphan. The slot IS now
    held against every strategy mapped to the same account, per the
    master-prompt Option A specification.

    The dashboard/labeling half of REDTEAM-1-FIX-3A is preserved (the
    pseudo-label is still used so manual-fill P&L is not misattributed
    to a real strategy in dashboard aggregations — tested in
    `test_multi_strategy_account_orphan_keeps_pseudo_label` above).
    Only the slot-interlock half is reversed.
    """
    _write_pos_file(outgoing_dir, "Sim101", "SHORT;1;30100.00")
    pm = PositionManager()
    reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["Sim101"],
        oco_writer=lambda **kw: ["ok1", "ok2"],
    )
    # The orphan still labels as `_reconciled_Sim101` (labeling unchanged),
    # but is_flat_for now ALSO checks the routed account — real strategies
    # routing to Sim101 see the slot as held.
    assert pm.is_flat_for("big_move_signal") is False, (
        "H3 reversal: a Sim101-routed strategy must see the slot as held "
        "when a `_reconciled_Sim101` orphan exists."
    )
    assert pm.is_flat_for("es_nq_confluence") is False
    # And a real big_move_signal open is refused at the slot-collision
    # guard inside open_position (line 634, which calls is_flat_for).
    ok = pm.open_position(
        trade_id="trade_real_big_move",
        direction="LONG",
        entry_price=30200.0,
        contracts=1,
        stop_price=30180.0,
        target_price=30240.0,
        strategy="big_move_signal",
        reason="big_move_signal_long",
        market_snapshot={},
        account="Sim101",
    )
    assert ok is False, (
        "H3 reversal: a real strategy entry on the same routed account as "
        "a reconciled orphan must be refused at the slot-collision guard."
    )
    # And a strategy that routes to a DIFFERENT account must still be
    # free to enter (the fix is account-scoped, not global).
    ok_other = pm.open_position(
        trade_id="trade_real_bias",
        direction="SHORT",
        entry_price=30100.0,
        contracts=1,
        stop_price=30125.0,
        target_price=30062.5,
        strategy="bias_momentum",  # routes to "SimBias Momentum"
        reason="bias_momentum_short",
        market_snapshot={},
        account="SimBias Momentum",
    )
    assert ok_other is True, (
        "Cross-account isolation: a strategy routing to a different "
        "account must NOT be blocked by a Sim101 orphan."
    )


def test_unrouted_account_uses_pseudo_label_with_no_attribution(outgoing_dir):
    """Defensive branch — an account that no strategy is mapped to (test or
    new operator setup) still adopts safely with the pseudo-label, and
    `strategy_original_attribution` is None (no inference possible)."""
    _write_pos_file(outgoing_dir, "SimNeverMapped", "LONG;1;30000.00")
    pm = PositionManager()
    reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["SimNeverMapped"],
        oco_writer=lambda **kw: ["ok1", "ok2"],
    )
    pos = pm.active_positions[0]
    assert pos.strategy == "_reconciled_SimNeverMapped"
    assert pos.metadata.get("source") == "manual_reconciled"
    assert pos.metadata.get("strategy_original_attribution") is None
