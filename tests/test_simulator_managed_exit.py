"""Phase C3 (Fix B, 2026-06-02 overnight): managed_exit wiring tests.

The simulator must call strategy.check_exit() on each bar AFTER the
stop/target check. Precedence:
    stop_loss > target > managed_exit > time_exit
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import simulate_trade  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────

def _bars(rows: list[tuple]) -> pd.DataFrame:
    """rows: list of (ts_offset_min, open, high, low, close).
    Returns a DataFrame with .ts column as pd.Timestamp."""
    base = pd.Timestamp("2024-01-02 09:30:00", tz="UTC")
    out = []
    for off, o, h, lo, c in rows:
        out.append({
            "ts": base + pd.Timedelta(minutes=off),
            "open": o, "high": h, "low": lo, "close": c, "vwap": 0.0,
        })
    return pd.DataFrame(out)


# ── (a) check_exit fires before stop hits → managed_exit ───────────

def test_check_exit_fires_before_stop():
    df = _bars([
        (1, 100.0, 101.0, 99.5, 100.5),   # within stop band; managed_exit fires here
        (2, 100.5, 102.0,  95.0,  96.0),  # would have hit stop=95 -- shouldn't reach
    ])
    entry_ts = df.ts.iloc[0] - pd.Timedelta(seconds=1)
    fired = {"called": 0}

    def fake_check_exit(position, market, bars_1m, session_info):
        fired["called"] += 1
        return (True, "test_signal_flip")

    res = simulate_trade(
        signal_strategy="x",
        signal_direction="LONG",
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        mnq_1m_df=df,
        check_exit_fn=fake_check_exit,
    )
    assert res.exit_reason == "managed_exit:test_signal_flip", res.exit_reason
    assert res.exit_price == 100.5, res.exit_price  # close of bar 1
    assert fired["called"] >= 1


# ── (b) no check_exit method → behavior unchanged ──────────────────

def test_no_check_exit_behaves_like_legacy():
    df = _bars([
        (1, 100.0, 101.0,  99.5, 100.5),  # nothing hits
        (2, 100.5, 102.0,  94.0,  96.0),  # stop=95.0 hits on low 94.0
    ])
    entry_ts = df.ts.iloc[0] - pd.Timedelta(seconds=1)
    res = simulate_trade(
        signal_strategy="x",
        signal_direction="LONG",
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        mnq_1m_df=df,
        check_exit_fn=None,   # no hook -- legacy path
    )
    assert res.exit_reason == "stop"
    assert res.exit_price == 95.0


# ── (c) stop and managed_exit fire on same bar → stop wins ─────────

def test_stop_precedence_over_managed_exit_same_bar():
    """A bar whose low touches the stop AND whose close would have
    triggered managed_exit must exit on stop, not managed_exit."""
    df = _bars([
        (1, 100.0, 100.5,  94.0,  96.0),  # low=94 < stop=95 -> stop fires
    ])
    entry_ts = df.ts.iloc[0] - pd.Timedelta(seconds=1)
    calls = {"n": 0}

    def always_exit(position, market, bars_1m, session_info):
        calls["n"] += 1
        return (True, "managed_should_lose")

    res = simulate_trade(
        signal_strategy="x",
        signal_direction="LONG",
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        mnq_1m_df=df,
        check_exit_fn=always_exit,
    )
    assert res.exit_reason == "stop", res.exit_reason
    # check_exit must NOT have been invoked on that bar (stop short-circuits)
    assert calls["n"] == 0


# ── (d) target precedence over managed_exit on same bar ────────────

def test_target_precedence_over_managed_exit_same_bar():
    df = _bars([
        (1, 100.0, 111.0,  99.5,  108.0),  # high=111 >= target=110 -> target fires
    ])
    entry_ts = df.ts.iloc[0] - pd.Timedelta(seconds=1)
    calls = {"n": 0}

    def always_exit(position, market, bars_1m, session_info):
        calls["n"] += 1
        return (True, "managed_should_lose")

    res = simulate_trade(
        signal_strategy="x",
        signal_direction="LONG",
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        mnq_1m_df=df,
        check_exit_fn=always_exit,
    )
    assert res.exit_reason == "target"
    assert calls["n"] == 0


# ── (e) check_exit raising an exception is treated as no-op ────────

def test_check_exit_exception_swallowed():
    df = _bars([
        (1, 100.0, 101.0,  99.5, 100.5),
        (2, 100.5, 102.0,  94.0,  96.0),   # stop=95 fires
    ])
    entry_ts = df.ts.iloc[0] - pd.Timedelta(seconds=1)

    def explode(*a, **k):
        raise RuntimeError("simulated missing state")

    res = simulate_trade(
        signal_strategy="x",
        signal_direction="LONG",
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        mnq_1m_df=df,
        check_exit_fn=explode,
    )
    # Falls through to stop hit
    assert res.exit_reason == "stop"


# ── (f) signal_metadata passes through to position.metadata ────────

def test_signal_metadata_flows_into_position():
    df = _bars([(1, 100.0, 101.0, 99.5, 100.5)])
    entry_ts = df.ts.iloc[0] - pd.Timedelta(seconds=1)
    seen: dict = {}

    def check(position, market, bars_1m, session_info):
        seen["meta"] = dict(position.metadata)
        seen["dir"] = position.direction
        return (False, "")

    res = simulate_trade(
        signal_strategy="x",
        signal_direction="SHORT",
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=110.0,
        target_price=90.0,
        mnq_1m_df=df,
        check_exit_fn=check,
        signal_metadata={"UB": 102.0, "LB": 98.0},
    )
    assert seen.get("meta") == {"UB": 102.0, "LB": 98.0}
    assert seen.get("dir") == "SHORT"


# ── (g) SHORT direction managed exit ───────────────────────────────

def test_short_managed_exit():
    df = _bars([(1, 100.0, 100.5, 99.5, 99.8)])
    entry_ts = df.ts.iloc[0] - pd.Timedelta(seconds=1)

    def exit_short(position, market, bars_1m, session_info):
        if position.direction == "SHORT":
            return (True, "short_signal_flip")
        return (False, "")

    res = simulate_trade(
        signal_strategy="x",
        signal_direction="SHORT",
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=105.0,
        target_price=90.0,
        mnq_1m_df=df,
        check_exit_fn=exit_short,
    )
    assert res.exit_reason == "managed_exit:short_signal_flip"
    assert res.exit_price == 99.8
