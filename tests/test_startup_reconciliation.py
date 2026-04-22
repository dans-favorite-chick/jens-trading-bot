"""B77 tests — startup reconciliation from NT8 outgoing/*_position.txt.

Exercises core.startup_reconciliation.reconcile_positions_from_nt8 directly
with a mocked outgoing/ directory and a mocked OCO writer. Does NOT spin up
a BaseBot — we just want to confirm the reconcile logic produces the right
Position objects and calls the OCO writer once per adopted position.
"""
from __future__ import annotations

import os
import pytest

from core.position_manager import PositionManager
from core.startup_reconciliation import reconcile_positions_from_nt8


INSTRUMENT = "MNQM6"


def _write_pos_file(dirpath: str, account: str, content: str) -> None:
    fname = f"{INSTRUMENT} Globex_{account}_position.txt"
    with open(os.path.join(dirpath, fname), "w") as f:
        f.write(content)


@pytest.fixture
def outgoing_dir(tmp_path):
    d = tmp_path / "outgoing"
    d.mkdir()
    return str(d)


def test_reconcile_adopts_nonflat_and_attaches_oco(outgoing_dir):
    """2 non-FLAT + 15 FLAT accounts → 2 adoptions, 2 OCO calls."""
    # 2 non-FLAT positions
    _write_pos_file(outgoing_dir, "SimBias Momentum", "LONG;1;26741.25")
    _write_pos_file(outgoing_dir, "SimVWapp Pullback", "SHORT;2;26800.00")
    # 15 FLAT accounts
    for i in range(15):
        _write_pos_file(outgoing_dir, f"SimFlat{i}", "FLAT;0;0")

    pm = PositionManager()
    oco_calls = []

    def mock_oco(direction, qty, stop_price, target_price, trade_id, account):
        oco_calls.append({
            "direction": direction, "qty": qty,
            "stop_price": stop_price, "target_price": target_price,
            "trade_id": trade_id, "account": account,
        })
        return [f"/fake/stop_{trade_id}.oif", f"/fake/target_{trade_id}.oif"]

    # Explicit account list so we don't depend on the production routing map.
    accounts = [
        "SimBias Momentum",
        "SimVWapp Pullback",
    ] + [f"SimFlat{i}" for i in range(15)]

    adopted = reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=accounts,
        oco_writer=mock_oco,
    )

    assert len(adopted) == 2
    assert pm.active_count == 2
    assert len(oco_calls) == 2

    # Check LONG adoption
    long_pos = next(p for p in pm.active_positions if p.direction == "LONG")
    assert long_pos.reconciled is True
    assert long_pos.contracts == 1
    assert long_pos.entry_price == 26741.25
    assert long_pos.account == "SimBias Momentum"
    assert long_pos.trade_id.startswith("RECONCILED_")
    # Strategy inferred from routing table
    assert long_pos.strategy == "bias_momentum"
    # Safety-net stop sits 100 ticks below entry (0.25 tick_size) = 25pts
    assert long_pos.stop_price == pytest.approx(26741.25 - 25.0)
    assert long_pos.target_price == pytest.approx(26741.25 + 37.5)

    # Check SHORT adoption
    short_pos = next(p for p in pm.active_positions if p.direction == "SHORT")
    assert short_pos.reconciled is True
    assert short_pos.contracts == 2
    assert short_pos.entry_price == 26800.00
    assert short_pos.strategy == "vwap_pullback"
    assert short_pos.stop_price == pytest.approx(26800.00 + 25.0)
    assert short_pos.target_price == pytest.approx(26800.00 - 37.5)


def test_flat_accounts_produce_no_positions(outgoing_dir):
    for i in range(5):
        _write_pos_file(outgoing_dir, f"Sim{i}", "FLAT;0;0")

    pm = PositionManager()
    oco_calls = []
    adopted = reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=[f"Sim{i}" for i in range(5)],
        oco_writer=lambda **kw: oco_calls.append(kw) or ["ok"],
    )
    assert adopted == []
    assert pm.active_count == 0
    assert oco_calls == []


def test_missing_position_files_are_ignored(outgoing_dir):
    """Routed accounts with no outgoing file (bot never traded them) skip cleanly."""
    pm = PositionManager()
    adopted = reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["SimNeverTraded1", "SimNeverTraded2"],
        oco_writer=lambda **kw: ["ok"],
    )
    assert adopted == []
    assert pm.active_count == 0


def test_uninferable_account_gets_reconciled_placeholder(outgoing_dir):
    """Account with no entry in STRATEGY_ACCOUNT_MAP still adopts with placeholder strategy."""
    _write_pos_file(outgoing_dir, "SimRandomUnknownAccount", "LONG;3;27000.00")

    pm = PositionManager()
    adopted = reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["SimRandomUnknownAccount"],
        oco_writer=lambda **kw: ["ok1", "ok2"],
    )
    assert len(adopted) == 1
    pos = pm.active_positions[0]
    assert pos.strategy == "_reconciled"
    assert pos.reconciled is True
    assert pos.contracts == 3


def test_reconciled_flag_defaults_false_for_normal_positions():
    """open_position() without reconciled=True yields reconciled=False."""
    pm = PositionManager()
    pm.open_position(
        trade_id="normal_trade",
        direction="LONG",
        entry_price=100.0,
        contracts=1,
        stop_price=99.0,
        target_price=102.0,
        strategy="bias_momentum",
        reason="test",
    )
    pos = pm.get_position("normal_trade")
    assert pos is not None
    assert pos.reconciled is False


def test_oco_failure_still_adopts_but_records_failure(outgoing_dir):
    _write_pos_file(outgoing_dir, "SimBias Momentum", "LONG;1;26741.25")
    pm = PositionManager()

    def failing_oco(**kw):
        return []  # NT8 rejected

    adopted = reconcile_positions_from_nt8(
        positions=pm,
        outgoing_dir=outgoing_dir,
        instrument=INSTRUMENT,
        routed_accounts=["SimBias Momentum"],
        oco_writer=failing_oco,
    )
    assert len(adopted) == 1
    assert adopted[0]["oco_ok"] is False
    # Position is still adopted even though OCO failed — operator will see
    # the ERROR log and flatten manually.
    assert pm.active_count == 1
