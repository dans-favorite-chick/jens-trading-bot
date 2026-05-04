"""L2 ROI audit — statistical helpers + smoke tests.

Verifies the decision tool answers correctly across the cause matrix:
  - Predictive DOM feature → +lift, significant
  - Random DOM feature → low lift, not significant
  - DOM field detection (positive + negative classification)
  - Live tool runs against synthetic data and produces a report
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.audit_l2_roi import (  # noqa: E402
    cis_overlap,
    compute_lift,
    extract_dom_features,
    is_dom_field,
    sample_tier,
    wilson_ci,
)

TOOL = ROOT / "tools" / "audit_l2_roi.py"


# ─── statistical primitives ──────────────────────────────────────────

def test_wilson_ci_zero_n():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_sample_tier_thresholds():
    assert sample_tier(29) == "INSUFFICIENT"
    assert sample_tier(30) == "PRELIMINARY"
    assert sample_tier(100) == "TENTATIVE"
    assert sample_tier(385) == "VALIDATED"
    assert sample_tier(666) == "HIGH_CONF"


def test_cis_overlap_detected():
    assert cis_overlap((0.40, 0.55), (0.50, 0.65))
    assert not cis_overlap((0.20, 0.30), (0.40, 0.55))


# ─── DOM field classifier ───────────────────────────────────────────

def test_is_dom_field_classifies_correctly():
    # Positive cases (should be DOM)
    assert is_dom_field("dom_imbalance")
    assert is_dom_field("market.dom_bid_heavy")
    assert is_dom_field("bid_stack")
    assert is_dom_field("ask_stack")
    assert is_dom_field("market.dom_ask_stack")
    assert is_dom_field("cvd")
    assert is_dom_field("market.cvd")
    # Negative cases (NOT DOM)
    assert not is_dom_field("strategy")
    assert not is_dom_field("pnl_dollars")
    assert not is_dom_field("market.regime")
    assert not is_dom_field("market.atr_5m")
    assert not is_dom_field("entry_price")


# ─── feature extraction ─────────────────────────────────────────────

def test_extract_dom_features_finds_market_fields():
    """The actual schema uses `market_snapshot`; tool must read that."""
    t = {"strategy": "x",
         "market_snapshot": {"dom_imbalance": 0.6,
                             "dom_bid_heavy": True,
                             "regime": "X"}}
    feats = extract_dom_features(t)
    assert "market.dom_imbalance" in feats
    assert "market.dom_bid_heavy" in feats
    assert "market.regime" not in feats


def test_extract_dom_features_legacy_market_key_too():
    """Legacy `market` key still works for backward-compat."""
    t = {"strategy": "x",
         "market": {"dom_imbalance": 0.5}}
    feats = extract_dom_features(t)
    assert "market.dom_imbalance" in feats


def test_extract_dom_features_top_level_dom():
    """A top-level dom_* field is also picked up."""
    t = {"strategy": "x", "dom_bid_stack": 5}
    feats = extract_dom_features(t)
    assert "dom_bid_stack" in feats


def test_extract_dom_features_skips_non_scalars():
    """Lists/dicts should not be extracted (we only do scalar stats)."""
    t = {"strategy": "x",
         "market_snapshot": {"dom_levels": [1, 2, 3]}}
    feats = extract_dom_features(t)
    assert "market.dom_levels" not in feats


# ─── compute_lift ───────────────────────────────────────────────────

def test_compute_lift_binary_predictive():
    """DOM bid_heavy=True → wins; False → losses → strong + lift, sig."""
    trades = []
    for _ in range(40):
        trades.append({"strategy": "x", "pnl_dollars": +10.0,
                       "market_snapshot": {"dom_bid_heavy": True}})
    for _ in range(40):
        trades.append({"strategy": "x", "pnl_dollars": -10.0,
                       "market_snapshot": {"dom_bid_heavy": False}})
    result = compute_lift(trades, "market.dom_bid_heavy")
    assert result is not None
    assert result["lift_pp"] > 50
    assert result["significant"] is True


def test_compute_lift_binary_no_predictive():
    """DOM feature uncorrelated with outcome → low lift."""
    trades = []
    for i in range(40):
        trades.append({"strategy": "x",
                       "pnl_dollars": +10.0 if i % 2 else -10.0,
                       "market_snapshot": {"dom_bid_heavy": True}})
    for i in range(40):
        trades.append({"strategy": "x",
                       "pnl_dollars": +10.0 if i % 2 else -10.0,
                       "market_snapshot": {"dom_bid_heavy": False}})
    result = compute_lift(trades, "market.dom_bid_heavy")
    assert result is not None
    assert abs(result["lift_pp"]) < 15


def test_compute_lift_returns_none_on_missing_field():
    trades = [{"strategy": "x", "pnl_dollars": +10.0,
               "market_snapshot": {}}]
    result = compute_lift(trades, "market.nonexistent")
    assert result is None


def test_compute_lift_numeric_median_split():
    """Numeric DOM imbalance: high values → wins; low → losses."""
    trades = []
    # high imbalance trades win
    for i in range(40):
        trades.append({"strategy": "x", "pnl_dollars": +10.0,
                       "market_snapshot": {"dom_imbalance": 0.8 + i * 0.001}})
    # low imbalance trades lose
    for i in range(40):
        trades.append({"strategy": "x", "pnl_dollars": -10.0,
                       "market_snapshot": {"dom_imbalance": 0.2 - i * 0.001}})
    result = compute_lift(trades, "market.dom_imbalance")
    assert result is not None
    assert result["type"] == "numeric_median_split"
    assert result["lift_pp"] > 50
    assert result["significant"] is True


def test_compute_lift_handles_single_value_numeric():
    """If all trades have the same numeric value, lift is meaningless."""
    trades = [{"strategy": "x", "pnl_dollars": +10.0,
               "market_snapshot": {"dom_imbalance": 0.5}}
              for _ in range(20)]
    result = compute_lift(trades, "market.dom_imbalance")
    assert result is None


# ─── live tool smoke ────────────────────────────────────────────────

def _run(tmp_path: Path, *cli_args: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(TOOL), *cli_args],
        cwd=tmp_path, capture_output=True, text=True,
    )
    return result.returncode, result.stdout + result.stderr


def test_tool_runs_against_synthetic_data(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "strategies").mkdir()
    trades = [
        {"strategy": "test_strat", "pnl_dollars": +10.0,
         "market_snapshot": {"dom_imbalance": 0.7,
                             "dom_bid_heavy": True},
         "ts": "2026-04-01T09:30:00",
         "confluences": ["DOM bid heavy"]}
        for _ in range(30)
    ]
    trades += [
        {"strategy": "test_strat", "pnl_dollars": -10.0,
         "market_snapshot": {"dom_imbalance": 0.3,
                             "dom_bid_heavy": False},
         "ts": "2026-04-01T10:30:00", "confluences": []}
        for _ in range(30)
    ]
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, out = _run(tmp_path)
    assert rc == 0, out
    reports = list((tmp_path / "out").glob("l2_roi_audit_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Statistical lift" in text
    assert "Architectural dependency" in text
    assert "Economic ROI" in text
    assert "Recommendation" in text
    assert "VERDICT" in out  # stdout summary line


def test_tool_no_data_no_deps_says_cancel(tmp_path):
    """Synthetic dataset with NO DOM fields and NO strategy code →
    verdict should be CANCEL with strong evidence."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "strategies").mkdir()
    # Trades with NO DOM fields whatsoever
    trades = [
        {"strategy": "x", "pnl_dollars": +5.0,
         "market_snapshot": {"regime": "X", "atr_5m": 4.5},
         "ts": "2026-04-01T09:30:00"}
        for _ in range(30)
    ]
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, out = _run(tmp_path)
    assert rc == 0, out
    text = next((tmp_path / "out").glob("l2_roi_audit_*.md")).read_text(
        encoding="utf-8"
    )
    # No DOM data + no strategies = cancel verdict
    assert "CANCEL" in text


