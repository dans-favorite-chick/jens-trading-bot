"""Tests for agents.market_advisor — deterministic guidance producer."""

from __future__ import annotations

import pytest

from agents import market_advisor as ma


def _base_market(**overrides) -> dict:
    """Plausible MNQ snapshot; overrides replace top-level keys only."""
    m = {
        "price": 27400.0,
        "vwap": 27350.0,
        "vwap_std": 60.0,        # realistic σ for MNQ on a normal day
        "atr_1m": 5.0,
        "atr_5m": 18.0,
        "atr_15m": 55.0,
        "atr_60m": 180.0,
        "cvd": 0,
        "bar_delta": 0,
        "tf_bias": {"1m": "NEUTRAL", "5m": "NEUTRAL", "15m": "NEUTRAL", "60m": "NEUTRAL"},
        "menthorq": {
            "gex_regime": "POSITIVE",
            "direction_bias": "NEUTRAL",
            "hvl": 27300.0,
            "vanna": "NEUTRAL",
            "charm": "NEUTRAL",
            "cta_positioning": "NEUTRAL",
            "net_gex_bn": 2.0,
            "notes": "",
        },
        "intel": {"vix": 18.0},
    }
    m.update(overrides)
    return m


class TestSentiment:
    def test_neutral_when_no_signals(self):
        g = ma.compute_guidance(_base_market(), fmp_snap=None)
        assert g.sentiment == "NEUTRAL"

    def test_bullish_when_tf_and_mq_align(self):
        m = _base_market()
        m["tf_bias"] = {"1m": "BULLISH", "5m": "BULLISH", "15m": "BULLISH", "60m": "NEUTRAL"}
        m["menthorq"]["direction_bias"] = "LONG"
        m["menthorq"]["vanna"] = "BULLISH"
        g = ma.compute_guidance(m, fmp_snap=None)
        assert g.sentiment == "BULLISH"
        assert g.direction_conf > 70

    def test_bearish_when_tf_and_mq_align(self):
        m = _base_market()
        m["tf_bias"] = {"1m": "BEARISH", "5m": "BEARISH", "15m": "BEARISH", "60m": "BEARISH"}
        m["menthorq"]["direction_bias"] = "SHORT"
        g = ma.compute_guidance(m, fmp_snap=None)
        assert g.sentiment == "BEARISH"


class TestVolatilityRegime:
    def test_normal_on_balanced_atr(self):
        g = ma.compute_guidance(_base_market(), fmp_snap=None)
        assert g.volatility_regime == "NORMAL"

    def test_expanded_on_high_5m_atr(self):
        m = _base_market(atr_5m=40.0)  # 40*3 / 55 = 2.18 → EXTREME
        g = ma.compute_guidance(m, fmp_snap=None)
        assert g.volatility_regime == "EXTREME"

    def test_compressed_on_low_5m_atr(self):
        m = _base_market(atr_5m=8.0)   # 8*3 / 55 = 0.44 → COMPRESSED
        g = ma.compute_guidance(m, fmp_snap=None)
        assert g.volatility_regime == "COMPRESSED"


class TestMarketRegime:
    def test_trending_bull(self):
        m = _base_market(atr_5m=22.0)
        m["tf_bias"] = {"1m": "BULLISH", "5m": "BULLISH", "15m": "BULLISH", "60m": "BULLISH"}
        m["menthorq"]["direction_bias"] = "LONG"
        g = ma.compute_guidance(m, fmp_snap=None)
        assert g.market_regime == "TRENDING_BULL"
        assert g.suggested_rr_tier >= 2.5

    def test_overextended_from_vwap(self):
        m = _base_market(price=27600.0)  # 250 points = 4.2σ above VWAP
        g = ma.compute_guidance(m, fmp_snap=None)
        assert g.market_regime == "OVEREXTENDED"
        assert g.suggested_rr_tier == 1.5
        assert "vwap_extreme" in g.caution_flags

    def test_choppy_when_compressed_and_neutral(self):
        m = _base_market(atr_5m=8.0)
        g = ma.compute_guidance(m, fmp_snap=None)
        assert g.market_regime == "CHOPPY"
        assert g.suggested_rr_tier == 2.0


class TestCautionFlags:
    def test_rsi_overbought_flag(self):
        m = _base_market()
        m["rsi"] = 78
        g = ma.compute_guidance(m, fmp_snap=None)
        assert "rsi_overbought" in g.caution_flags

    def test_rsi_extreme_overbought_flag(self):
        m = _base_market()
        m["rsi"] = 90
        g = ma.compute_guidance(m, fmp_snap=None)
        assert "rsi_extreme_ob" in g.caution_flags
        assert g.market_regime == "OVEREXTENDED"

    def test_vix_elevated_flag(self):
        m = _base_market()
        m["intel"]["vix"] = 32
        g = ma.compute_guidance(m, fmp_snap=None)
        assert "vix_elevated" in g.caution_flags

    def test_gamma_regime_unknown_flag(self):
        m = _base_market()
        m["menthorq"]["gex_regime"] = "UNKNOWN"
        g = ma.compute_guidance(m, fmp_snap=None)
        assert "gamma_regime_unknown" in g.caution_flags

    def test_fmp_hard_disagreement_flag(self):
        m = _base_market()
        fmp_snap = {"local": 27400.0, "reference": 26900.0, "source": "QQQ", "deviation_pct": 0.0186}
        g = ma.compute_guidance(m, fmp_snap=fmp_snap)
        assert any(f.startswith("fmp_disagrees_") for f in g.caution_flags)
        # Jennifer's policy: FMP disagreement forces RR <= 2.0
        assert g.suggested_rr_tier <= 2.0


class TestEnrichment:
    def test_enrich_adds_guidance_to_market_dict(self):
        m = _base_market()
        out = ma.enrich_market_snapshot(m, fmp_snap=None)
        assert "advisor_guidance" in out
        assert "suggested_rr_tier" in out["advisor_guidance"]
        # Original dict not mutated
        assert "advisor_guidance" not in m

    def test_enrich_never_crashes_on_bad_data(self):
        m = {"price": "not_a_number"}  # deliberately malformed
        out = ma.enrich_market_snapshot(m, fmp_snap=None)
        # We get back the original dict or an enriched one; either way no throw.
        assert isinstance(out, dict)
