"""noise_area managed-exit bar-close + min-hold fixes (2026-05-15).

Today's data: 4 noise_area trades, 3 losses, all exited via signal_flip
within ~2 minutes of entry. The one winner escaped via a different
exit path (ema_dom_exit at the 70%-of-target floor).

Root cause: the managed-exit check fired on TICK PRICE crossing
UB/LB/VWAP, with no minimum hold window. Same anti-pattern as the
BE-stop tick-touch bug fixed yesterday (#18). The Zarattini noise-cone
paper specifies "confirmed return to cone" — i.e. a bar close back
inside, not a tick.

Fix (this commit):
1. Use last_bar.close (most-recent closed 1m bar) instead of
   market["price"] (latest tick) for all signal_flip comparisons.
2. Suppress signal_flip triggers entirely during the first N seconds
   from entry (config: `min_hold_seconds_before_signal_flip`, default
   300s = 5 min).

EoD flat is unaffected by either change — fires anytime.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")
_CT = ZoneInfo("America/Chicago")


def _make_strategy(min_hold_s: int | None = None):
    """Build a noise_area instance with a controllable min-hold."""
    from strategies.noise_area import NoiseAreaMomentum
    cfg = {
        "enabled": True,
        "validated": False,
        "lookback_days": 14,
        "band_mult": 0.7,
        "trade_freq_minutes": 30,
        "require_vwap_confluence": True,
        "min_noise_history_days": 10,
        "eod_flat_time_et": "16:54",
        "prod_eod_flat_time_et": "10:55",
        "is_prod_bot": False,
    }
    if min_hold_s is not None:
        cfg["min_hold_seconds_before_signal_flip"] = min_hold_s
    return NoiseAreaMomentum(cfg)


def _make_position(direction: str, entry_time: float, ub: float, lb: float):
    return SimpleNamespace(
        direction=direction,
        entry_time=entry_time,
        metadata={"UB": ub, "LB": lb},
    )


def _make_bar(end_dt_et: datetime, close: float, high: float = None, low: float = None):
    return SimpleNamespace(
        end_time=end_dt_et.timestamp(),
        close=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
    )


# ── Bar-close confirmation (vs tick) ───────────────────────────────────

def test_short_NOT_exited_when_tick_above_LB_but_bar_closes_below():
    """Pre-fix scenario: SHORT entry, price tick crosses ABOVE LB
    momentarily, but the bar closes BACK below. Old code exited on
    the tick (signal_flip_returned_above_LB). New code requires bar
    close above LB → no exit."""
    strat = _make_strategy(min_hold_s=0)
    open_dt_et = datetime(2026, 5, 15, 14, 30, tzinfo=_ET)
    position = _make_position("SHORT",
                              entry_time=open_dt_et.timestamp() - 600,
                              ub=29300.0, lb=29150.0)
    market = {
        "price": 29170.0,    # TICK above LB = old trigger
        "vwap": 29250.0,
    }
    bars_1m = [_make_bar(open_dt_et, close=29140.0)]  # bar closed BELOW LB AND BELOW vwap
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is False, (
        f"bar close 29140 (below both LB 29150 and VWAP 29250) — "
        f"must NOT exit. Got reason={reason!r}"
    )


def test_short_exits_when_bar_closes_above_LB():
    """Confirmed flip: bar closed above LB for a SHORT → fire."""
    strat = _make_strategy(min_hold_s=0)
    open_dt_et = datetime(2026, 5, 15, 14, 30, tzinfo=_ET)
    position = _make_position("SHORT",
                              entry_time=open_dt_et.timestamp() - 600,
                              ub=29300.0, lb=29150.0)
    market = {"price": 29170.0, "vwap": 29250.0}
    bars_1m = [_make_bar(open_dt_et, close=29160.0)]  # close > LB
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is True
    assert reason == "signal_flip_returned_above_LB"


def test_short_vwap_path_isolated():
    """Isolate the VWAP path: LB=None forces VWAP comparison only.
    Bar closes above VWAP → fire signal_flip_above_vwap."""
    strat = _make_strategy(min_hold_s=0)
    open_dt_et = datetime(2026, 5, 15, 14, 30, tzinfo=_ET)
    # Position with no LB in metadata
    position = SimpleNamespace(
        direction="SHORT",
        entry_time=open_dt_et.timestamp() - 600,
        metadata={"UB": 29300.0, "LB": None},
    )
    market = {"price": 29220.0, "vwap": 29200.0}
    bars_1m = [_make_bar(open_dt_et, close=29210.0)]  # close > vwap
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is True
    assert reason == "signal_flip_above_vwap"


def test_long_NOT_exited_when_tick_below_ub_but_bar_closes_above():
    """LONG mirror: tick below UB momentarily, bar closes back above."""
    strat = _make_strategy(min_hold_s=0)
    open_dt_et = datetime(2026, 5, 15, 14, 30, tzinfo=_ET)
    position = _make_position("LONG",
                              entry_time=open_dt_et.timestamp() - 600,
                              ub=29250.0, lb=29150.0)
    market = {"price": 29240.0, "vwap": 29200.0}  # tick below UB
    bars_1m = [_make_bar(open_dt_et, close=29260.0)]  # bar closed ABOVE UB
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is False, (
        f"bar close 29260 (above UB) — must NOT trigger "
        f"signal_flip_returned_below_UB. Got reason={reason!r}"
    )


def test_long_exits_when_bar_closes_below_ub():
    strat = _make_strategy(min_hold_s=0)
    open_dt_et = datetime(2026, 5, 15, 14, 30, tzinfo=_ET)
    position = _make_position("LONG",
                              entry_time=open_dt_et.timestamp() - 600,
                              ub=29250.0, lb=29150.0)
    market = {"price": 29240.0, "vwap": 29200.0}
    bars_1m = [_make_bar(open_dt_et, close=29230.0)]  # bar close < UB
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is True
    assert reason == "signal_flip_returned_below_UB"


# ── Min-hold window ───────────────────────────────────────────────────

def test_signal_flip_suppressed_during_min_hold_window():
    """Position entered 60s ago. Bar close would fire signal_flip but
    we're inside the 300s min-hold window → suppress."""
    strat = _make_strategy(min_hold_s=300)
    open_dt_et = datetime(2026, 5, 15, 14, 30, tzinfo=_ET)
    position = _make_position("SHORT",
                              entry_time=open_dt_et.timestamp() - 60,  # 60s ago
                              ub=29250.0, lb=29150.0)
    market = {"price": 29220.0, "vwap": 29200.0}
    bars_1m = [_make_bar(open_dt_et, close=29210.0)]  # confirmed flip
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is False, (
        "Within 60s of entry — signal_flip must be suppressed. "
        f"Got reason={reason!r}"
    )


