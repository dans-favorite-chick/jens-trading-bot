"""Validation tracker — statistical tiers + Wilson CI + decision tree."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.validation_tracker import (
    decision_recommendation,
    statistical_tier,
    tier_next_threshold,
    wilson_ci,
)

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "validation_tracker.py"


# ─── Wilson 95% CI ───────────────────────────────────────────────────

def test_wilson_ci_zero_trades():
    lo, hi = wilson_ci(0, 0)
    assert lo == 0.0 and hi == 0.0


def test_wilson_ci_perfect_record_lower_bound_meaningful():
    """5/5 wins → lower-CI bound is meaningfully below 100%, even though
    upper bound clamps at 1.0. The dangerous illusion this prevents:
    a 5-trade sample with all wins reading as 'WR=100%' point estimate.
    With Wilson, the LCI shows the truth — the data only supports a
    range that bottoms out somewhere in the 50-60% region."""
    lo, hi = wilson_ci(5, 5)
    assert hi <= 1.0           # bounded at 1
    assert 0.55 < lo < 0.70    # LCI well below 100% — the point of Wilson


def test_wilson_ci_50pct_50_trades_is_wide():
    """50% WR / 50 trades → CI ~36%-64% per published Wilson tables."""
    lo, hi = wilson_ci(25, 50)
    assert 0.30 < lo < 0.40
    assert 0.60 < hi < 0.70


def test_wilson_ci_narrows_with_more_data():
    """Same proportion, larger n → narrower CI."""
    lo_50,  hi_50  = wilson_ci(25,  50)
    lo_500, hi_500 = wilson_ci(250, 500)
    assert (hi_500 - lo_500) < (hi_50 - lo_50)


# ─── Tier classification ─────────────────────────────────────────────

def test_statistical_tier_thresholds():
    assert statistical_tier(0)    == "INSUFFICIENT_SAMPLE"
    assert statistical_tier(29)   == "INSUFFICIENT_SAMPLE"
    assert statistical_tier(30)   == "PRELIMINARY"
    assert statistical_tier(99)   == "PRELIMINARY"
    assert statistical_tier(100)  == "TENTATIVE"
    assert statistical_tier(384)  == "TENTATIVE"
    assert statistical_tier(385)  == "VALIDATED"
    assert statistical_tier(665)  == "VALIDATED"
    assert statistical_tier(666)  == "HIGH_CONFIDENCE"


def test_tier_next_threshold():
    assert tier_next_threshold(10)  == 30
    assert tier_next_threshold(50)  == 100
    assert tier_next_threshold(200) == 385
    assert tier_next_threshold(500) == 666
    assert tier_next_threshold(700) is None


# ─── Decision tree ───────────────────────────────────────────────────

def test_decision_kill_at_low_pf_with_sufficient_sample():
    """30+ trades with PF < 0.7 → kill candidate."""
    d = decision_recommendation(n=50, wr=0.30, pf=0.5, lo_wr=0.20)
    assert "KILL" in d


def test_decision_no_kill_below_30_trades():
    """Even disastrous PF doesn't kill if n < 30."""
    d = decision_recommendation(n=20, wr=0.10, pf=0.2, lo_wr=0.05)
    assert "WATCH" in d
    assert "insufficient" in d.lower()


def test_decision_graduate_requires_tentative_or_higher():
    """PF>1.5 with strong WR but only n=50 (PRELIMINARY) → still WATCH, not graduate."""
    d_50  = decision_recommendation(n=50,  wr=0.65, pf=2.0, lo_wr=0.55)
    d_150 = decision_recommendation(n=150, wr=0.65, pf=2.0, lo_wr=0.55)
    assert "WATCH" in d_50
    assert "GRADUATE" in d_150 or "SCALE" in d_150


def test_decision_moderate_edge_says_watch():
    """PF=1.4 with reasonable WR-LCI → WATCH (moderate)."""
    d = decision_recommendation(n=80, wr=0.55, pf=1.4, lo_wr=0.45)
    assert "WATCH" in d
    assert "moderate" in d.lower()


def test_decision_marginal_pf_says_watch_noise():
    """1.0 < PF < 1.3 → WATCH (could be noise)."""
    d = decision_recommendation(n=80, wr=0.50, pf=1.15, lo_wr=0.40)
    assert "WATCH" in d


# ─── Smoke / live-run ────────────────────────────────────────────────

def _run(tmp_path: Path, *cli_args: str) -> tuple[int, str]:
    """Run the validation tracker with cwd=tmp_path. Returns (rc, report_text)."""
    result = subprocess.run(
        [sys.executable, str(TOOL), *cli_args],
        cwd=tmp_path, capture_output=True, text=True,
    )
    reports = list((tmp_path / "out").glob("validation_status_*.md"))
    text = reports[0].read_text(encoding="utf-8") if reports else ""
    return result.returncode, text


def test_tracker_smoke_run(tmp_path):
    """Tool runs against synthetic trade_memory and produces a report."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    # 50 trades — sits in PRELIMINARY tier. Alternating ±10/±5 for a winning edge.
    trades = []
    for i in range(50):
        pnl = 10.0 if i % 3 != 0 else -5.0
        trades.append({
            "strategy": "bias_momentum",
            "pnl_dollars": pnl,
            "entry_time": f"2026-04-{(i % 28) + 1:02d}T09:30:00",
        })
    (tmp_path / "logs/trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "PRELIMINARY" in text   # n=50 sits in PRELIMINARY
    assert "bias_momentum" in text
    assert "WR" in text
    assert "Trailing 7-Day" in text


def test_tracker_handles_empty_trade_memory(tmp_path):
    """No trades → tool still emits a report without crashing."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "logs/trade_memory.json").write_text("[]", encoding="utf-8")
    rc, text = _run(tmp_path)
    assert rc == 0
    assert "Statistical Tier Reference" in text


def test_tracker_post_b13_only_filter(tmp_path):
    """--post-b13-only restricts to trades with cost_total_dollars."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    trades = []
    # 30 pre-B13 trades for "old_strategy" — should be filtered out
    for i in range(30):
        trades.append({"strategy": "old_strategy", "pnl_dollars": 5.0,
                       "entry_time": f"2026-03-{(i%28)+1:02d}T09:00:00"})
    # 10 post-B13 trades for "new_strategy" — should be kept
    for i in range(10):
        trades.append({"strategy": "new_strategy",
                       "pnl_dollars": 5.0, "pnl_dollars_net": 5.0,
                       "pnl_dollars_gross": 7.0, "cost_total_dollars": 2.0,
                       "entry_time": f"2026-05-{(i%28)+1:02d}T09:00:00"})
    (tmp_path / "logs/trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, text = _run(tmp_path, "--post-b13-only")
    assert rc == 0
    assert "new_strategy" in text
    assert "old_strategy" not in text  # filtered out
