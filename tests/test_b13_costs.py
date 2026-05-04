"""B13: commission/fee/slippage in P&L."""
from __future__ import annotations

import time

import pytest

from config.settings import (
    TICK_SIZE,
    COMMISSION_PER_SIDE,
    EXCHANGE_FEES_PER_SIDE,
    SLIPPAGE_TICKS_PER_SIDE,
)
from core.position_manager import PositionManager, compute_trade_costs


def test_costs_one_contract_default():
    """Default rates round-trip cost > 0 and matches manual calc."""
    costs = compute_trade_costs(1)
    expected = (
        2 * COMMISSION_PER_SIDE
        + 2 * EXCHANGE_FEES_PER_SIDE
        + 2 * SLIPPAGE_TICKS_PER_SIDE * (TICK_SIZE * 2)
    )
    assert costs["cost_total_dollars"] == round(expected, 2)
    assert costs["cost_total_dollars"] > 0


def test_costs_scale_linearly():
    c1 = compute_trade_costs(1)["cost_total_dollars"]
    c3 = compute_trade_costs(3)["cost_total_dollars"]
    assert abs(c3 - 3 * c1) < 0.01


def test_costs_breakdown_fields_present():
    c = compute_trade_costs(2)
    for key in (
        "commission_dollars",
        "exchange_fees_dollars",
        "slippage_dollars",
        "fees_dollars",
        "cost_total_dollars",
    ):
        assert key in c
    # Conservation: fees + slippage = total
    assert (
        abs(c["fees_dollars"] + c["slippage_dollars"] - c["cost_total_dollars"]) < 0.01
    )


def _open_one_contract_long(pm, entry=20000.0, stop=19990.0, target=20020.0,
                             strategy="test"):
    """Helper: open a 1-contract LONG position via PositionManager API."""
    import uuid
    return pm.open_position(
        trade_id=uuid.uuid4().hex[:8],
        direction="LONG",
        entry_price=entry,
        contracts=1,
        stop_price=stop,
        target_price=target,
        strategy=strategy,
        reason="test",
        account="Sim101",
    )


def test_small_winner_becomes_loser_after_costs():
    """+gross of a few ticks becomes a NET loss after ~$5 round-turn."""
    pm = PositionManager()
    _open_one_contract_long(pm, entry=20000.00, target=20010.00)
    # Gross +6 ticks = 6 * 0.50 = $3 — below total cost of ~$4-5.
    trade = pm.close_position(exit_price=20001.50, exit_reason="manual")
    assert trade is not None
    assert trade["pnl_dollars_gross"] > 0      # was a win gross
    assert trade["pnl_dollars"]       < 0      # net is a loss
    assert trade["pnl_dollars_net"]   < 0      # confirms split field
    assert trade["result"] == "LOSS"


def test_trade_record_has_all_b13_cost_fields():
    pm = PositionManager()
    _open_one_contract_long(pm, entry=20000.00, target=20020.00)
    trade = pm.close_position(exit_price=20020.00, exit_reason="target_hit")
    assert trade is not None
    for key in (
        "pnl_dollars",
        "pnl_dollars_gross",
        "pnl_dollars_net",
        "commission_dollars",
        "exchange_fees_dollars",
        "slippage_dollars",
        "fees_dollars",
        "cost_total_dollars",
    ):
        assert key in trade, f"missing B13 field {key} in trade dict"
    # Conservation: gross - cost_total = net
    diff = (
        trade["pnl_dollars_gross"]
        - trade["cost_total_dollars"]
        - trade["pnl_dollars_net"]
    )
    assert abs(diff) < 0.01


def test_legacy_field_aliases_match_new_fields():
    """gross_pnl + commission legacy aliases must equal new B13 values."""
    pm = PositionManager()
    _open_one_contract_long(pm, entry=20000.00, target=20020.00)
    trade = pm.close_position(exit_price=20020.00, exit_reason="target_hit")
    assert trade["gross_pnl"] == trade["pnl_dollars_gross"]
    assert trade["commission"] == trade["commission_dollars"]


def test_pnl_dollars_default_is_net():
    """pnl_dollars (the field downstream halt logic reads) MUST be net."""
    pm = PositionManager()
    _open_one_contract_long(pm, entry=20000.00, target=20020.00)
    trade = pm.close_position(exit_price=20020.00, exit_reason="target_hit")
    # pnl_dollars must equal pnl_dollars_net, not pnl_dollars_gross
    assert trade["pnl_dollars"] == trade["pnl_dollars_net"]
    assert trade["pnl_dollars"] != trade["pnl_dollars_gross"]
