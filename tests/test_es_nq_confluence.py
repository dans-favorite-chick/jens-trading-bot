"""Phase 12C — ES/NQ Confluence LONG strategy tests (2026-05-18).

Selected from 5-year Databento backtest: 131 trades, 50.4% WR, $1,548
total, PF 2.63, max DD $72, 6/6 years positive incl. 2022 bear.

These tests cover the strategy's logic in isolation. The strategy
requires MES data in market["mes_bars_5m"] which Phoenix does NOT have
live as of ship date — tests inject synthetic MES bars to exercise the
firing paths.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.es_nq_confluence import ESNQConfluence


# ── Test fixtures ─────────────────────────────────────────────────────

def _make_strategy(boost_thr: float = 25.0, corr_thr: float = 0.85,
                    lookback: int = 50, stop_t: int = 24, target_t: int = 96):
    return ESNQConfluence({
        "enabled": True,
        "validated": False,
        "boost_threshold": boost_thr,
        "corr_threshold": corr_thr,
        "corr_lookback": lookback,
        "stop_ticks": stop_t,
        "target_ticks": target_t,
        "is_prod_bot": False,
    })


def _bar(end_time: float, close: float):
    return SimpleNamespace(end_time=end_time, close=close,
                            open=close, high=close, low=close, volume=100)


def _flat_mnq_bars(n: int, start_close: float = 29000.0):
    """Generate N bars at constant price (zero returns)."""
    return [_bar(1_700_000_000 + i * 300, start_close) for i in range(n)]


def _flat_mes_bars(n: int, start_close: float = 5800.0):
    return [_bar(1_700_000_000 + i * 300, start_close) for i in range(n)]


def _correlated_walk_mnq_mes(n: int, mnq_start: float = 29000.0,
                              mes_start: float = 5800.0,
                              boost_bp: float = 0.0):
    """Generate N pairs where MNQ and MES move together (high correlation
    of their returns), with the LAST bar having NQ outperforming ES by
    `boost_bp` basis-points × 100 (matches the strategy's boost formula
    domain: boost = (mnq_ret - mes_ret) × 10000).

    To preserve correlation, the last bar moves BOTH series proportionally
    in the same direction; the boost is the *delta* between their return
    magnitudes on that last bar. Example: boost_bp=80 produces a last
    MNQ return that exceeds the last MES return by 80bp×100.
    """
    mnq_bars, mes_bars = [], []
    mnq_price, mes_price = mnq_start, mes_start
    for i in range(n - 1):  # leave the last bar to construct deliberately
        step = ((i * 7) % 11 - 5) * 0.20
        mnq_price += step
        mes_price += step * (mes_start / mnq_start)
        mnq_bars.append(_bar(1_700_000_000 + i * 300, mnq_price))
        mes_bars.append(_bar(1_700_000_000 + i * 300, mes_price))
    # Construct the LAST bar with a controlled boost: both move up,
    # MNQ a bit more. Keeps correlation high (both bars move same sign)
    # while creating the desired return delta on the last bar.
    if boost_bp != 0:
        mes_move_pct = 0.0010  # +0.10% baseline move for both
        mnq_move_pct = mes_move_pct + (boost_bp / 10_000.0)
    else:
        mes_move_pct = 0.0
        mnq_move_pct = 0.0
    last_mnq = mnq_price * (1.0 + mnq_move_pct)
    last_mes = mes_price * (1.0 + mes_move_pct)
    mnq_bars.append(_bar(1_700_000_000 + (n - 1) * 300, last_mnq))
    mes_bars.append(_bar(1_700_000_000 + (n - 1) * 300, last_mes))
    return mnq_bars, mes_bars


def _market(price: float = 29150.0):
    return {"price": price, "regime": "BALANCED"}


# ── Warmup gates ──────────────────────────────────────────────────────

def test_skip_when_mnq_bars_below_warmup():
    s = _make_strategy(lookback=50)
    bars_5m = _flat_mnq_bars(40)  # 40 < 51 required
    sig = s.evaluate(_market(), bars_5m, [], {})
    assert sig is None


def test_skip_when_mes_bars_missing():
    """The MES feed-not-wired case: market dict has no mes_bars_5m."""
    s = _make_strategy(lookback=50)
    bars_5m = _flat_mnq_bars(60)
    sig = s.evaluate(_market(), bars_5m, [], {})
    assert sig is None


def test_skip_when_mes_bars_below_warmup():
    s = _make_strategy(lookback=50)
    bars_5m = _flat_mnq_bars(60)
    mkt = _market()
    mkt["mes_bars_5m"] = _flat_mes_bars(30)  # too few
    sig = s.evaluate(mkt, bars_5m, [], {})
    assert sig is None


# ── Boost / corr gates ────────────────────────────────────────────────

def test_no_signal_when_boost_below_threshold():
    """Flat MNQ + flat MES → boost = 0 — well below 25 threshold."""
    s = _make_strategy(lookback=50)
    bars_5m = _flat_mnq_bars(60)
    mkt = _market()
    mkt["mes_bars_5m"] = _flat_mes_bars(60)
    sig = s.evaluate(mkt, bars_5m, [], {})
    assert sig is None


def test_no_signal_when_corr_below_threshold():
    """Strong boost but uncorrelated series should NOT fire."""
    s = _make_strategy(lookback=50, corr_thr=0.85)
    # Build MNQ that moves randomly vs MES that's flat — zero correlation.
    mnq_bars = [_bar(1_700_000_000 + i * 300, 29000 + (i % 7 - 3) * 2.0)
                 for i in range(60)]
    mes_bars = _flat_mes_bars(60)
    mkt = _market(price=29150.0)
    mkt["mes_bars_5m"] = mes_bars
    sig = s.evaluate(mkt, mnq_bars, [], {})
    # mes_bars are FLAT → mes returns all zero → corr is undefined (zero
    # variance in MES). Strategy should SKIP corr_undefined and return None.
    assert sig is None


def test_signal_fires_with_strong_pos_boost_and_high_corr():
    """High boost + high corr + LONG-bias → SIGNAL LONG with correct fields."""
    s = _make_strategy(lookback=50, boost_thr=25.0, corr_thr=0.85,
                       stop_t=24, target_t=96)
    # NQ leads ES by 80bp×100 on the LAST bar — well above 25 threshold.
    mnq_bars, mes_bars = _correlated_walk_mnq_mes(60, boost_bp=80.0)
    mkt = _market(price=29150.0)
    mkt["mes_bars_5m"] = mes_bars
    sig = s.evaluate(mkt, mnq_bars, [], {})
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.strategy == "es_nq_confluence"
    assert sig.stop_ticks == 24
    assert sig.target_rr == pytest.approx(4.0)
    # Stop = entry - 24*0.25 = entry - 6.00
    assert sig.stop_price == pytest.approx(29150.0 - 6.00)
    # Target = entry + 96*0.25 = entry + 24.00
    assert sig.target_price == pytest.approx(29150.0 + 24.00)
    # atr_stop_override=True (strategy computes its own stop)
    assert sig.atr_stop_override is True
    # Metadata carries boost + corr for downstream analysis
    assert "boost" in sig.metadata
    assert "corr" in sig.metadata
    # Boost should be near 80 (we constructed it that way)
    assert sig.metadata["boost"] == pytest.approx(80.0, abs=1.0)


def test_signal_skipped_on_negative_boost():
    """NEG boost (NQ lagging ES) should NOT fire LONG."""
    s = _make_strategy(lookback=50)
    mnq_bars, mes_bars = _correlated_walk_mnq_mes(60, boost_bp=-80.0)
    mkt = _market(price=29150.0)
    mkt["mes_bars_5m"] = mes_bars
    sig = s.evaluate(mkt, mnq_bars, [], {})
    assert sig is None


# ── Per-bar dedup ─────────────────────────────────────────────────────

def test_no_duplicate_signal_on_same_bar():
    s = _make_strategy(lookback=50)
    mnq_bars, mes_bars = _correlated_walk_mnq_mes(60, boost_bp=80.0)
    mkt = _market(price=29150.0)
    mkt["mes_bars_5m"] = mes_bars
    sig1 = s.evaluate(mkt, mnq_bars, [], {})
    assert sig1 is not None
    sig2 = s.evaluate(mkt, mnq_bars, [], {})
    assert sig2 is None  # same bar — must not fire again


def test_fires_again_on_next_bar():
    s = _make_strategy(lookback=50)
    mnq_bars, mes_bars = _correlated_walk_mnq_mes(60, boost_bp=80.0)
    mkt = _market(price=29150.0)
    mkt["mes_bars_5m"] = mes_bars
    sig1 = s.evaluate(mkt, mnq_bars, [], {})
    assert sig1 is not None
    # Add a new (correlated) bar so the dedup ts changes
    mnq_bars2, mes_bars2 = _correlated_walk_mnq_mes(61, boost_bp=80.0)
    mkt["mes_bars_5m"] = mes_bars2
    sig2 = s.evaluate(mkt, mnq_bars2, [], {})
    assert sig2 is not None


# ── Config + wiring pins ──────────────────────────────────────────────

def test_config_has_es_nq_confluence_block():
    from config.strategies import STRATEGIES
    assert "es_nq_confluence" in STRATEGIES
    cfg = STRATEGIES["es_nq_confluence"]
    assert cfg["enabled"] is True
    assert cfg["validated"] is False, (
        "validated=False intentional: strategy is dormant until MES feed "
        "lands. Promote only after 30+ live trades + Wilson-CI clearance."
    )
    assert cfg["boost_threshold"] == 25.0
    assert cfg["corr_threshold"] == 0.85
    assert cfg["stop_ticks"] == 24    # = $12 risk on 1 MNQ
    assert cfg["target_ticks"] == 96  # = $48 reward (RR 4:1)


def test_base_bot_imports_es_nq_confluence():
    src = (ROOT / "bots" / "base_bot.py").read_text(encoding="utf-8")
    assert "from strategies.es_nq_confluence import ESNQConfluence" in src
    assert '"es_nq_confluence": ESNQConfluence' in src


def test_strategy_keys_includes_es_nq_confluence():
    from core.strategy_risk_registry import STRATEGY_KEYS
    assert "es_nq_confluence" in STRATEGY_KEYS


def test_known_strategies_includes_es_nq_confluence():
    from tools.strategy_change_log import _KNOWN_STRATEGIES
    assert "es_nq_confluence" in _KNOWN_STRATEGIES


def test_account_routing_includes_es_nq_confluence():
    from config.account_routing import STRATEGY_ACCOUNT_MAP, get_account_for_signal
    assert STRATEGY_ACCOUNT_MAP.get("es_nq_confluence") == "Sim101"
    assert get_account_for_signal("es_nq_confluence") == "Sim101"