def test_signal_flip_fires_after_min_hold_window():
    """Position entered 600s ago, past the 300s min-hold. Bar-close
    flip back above LB for a SHORT → fire."""
    strat = _make_strategy(min_hold_s=300)
    open_dt_et = datetime(2026, 5, 15, 14, 30, tzinfo=_ET)
    position = _make_position("SHORT",
                              entry_time=open_dt_et.timestamp() - 600,
                              ub=29300.0, lb=29150.0)
    market = {"price": 29170.0, "vwap": 29250.0}
    bars_1m = [_make_bar(open_dt_et, close=29160.0)]  # close > LB
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is True
    assert reason == "signal_flip_returned_above_LB"


# ── EoD flat unaffected ───────────────────────────────────────────────

def test_eod_flat_fires_even_within_min_hold():
    """Position entered 30s ago, well within min-hold. But EoD time has
    passed → EoD must fire anyway (capital safety > min-hold)."""
    strat = _make_strategy(min_hold_s=300)
    # Use a fixed bar time at 16:55 ET — past sim EoD of 16:54
    eod_past_dt_et = datetime(2026, 5, 15, 16, 55, tzinfo=_ET)
    position = _make_position("LONG",
                              entry_time=eod_past_dt_et.timestamp() - 30,
                              ub=29250.0, lb=29150.0)
    market = {"price": 29280.0, "vwap": 29200.0}
    bars_1m = [_make_bar(eod_past_dt_et, close=29280.0)]
    should_exit, reason = strat.check_exit(position, market, bars_1m, {})
    assert should_exit is True
    assert reason == "eod_flat"


# ── Config plumbing ───────────────────────────────────────────────────

def test_config_has_min_hold_seconds_default_5min():
    from config.strategies import STRATEGIES
    cfg = STRATEGIES["noise_area"]
    assert cfg.get("min_hold_seconds_before_signal_flip") == 300, (
        "min_hold_seconds_before_signal_flip should default to 300s "
        "(5 min) per Zarattini paper interpretation. Today's losses "
        "all happened within 2 min of entry."
    )


def test_source_uses_bar_close_not_tick():
    src = (ROOT / "strategies" / "noise_area.py").read_text(encoding="utf-8")
    # Must reference last_bar.close in the comparison block
    assert "bar_close = float(getattr(last_bar, \"close\"" in src, (
        "noise_area must compare against bar close, not tick price. "
        "Tick-touch comparison is the 2026-05-15 bug-class."
    )
    # The min-hold protection must be present
    assert "min_hold_seconds_before_signal_flip" in src
    assert "held_s < min_hold_s" in src
