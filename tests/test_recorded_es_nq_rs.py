"""Tests for tools/replay_enrichment/recorded_es_nq_rs.py.

Uses synthetic 5-minute-spaced DataFrames with hand-picked closes so the
relative-strength math is verifiable by hand.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

# Load the module by file path so the test does not depend on whether the
# parent has dropped an __init__.py into tools/replay_enrichment/ yet.
_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "replay_enrichment"
    / "recorded_es_nq_rs.py"
)
_spec = importlib.util.spec_from_file_location("recorded_es_nq_rs", _MODULE_PATH)
recorded_es_nq_rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorded_es_nq_rs)
es_nq_rs_at = recorded_es_nq_rs.es_nq_rs_at


def _make_df(closes, start="2026-05-28T14:00:00Z", tz_naive=False):
    """Build a minimal _load_bars_from_csv-style DataFrame.

    5-minute spacing; `closes` drive open/high/low/close/volume.
    """
    n = len(closes)
    ts = pd.date_range(start=start, periods=n, freq="5min", tz="UTC")
    if tz_naive:
        ts = ts.tz_localize(None)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": [float(c) for c in closes],
            "high": [float(c) for c in closes],
            "low": [float(c) for c in closes],
            "close": [float(c) for c in closes],
            "volume": [100] * n,
        }
    )


def test_hand_computed_relative_strength():
    # 7 bars each. iloc[-1] = now, iloc[-7] = 30m ago (the first bar here).
    # MNQ: 30m ago = 20000, now = 20200  -> +1.0%
    # MES: 30m ago =  5000, now =  5050  -> +1.0%
    # relative_strength = 1.0 - 1.0 = 0.0
    mnq = _make_df([20000, 20050, 20100, 20120, 20150, 20180, 20200])
    mes = _make_df([5000, 5010, 5020, 5030, 5040, 5045, 5050])
    eval_ts = "2026-05-28T14:30:00Z"  # at/after the 7th bar (14:30)
    result = es_nq_rs_at(eval_ts, mnq, mes)
    assert result == pytest.approx(0.0, abs=1e-9)

    # Now make MNQ +2% while MES stays +1% -> RS = +1.0 exactly.
    mnq2 = _make_df([20000, 20050, 20100, 20200, 20300, 20380, 20400])
    res2 = es_nq_rs_at(eval_ts, mnq2, mes)
    assert res2 == pytest.approx(1.0, abs=1e-9)


def test_sign_convention_nq_leading_positive():
    # MNQ rises more (+3%) than MES (+0.5%) -> positive (NQ leading).
    mnq = _make_df([20000, 20100, 20200, 20300, 20400, 20500, 20600])  # +3.0%
    mes = _make_df([5000, 5005, 5010, 5015, 5020, 5022, 5025])         # +0.5%
    result = es_nq_rs_at("2026-05-28T14:30:00Z", mnq, mes)
    assert result is not None
    assert result > 0
    assert result == pytest.approx(2.5, abs=1e-9)


def test_sign_convention_es_leading_negative():
    # MES rises more than MNQ -> negative relative strength.
    mnq = _make_df([20000, 20010, 20020, 20030, 20040, 20045, 20050])  # +0.25%
    mes = _make_df([5000, 5020, 5040, 5060, 5080, 5090, 5100])         # +2.0%
    result = es_nq_rs_at("2026-05-28T14:30:00Z", mnq, mes)
    assert result is not None
    assert result < 0


def test_returns_none_when_fewer_than_7_bars():
    mnq = _make_df([20000, 20050, 20100, 20150, 20200, 20250])  # only 6 bars
    mes = _make_df([5000, 5010, 5020, 5030, 5040, 5050, 5060])  # 7 bars
    assert es_nq_rs_at("2026-05-28T14:30:00Z", mnq, mes) is None
    # Symmetric: too few in the ES df.
    assert es_nq_rs_at("2026-05-28T14:30:00Z", mes, mnq) is None


def test_eval_ts_trims_future_bars():
    # 10 bars; eval at the 7th bar's time should use bars 1..7 only,
    # i.e. now=bar7, 30m_ago=bar1 (same as the hand-computed case).
    mnq = _make_df(
        [20000, 20050, 20100, 20120, 20150, 20180, 20200, 99999, 99999, 99999]
    )
    mes = _make_df(
        [5000, 5010, 5020, 5030, 5040, 5045, 5050, 99999, 99999, 99999]
    )
    # 7th bar is at 14:00 + 6*5min = 14:30.
    result = es_nq_rs_at("2026-05-28T14:30:00Z", mnq, mes)
    assert result == pytest.approx(0.0, abs=1e-9)


def test_unsorted_and_tz_naive_input():
    # Shuffle rows and use tz-naive timestamps; function must sort + localize.
    mnq = _make_df(
        [20000, 20050, 20100, 20120, 20150, 20180, 20200], tz_naive=True
    ).sample(frac=1.0, random_state=1).reset_index(drop=True)
    mes = _make_df(
        [5000, 5010, 5020, 5030, 5040, 5045, 5050], tz_naive=True
    ).sample(frac=1.0, random_state=2).reset_index(drop=True)
    # tz-naive eval_ts too.
    result = es_nq_rs_at("2026-05-28T14:30:00", mnq, mes)
    assert result == pytest.approx(0.0, abs=1e-9)


def test_zero_denominator_returns_none():
    # 30m-ago close is 0 -> denom zero -> None.
    mnq = _make_df([0, 20050, 20100, 20120, 20150, 20180, 20200])
    mes = _make_df([5000, 5010, 5020, 5030, 5040, 5045, 5050])
    assert es_nq_rs_at("2026-05-28T14:30:00Z", mnq, mes) is None
