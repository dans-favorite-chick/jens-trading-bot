"""
tests/agents/test_backtest_engine.py
Unit tests for agents/backtest_engine.py — pure, no DB / live data required.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

# Ensure project root is importable regardless of cwd.
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.backtest_engine import (
    DAILY_LOSS_HALT_USD,
    ENTRY_CONTEXT_KEYS,
    WARMUP_BARS_PER_SESSION,
    _capture_entry_context,
    _entry_session_date,
    _enrich_with_mae_mfe_regime_tod,
    _resolve_lookback,
    _session_date_ct,
)

_CT = ZoneInfo("America/Chicago")
_UTC = ZoneInfo("UTC")

# ---------------------------------------------------------------------------
# Shared mock trade-result dataclass (mimics prb.TradeResult).
# ---------------------------------------------------------------------------

@dataclass
class MockTradeResult:
    strategy: str = "bias_momentum"
    direction: str = "LONG"
    entry_ts: pd.Timestamp = field(default_factory=lambda: pd.Timestamp("2024-08-15 13:30:00+00:00"))
    entry_price: float = 19000.0
    stop_price: float = 18994.0
    target_price: float = 19012.0
    exit_ts: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_dollars: float = 0.0
    pnl_ticks: int = 0
    hold_min: float = 0.0
    entry_context: Optional[dict] = None


# ---------------------------------------------------------------------------
# 1. TestResolveLookback
# ---------------------------------------------------------------------------

class TestResolveLookback:
    """_resolve_lookback returns the correct (start_ts, end_ts) window."""

    @staticmethod
    def _make_df(start: str, end: str) -> pd.DataFrame:
        ts = pd.date_range(start, end, freq="1min", tz="UTC")
        return pd.DataFrame({"ts": ts, "close": 19000.0})

    def test_days_5_returns_correct_window(self):
        df = self._make_df("2024-01-01", "2024-12-31 23:59")
        start_ts, end_ts = _resolve_lookback(5, df)
        assert end_ts == df["ts"].max()
        assert start_ts == end_ts - pd.Timedelta(days=5)

    def test_days_0_start_equals_end(self):
        df = self._make_df("2024-01-01", "2024-12-31 23:59")
        start_ts, end_ts = _resolve_lookback(0, df)
        assert start_ts == end_ts
        assert end_ts == df["ts"].max()


# ---------------------------------------------------------------------------
# 2. TestSessionDateCt
# ---------------------------------------------------------------------------

class TestSessionDateCt:
    """_session_date_ct extracts CT date from market dict."""

    def test_naive_datetime_returns_date(self):
        market = {"now_ct": datetime(2024, 8, 15, 14, 30)}
        result = _session_date_ct(market)
        assert result == date(2024, 8, 15)

    def test_none_now_ct_returns_none(self):
        result = _session_date_ct({"now_ct": None})
        assert result is None

    def test_missing_now_ct_key_returns_none(self):
        result = _session_date_ct({})
        assert result is None


# ---------------------------------------------------------------------------
# 3. TestEntrySessionDate
# ---------------------------------------------------------------------------

class TestEntrySessionDate:
    """_entry_session_date converts entry_ts (UTC) to CT calendar date."""

    def test_afternoon_utc_stays_same_day(self):
        # 13:30 UTC = 08:30 CDT (UTC-5 during DST) → 2024-08-15
        tr = MockTradeResult(entry_ts=pd.Timestamp("2024-08-15 13:30:00+00:00"))
        assert _entry_session_date(tr) == date(2024, 8, 15)

    def test_early_morning_utc_same_calendar_day(self):
        # 05:30 UTC = 00:30 CDT → 2024-08-15 (same calendar day)
        tr = MockTradeResult(entry_ts=pd.Timestamp("2024-08-15 05:30:00+00:00"))
        assert _entry_session_date(tr) == date(2024, 8, 15)

    def test_very_early_utc_crosses_to_previous_day(self):
        # 04:30 UTC = 23:30 CDT on 2024-08-14 → previous calendar day
        tr = MockTradeResult(entry_ts=pd.Timestamp("2024-08-15 04:30:00+00:00"))
        assert _entry_session_date(tr) == date(2024, 8, 14)


# ---------------------------------------------------------------------------
# 4. TestCaptureEntryContext
# ---------------------------------------------------------------------------

class TestCaptureEntryContext:
    """_capture_entry_context snapshots market + signal state."""

    @dataclass
    class _Signal:
        strategy: str = "bias_momentum"
        direction: str = "LONG"
        entry_score: int = 50
        stop_ticks: int = 24
        target_rr: float = 2.5
        confidence: int = 80
        entry_price: Optional[float] = None
        stop_price: Optional[float] = None
        target_price: Optional[float] = None

    def _full_market(self):
        return {
            "atr_5m": 4.2,
            "cvd": 1500,
            "vwap": 19250.0,
            "now_ct": datetime(2024, 8, 15, 9, 0),
        }

    def test_happy_path_all_keys_present(self):
        sig = self._Signal()
        ctx = _capture_entry_context(self._full_market(), sig)
        assert ctx["strategy"] == "bias_momentum"
        assert ctx["direction"] == "LONG"
        assert ctx["atr_5m"] == 4.2
        assert ctx["market_open_minutes"] == 30.0  # 09:00 − 08:30 = 30 min

    def test_missing_market_keys_produce_none(self):
        ctx = _capture_entry_context({}, self._Signal())
        for k in ENTRY_CONTEXT_KEYS:
            assert ctx[k] is None

    def test_none_now_ct_omits_market_open_minutes(self):
        market = {"now_ct": None}
        ctx = _capture_entry_context(market, self._Signal())
        assert "market_open_minutes" not in ctx

    def test_string_numeric_value_coerced_to_float(self):
        market = {"atr_5m": "4.2", "now_ct": None}
        ctx = _capture_entry_context(market, self._Signal())
        assert ctx["atr_5m"] == pytest.approx(4.2)

    def test_bool_true_coerced_to_int_one(self):
        market = {"atr_5m": True, "now_ct": None}
        ctx = _capture_entry_context(market, self._Signal())
        assert ctx["atr_5m"] == 1

    def test_non_numeric_string_becomes_none(self):
        market = {"atr_5m": "trending", "now_ct": None}
        ctx = _capture_entry_context(market, self._Signal())
        assert ctx["atr_5m"] is None


# ---------------------------------------------------------------------------
# 5. TestWarmupAndHaltGates
# ---------------------------------------------------------------------------

def _entry_session_date_from_utc(entry_ts: pd.Timestamp) -> date:
    """Pure re-impl of engine's _entry_session_date for bookkeeping tests."""
    return entry_ts.tz_convert(_CT).date()


