"""
Unit tests for tools.reconcile_sim_vs_backtest.

We do NOT spin up the full CSVEnrichmentPipeline (slow, depends on
~1.7M-row CSVs). Instead we exercise the harness's comparison +
classification logic directly with fabricated sim trades and
fabricated BacktestReplayResult objects.

Coverage:
  1. Match within tolerance  -> classification=REPLAYED, within=True
  2. Match outside tolerance -> classification=REPLAYED, within=False
  3. Missing strategy-blocking field on sim trade -> BLOCKED
  4. Backtest fired but opposite direction -> BACKTEST_ONLY
  5. Backtest did not fire -> SIM_ONLY
  6. Summary aggregation across a mix
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.reconcile_sim_vs_backtest import (
    BacktestReplayResult,
    ComparisonRow,
    compare_one,
    _summarize,
)

# Default tolerances mirroring tests/reconciliation_tolerances.yaml.
TOL = {
    "entry_time_seconds": 60,
    "entry_price_ticks": 2,
    "stop_price_ticks": 2,
    "exit_reason_must_match": False,
    "net_pnl_pct": 25.0,
}

# Reusable sim-trade builder. A "full" sim trade carries the strategy-blocking
# fields in its market_snapshot so the harness will treat it as evaluable.
def _make_sim(
    trade_id: str = "abc12345",
    entry_dt: datetime = datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc),
    direction: str = "LONG",
    entry_price: float = 29500.00,
    stop_price: float = 29485.00,
    exit_reason: str = "target",
    pnl: float = 18.75,
    include_blockers: bool = True,
):
    ms = {"price": entry_price, "atr_5m": 20.0}
    if include_blockers:
        # Stub values for the 4 strategy-blocking fields. Truthiness is
        # what the harness checks — actual values don't matter for the
        # blocking-status logic.
        ms.update({
            "day_type": "TREND",
            "cr_verdict": "CONT_LONG",
            "cvd_health": {"veto": False},
            "es_nq_rs": 1.05,
        })
    return {
        "trade_id": trade_id,
        "bot_id": "sim",
        "strategy": "bias_momentum",
        "entry_time": entry_dt.timestamp(),
        "exit_time": (entry_dt.timestamp() + 600),
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "exit_price": entry_price + (pnl / 0.50) * 0.25 if direction == "LONG" else entry_price - (pnl / 0.50) * 0.25,
        "exit_reason": exit_reason,
        "pnl_dollars_net": pnl,
        "pnl_dollars": pnl,
        "result": "WIN" if pnl > 0 else "LOSS",
        "market_snapshot": ms,
    }


def _make_replay(
    fired: bool = True,
    direction: str = "LONG",
    entry_ts: datetime = datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc),
    entry_price: float = 29500.00,
    stop_price: float = 29485.00,
    exit_reason: str = "target",
    pnl: float = 18.75,
    reason: str = "",
):
    return BacktestReplayResult(
        fired=fired,
        direction=direction if fired else None,
        entry_ts=pd.Timestamp(entry_ts) if fired else None,
        entry_price=entry_price if fired else None,
        stop_price=stop_price if fired else None,
        target_price=entry_price + 37.5 if fired and direction == "LONG" else None,
        exit_ts=pd.Timestamp(entry_ts) + pd.Timedelta(minutes=10) if fired else None,
        exit_price=entry_price + (pnl / 0.50) * 0.25 if fired and direction == "LONG" else None,
        exit_reason=exit_reason if fired else None,
        pnl_dollars=pnl if fired else 0.0,
        pnl_ticks=int(round(pnl / 0.50)) if fired else 0,
        reason=reason,
    )


# ════════════════════════════════════════════════════════════════════
# Test 1: matched within tolerance
# ════════════════════════════════════════════════════════════════════
def test_match_within_tolerance():
    sim = _make_sim()
    # Backtester fires 15s later, 1 tick off on entry, identical otherwise.
    replay = _make_replay(
        entry_ts=datetime(2026, 5, 10, 14, 30, 15, tzinfo=timezone.utc),
        entry_price=29500.25,  # +1 tick
        stop_price=29485.00,
        pnl=18.75,
    )
    row = compare_one(sim, replay, TOL, "bias_momentum")
    assert row.classification == "REPLAYED", f"got {row.classification}, notes={row.notes}"
    assert row.within_tolerance is True, f"notes={row.notes}"
    assert row.delta_entry_seconds is not None and row.delta_entry_seconds <= 60
    assert row.delta_entry_ticks is not None and row.delta_entry_ticks <= 2


# ════════════════════════════════════════════════════════════════════
# Test 2: matched but outside tolerance
# ════════════════════════════════════════════════════════════════════
def test_match_outside_tolerance_entry_price():
    sim = _make_sim()
    # 10-tick entry-price drift (way over the 2-tick tolerance).
    replay = _make_replay(
        entry_ts=datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc),
        entry_price=29502.50,  # +10 ticks
        stop_price=29485.00,
        pnl=18.75,
    )
    row = compare_one(sim, replay, TOL, "bias_momentum")
    assert row.classification == "REPLAYED"
    assert row.within_tolerance is False
    assert any("entry_price delta" in n for n in row.notes), row.notes
    assert row.delta_entry_ticks == 10.0


def test_match_outside_tolerance_pnl():
    sim = _make_sim(pnl=20.00)
    # Backtest P&L = -20 (huge divergence).
    replay = _make_replay(pnl=-20.00, entry_price=29500.00)
    row = compare_one(sim, replay, TOL, "bias_momentum")
    assert row.classification == "REPLAYED"
    assert row.within_tolerance is False
    assert any("pnl delta" in n for n in row.notes), row.notes


# ════════════════════════════════════════════════════════════════════
# Test 3: missing strategy-blocking field -> BLOCKED
# ════════════════════════════════════════════════════════════════════
def test_blocked_when_field_missing():
    sim = _make_sim(include_blockers=False)  # missing day_type, etc.
    # Even with a perfect backtest match, BLOCKED takes precedence
    # because we can't trust the comparison.
    replay = _make_replay()
    row = compare_one(sim, replay, TOL, "bias_momentum")
    assert row.classification == "BLOCKED"
    assert row.within_tolerance is None
    # The missing field names should be reported.
    blocked_note = next((n for n in row.notes
                          if "missing strategy-blocking fields" in n), None)
    assert blocked_note is not None, row.notes
    for f in ("day_type", "cr_verdict", "cvd_health", "es_nq_rs"):
        assert f in blocked_note, f"{f} not in {blocked_note}"
    # Backtest data should still be carried through for diagnostic value.
    assert row.bt_direction == "LONG"
    assert row.bt_entry_price == 29500.00


def test_blocked_partial_missing():
    """Only some blocking fields missing — still BLOCKED."""
    sim = _make_sim(include_blockers=True)
    # Knock out one of the four.
    del sim["market_snapshot"]["es_nq_rs"]
    replay = _make_replay()
    row = compare_one(sim, replay, TOL, "bias_momentum")
    assert row.classification == "BLOCKED"
    blocked_note = next((n for n in row.notes
                          if "missing strategy-blocking fields" in n), None)
    assert blocked_note is not None
    assert "es_nq_rs" in blocked_note
    assert "day_type" not in blocked_note  # the present ones are NOT listed


# ════════════════════════════════════════════════════════════════════
# Test 4: backtest fired opposite direction -> BACKTEST_ONLY
# ════════════════════════════════════════════════════════════════════
def test_backtest_only_direction_mismatch():
    sim = _make_sim(direction="LONG")
    replay = _make_replay(direction="SHORT")
    row = compare_one(sim, replay, TOL, "bias_momentum")
    assert row.classification == "BACKTEST_ONLY"
    assert row.bt_direction == "SHORT"
    assert row.within_tolerance is None
    assert any("backtest direction=SHORT" in n for n in row.notes)


# ════════════════════════════════════════════════════════════════════
# Test 5: backtest did not fire -> SIM_ONLY
# ════════════════════════════════════════════════════════════════════
def test_sim_only_when_no_signal():
    sim = _make_sim()
    replay = _make_replay(fired=False, reason="no signal in ±5m window")
    row = compare_one(sim, replay, TOL, "bias_momentum")
    assert row.classification == "SIM_ONLY"
    assert row.bt_direction is None
    assert "no signal" in (row.notes[0] if row.notes else "")


# ════════════════════════════════════════════════════════════════════
# Test 6: aggregate summary across a mix
# ════════════════════════════════════════════════════════════════════
def test_summary_aggregation():
    sim_ok = _make_sim(trade_id="ok")
    sim_bad = _make_sim(trade_id="bad")
    sim_blk = _make_sim(trade_id="blk", include_blockers=False)
    sim_so = _make_sim(trade_id="sim_only")
    sim_bo = _make_sim(trade_id="bt_only", direction="LONG")

    rows = [
        compare_one(sim_ok, _make_replay(), TOL, "bias_momentum"),
        compare_one(sim_bad, _make_replay(entry_price=29510.00), TOL, "bias_momentum"),  # 40t off
        compare_one(sim_blk, _make_replay(), TOL, "bias_momentum"),
        compare_one(sim_so, _make_replay(fired=False, reason="no signal"), TOL, "bias_momentum"),
        compare_one(sim_bo, _make_replay(direction="SHORT"), TOL, "bias_momentum"),
    ]

    s = _summarize(rows)
    assert s["total"] == 5
    assert s["replayed"] == 2  # ok, bad
    assert s["within_tolerance"] == 1
    assert s["outside_tolerance"] == 1
    assert s["blocked"] == 1
    assert s["sim_only"] == 1
    assert s["backtest_only"] == 1
    # Blocking field counts should pick up all 4 fields from the one blocked trade.
    for f in ("day_type", "cr_verdict", "cvd_health", "es_nq_rs"):
        assert s["blocking_field_counts"].get(f) == 1, s["blocking_field_counts"]
    # Divergence stats only reflect replayed trades.
    assert s["deltas"]["entry_ticks"]["n"] == 2
    assert s["deltas"]["entry_ticks"]["max"] >= 40.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
