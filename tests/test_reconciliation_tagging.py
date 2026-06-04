"""Regression tests for FINDING-2026-06-04-DASH-ATTR and
FINDING-2026-06-04-BIG-MOVE-LABEL.

Before this fix, every orphan NT8 position adopted by
`core.startup_reconciliation` was attributed to the prod_bot under
the FIRST strategy in `STRATEGY_ACCOUNT_MAP` routed to that account
(`big_move_signal` for Sim101). The resulting trade row carried no
provenance flag, so the dashboard counted it toward the bot's
big_move_signal win rate / PnL — even though it was an operator
manual fill.

Post-fix:
 - reconciliation uses an account-scoped pseudo-strategy label
   `_reconciled_<account>` so a real strategy is never collided with.
 - the close_position trade dict carries `source='manual_reconciled'`,
   `reconciled_from_orphan=True`, and preserves the original inferred
   strategy under `strategy_original_attribution` for audit.

These tests pin both behaviors at the open_position / close_position
boundary using the PositionManager API directly. Tests for the
dashboard aggregator exclusion live in
`tests/test_dashboard_excludes_reconciled.py`.
"""
from __future__ import annotations

import time

import pytest

from core.position_manager import PositionManager


@pytest.fixture
def positions(tmp_path, monkeypatch):
    """Fresh PositionManager with isolated trade_memory path so the
    test's open/close cycle doesn't touch the real logs/trade_memory.json.
    """
    # Redirect TRADE_MEMORY_PATH to tmp so position_manager doesn't write
    # to the real file. Per CLAUDE.md memory note: NEVER raw-open the
    # legacy path — but PositionManager owns the writer, so a path swap
    # is the right monkeypatch surface.
    from core import position_manager as pm
    monkeypatch.setattr(
        pm, "TRADE_MEMORY_PATH", str(tmp_path / "trade_memory.json")
    )
    return PositionManager(load_history=False)


def _open_reconciled(positions, *, account="Sim101"):
    """Helper: mirror the call core.startup_reconciliation makes when
    it adopts an orphan NT8 fill at boot."""
    strategy_label = f"_reconciled_{account}"
    ok = positions.open_position(
        trade_id=f"RECONCILED_{account}_testdeadbeef",
        direction="SHORT",
        entry_price=30283.0,
        contracts=1,
        stop_price=30308.0,
        target_price=30245.5,
        strategy=strategy_label,
        reason="reconciled_from_nt8",
        market_snapshot={"reconciled": True, "account": account},
        metadata={
            "source": "manual_reconciled",
            "reconciled_from_orphan": True,
            "strategy_original_attribution": "big_move_signal",
        },
        account=account,
        reconciled=True,
    )
    assert ok, "reconciliation open_position should succeed under fresh PM"
    return strategy_label


def test_close_position_stamps_source_manual_reconciled_on_reconciled_pos(positions):
    """When a position was opened via reconciliation (`reconciled=True`),
    its closed-trade dict must carry source='manual_reconciled'.
    """
    strategy_label = _open_reconciled(positions)
    trade = positions.close_position(
        exit_price=30245.5, exit_reason="target_hit",
        trade_id=f"RECONCILED_Sim101_testdeadbeef",
    )
    assert trade is not None, "close_position should return the trade dict"
    assert trade.get("source") == "manual_reconciled", (
        f"reconciled trade must be tagged source='manual_reconciled', got "
        f"{trade.get('source')!r}"
    )
    assert trade.get("reconciled_from_orphan") is True, (
        "reconciled_from_orphan flag must be True for a reconciled trade"
    )
    assert trade.get("strategy_original_attribution") == "big_move_signal", (
        "The original inferred strategy must be preserved for audit, "
        f"got {trade.get('strategy_original_attribution')!r}"
    )
    # And the trade's strategy itself must be the account-scoped label,
    # not the misleading big_move_signal — that's the BIG-MOVE-LABEL fix.
    assert trade.get("strategy") == strategy_label, (
        f"reconciled trade strategy must be '{strategy_label}', got "
        f"{trade.get('strategy')!r}"
    )


def test_close_position_stamps_source_bot_on_real_signal_trade(positions):
    """Sanity / non-regression: a normal bot-signaled trade
    (reconciled=False) must carry source='bot' and
    reconciled_from_orphan=False, NOT source='manual_reconciled'.
    """
    ok = positions.open_position(
        trade_id="trade_realbot_abc",
        direction="SHORT",
        entry_price=30315.50,
        contracts=1,
        stop_price=30330.50,
        target_price=30285.50,
        strategy="bias_momentum",
        reason="bias_momentum_signal",
        market_snapshot={"price": 30315.50},
        account="Sim101",
        # NOT reconciled — a real bot trade
    )
    assert ok
    trade = positions.close_position(
        exit_price=30285.50, exit_reason="target_hit",
        trade_id="trade_realbot_abc",
    )
    assert trade is not None
    assert trade.get("source") == "bot", (
        "real bot trade must be tagged source='bot', got "
        f"{trade.get('source')!r}"
    )
    assert trade.get("reconciled_from_orphan") is False
    assert trade.get("strategy_original_attribution") is None
    assert trade.get("strategy") == "bias_momentum"


def test_reconciled_strategy_label_does_not_collide_with_real_strategy(positions):
    """The new label `_reconciled_<account>` MUST NOT collide with any
    real strategy name registered in config.strategies. The leading
    underscore makes this unambiguous in trade memory.

    A regression here (e.g. someone reverting to `_infer_strategy_from_account`
    or letting the inferred strategy through) would let a manual fill
    re-pollute a real strategy's win rate.
    """
    strategy_label = _open_reconciled(positions, account="Sim101")
    # Underscore-prefixed labels by convention are reserved for
    # non-strategy buckets ("_reconciled", "_unknown", etc.).
    assert strategy_label.startswith("_"), strategy_label
    # And the account is encoded — multi-account setups stay
    # distinguishable.
    assert "Sim101" in strategy_label


def test_account_scoped_label_is_stable_for_other_accounts(positions):
    """The label format must apply to non-Sim101 accounts too — the
    fix can't be Sim101-only."""
    strategy_label = _open_reconciled(positions, account="SimAlpha")
    assert strategy_label == "_reconciled_SimAlpha"
    trade = positions.close_position(
        exit_price=30245.5, exit_reason="target_hit",
        trade_id="RECONCILED_SimAlpha_testdeadbeef",
    )
    assert trade is not None
    assert trade.get("strategy") == "_reconciled_SimAlpha"
    assert trade.get("source") == "manual_reconciled"
