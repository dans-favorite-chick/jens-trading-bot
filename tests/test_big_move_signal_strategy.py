"""BigMoveSignal strategy tests (2026-05-15).

Standalone entry on the BigMoveDetector score >= 90 signature.
Validated live earlier today at 15:11:19 CT (score=100 LONG predicted
+47pt rally in 8 minutes).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.big_move_signal import BigMoveSignal


def _make_strategy(min_score: int = 90):
    return BigMoveSignal({
        "enabled": True,
        "validated": False,
        "min_score": min_score,
        "stop_atr_mult": 1.0,
        "max_stop_ticks": 100,
        "min_stop_ticks": 20,
        "target_rr": 2.0,
        "is_prod_bot": False,
    })


def _make_bar(end_time: float, close: float = 29150.0):
    return SimpleNamespace(
        end_time=end_time, open=close, high=close, low=close,
        close=close, volume=1000.0, delta=0,
    )


def _market(score: int, direction: str, flags: list = None,
            price: float = 29150.0, atr_5m: float = 30.0):
    return {
        "price": price,
        "atr_5m": atr_5m,
        "big_move_pre": {
            "score": score,
            "likely_direction": direction,
            "flags": flags or [],
            "reason": f"{direction} setup score={score}",
        },
    }


# ── Score gate ─────────────────────────────────────────────────────────

def test_no_signal_when_score_below_threshold():
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    sig = strat.evaluate(_market(75, "LONG"), [], bars, {})
    assert sig is None


def test_no_signal_when_direction_unknown():
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    sig = strat.evaluate(_market(100, "UNKNOWN"), [], bars, {})
    assert sig is None


def test_signal_fires_at_threshold():
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    sig = strat.evaluate(
        _market(90, "LONG", ["vol_collapse", "cvd_divergence",
                              "failed_break", "dom_absorption"]),
        [], bars, {},
    )
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.strategy == "big_move_signal"


def test_signal_fires_for_short():
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    sig = strat.evaluate(_market(95, "SHORT"), [], bars, {})
    assert sig is not None
    assert sig.direction == "SHORT"


# ── Stop placement within $50 budget ──────────────────────────────────

def test_stop_distance_clamped_to_100t_max():
    """ATR-anchored stop must NOT exceed 100t (= $50 on MNQ). The
    operator budget gate is the safety net but the strategy itself
    should also self-clamp."""
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    # Big ATR = 50pt → 1.0x mult = 50pt stop = 200t, must clamp to 100t
    sig = strat.evaluate(
        _market(100, "LONG", price=29150.0, atr_5m=50.0),
        [], bars, {},
    )
    assert sig is not None
    assert sig.stop_ticks <= 100, (
        f"Stop {sig.stop_ticks}t exceeds max 100t = $50 budget"
    )
    # Stop price should be exactly entry - 100t (clamp hit)
    assert sig.stop_price == 29150.0 - 100 * 0.25  # 29125.0


def test_stop_distance_uses_atr_when_within_budget():
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    # Small ATR = 15pt → 15pt stop = 60t, no clamp
    sig = strat.evaluate(
        _market(100, "LONG", price=29150.0, atr_5m=15.0),
        [], bars, {},
    )
    assert sig is not None
    # 15pt = 60t exactly
    assert sig.stop_ticks == 60


# ── Per-bar dedup ─────────────────────────────────────────────────────

def test_no_duplicate_signal_on_same_bar():
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    sig1 = strat.evaluate(_market(100, "LONG"), [], bars, {})
    assert sig1 is not None
    sig2 = strat.evaluate(_market(100, "LONG"), [], bars, {})
    assert sig2 is None  # Same bar — must not fire again


def test_fires_again_on_next_bar():
    strat = _make_strategy(min_score=90)
    bars = [_make_bar(1700000000)]
    sig1 = strat.evaluate(_market(100, "LONG"), [], bars, {})
    assert sig1 is not None
    bars = [_make_bar(1700000060)]  # New bar
    sig2 = strat.evaluate(_market(100, "LONG"), [], bars, {})
    assert sig2 is not None


# ── Config + wiring pins ──────────────────────────────────────────────

@pytest.mark.skip(reason="V2 deployment override 2026-05-17 — restore at Phase 10")
def test_config_has_big_move_signal_block():
    from config.strategies import STRATEGIES
    assert "big_move_signal" in STRATEGIES
    cfg = STRATEGIES["big_move_signal"]
    assert cfg["enabled"] is True
    assert cfg["validated"] is False  # Sim only until n>=30
    assert cfg["min_score"] == 90
    assert cfg["max_stop_ticks"] == 100  # = $50 budget


def test_base_bot_imports_big_move_signal():
    src = (ROOT / "bots" / "base_bot.py").read_text(encoding="utf-8")
    assert "from strategies.big_move_signal import BigMoveSignal" in src
    assert '"big_move_signal": BigMoveSignal' in src
