"""Indicator predictive-value audit tests."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.indicator_audit import (  # noqa: E402
    analyze_indicator,
    bin_label,
    cis_overlap,
    discover_schema,
    extract_features,
    quartile_bin,
    sample_tier,
    wilson_ci,
)

TOOL = ROOT / "tools" / "indicator_audit.py"


# ─── Wilson CI ───────────────────────────────────────────────────────

def test_wilson_ci_zero_n():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_perfect_record_lower_bound_meaningful():
    """5/5 wins → upper bound clamps at 1.0 but lower bound is well below."""
    lo, hi = wilson_ci(5, 5)
    assert hi <= 1.0
    assert 0.55 < lo < 0.70


def test_wilson_ci_widens_with_smaller_n():
    lo10, hi10 = wilson_ci(5, 10)
    lo100, hi100 = wilson_ci(50, 100)
    assert (hi10 - lo10) > (hi100 - lo100)


# ─── sample tier ─────────────────────────────────────────────────────

def test_sample_tier_thresholds():
    assert sample_tier(0) == "INSUFFICIENT"
    assert sample_tier(29) == "INSUFFICIENT"
    assert sample_tier(30) == "PRELIMINARY"
    assert sample_tier(99) == "PRELIMINARY"
    assert sample_tier(100) == "TENTATIVE"
    assert sample_tier(384) == "TENTATIVE"
    assert sample_tier(385) == "VALIDATED"
    assert sample_tier(665) == "VALIDATED"
    assert sample_tier(666) == "HIGH_CONF"


# ─── CI overlap ──────────────────────────────────────────────────────

def test_overlapping_cis_detected():
    assert cis_overlap((0.40, 0.55), (0.50, 0.65))


def test_non_overlapping_cis_detected():
    assert not cis_overlap((0.20, 0.30), (0.40, 0.55))


def test_touching_cis_overlap():
    """Boundary case: ci_a.high == ci_b.low → touching, treat as overlap."""
    assert cis_overlap((0.30, 0.50), (0.50, 0.70))


# ─── quartile binning ────────────────────────────────────────────────

def test_quartile_bin_basic():
    cuts = quartile_bin([1, 2, 3, 4, 5, 6, 7, 8])
    # cuts: q1_max=3, q2_max=5, q3_max=7
    assert bin_label(2, cuts) == "Q1"   # <=3 -> Q1
    assert bin_label(4, cuts) == "Q2"   # <=5 -> Q2
    assert bin_label(7, cuts) == "Q3"   # <=7 -> Q3 (boundary)
    assert bin_label(8, cuts) == "Q4"   # > 7 -> Q4


def test_quartile_bin_empty_returns_empty():
    assert quartile_bin([]) == {}


# ─── feature extraction ──────────────────────────────────────────────

def test_extract_features_handles_minimal_trade():
    t = {"strategy": "bias_momentum", "tier": "A", "direction": "LONG",
         "pnl_dollars": 12.50}
    feats = extract_features(t)
    assert feats["strategy"] == "bias_momentum"
    assert feats["tier"] == "A"
    assert feats["direction"] == "LONG"


def test_extract_features_flattens_market_snapshot():
    t = {"strategy": "x",
         "market": {"regime": "POSITIVE_STRONG", "atr_5m": 4.5,
                    "tf_votes_bullish": 3}}
    feats = extract_features(t)
    assert feats["market.regime"] == "POSITIVE_STRONG"
    assert feats["market.atr_5m"] == 4.5
    assert feats["market.tf_votes_bullish"] == 3


def test_extract_features_flattens_market_snapshot_alt_key():
    """Phoenix's actual schema uses `market_snapshot` (not `market`)."""
    t = {"strategy": "x",
         "market_snapshot": {"regime": "OPEN_MOMENTUM", "vix": 18.5}}
    feats = extract_features(t)
    assert feats["market.regime"] == "OPEN_MOMENTUM"
    assert feats["market.vix"] == 18.5


def test_extract_features_flattens_confluences_as_booleans():
    t = {"strategy": "x",
         "confluences": ["VWAP reclaim", "EMA stack bullish"]}
    feats = extract_features(t)
    assert feats.get("conf:VWAP reclaim") is True
    assert feats.get("conf:EMA stack bullish") is True


def test_extract_features_handles_string_confluences():
    """Some schemas store a single confluence as a string."""
    t = {"strategy": "x", "confluences": "VWAP reclaim"}
    feats = extract_features(t)
    assert feats.get("conf:VWAP reclaim") is True


# ─── schema discovery ───────────────────────────────────────────────

def test_discover_skips_sparse_features():
    """Features present in <5% of trades dropped (default min_presence)."""
    trades = [{"strategy": "x", "tier": "A"} for _ in range(95)]
    trades += [{"strategy": "x", "tier": "A", "rare_field": "yes"}
               for _ in range(4)]
    schema = discover_schema(trades, min_presence=0.05)
    assert "strategy" in schema
    assert "tier" in schema
    assert "rare_field" not in schema


