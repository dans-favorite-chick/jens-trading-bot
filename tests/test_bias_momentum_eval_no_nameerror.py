"""
Smoke test: BiasMomentumFollow.evaluate() must not raise NameError.

The strategy crashed on every call due to a missing `votes` variable.
This test guards against regression — evaluate() returning None is fine.
"""
import types
import pytest
from strategies.bias_momentum import BiasMomentumFollow


def _make_bar(close=19000.0, open_=18990.0, high=19010.0, low=18980.0, volume=500):
    bar = types.SimpleNamespace()
    bar.close = close
    bar.open = open_
    bar.high = high
    bar.low = low
    bar.volume = volume
    return bar


def _minimal_snapshot():
    return {
        "close": 19000.0,
        "vwap": 18990.0,
        "ema9": 18995.0,
        "ema21": 18985.0,
        "ema9_15m": 18993.0,
        "ema21_15m": 18983.0,
        "atr_1m": 4.0,
        "atr_5m": 8.0,
        "cvd": 500_000,
        "bar_delta": 120,
        "tf_bias": {"1m": "BULLISH", "5m": "BULLISH", "60m": "BULLISH"},
        "tf_votes_bullish": 3,
        "tf_votes_bearish": 1,
        "day_type": "RANGE",
        "mq_direction_bias": "NEUTRAL",
        "cr_verdict": "UNKNOWN",
        "avg_vol_5m": 400.0,
        "vol_climax_ratio": 1.1,
        "vsa_signal_5m": "NEUTRAL",
        "delta_history_5m": [],
        "high_history_5m": [],
        "low_history_5m": [],
        "macd_histogram": 0.05,
        "macd_histogram_prev": 0.04,
        "macd_warm": True,
        "dom_imbalance": 0.55,
        "dom_signal": {},
        "vwap_std": 5.0,
        "vwap_upper1": 18995.0,
        "vwap_upper2": 19000.0,
        "vwap_lower1": 18985.0,
        "vwap_lower2": 18980.0,
        "avwap_pd_close": 18970.0,
        "mq_nearest_resistance": 0.0,
        "mq_nearest_support": 0.0,
        "mq_hvl": 0.0,
    }


def _minimal_session():
    return {"regime": "MID_MORNING"}


def test_evaluate_no_nameerror():
    """evaluate() must not raise NameError regardless of whether it returns a signal."""
    strategy = BiasMomentumFollow(config={})
    snapshot = _minimal_snapshot()
    bars_5m = [_make_bar() for _ in range(5)]
    bars_1m = [_make_bar() for _ in range(10)]
    session = _minimal_session()

    try:
        result = strategy.evaluate(snapshot, bars_5m, bars_1m, session)
        # None is fine — no signal is a valid outcome
    except NameError as e:
        pytest.fail(f"evaluate() raised NameError: {e}")
    except Exception:
        # Any other exception (KeyError, AttributeError, etc.) is NOT a NameError regression
        pass
