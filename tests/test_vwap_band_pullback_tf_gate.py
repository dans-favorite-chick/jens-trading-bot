"""vwap_band_pullback TF-vote gate (#15, 2026-05-13).

The 3-of-N TF-vote gate was too strict for a mean-reversion entry: VWAP
bands tend to touch on the LAST candle before reversal, when only the
lowest TF (5m or below) has flipped direction. 3-of-N over-gated.

Dropped to 2-of-N (config-driven, default 2). 1-of-N would be reckless
(no trend filter at all), so the floor is 2.

These tests pin the new behavior + the config-driven knob so any
accidental return to hardcoded 3 trips the suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_strategy(min_tf_votes: int | None = None):
    """Build a vwap_band_pullback strategy with a custom min_tf_votes."""
    from strategies.vwap_band_pullback import VwapBandPullback
    cfg = {
        "min_bars": 50, "rsi_period": 2,
        "rsi_long_threshold": 30, "rsi_short_threshold": 70,
        "atr_period": 14, "min_volume_ratio": 0.8, "target_rr": 2.0,
        "min_stop_ticks": 40, "max_stop_ticks": 120, "max_hold_min": 60,
    }
    if min_tf_votes is not None:
        cfg["min_tf_votes"] = min_tf_votes
    return VwapBandPullback(cfg)


def test_config_carries_min_tf_votes_2():
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["vwap_band_pullback"]
    assert cfg["min_tf_votes"] == 2, (
        "vwap_band_pullback min_tf_votes must stay at 2 — see "
        "tests/test_vwap_band_pullback_tf_gate.py rationale."
    )


def test_2_bullish_votes_allows_long_signal():
    """With min_tf_votes=2, 2 bullish + 1 bearish should pass safe_to_long."""
    s = _make_strategy(min_tf_votes=2)
    mtf = s._derive_mtf_trend_from_market({
        "tf_votes_bullish": 2, "tf_votes_bearish": 1,
    })
    assert mtf.safe_to_long is True
    assert mtf.safe_to_short is False
    assert mtf.htf_trend == "UP"


def test_2_bearish_votes_allows_short_signal():
    s = _make_strategy(min_tf_votes=2)
    mtf = s._derive_mtf_trend_from_market({
        "tf_votes_bullish": 1, "tf_votes_bearish": 2,
    })
    assert mtf.safe_to_short is True
    assert mtf.safe_to_long is False
    assert mtf.htf_trend == "DOWN"


def test_1_vote_each_side_is_neutral():
    """1 vs 1 (or 1 vs 0) is below the 2-vote floor — NEUTRAL/no-signal."""
    s = _make_strategy(min_tf_votes=2)
    mtf = s._derive_mtf_trend_from_market({
        "tf_votes_bullish": 1, "tf_votes_bearish": 1,
    })
    assert mtf.safe_to_long is False
    assert mtf.safe_to_short is False
    assert mtf.htf_trend == "NEUTRAL"


def test_old_3_vote_threshold_now_blocks_what_2_passes():
    """Regression-style: with min_tf_votes=3 (old behavior), 2 bullish
    votes is BLOCKED. With min_tf_votes=2 (new), it passes. This
    cements that the config knob actually does what it says."""
    market = {"tf_votes_bullish": 2, "tf_votes_bearish": 0}
    old = _make_strategy(min_tf_votes=3)
    new = _make_strategy(min_tf_votes=2)
    assert old._derive_mtf_trend_from_market(market).safe_to_long is False
    assert new._derive_mtf_trend_from_market(market).safe_to_long is True


def test_equal_votes_does_not_clear_directional_gate():
    """Even at min_tf_votes=2, bullish == bearish must NOT clear the
    gate — direction is required (bullish > bearish)."""
    s = _make_strategy(min_tf_votes=2)
    mtf = s._derive_mtf_trend_from_market({
        "tf_votes_bullish": 2, "tf_votes_bearish": 2,
    })
    assert mtf.safe_to_long is False
    assert mtf.safe_to_short is False


def test_default_min_tf_votes_falls_back_to_2():
    """If the config omits min_tf_votes entirely, the strategy should
    default to 2 (the new floor), not the old 3."""
    s = _make_strategy(min_tf_votes=None)  # config has no key
    mtf = s._derive_mtf_trend_from_market({
        "tf_votes_bullish": 2, "tf_votes_bearish": 0,
    })
    assert mtf.safe_to_long is True