def _should_block(session_bar_count: int,
                  session_halted: dict,
                  current_session: date) -> tuple[bool, str]:
    """Replicate the engine's two-gate predicate (warmup + daily halt)."""
    if session_bar_count <= WARMUP_BARS_PER_SESSION:
        return True, "warmup"
    if session_halted.get(current_session, False):
        return True, "halt"
    return False, "ok"


class TestWarmupAndHaltGates:
    """Gate predicates and session-attribution bookkeeping invariants."""

    _d = date(2026, 5, 13)

    # ── warmup gate ─────────────────────────────────────────────────

    def test_bar_25_still_blocked(self):
        blocked, reason = _should_block(25, {}, self._d)
        assert blocked is True
        assert reason == "warmup"

    def test_bar_26_passes_warmup(self):
        blocked, _ = _should_block(26, {}, self._d)
        assert blocked is False

    def test_halted_session_blocks_regardless_of_bar_count(self):
        blocked, reason = _should_block(100, {self._d: True}, self._d)
        assert blocked is True
        assert reason == "halt"

    # ── session attribution ─────────────────────────────────────────

    def test_late_night_trade_attributed_to_entry_session(self):
        """Trade entered 2026-05-12 23:50 CT (04:50 UTC 5/13), exiting 5/13 00:30 CT
        with pnl=-50 must halt session 2026-05-12, NOT 2026-05-13."""
        entry_utc = pd.Timestamp("2026-05-13 04:50:00+00:00")  # 23:50 CT 2026-05-12
        tr = MockTradeResult(entry_ts=entry_utc, pnl_dollars=-50.0)

        session_pnl: dict[date, float] = defaultdict(float)
        session_halted: dict[date, bool] = defaultdict(bool)

        es = _entry_session_date_from_utc(tr.entry_ts)
        session_pnl[es] += tr.pnl_dollars
        if session_pnl[es] <= -DAILY_LOSS_HALT_USD:
            session_halted[es] = True

        may12 = date(2026, 5, 12)
        may13 = date(2026, 5, 13)
        assert session_halted[may12] is True
        assert session_halted[may13] is False

    def test_two_losses_same_day_trigger_halt(self):
        """Two losing trades on 2026-05-13 totalling -55 → session halted."""
        may13 = date(2026, 5, 13)
        trades = [
            MockTradeResult(
                entry_ts=pd.Timestamp("2026-05-13 15:00:00+00:00"),
                pnl_dollars=-30.0,
            ),
            MockTradeResult(
                entry_ts=pd.Timestamp("2026-05-13 16:00:00+00:00"),
                pnl_dollars=-25.0,
            ),
        ]
        session_pnl: dict[date, float] = defaultdict(float)
        session_halted: dict[date, bool] = defaultdict(bool)
        for tr in trades:
            es = _entry_session_date_from_utc(tr.entry_ts)
            session_pnl[es] += tr.pnl_dollars
            if session_pnl[es] <= -DAILY_LOSS_HALT_USD:
                session_halted[es] = True

        assert session_halted[may13] is True
        assert session_pnl[may13] == pytest.approx(-55.0)

    def test_winning_trade_does_not_unfire_halt(self):
        """-100 then +200 on same day: halt fires after trade 1, stays fired."""
        may13 = date(2026, 5, 13)
        trades = [
            MockTradeResult(
                entry_ts=pd.Timestamp("2026-05-13 15:00:00+00:00"),
                pnl_dollars=-100.0,
            ),
            MockTradeResult(
                entry_ts=pd.Timestamp("2026-05-13 16:00:00+00:00"),
                pnl_dollars=200.0,
            ),
        ]
        session_pnl: dict[date, float] = defaultdict(float)
        session_halted: dict[date, bool] = defaultdict(bool)
        for tr in trades:
            es = _entry_session_date_from_utc(tr.entry_ts)
            session_pnl[es] += tr.pnl_dollars
            if session_pnl[es] <= -DAILY_LOSS_HALT_USD:
                session_halted[es] = True

        # Net P&L is positive, but halt must remain sticky.
        assert session_halted[may13] is True
        assert session_pnl[may13] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 6. TestEnrichWithMaeMfeRegimeTod
