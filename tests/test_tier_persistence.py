"""Sprint F: tier classifier persists from entry signal to closed-trade record.

Sprint E indicator_audit found NO `tier` field in trade records, blocking
empirical validation of the A++/A/B/C tier classifier and Sprint B's
proposed tier-based-sizing. These tests verify:

  - Position dataclass carries a tier field (Optional[str], default None)
  - PositionManager.open_position accepts a tier kwarg and stores it
  - close_position emits the tier in the trade dict
  - scale_out_partial emits the tier on partial-exit trades too
  - Pre-Sprint-F call sites (no tier) still work, with tier=None
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.position_manager import PositionManager, Position  # noqa: E402


@pytest.fixture
def pm() -> PositionManager:
    return PositionManager()


def _open_long(pm: PositionManager, *, tier: str | None = None,
               trade_id: str | None = None) -> str:
    tid = trade_id or uuid.uuid4().hex[:8]
    pm.open_position(
        trade_id=tid, direction="LONG", entry_price=20000.0,
        contracts=1, stop_price=19990.0, target_price=20020.0,
        strategy="bias_momentum", reason="test",
        tier=tier,
    )
    return tid


# ─── Position dataclass field ────────────────────────────────────────

def test_position_dataclass_has_tier_field_defaulting_none():
    p = Position(
        trade_id="x", direction="LONG", entry_price=1.0, entry_time=0.0,
        contracts=1, stop_price=0.0, target_price=2.0,
        strategy="x", reason="x", market_snapshot={},
    )
    assert hasattr(p, "tier")
    assert p.tier is None


def test_position_dataclass_accepts_tier_value():
    p = Position(
        trade_id="x", direction="LONG", entry_price=1.0, entry_time=0.0,
        contracts=1, stop_price=0.0, target_price=2.0,
        strategy="x", reason="x", market_snapshot={}, tier="A++",
    )
    assert p.tier == "A++"


# ─── open_position ───────────────────────────────────────────────────

def test_open_position_stores_tier(pm):
    tid = _open_long(pm, tier="A++")
    pos = pm.get_position(tid)
    assert pos is not None
    assert pos.tier == "A++"


def test_open_position_default_tier_is_none(pm):
    """Pre-Sprint-F call sites that don't pass tier still work."""
    tid = _open_long(pm)  # no tier kwarg
    pos = pm.get_position(tid)
    assert pos is not None
    assert pos.tier is None


def test_open_position_accepts_all_tier_values(pm):
    """Use a different strategy per tier so the per-strategy lock doesn't
    block subsequent opens."""
    for i, tier in enumerate(("A++", "A", "B", "C", None)):
        tid = f"t-{i}"
        pm.open_position(
            trade_id=tid, direction="LONG", entry_price=20000.0,
            contracts=1, stop_price=19990.0, target_price=20020.0,
            strategy=f"strat_{i}", reason="t", tier=tier,
        )
        assert pm.get_position(tid).tier == tier


# ─── close_position emits tier ────────────────────────────────────────

def test_close_persists_tier_to_trade_dict(pm):
    """Standard close path: tier flows into the trade record."""
    tid = _open_long(pm, tier="A")
    trade = pm.close_position(exit_price=20010.0, exit_reason="target_hit",
                                trade_id=tid)
    assert trade is not None
    assert trade.get("tier") == "A"


def test_close_emits_none_when_no_tier(pm):
    """Pre-Sprint-F path: tier is None, but the key still exists for
    schema consistency (so consumers can rely on it being present)."""
    tid = _open_long(pm)  # no tier
    trade = pm.close_position(exit_price=20010.0, exit_reason="target_hit",
                                trade_id=tid)
    assert trade is not None
    assert "tier" in trade
    assert trade["tier"] is None


def test_close_emits_each_tier_value_correctly(pm):
    """All canonical tier values round-trip through close."""
    for i, tier in enumerate(("A++", "A", "B", "C")):
        tid = f"trade-{i}"
        pm.open_position(
            trade_id=tid, direction="LONG", entry_price=20000.0,
            contracts=1, stop_price=19990.0, target_price=20020.0,
            strategy=f"strat_{i}",  # different strategy each → no flat conflict
            reason="t", tier=tier,
        )
        trade = pm.close_position(exit_price=20010.0,
                                    exit_reason="target_hit", trade_id=tid)
        assert trade["tier"] == tier


# ─── scale_out_partial emits tier ────────────────────────────────────

def test_scale_out_partial_persists_tier(pm):
    """Partial-exit trade dict also carries the tier (for consistent
    indicator-audit input across full + partial trades)."""
    pm.open_position(
        trade_id="multi", direction="LONG", entry_price=20000.0,
        contracts=2, stop_price=19990.0, target_price=20040.0,
        strategy="bias_momentum", reason="t", tier="B",
    )
    partial = pm.scale_out_partial(
        exit_price=20010.0, exit_reason="scale_out_1R",
        n_contracts=1, trade_id="multi",
    )
    assert partial is not None
    assert partial.get("tier") == "B"
    assert partial.get("partial") is True


# ─── back-compat: trade dict shape preserved ─────────────────────────

def test_close_trade_dict_still_has_all_existing_fields(pm):
    """Ensure adding `tier` didn't accidentally remove anything else."""
    tid = _open_long(pm, tier="A++")
    trade = pm.close_position(exit_price=20010.0, exit_reason="target_hit",
                                trade_id=tid)
    required = {
        "trade_id", "direction", "entry_price", "exit_price", "contracts",
        "stop_price", "target_price", "pnl_ticks", "pnl_dollars",
        "pnl_dollars_gross", "pnl_dollars_net", "commission_dollars",
        "exchange_fees_dollars", "slippage_dollars", "fees_dollars",
        "cost_total_dollars", "gross_pnl", "commission", "result",
        "hold_time_s", "strategy", "sub_strategy", "account",
        "entry_reason", "exit_reason", "entry_time", "exit_time",
        "market_snapshot", "tier",
    }
    missing = required - set(trade.keys())
    assert not missing, f"trade dict missing fields: {missing}"