def test_discover_empty_returns_empty():
    assert discover_schema([]) == {}


# ─── analyze_indicator ──────────────────────────────────────────────

def test_analyze_finds_predictive_confluence():
    """A confluence that fires only on winners → 100% WR present, lift > 50pp,
    significant."""
    trades = []
    for _ in range(50):
        trades.append({"strategy": "x", "pnl_dollars": +10.0,
                       "confluences": ["MAGIC"]})
    for _ in range(50):
        trades.append({"strategy": "x", "pnl_dollars": -10.0,
                       "confluences": []})
    rows = analyze_indicator(trades, "conf:MAGIC", min_sample=10)
    true_row = next((r for r in rows if r["value"] == "True"), None)
    assert true_row is not None
    assert true_row["wr_with"] == 100.0
    assert true_row["lift_pp"] > 50
    assert true_row["significant"] is True


def test_analyze_finds_contra_indicator():
    """A confluence that fires only on losers → 0% WR present, lift < -50pp,
    significant. This is the "BAD_OMEN" case."""
    trades = []
    for _ in range(50):
        trades.append({"strategy": "x", "pnl_dollars": +10.0,
                       "confluences": []})
    for _ in range(50):
        trades.append({"strategy": "x", "pnl_dollars": -10.0,
                       "confluences": ["BAD_OMEN"]})
    rows = analyze_indicator(trades, "conf:BAD_OMEN", min_sample=10)
    true_row = next((r for r in rows if r["value"] == "True"), None)
    assert true_row is not None
    assert true_row["wr_with"] == 0.0
    assert true_row["lift_pp"] < -50
    assert true_row["significant"] is True


def test_analyze_returns_empty_when_feature_absent():
    """Feature not present in any trade → empty rows, no crash."""
    trades = [{"strategy": "x", "pnl_dollars": +10.0} for _ in range(20)]
    rows = analyze_indicator(trades, "conf:NONEXISTENT", min_sample=10)
    assert rows == []


def test_analyze_quartile_bins_numeric_features():
    """Numeric feature with >4 unique values gets quartile-binned."""
    trades = []
    for i in range(40):
        # stop_ticks ranges 10-49, alternating winners/losers
        trades.append({"strategy": "x", "stop_ticks": 10 + i,
                       "pnl_dollars": +10.0 if i % 2 == 0 else -10.0})
    rows = analyze_indicator(trades, "stop_ticks", min_sample=5)
    # Should produce Q1-Q4 buckets
    values = sorted({r["value"] for r in rows})
    assert "Q1" in values
    assert "Q4" in values


# ─── tool runs end-to-end ───────────────────────────────────────────

def _run(tmp_path: Path, *cli_args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(TOOL), *cli_args],
        cwd=tmp_path, capture_output=True, text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_tool_runs_against_synthetic_trade_memory(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    trades = [{"strategy": "bias_momentum", "tier": "A++",
               "direction": "LONG", "pnl_dollars": +10,
               "confluences": ["GOOD"], "ts": "2026-04-01T09:30:00"}
              for _ in range(30)]
    trades += [{"strategy": "bias_momentum", "tier": "C",
                "direction": "SHORT", "pnl_dollars": -10,
                "confluences": [], "ts": "2026-04-01T09:30:00"}
               for _ in range(30)]
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, out, err = _run(tmp_path)
    assert rc == 0, err
    reports = list((tmp_path / "out").glob("indicator_audit_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Top Predictive" in text
    assert "Tier Classifier Validation" in text


def test_tool_discover_mode(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    trades = [{"strategy": "x", "tier": "A", "pnl_dollars": +10}
              for _ in range(30)]
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, out, err = _run(tmp_path, "--discover")
    assert rc == 0, err
    assert "strategy" in out
    assert "tier" in out
    # In --discover mode, no audit file is written
    reports = list((tmp_path / "out").glob("indicator_audit_*.md"))
    assert len(reports) == 0


def test_tool_handles_empty_trade_memory(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "trade_memory.json").write_text("[]",
                                                          encoding="utf-8")
    rc, out, err = _run(tmp_path)
    assert rc == 0
    assert "No trades" in out


def test_tool_post_b13_filter(tmp_path):
    """--post-b13-only must skip trades without cost_total_dollars."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "out").mkdir()
    trades = [
        # 30 pre-B13
        *({"strategy": "old", "pnl_dollars": +5}
          for _ in range(30)),
        # 30 post-B13
        *({"strategy": "new", "pnl_dollars": +5,
           "pnl_dollars_net": 5, "cost_total_dollars": 1.0}
          for _ in range(30)),
    ]
    (tmp_path / "logs" / "trade_memory.json").write_text(
        json.dumps(trades), encoding="utf-8"
    )
    rc, out, err = _run(tmp_path, "--post-b13-only", "--discover")
    assert rc == 0, err
    assert "Loaded 30 trades" in out
