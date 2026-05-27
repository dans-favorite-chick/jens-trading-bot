"""P4-6: tests for tools/ai_uplift_harness.py.

Coverage targets:
  - empty / unverdicted trade memory → INSUFFICIENT (no crash)
  - "perfect filter" — every loser is NO_GO, every winner is GO → high
    positive lift, CI excludes zero on the positive side
  - useless filter — verdicts uncorrelated with P&L → CI crosses zero
  - bootstrap CI is reproducible across runs with same seed
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.ai_uplift_harness import (
    analyze_agent,
    bootstrap_ci,
    build_cohorts,
)


def _trade(pnl: float, verdict: str | None, agent: str = "pretrade",
           ts: str = "2026-05-01T10:00:00") -> dict:
    """Build a minimal trade row the harness will accept."""
    t = {
        "trade_id": f"t_{pnl}_{verdict}_{random.random()}",
        "strategy": "bias_momentum",
        "pnl_dollars": pnl,
        "exit_time": ts,
        "result": "WIN" if pnl > 0 else "LOSS",
    }
    if verdict is not None:
        t["agent_verdicts"] = {agent: verdict}
    return t


# ─── 1. no decisions / no verdicts ─────────────────────────────────────

def test_no_decisions_reports_insufficient_data():
    """Empty trade list → INSUFFICIENT, no crash."""
    r = analyze_agent([], agent="pretrade", min_decisions=10)
    assert r.insufficient
    assert r.decisions == 0
    assert "No usable verdicts" in r.insufficient_reason


def test_trades_without_verdicts_report_insufficient():
    """Trades present but no agent_verdicts field → INSUFFICIENT."""
    trades = [_trade(10.0, verdict=None) for _ in range(50)]
    r = analyze_agent(trades, agent="pretrade", min_decisions=10)
    assert r.insufficient
    assert r.decisions == 0


def test_below_min_decisions_reports_insufficient():
    """5 verdicts with min_decisions=100 → INSUFFICIENT, mentions count."""
    trades = [_trade(10.0, verdict="CLEAR") for _ in range(5)]
    r = analyze_agent(trades, agent="pretrade", min_decisions=100)
    assert r.insufficient
    assert "5 usable decisions" in r.insufficient_reason


# ─── 2. perfect filter ────────────────────────────────────────────────

def test_perfect_filter_reports_high_lift():
    """Agent vetoes every losing trade → counterfactual = sum of winners.

    With clean separation (winners are CLEAR, losers are SIT_OUT), the
    per-decision lift = -mean(losers) * (n_losers / n_total), and the
    bootstrap CI must exclude zero on the positive side.
    """
    winners = [_trade(50.0, verdict="CLEAR") for _ in range(60)]
    losers = [_trade(-50.0, verdict="SIT_OUT") for _ in range(60)]
    trades = winners + losers
    r = analyze_agent(
        trades, agent="pretrade", min_decisions=10, seed=42, resamples=2000,
    )
    assert not r.insufficient
    assert r.decisions == 120
    assert r.cohort_a.n == 60
    assert r.cohort_b.n == 60
    # All losers are blocked → counterfactual is just the winners total.
    assert r.counterfactual_total == pytest.approx(60 * 50.0)
    assert r.actual_total == pytest.approx(0.0)
    # Per-decision lift = +$25 ($3000 lift / 120 decisions).
    assert r.per_decision_lift == pytest.approx(25.0, rel=1e-6)
    # CI must exclude zero (positive side).
    assert r.bootstrap_ci_low > 0
    assert r.verdict_label == "AGENT_USEFUL"


# ─── 3. useless filter ────────────────────────────────────────────────

def test_useless_filter_reports_zero_lift():
    """Verdicts are random; P&L is symmetric around zero → CI crosses zero."""
    rng = random.Random(0)
    trades = []
    for _ in range(400):
        pnl = rng.gauss(0.0, 20.0)
        # Random GO/NO-GO independent of P&L.
        verdict = "CLEAR" if rng.random() < 0.7 else "SIT_OUT"
        trades.append(_trade(pnl, verdict=verdict))
    r = analyze_agent(
        trades, agent="pretrade", min_decisions=50, seed=42, resamples=2000,
    )
    assert not r.insufficient
    assert r.decisions == 400
    # When verdicts are random and P&L mean is ~0, CI should straddle 0.
    assert r.bootstrap_ci_low < 0
    assert r.bootstrap_ci_high > 0
    assert r.verdict_label == "AGENT_NOT_DEMONSTRABLY_USEFUL"


# ─── 4. bootstrap reproducibility ──────────────────────────────────────

def test_bootstrap_reproducible_with_seed():
    """Same seed + same inputs → exactly identical CI bounds."""
    pnls_blocked = [-10.0, -25.0, 15.0, -3.0, -40.0, 8.0] * 10
    lo1, hi1 = bootstrap_ci(pnls_blocked, pnls_allowed_n=200, seed=42, resamples=1000)
    lo2, hi2 = bootstrap_ci(pnls_blocked, pnls_allowed_n=200, seed=42, resamples=1000)
    assert lo1 == lo2
    assert hi1 == hi2
    # Different seed → at least one bound differs (statistical: vanishingly
    # unlikely to match exactly across 1000 resamples).
    lo3, hi3 = bootstrap_ci(pnls_blocked, pnls_allowed_n=200, seed=7, resamples=1000)
    assert (lo1, hi1) != (lo3, hi3)


def test_full_analyze_reproducible_with_seed():
    """End-to-end analyze_agent with same seed → identical CI."""
    trades = []
    rng = random.Random(123)
    for _ in range(200):
        pnl = rng.gauss(0.0, 20.0)
        v = "CLEAR" if rng.random() < 0.7 else "SIT_OUT"
        trades.append(_trade(pnl, verdict=v))
    r1 = analyze_agent(trades, agent="pretrade", min_decisions=10, seed=42)
    r2 = analyze_agent(trades, agent="pretrade", min_decisions=10, seed=42)
    assert r1.bootstrap_ci_low == r2.bootstrap_ci_low
    assert r1.bootstrap_ci_high == r2.bootstrap_ci_high


# ─── 5. verdict shape flexibility ──────────────────────────────────────

def test_extracts_verdict_from_flat_field():
    """trade['pretrade_verdict'] = 'SIT_OUT' is recognized."""
    trades = []
    for _ in range(20):
        trades.append({
            "trade_id": str(random.random()),
            "pnl_dollars": -10.0,
            "exit_time": "2026-05-01T10:00:00",
            "pretrade_verdict": "SIT_OUT",
        })
    for _ in range(20):
        trades.append({
            "trade_id": str(random.random()),
            "pnl_dollars": 20.0,
            "exit_time": "2026-05-01T10:00:00",
            "pretrade_verdict": "CLEAR",
        })
    a, b, missing = build_cohorts(trades, "pretrade")
    assert a.n == 20
    assert b.n == 20
    assert missing == 0


def test_extracts_verdict_from_market_snapshot():
    """Nested under market_snapshot['agent_verdicts'] is also recognized."""
    t = {
        "trade_id": "x",
        "pnl_dollars": 5.0,
        "exit_time": "2026-05-01T10:00:00",
        "market_snapshot": {
            "agent_verdicts": {"council": "BLOCK"},
        },
    }
    a, b, missing = build_cohorts([t], "council")
    assert b.n == 1
    assert a.n == 0
    assert missing == 0


def test_unrecognized_verdict_dropped_not_crashed():
    """A trade with a gibberish verdict is dropped (counted as missing)."""
    t = {"trade_id": "y", "pnl_dollars": 1.0, "exit_time": "2026-05-01T10:00:00",
         "agent_verdicts": {"pretrade": "MAYBE_PROBABLY"}}
    a, b, missing = build_cohorts([t], "pretrade")
    assert a.n == 0 and b.n == 0
    assert missing == 1


# ─── 6. since-filter ───────────────────────────────────────────────────

def test_since_filter_restricts_window():
    """Trades before --since are excluded."""
    old = [_trade(10.0, verdict="CLEAR", ts="2026-03-01T10:00:00") for _ in range(50)]
    new = [_trade(10.0, verdict="CLEAR", ts="2026-05-01T10:00:00") for _ in range(50)]
    r = analyze_agent(
        old + new, agent="pretrade",
        since="2026-04-15", min_decisions=10, seed=42,
    )
    assert r.decisions == 50
