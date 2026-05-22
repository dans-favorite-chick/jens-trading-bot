"""
tests/test_confluence_gates.py — pin the shared confluence-voter gate behavior

Created 2026-05-22 (ship pt6). These helpers are now used by 8+ strategies
(bias_momentum, spring_setup, vwap_pullback_v2, vwap_band_pullback,
raschke_baseline, e_multi_day_breakout, opening_session.orb,
opening_session.open_drive). Any behavioral change must be deliberate
and documented.

Per a16cf0ef per-strategy research: the canonical gate pattern is

    passed, reason = tf60m_es_gate(market, direction, ...)
    if not passed:
        return None  # rejection already logged

These tests pin:
1. AGREE → pass
2. DISAGREE on tf_60m → reject with reason
3. DISAGREE on ES → reject with reason
4. Missing voter data → graceful degrade (pass)
5. config flag off → bypass
6. Both formats of tf_bias (dict or flat string)
7. Both formats of es_correlation (es_nq_rs or intermarket dict)
"""
from __future__ import annotations

import logging

from core.confluence_gates import regime_veto, tf5m_es_gate, tf60m_es_gate


def _logger():
    log = logging.getLogger("test_confluence_gates")
    log.setLevel(logging.DEBUG)
    return log


# ─── tf60m_es_gate ─────────────────────────────────────────────────


def test_tf60m_es_gate_both_agree_passes():
    market = {"tf_bias": {"60m": "BULL"}, "es_nq_rs": 0.5}
    passed, reason = tf60m_es_gate(market, "LONG", logger=_logger())
    assert passed is True
    assert reason is None


def test_tf60m_es_gate_tf60m_disagree_rejects():
    market = {"tf_bias": {"60m": "BEAR"}, "es_nq_rs": 0.5}
    passed, reason = tf60m_es_gate(market, "LONG", logger=_logger())
    assert passed is False
    assert "TF60M_GATE" in reason


def test_tf60m_es_gate_es_disagree_rejects():
    market = {"tf_bias": {"60m": "BULL"}, "es_nq_rs": -0.3}
    passed, reason = tf60m_es_gate(market, "LONG", logger=_logger())
    assert passed is False
    assert "ES_GATE" in reason


def test_tf60m_es_gate_missing_data_passes_graceful():
    """When both voter feeds are unavailable, graceful-degrade through."""
    market = {}
    passed, reason = tf60m_es_gate(market, "LONG", logger=_logger())
    assert passed is True
    assert reason is None


def test_tf60m_es_gate_neutral_bias_passes():
    """NEUTRAL bias is treated as 'no opinion' — not a disagreement."""
    market = {"tf_bias": {"60m": "NEUTRAL"}, "es_nq_rs": 0.0}
    passed, _ = tf60m_es_gate(market, "LONG", logger=_logger())
    assert passed is True


def test_tf60m_es_gate_config_disabled_passes():
    market = {"tf_bias": {"60m": "BEAR"}, "es_nq_rs": -0.5}  # would normally fail
    cfg = {"require_tf60m_es_gate": False}
    passed, _ = tf60m_es_gate(market, "LONG", config=cfg, logger=_logger())
    assert passed is True


def test_tf60m_es_gate_flat_string_tf_format_works():
    """tf_bias_60m flat-string format is the legacy path; must still work."""
    market = {"tf_bias_60m": "BEAR", "es_nq_rs": 0.5}
    passed, reason = tf60m_es_gate(market, "LONG", logger=_logger())
    assert passed is False
    assert "TF60M_GATE" in reason


def test_tf60m_es_gate_intermarket_dict_es_format_works():
    """es_correlation in the intermarket dict (alt path) must work."""
    market = {
        "tf_bias": {"60m": "BULL"},
        "intermarket": {"nq_es_relative_strength": -0.4},
    }
    passed, reason = tf60m_es_gate(market, "LONG", logger=_logger())
    assert passed is False
    assert "ES_GATE" in reason


# ─── tf5m_es_gate ──────────────────────────────────────────────────


def test_tf5m_es_gate_both_agree_passes():
    market = {"tf_bias": {"5m": "BULL"}, "es_nq_rs": 0.5}
    passed, _ = tf5m_es_gate(market, "LONG", logger=_logger())
    assert passed is True


def test_tf5m_es_gate_tf5m_disagree_rejects():
    market = {"tf_bias": {"5m": "BEAR"}, "es_nq_rs": 0.5}
    passed, reason = tf5m_es_gate(market, "LONG", logger=_logger())
    assert passed is False
    assert "TF5M_GATE" in reason


def test_tf5m_es_gate_config_disabled_passes():
    market = {"tf_bias": {"5m": "BEAR"}, "es_nq_rs": -0.5}
    cfg = {"require_tf5m_es_gate": False}
    passed, _ = tf5m_es_gate(market, "LONG", config=cfg, logger=_logger())
    assert passed is True


# ─── regime_veto ───────────────────────────────────────────────────


def test_regime_veto_in_list_rejects():
    market = {"regime": "OPEN_MOMENTUM"}
    passed, reason = regime_veto(market, ("OPEN_MOMENTUM",), logger=_logger())
    assert passed is False
    assert "REGIME_VETO" in reason
    assert "OPEN_MOMENTUM" in reason


def test_regime_veto_not_in_list_passes():
    market = {"regime": "MID_MORNING"}
    passed, _ = regime_veto(market, ("OPEN_MOMENTUM",), logger=_logger())
    assert passed is True


def test_regime_veto_missing_regime_passes_graceful():
    market = {}
    passed, _ = regime_veto(market, ("OPEN_MOMENTUM",), logger=_logger())
    assert passed is True


def test_regime_veto_config_disabled_passes():
    market = {"regime": "OPEN_MOMENTUM"}
    cfg = {"veto_regimes_enabled": False}
    passed, _ = regime_veto(
        market, ("OPEN_MOMENTUM",), config=cfg, logger=_logger()
    )
    assert passed is True


def test_regime_veto_custom_config_key():
    """Strategies can use custom config keys for back-out per-strategy."""
    market = {"regime": "OPEN_MOMENTUM"}
    cfg = {"orb_regime_veto_enabled": False}
    passed, _ = regime_veto(
        market, ("OPEN_MOMENTUM",),
        config=cfg, logger=_logger(),
        config_key="orb_regime_veto_enabled",
    )
    assert passed is True


def test_regime_veto_multiple_regimes():
    """ORB uses (AFTERNOON_CHOP, LATE_AFTERNOON) — multi-regime case."""
    market_a = {"regime": "AFTERNOON_CHOP"}
    market_b = {"regime": "LATE_AFTERNOON"}
    market_c = {"regime": "MID_MORNING"}
    veto_list = ("AFTERNOON_CHOP", "LATE_AFTERNOON")
    assert regime_veto(market_a, veto_list, logger=_logger())[0] is False
    assert regime_veto(market_b, veto_list, logger=_logger())[0] is False
    assert regime_veto(market_c, veto_list, logger=_logger())[0] is True


# ─── SHORT direction symmetry ──────────────────────────────────────


def test_tf60m_es_gate_short_agreement():
    market = {"tf_bias": {"60m": "BEAR"}, "es_nq_rs": -0.5}
    passed, _ = tf60m_es_gate(market, "SHORT", logger=_logger())
    assert passed is True


def test_tf60m_es_gate_short_es_disagree():
    market = {"tf_bias": {"60m": "BEAR"}, "es_nq_rs": 0.5}  # positive RS = bullish
    passed, reason = tf60m_es_gate(market, "SHORT", logger=_logger())
    assert passed is False
    assert "ES_GATE" in reason
