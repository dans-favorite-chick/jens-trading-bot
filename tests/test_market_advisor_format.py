"""Regression tests for FINDING-2026-06-04-MA-FMT.

The pre-fix `compute_guidance()` formatted `vol_ctx.get('ratio', '?')`
with `:.2f`. When `_classify_volatility` returned the insufficient-data
branch (vol_ctx = {"atr_5m": ..., "atr_15m": ..., "reason":
"insufficient_data"}), the '?' default was passed to `:.2f` and raised
`ValueError: Unknown format code 'f' for object of type 'str'`.

The exception was swallowed by `enrich_market_snapshot` as a non-blocking
WARNING and the bot proceeded without advisor enrichment — but every
eval logged a stderr warning.

These tests assert the fix: the reasoning string degrades to a '?' atr
ratio without raising.
"""
from __future__ import annotations

import pytest

from agents import market_advisor as ma


@pytest.fixture(autouse=True)
def _mock_fmp_sanity(monkeypatch):
    """Mirror the autouse fixture in test_market_advisor.py — prevent
    live FMP fetch (deterministic + offline)."""
    from core import fmp_sanity
    monkeypatch.setattr(fmp_sanity, "check_mnq_vs_fmp", lambda *a, **kw: None)


def _insufficient_atr_market() -> dict:
    """Drive _classify_volatility into the insufficient_data branch by
    leaving atr_5m / atr_15m at 0. vol_ctx will lack the 'ratio' key.
    """
    return {
        "atr_5m": 0.0,
        "atr_15m": 0.0,
        "vwap": 30000.0,
        "ema9": 30000.0,
        "ema21": 30000.0,
        "vix": 18.5,
        "regime": "OVERNIGHT_RANGE",
        "bar_delta": 0.0,
        "cvd": 0.0,
    }


def test_compute_guidance_does_not_raise_when_vol_ctx_lacks_ratio():
    """The bug path: _classify_volatility returns NORMAL with vol_ctx
    that has no 'ratio' key. Pre-fix, the reasoning f-string blew up.
    """
    g = ma.compute_guidance(_insufficient_atr_market(), fmp_snap=None)
    # Reasoning must be a string and must NOT have raised.
    assert isinstance(g.reasoning, str)
    assert "atr_ratio~?" in g.reasoning


def test_enrich_market_snapshot_returns_enriched_dict_in_insufficient_atr_case():
    """Belt-and-suspenders: enrich_market_snapshot catches exceptions
    and falls back to returning the original market dict. Pre-fix, the
    fallback path fired here. Post-fix, enrichment SUCCEEDS and the
    `advisor_guidance` key is present.
    """
    market = _insufficient_atr_market()
    out = ma.enrich_market_snapshot(market, fmp_snap=None)
    assert "advisor_guidance" in out, (
        "Post-fix, enrichment must succeed (not silently fall back) when "
        "vol_ctx lacks 'ratio'."
    )
    # The original market dict must remain untouched (non-destructive).
    assert "advisor_guidance" not in market


def test_compute_guidance_still_formats_numeric_ratio_to_two_decimals():
    """Sanity: when vol_ctx HAS a numeric 'ratio', the format spec still
    produces a 2-decimal float-formatted value (no regression on the
    happy path).
    """
    # Drive _classify_volatility past insufficient_data: positive ATRs
    # in any ratio gets a 'ratio' in vol_ctx.
    market = {
        "atr_5m": 30.0,
        "atr_15m": 90.0,  # ratio = (30*3)/90 = 1.0
        "vix": 15.0,
        "regime": "MIDDAY",
        "bar_delta": 0.0,
        "cvd": 0.0,
    }
    g = ma.compute_guidance(market, fmp_snap=None)
    assert "atr_ratio~1.00" in g.reasoning


def test_reasoning_is_log_safe_under_random_missing_fields():
    """If the future drops more vol_ctx fields, reasoning must still
    be a logged string, not an exception. Loose smoke check."""
    market = {"vwap": 30000.0}  # almost no fields
    g = ma.compute_guidance(market, fmp_snap=None)
    assert isinstance(g.reasoning, str)
    assert len(g.reasoning) > 0
