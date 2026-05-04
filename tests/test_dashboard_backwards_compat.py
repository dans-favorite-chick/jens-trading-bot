"""Dashboard handles pre/post-B13 trades safely.

The dashboard reads logs/trade_memory.json which contains trades from
before AND after the B13 cost-accounting fix (commit 9b82f25). Pre-B13
trades have only `pnl_dollars`; post-B13 trades have the full
breakdown (`pnl_dollars_gross`, `pnl_dollars_net`, `cost_total_dollars`,
`commission_dollars`, `exchange_fees_dollars`, `slippage_dollars`).

Aggregating naively (e.g. `t["pnl_dollars_gross"]`) over a mixed window
crashes on KeyError or sums None values. These tests verify the
dashboard.trade_accessors helpers handle both eras correctly.
"""
from __future__ import annotations

import pytest

from dashboard.trade_accessors import (
    safe_pnl_net,
    safe_pnl_gross,
    safe_cost_total,
    is_post_b13,
    split_pre_post,
)


# ─── factory helpers ─────────────────────────────────────────────────

def make_pre_b13_trade(pnl: float = 10.0) -> dict:
    """Pre-B13: only pnl_dollars present, no cost fields."""
    return {
        "strategy": "bias_momentum",
        "pnl_dollars": pnl,
        "result": "WIN" if pnl > 0 else "LOSS",
    }


def make_post_b13_trade(gross: float = 12.0, cost: float = 5.0) -> dict:
    """Post-B13: full cost breakdown."""
    net = gross - cost
    return {
        "strategy": "bias_momentum",
        "pnl_dollars_gross": gross,
        "pnl_dollars_net":   net,
        "pnl_dollars":       net,
        "cost_total_dollars": cost,
        "commission_dollars": cost * 0.5,
        "exchange_fees_dollars": cost * 0.2,
        "slippage_dollars":   cost * 0.3,
        "result": "WIN" if net > 0 else "LOSS",
    }


# ─── safe_pnl_net ────────────────────────────────────────────────────

def test_safe_pnl_net_handles_pre_b13():
    """Pre-B13 trade: falls back to pnl_dollars."""
    t = make_pre_b13_trade(pnl=15.0)
    assert safe_pnl_net(t) == 15.0


def test_safe_pnl_net_handles_post_b13():
    """Post-B13 trade: uses pnl_dollars_net."""
    t = make_post_b13_trade(gross=12.0, cost=5.0)
    assert safe_pnl_net(t) == 7.0


def test_safe_pnl_net_handles_loss():
    """Negative P&L works for both eras."""
    assert safe_pnl_net(make_pre_b13_trade(pnl=-8.0)) == -8.0
    assert safe_pnl_net(make_post_b13_trade(gross=-3.0, cost=5.0)) == -8.0


def test_safe_pnl_net_handles_explicit_none():
    """Defensive: pnl_dollars=None doesn't crash; returns 0.0."""
    assert safe_pnl_net({"pnl_dollars": None}) == 0.0


def test_safe_pnl_net_handles_empty_trade():
    """Defensive: trade with no pnl fields returns 0.0."""
    assert safe_pnl_net({}) == 0.0


# ─── safe_pnl_gross ──────────────────────────────────────────────────

def test_safe_pnl_gross_for_pre_b13_returns_pnl_dollars():
    """Pre-B13 has no explicit gross; pnl_dollars IS gross (no cost was
    being subtracted at that time)."""
    t = make_pre_b13_trade(pnl=10.0)
    assert safe_pnl_gross(t) == 10.0


def test_safe_pnl_gross_for_post_b13_uses_explicit_field():
    t = make_post_b13_trade(gross=12.0, cost=5.0)
    assert safe_pnl_gross(t) == 12.0


# ─── safe_cost_total ─────────────────────────────────────────────────

def test_safe_cost_zero_for_pre_b13():
    """Pre-B13 trades have no cost_total_dollars; returns 0 (NOT None).
    Returning 0 is the right semantic — pre-B13 era did not measure cost,
    so summing 'cost' across mixed data attributes the unmeasured era a
    cost of zero, not poisons the sum with None."""
    assert safe_cost_total(make_pre_b13_trade()) == 0.0


def test_safe_cost_for_post_b13_uses_explicit_field():
    t = make_post_b13_trade(gross=12.0, cost=5.0)
    assert safe_cost_total(t) == 5.0


# ─── is_post_b13 ─────────────────────────────────────────────────────

def test_is_post_b13_classification():
    assert not is_post_b13(make_pre_b13_trade())
    assert     is_post_b13(make_post_b13_trade())


def test_is_post_b13_requires_both_fields():
    """A trade with cost_total_dollars but no pnl_dollars_gross is NOT
    treated as post-B13 (incomplete record)."""
    t = {"cost_total_dollars": 5.0, "pnl_dollars": 7.0}
    assert not is_post_b13(t)


# ─── aggregation safety ──────────────────────────────────────────────

def test_mixed_aggregation_doesnt_crash():
    """Sum of net P&L across mixed pre/post-B13 trades produces correct total."""
    trades = [
        make_pre_b13_trade(10.0),
        make_post_b13_trade(20.0, 5.0),  # net = 15
        make_pre_b13_trade(-5.0),
    ]
    total = sum(safe_pnl_net(t) for t in trades)
    assert total == 10.0 + 15.0 - 5.0


def test_mixed_gross_aggregation():
    """Gross sum across mixed eras: pre-B13 contributes pnl_dollars (gross-equivalent);
    post-B13 contributes the explicit gross."""
    trades = [
        make_pre_b13_trade(10.0),         # gross=10
        make_post_b13_trade(20.0, 5.0),   # gross=20
    ]
    total = sum(safe_pnl_gross(t) for t in trades)
    assert total == 30.0


def test_mixed_cost_aggregation_attributes_zero_to_pre_b13():
    """Total cost over a mixed window equals just the post-B13 portion."""
    trades = [
        make_pre_b13_trade(10.0),         # cost=0 (unmeasured)
        make_post_b13_trade(20.0, 5.0),   # cost=5
        make_post_b13_trade(15.0, 3.0),   # cost=3
    ]
    total_cost = sum(safe_cost_total(t) for t in trades)
    assert total_cost == 8.0


# ─── split_pre_post ──────────────────────────────────────────────────

def test_split_pre_post_partitions_correctly():
    trades = [
        make_pre_b13_trade(1.0),
        make_post_b13_trade(2.0, 1.0),
        make_pre_b13_trade(3.0),
        make_post_b13_trade(4.0, 1.0),
    ]
    pre, post = split_pre_post(trades)
    assert len(pre) == 2
    assert len(post) == 2
    # Order preserved within each partition
    assert pre[0]["pnl_dollars"]  == 1.0
    assert pre[1]["pnl_dollars"]  == 3.0
    assert post[0]["pnl_dollars_gross"] == 2.0
    assert post[1]["pnl_dollars_gross"] == 4.0


def test_split_pre_post_empty():
    pre, post = split_pre_post([])
    assert pre == []
    assert post == []