# ---------------------------------------------------------------------------

class TestEnrichWithMaeMfeRegimeTod:
    """_enrich_with_mae_mfe_regime_tod degrades gracefully on errors."""

    @staticmethod
    def _make_trade_df() -> pd.DataFrame:
        """Minimal trades DataFrame that passes the df.empty guard."""
        return pd.DataFrame({
            "strategy": ["bias_momentum"],
            "direction": ["LONG"],
            "entry_ts": [pd.Timestamp("2024-08-15 13:30:00+00:00")],
            "pnl_dollars": [10.0],
        })

    @staticmethod
    def _make_bars_df() -> pd.DataFrame:
        ts = pd.date_range("2024-08-15", periods=60, freq="1min", tz="UTC")
        return pd.DataFrame({"ts": ts, "open": 19000.0, "high": 19010.0,
                             "low": 18990.0, "close": 19005.0, "volume": 100})

    def test_empty_dataframe_returned_unchanged(self):
        empty = pd.DataFrame()
        result = _enrich_with_mae_mfe_regime_tod(empty, self._make_bars_df())
        assert result.empty

    def test_compute_mae_mfe_raises_returns_original_df_with_warning(
        self, monkeypatch, capfd
    ):
        """If compute_mae_mfe raises, the function catches it, prints WARN to
        stderr, and returns the original DataFrame unchanged."""
        import tools.portfolio_backtest.analytics as _analytics

        def _boom(df, bars):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(_analytics, "compute_mae_mfe", _boom)

        df = self._make_trade_df()
        result = _enrich_with_mae_mfe_regime_tod(df, self._make_bars_df())

        captured = capfd.readouterr()
        assert "WARN" in captured.err
        assert list(result.columns) == list(df.columns)
