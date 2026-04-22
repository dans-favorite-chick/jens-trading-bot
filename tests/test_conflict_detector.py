"""
S6 / B70 — Directional conflict detector tests.

Covers:
- No conflict when all positions share direction
- 2-strategy LONG-vs-SHORT conflict detected (single pair)
- Conflict clears when either side closes
- 3 LONG + 1 SHORT -> net=+2, gross=4, one conflict-prone strategy produces 3 pairs
- FLAT / unknown direction positions are ignored
- exposure_snapshot returns correct net/gross
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.position_manager import PositionManager
from core.strategy_risk_registry import StrategyRiskRegistry


def _open(pm: PositionManager, tid: str, strategy: str, direction: str,
          entry: float = 18000.0, contracts: int = 1, account: str = "Sim101"):
    assert pm.open_position(
        trade_id=tid, direction=direction, entry_price=entry,
        contracts=contracts, stop_price=entry - 10 if direction == "LONG" else entry + 10,
        target_price=entry + 20 if direction == "LONG" else entry - 20,
        strategy=strategy, reason="test", account=account,
    )


@pytest.fixture
def registry():
    return StrategyRiskRegistry()


def test_no_conflict_when_all_long(registry):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    _open(pm, "t2", "spring_setup", "LONG")
    _open(pm, "t3", "vwap_pullback", "LONG")
    conflicts = registry.detect_directional_conflicts(pm)
    assert conflicts == []


def test_no_conflict_when_all_short(registry):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "SHORT")
    _open(pm, "t2", "spring_setup", "SHORT")
    conflicts = registry.detect_directional_conflicts(pm)
    assert conflicts == []


def test_detect_two_strategy_conflict(registry):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG", account="Sim101")
    _open(pm, "t2", "spring_setup", "SHORT", account="Sim102")
    conflicts = registry.detect_directional_conflicts(pm)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert {c["strategy_a"], c["strategy_b"]} == {"bias_momentum", "spring_setup"}
    assert {c["dir_a"], c["dir_b"]} == {"LONG", "SHORT"}
    assert c["overlap_seconds"] >= 0


def test_conflict_resolves_when_one_side_closes(registry):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    _open(pm, "t2", "spring_setup", "SHORT")
    assert len(registry.detect_directional_conflicts(pm)) == 1
    pm.close_position(18010.0, "test_close", trade_id="t2")
    assert registry.detect_directional_conflicts(pm) == []


def test_three_long_one_short_yields_three_pairs(registry):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    _open(pm, "t2", "vwap_pullback", "LONG")
    _open(pm, "t3", "ib_breakout", "LONG")
    _open(pm, "t4", "spring_setup", "SHORT")
    conflicts = registry.detect_directional_conflicts(pm)
    # 3 LONG × 1 SHORT = 3 unique cross pairs
    assert len(conflicts) == 3
    # Exposure: net=+2 (3 long - 1 short), gross=4
    exp = registry.exposure_snapshot(pm)
    assert exp["net"] == 2
    assert exp["gross"] == 4
    assert len(exp["longs"]) == 3
    assert len(exp["shorts"]) == 1


def test_flat_positions_ignored(registry):
    pm = PositionManager()
    _open(pm, "t1", "bias_momentum", "LONG")
    _open(pm, "t2", "spring_setup", "SHORT")
    # Inject a position with FLAT direction to simulate degenerate state.
    pos = pm.get_position("t1")
    pos.direction = "FLAT"
    conflicts = registry.detect_directional_conflicts(pm)
    # Only one LONG/SHORT position remaining (t2 SHORT, t1 FLAT)
    assert conflicts == []


def test_exposure_snapshot_zero_when_empty(registry):
    pm = PositionManager()
    exp = registry.exposure_snapshot(pm)
    assert exp == {"net": 0, "gross": 0, "longs": [], "shorts": []}
