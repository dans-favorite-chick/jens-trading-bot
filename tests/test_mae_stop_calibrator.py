"""MAE-calibrated stop recommender framework (#17, 2026-05-13).

The recommender itself doesn't auto-apply — it just computes
percentile statistics over winning-trade MAE and reports a recommended
stop. These tests pin the math so future operators trust the output
before manually editing config/strategies.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.mae_stop_calibrator import (
    _percentile,
    collect_winner_maes,
    recommend_stop,
)


# ── _percentile sanity ─────────────────────────────────────────────────

def test_percentile_empty_is_zero():
    assert _percentile([], 50) == 0.0


def test_percentile_basic():
    assert _percentile([1, 2, 3, 4, 5], 50) == 3.0


# ── collect_winner_maes ────────────────────────────────────────────────

def test_collect_only_winners_with_mae_data():
    trades = [
        {"strategy": "a", "mae_ticks": 5.0, "pnl_dollars_net": 100.0},   # win
        {"strategy": "a", "mae_ticks": 8.0, "pnl_dollars_net": -50.0},   # loss — excluded
        {"strategy": "a", "mae_ticks": None, "pnl_dollars_net": 200.0},  # no MAE — excluded
        {"strategy": "b", "mae_ticks": 3.0, "pnl_dollars_net": 25.0},    # win
    ]
    out = collect_winner_maes(trades)
    assert out["a"] == [5.0]
    assert out["b"] == [3.0]


def test_collect_uses_pnl_dollars_fallback():
    """If pnl_dollars_net missing, fall back to pnl_dollars (pre-B13)."""
    trades = [
        {"strategy": "a", "mae_ticks": 5.0, "pnl_dollars": 100.0},
    ]
    out = collect_winner_maes(trades)
    assert out["a"] == [5.0]


def test_collect_excludes_breakeven_trades():
    """pnl=0 is not a winner — exclude (no edge evidence)."""
    trades = [
        {"strategy": "a", "mae_ticks": 5.0, "pnl_dollars_net": 0.0},
    ]
    out = collect_winner_maes(trades)
    assert out["a"] == []


# ── recommend_stop ─────────────────────────────────────────────────────

def test_recommend_empty_returns_insufficient():
    r = recommend_stop([])
    assert r["n_winners"] == 0
    assert r["confidence"] == "INSUFFICIENT"


def test_recommend_below_30_is_insufficient():
    r = recommend_stop([5.0] * 20)
    assert r["confidence"] == "INSUFFICIENT"


def test_recommend_30_to_49_is_low():
    r = recommend_stop([5.0] * 40)
    assert r["confidence"] == "LOW"


def test_recommend_50_to_199_is_ok():
    r = recommend_stop([5.0] * 100)
    assert r["confidence"] == "OK"


def test_recommend_200_plus_is_high():
    r = recommend_stop([5.0] * 250)
    assert r["confidence"] == "HIGH"


def test_recommend_p75_rounds_up_with_buffer():
    """p75 of [10,10,10,10,10,10,10,10,10,10,20] = 10, buffer 20% = 12."""
    maes = [10.0] * 50 + [50.0]  # 51 trades — p75 = 10
    r = recommend_stop(maes, buffer_pct=20.0)
    # p75 = 10, recommended = ceil(10 * 1.2) = 12
    assert r["recommended"] == 12


def test_recommend_conservative_uses_p95():
    """Conservative pulls from the 95th percentile, recommended from p75 —
    the conservative number should be at LEAST as large as recommended
    (or equal when the distribution is uniform)."""
    # Sloped distribution so p95 is meaningfully > p75
    maes = list(range(1, 101))  # 1..100
    r = recommend_stop(maes, buffer_pct=0.0)
    # p75 = 75.x, p95 = 95.x — conservative > recommended
    assert r["conservative"] > r["recommended"]
    assert r["recommended"] >= 75
    assert r["conservative"] >= 95


def test_buffer_zero_pct_returns_raw_percentiles():
    maes = [10.0] * 50
    r = recommend_stop(maes, buffer_pct=0.0)
    assert r["recommended"] == 10  # p75 with no buffer