def test_tool_handles_empty_trade_memory(tmp_path):
    """No trades → tool exits cleanly with informational message."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "logs" / "trade_memory.json").write_text(
        "[]", encoding="utf-8"
    )
    rc, out = _run(tmp_path)
    assert rc == 0
    # Empty-data report still written
    reports = list((tmp_path / "out").glob("l2_roi_audit_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "No trades" in text


def test_tool_post_b13_filter(tmp_path):
    """--post-b13-only filter restricts the dataset correctly."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "strategies").mkdir()
    trades = [
        # 40 pre-B13 (no cost_total_dollars)
        *({"strategy": "x", "pnl_dollars": +5,
           "market_snapshot": {"dom_bid_heavy": True}}
          for _ in range(40)),
        # 20 post-B13
        *({"strategy": "y", "pnl_dollars": +5,
           "pnl_dollars_net": 5, "cost_total_dollars": 1.0,
           "market_snapshot": {"dom_bid_heavy": False}}
          for _ in range(20)),
    ]
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, out = _run(tmp_path, "--post-b13-only")
    assert rc == 0
    text = next((tmp_path / "out").glob("l2_roi_audit_*.md")).read_text(
        encoding="utf-8"
    )
    # Only 20 post-B13 trades should be analyzed
    assert "Trades analyzed: 20" in text
