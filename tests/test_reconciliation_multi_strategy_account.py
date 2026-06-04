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


def test_multi_strategy_account_does_NOT_block_real_signal_on_other_strategies(
    outgoing_dir,
):
    """The whole point of keeping the pseudo-label on Sim101: a real
    big_move_signal or es_nq_confluence signal MUST still be able to fire
    while an orphan is open on Sim101. Sim101 is multi-strategy by design.
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
    # The orphan claims `_reconciled_Sim101` only; the real strategy slots
    # remain free.
    assert pm.is_flat_for("big_move_signal") is True
    assert pm.is_flat_for("es_nq_confluence") is True
    # And a real big_move_signal can open concurrently — this is the
    # legitimate multi-strategy-account behavior.
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
    assert ok is True, (
        "Multi-strategy account must NOT block a real strategy signal while "
        "an orphan-adopted position is open under the pseudo-label."
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
