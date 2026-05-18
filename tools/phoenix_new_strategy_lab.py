"""
Phoenix new-strategy lab (Phase 13)
====================================

Standalone backtest battery for 7 candidate strategies (a-g) against the
5-year MNQ CSV data. Each strategy is a pure function — does NOT touch
production strategy code. Winners can be promoted to a proper
`strategies/*.py` class in a later sprint.

Strategies tested:
  a) asian_session_continuation — overnight 0-3 CT breakout follow-through
  b) rth_open_drive_scalp       — direction of first 5min RTH bar
  c) poc_magnet_reversion       — far-from-POC at session start → mean revert
  d) orb_fade_fixed             — reimplements orb_fade WITHOUT the time.time()
                                   freshness bug (B3 fix proof-of-concept)
  e) multi_day_breakout         — break of 3-day high/low
  f) eod_mean_reversion         — last RTH hour counter-trend
  g) inside_bar_breakout        — 5m inside bar → next-bar break

Each strategy operates on the standard pipeline yield:
    (eval_ts, market, bars_1m, bars_5m, session_info)

Output:
  backtest_results/phoenix_new_strategy_lab.csv  — every trade
  backtest_results/phoenix_new_strategy_summary.csv  — per-strategy summary
  stdout                                          — formatted summary
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import CSVEnrichmentPipeline, simulate_trade  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("strategy_lab")
logger.setLevel(logging.INFO)

_CT = ZoneInfo("America/Chicago")
TICK = 0.25
TICK_VALUE = 0.50


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _in_window(now_ct: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, end_hhmm.split(":"))
    t = now_ct.time()
    return dtime(sh, sm) <= t < dtime(eh, em)


def _ct(ts: pd.Timestamp) -> datetime:
    return ts.tz_convert(_CT).to_pydatetime()


@dataclass
class StratSignal:
    direction: str  # LONG/SHORT
    entry_price: float
    stop_price: float
    target_price: float
    note: str = ""


@dataclass
class LabState:
    # Per-day tracking
    on_high: Optional[float] = None      # overnight high (17:00-23:59 CT)
    on_low: Optional[float] = None
    on_day: Optional[str] = None         # date string of the overnight session

    # Last-fired bar dedup
    last_fired_ts: dict = field(default_factory=dict)  # strategy -> ts

    # Multi-day tracking — last N RTH session highs/lows
    rth_highs: list = field(default_factory=list)  # list of (date_str, high)
    rth_lows: list = field(default_factory=list)
    cur_rth_high: Optional[float] = None
    cur_rth_low: Optional[float] = None
    cur_rth_date: Optional[str] = None

    # Once-per-day dedup
    fired_today: dict = field(default_factory=dict)  # (strat, date_str) -> True


def _dedup_bar(state: LabState, strat: str, eval_ts: pd.Timestamp) -> bool:
    """Return True if this strategy already fired on this exact bar."""
    if state.last_fired_ts.get(strat) == eval_ts:
        return True
    return False


def _mark_fired(state: LabState, strat: str, eval_ts: pd.Timestamp) -> None:
    state.last_fired_ts[strat] = eval_ts


def _fired_today(state: LabState, strat: str, date_str: str) -> bool:
    return state.fired_today.get((strat, date_str), False)


def _mark_today(state: LabState, strat: str, date_str: str) -> None:
    state.fired_today[(strat, date_str)] = True


def _update_overnight_and_rth_state(state: LabState, eval_ts: pd.Timestamp,
                                     market: dict) -> None:
    """Maintain overnight range + per-RTH-day high/low for downstream strategies."""
    now_ct = _ct(eval_ts)
    date_str = now_ct.strftime("%Y-%m-%d")
    price = float(market.get("price") or 0)
    high = float(market.get("price") or 0)
    low = float(market.get("price") or 0)
    # Use last 1m bar high/low for finer extremes
    bars_1m = market.get("_bars_1m_ref")  # set by runner
    if bars_1m:
        last = bars_1m[-1]
        high = float(getattr(last, "high", price))
        low = float(getattr(last, "low", price))

    hh = now_ct.hour
    mm = now_ct.minute

    # Overnight session = 17:00 CT (prev day) through 08:30 CT (today)
    # We accumulate the overnight session and "freeze" it at 03:00 CT for
    # strategy (a) to use during 03:00-08:00 CT continuation window.
    in_overnight = (hh >= 17) or (hh < 8) or (hh == 8 and mm < 30)
    if in_overnight:
        # Use the date of the morning-side of the overnight (next-day date)
        if hh >= 17:
            # Evening — overnight session belongs to NEXT calendar day
            from datetime import timedelta as _td
            on_date = (now_ct + _td(days=1)).strftime("%Y-%m-%d")
        else:
            on_date = date_str
        if state.on_day != on_date:
            state.on_day = on_date
            state.on_high = high
            state.on_low = low
        else:
            state.on_high = max(state.on_high, high) if state.on_high else high
            state.on_low = min(state.on_low, low) if state.on_low else low

    # RTH session high/low (08:30-15:00 CT)
    in_rth = (hh == 8 and mm >= 30) or (9 <= hh < 15)
    if in_rth:
        if state.cur_rth_date != date_str:
            # Push previous day's RTH high/low to history
            if state.cur_rth_date is not None and state.cur_rth_high is not None:
                state.rth_highs.append((state.cur_rth_date, state.cur_rth_high))
                state.rth_lows.append((state.cur_rth_date, state.cur_rth_low))
                # Keep last 10 days
                state.rth_highs = state.rth_highs[-10:]
                state.rth_lows = state.rth_lows[-10:]
            state.cur_rth_date = date_str
            state.cur_rth_high = high
            state.cur_rth_low = low
        else:
            state.cur_rth_high = max(state.cur_rth_high, high)
            state.cur_rth_low = min(state.cur_rth_low, low)


# ════════════════════════════════════════════════════════════════════
# (a) Asian session continuation
# ════════════════════════════════════════════════════════════════════
def eval_asian_continuation(eval_ts, market, bars_1m, bars_5m,
                             session_info, state: LabState) -> Optional[StratSignal]:
    """If price breaks the overnight 17:00-03:00 CT range during 03:00-08:00 CT,
    take continuation. Stop at the opposite range edge or 12 ticks max.
    Target: 2:1 RR."""
    now_ct = _ct(eval_ts)
    if not _in_window(now_ct, "03:00", "08:00"):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if _fired_today(state, "asian_continuation", date_str):
        return None
    if state.on_high is None or state.on_low is None or state.on_day != date_str:
        return None

    price = float(market.get("price") or 0)
    atr = float(market.get("atr_5m") or 0) or 2.0
    on_range = state.on_high - state.on_low
    if on_range < TICK * 8:  # too tight to be meaningful
        return None

    # Need a fresh 5m close confirming the break
    if len(bars_5m) < 1:
        return None
    last5 = bars_5m[-1]
    close5 = float(getattr(last5, "close", price))

    direction = None
    if close5 > state.on_high + 0.5 * atr:
        direction = "LONG"
    elif close5 < state.on_low - 0.5 * atr:
        direction = "SHORT"
    if direction is None:
        return None

    # Stop: opposite half of the overnight range, capped at 14 ticks
    if direction == "LONG":
        stop_dist = min(price - state.on_low, 14 * TICK)
        stop_dist = max(stop_dist, 6 * TICK)
        stop = price - stop_dist
        target = price + stop_dist * 2.0
    else:
        stop_dist = min(state.on_high - price, 14 * TICK)
        stop_dist = max(stop_dist, 6 * TICK)
        stop = price + stop_dist
        target = price - stop_dist * 2.0

    _mark_today(state, "asian_continuation", date_str)
    return StratSignal(direction, price, stop, target,
                       note=f"ON_break {direction} close5={close5:.2f} on=[{state.on_low:.2f},{state.on_high:.2f}]")


# ════════════════════════════════════════════════════════════════════
# (b) RTH open drive scalp
# ════════════════════════════════════════════════════════════════════
def eval_rth_open_drive_scalp(eval_ts, market, bars_1m, bars_5m,
                                session_info, state: LabState) -> Optional[StratSignal]:
    """At 08:35 CT (after first 5min bar closes), take direction of bar.
    LONG if close > open + 60% of range (strong drive up).
    SHORT if close < open - 60% of range.
    Stop: bar midpoint. Target: 2:1 RR."""
    now_ct = _ct(eval_ts)
    # Fire only at 08:35-08:36 CT
    if not (now_ct.hour == 8 and now_ct.minute in (35, 36)):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if _fired_today(state, "rth_open_drive_scalp", date_str):
        return None

    o = market.get("rth_5min_open")
    h = market.get("rth_5min_high")
    l = market.get("rth_5min_low")
    c = market.get("rth_5min_close")
    if None in (o, h, l, c):
        return None
    rng = h - l
    if rng < TICK * 4:
        return None

    bar_progress = (c - o) / rng  # +1 = closed at top, -1 = bottom
    # Strong drive threshold: close in top/bottom 30%
    if c > o and (c - l) / rng > 0.70:
        direction = "LONG"
    elif c < o and (h - c) / rng > 0.70:
        direction = "SHORT"
    else:
        return None

    price = float(market.get("price") or c)
    mid = (h + l) / 2
    if direction == "LONG":
        stop = mid - TICK  # mid of OR + 1t safety
        stop_dist = price - stop
        if stop_dist < 4 * TICK or stop_dist > 20 * TICK:
            return None
        target = price + stop_dist * 2.0
    else:
        stop = mid + TICK
        stop_dist = stop - price
        if stop_dist < 4 * TICK or stop_dist > 20 * TICK:
            return None
        target = price - stop_dist * 2.0

    _mark_today(state, "rth_open_drive_scalp", date_str)
    return StratSignal(direction, price, stop, target,
                       note=f"OR_drive {direction} progress={bar_progress:+.0%}")


# ════════════════════════════════════════════════════════════════════
# (c) POC magnet reversion
# ════════════════════════════════════════════════════════════════════
def eval_poc_magnet_reversion(eval_ts, market, bars_1m, bars_5m,
                                session_info, state: LabState) -> Optional[StratSignal]:
    """If price opens >2 ATR away from prior_day_poc, mean-revert toward POC.
    Fires once per day at 08:30-09:30 CT."""
    now_ct = _ct(eval_ts)
    if not _in_window(now_ct, "08:30", "09:30"):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if _fired_today(state, "poc_magnet", date_str):
        return None

    poc = market.get("prior_day_poc")
    if not poc:
        return None
    atr = float(market.get("atr_5m") or 0) or 2.0
    price = float(market.get("price") or 0)
    distance = price - poc

    # Need price >= 2 ATR from POC
    if abs(distance) < 2 * atr:
        return None

    if distance > 0:
        direction = "SHORT"  # price above poc → revert down
        target = poc
        stop_dist = abs(distance) * 0.5
        stop = price + stop_dist
    else:
        direction = "LONG"
        target = poc
        stop_dist = abs(distance) * 0.5
        stop = price - stop_dist

    # Sanity: stop must be > 4t and < 30t
    if stop_dist < 4 * TICK or stop_dist > 30 * TICK:
        return None
    rr = abs(target - price) / stop_dist
    if rr < 1.5:
        return None

    _mark_today(state, "poc_magnet", date_str)
    return StratSignal(direction, price, stop, target,
                       note=f"POC_magnet {direction} dist={distance:+.2f} atr={atr:.2f}")


# ════════════════════════════════════════════════════════════════════
# (d) ORB Fade FIXED (B3 fix)
# ════════════════════════════════════════════════════════════════════
def eval_orb_fade_fixed(eval_ts, market, bars_1m, bars_5m,
                         session_info, state: LabState) -> Optional[StratSignal]:
    """Reimplements strategies/orb_fade.py WITHOUT the time.time() freshness
    bug. Same logic: find recent breakout-then-retrace pattern, require
    wick rejection + CVD divergence + volume."""
    now_ct = _ct(eval_ts)
    if not _in_window(now_ct, "08:45", "12:00"):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if state.fired_today.get(("orb_fade_fixed_count", date_str), 0) >= 2:
        return None
    if _dedup_bar(state, "orb_fade_fixed", eval_ts):
        return None

    or_high = market.get("rth_15min_high")
    or_low = market.get("rth_15min_low")
    if or_high is None or or_low is None or len(bars_1m) < 15:
        return None

    current_bar = bars_1m[-1]
    cbc = float(getattr(current_bar, "close", 0))
    cbh = float(getattr(current_bar, "high", 0))
    cbl = float(getattr(current_bar, "low", 0))
    crng = cbh - cbl
    if crng <= 0:
        return None

    # Current bar must be INSIDE OR (retraced)
    if cbc > or_high or cbc < or_low:
        return None

    # Find recent breakout (close > or_high + 2t or < or_low - 2t)
    min_pen = 2 * TICK
    lookback = 20
    scan = bars_1m[-(lookback + 1):-1]
    breakout_dir = None
    breakout_bar = None
    for b in reversed(scan):
        close = float(getattr(b, "close", 0))
        high = float(getattr(b, "high", 0))
        low = float(getattr(b, "low", 0))
        if high - low <= 0:
            continue
        if close > or_high + min_pen:
            breakout_dir = "LONG"
            breakout_bar = b
            break
        if close < or_low - min_pen:
            breakout_dir = "SHORT"
            breakout_bar = b
            break
    if breakout_bar is None:
        return None

    # Rejection wick on current bar
    if breakout_dir == "LONG":
        wick = (cbh - cbc) / crng  # upper wick
    else:
        wick = (cbc - cbl) / crng  # lower wick
    if wick < 0.30:
        return None

    # CVD divergence — sum of last 5 today bars' delta
    today_bars = []
    for b in reversed(bars_1m):
        bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
        if bt.date() != now_ct.date():
            break
        today_bars.append(b)
        if len(today_bars) >= 5:
            break
    today_bars.reverse()
    if len(today_bars) < 3:
        return None
    delta_sum = sum(float(getattr(b, "delta", 0) or 0) for b in today_bars)
    nonzero = sum(1 for b in today_bars if (getattr(b, "delta", 0) or 0) != 0)
    if nonzero == 0:
        return None
    if breakout_dir == "LONG" and delta_sum > 0:
        return None
    if breakout_dir == "SHORT" and delta_sum < 0:
        return None

    # Volume confirmation on breakout bar
    vol_lookback = 20
    recent = bars_1m[-(vol_lookback + 1):-1]
    avg_vol = sum(float(getattr(b, "volume", 0) or 0) for b in recent) / max(1, len(recent))
    breakout_vol = float(getattr(breakout_bar, "volume", 0) or 0)
    if avg_vol > 0 and breakout_vol < 1.3 * avg_vol:
        return None

    # Build the fade signal
    fade_dir = "SHORT" if breakout_dir == "LONG" else "LONG"
    price = float(market.get("price") or cbc)
    bh = float(getattr(breakout_bar, "high"))
    bl = float(getattr(breakout_bar, "low"))
    if fade_dir == "SHORT":
        stop = bh + 2 * TICK
        stop_dist = stop - price
    else:
        stop = bl - 2 * TICK
        stop_dist = price - stop
    if stop_dist < 6 * TICK or stop_dist > 30 * TICK:
        return None

    # Target: opposite OR boundary
    target = or_low if fade_dir == "SHORT" else or_high

    state.fired_today[("orb_fade_fixed_count", date_str)] = \
        state.fired_today.get(("orb_fade_fixed_count", date_str), 0) + 1
    _mark_fired(state, "orb_fade_fixed", eval_ts)
    return StratSignal(fade_dir, price, stop, target,
                       note=f"FADE {fade_dir} (failed {breakout_dir}) wick={wick:.0%} d={delta_sum:.0f}")


# ════════════════════════════════════════════════════════════════════
# (e) Multi-day breakout
# ════════════════════════════════════════════════════════════════════
def eval_multi_day_breakout(eval_ts, market, bars_1m, bars_5m,
                              session_info, state: LabState) -> Optional[StratSignal]:
    """Break of 3-day RTH high/low. Fires once per day, 08:45-13:00 CT.
    Stop: confirmation bar opposite extreme. Target: 2x stop."""
    now_ct = _ct(eval_ts)
    if not _in_window(now_ct, "08:45", "13:00"):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if _fired_today(state, "multi_day_breakout", date_str):
        return None
    # Need 3 prior days
    if len(state.rth_highs) < 3 or len(state.rth_lows) < 3:
        return None

    three_high = max(h for _, h in state.rth_highs[-3:])
    three_low = min(l for _, l in state.rth_lows[-3:])

    if len(bars_5m) < 1:
        return None
    last5 = bars_5m[-1]
    close5 = float(getattr(last5, "close", 0))
    high5 = float(getattr(last5, "high", 0))
    low5 = float(getattr(last5, "low", 0))

    price = float(market.get("price") or close5)
    direction = None
    if close5 > three_high + TICK:
        direction = "LONG"
    elif close5 < three_low - TICK:
        direction = "SHORT"
    if direction is None:
        return None

    # Stop at opposite extreme of the breakout 5m bar + 2t
    if direction == "LONG":
        stop = low5 - 2 * TICK
        stop_dist = price - stop
    else:
        stop = high5 + 2 * TICK
        stop_dist = stop - price
    if stop_dist < 6 * TICK or stop_dist > 30 * TICK:
        return None
    target = price + stop_dist * 2.0 if direction == "LONG" else price - stop_dist * 2.0

    _mark_today(state, "multi_day_breakout", date_str)
    return StratSignal(direction, price, stop, target,
                       note=f"3d_break {direction} 3dH={three_high:.2f} 3dL={three_low:.2f}")


# ════════════════════════════════════════════════════════════════════
# (f) EOD mean reversion
# ════════════════════════════════════════════════════════════════════
def eval_eod_mean_reversion(eval_ts, market, bars_1m, bars_5m,
                              session_info, state: LabState) -> Optional[StratSignal]:
    """Last RTH hour (14:00-15:00 CT). If trend strongly up (price > vwap + 1*sigma),
    take SHORT toward vwap. Mirror for LONG. Once per day."""
    now_ct = _ct(eval_ts)
    if not _in_window(now_ct, "14:00", "15:00"):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if _fired_today(state, "eod_reversion", date_str):
        return None

    vwap = float(market.get("vwap") or 0)
    vwap_upper = float(market.get("vwap_upper_1sigma") or market.get("vwap_upper") or 0)
    vwap_lower = float(market.get("vwap_lower_1sigma") or market.get("vwap_lower") or 0)
    price = float(market.get("price") or 0)
    atr = float(market.get("atr_5m") or 0) or 2.0
    if vwap <= 0 or price <= 0:
        return None

    # If vwap bands missing, derive a proxy from ATR
    if vwap_upper <= 0:
        vwap_upper = vwap + 1.0 * atr
    if vwap_lower <= 0:
        vwap_lower = vwap - 1.0 * atr

    direction = None
    if price > vwap_upper:
        direction = "SHORT"
        target = vwap
    elif price < vwap_lower:
        direction = "LONG"
        target = vwap
    if direction is None:
        return None

    target_dist = abs(target - price)
    if target_dist < 4 * TICK:
        return None
    stop_dist = target_dist * 0.5
    if stop_dist < 4 * TICK or stop_dist > 20 * TICK:
        return None
    if direction == "LONG":
        stop = price - stop_dist
    else:
        stop = price + stop_dist

    _mark_today(state, "eod_reversion", date_str)
    return StratSignal(direction, price, stop, target,
                       note=f"EOD_rev {direction} vwap={vwap:.2f} dist={price - vwap:+.2f}")


# ════════════════════════════════════════════════════════════════════
# (g) Inside bar breakout
# ════════════════════════════════════════════════════════════════════
def eval_inside_bar_breakout(eval_ts, market, bars_1m, bars_5m,
                               session_info, state: LabState) -> Optional[StratSignal]:
    """On 5m bar closes during RTH (08:45-14:00 CT): if PRIOR 5m bar was an
    inside bar (high <= 2-bars-back high AND low >= 2-bars-back low), and
    CURRENT bar closed beyond the inside bar's high/low → take that direction.
    Stop: opposite extreme of inside bar. Target: 2:1 RR."""
    now_ct = _ct(eval_ts)
    if not _in_window(now_ct, "08:45", "14:00"):
        return None
    if len(bars_5m) < 3:
        return None
    # Fire only on the 5-minute boundary close — i.e., when current minute
    # is a multiple of 5 (and is at minute :00, :05, ...) — that's when
    # bars_5m gets a new entry.
    if now_ct.minute % 5 != 0:
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if _dedup_bar(state, "inside_bar_breakout", eval_ts):
        return None

    parent = bars_5m[-3]   # 2 bars back: the "outer" bar
    inside = bars_5m[-2]   # 1 bar back: the candidate inside bar
    current = bars_5m[-1]  # most-recent: must break inside's high/low

    ph = float(getattr(parent, "high", 0))
    pl = float(getattr(parent, "low", 0))
    ih = float(getattr(inside, "high", 0))
    il = float(getattr(inside, "low", 0))
    cc = float(getattr(current, "close", 0))
    irng = ih - il
    prng = ph - pl

    # Inside bar requirement
    if not (ih <= ph and il >= pl):
        return None
    # Inside bar must have meaningful range
    if irng < 4 * TICK:
        return None
    # Inside bar must be tighter than parent
    if irng > 0.85 * prng:
        return None

    price = float(market.get("price") or cc)
    direction = None
    if cc > ih + TICK:
        direction = "LONG"
        stop = il - TICK
        stop_dist = price - stop
    elif cc < il - TICK:
        direction = "SHORT"
        stop = ih + TICK
        stop_dist = stop - price
    else:
        return None

    if stop_dist < 6 * TICK or stop_dist > 30 * TICK:
        return None
    target = price + stop_dist * 2.0 if direction == "LONG" else price - stop_dist * 2.0

    _mark_fired(state, "inside_bar_breakout", eval_ts)
    return StratSignal(direction, price, stop, target,
                       note=f"IB_break {direction} ib=[{il:.2f},{ih:.2f}]")


# ════════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════════
STRATEGIES: dict[str, Callable] = {
    "a_asian_continuation":    eval_asian_continuation,
    "b_rth_open_drive_scalp":  eval_rth_open_drive_scalp,
    "c_poc_magnet_reversion":  eval_poc_magnet_reversion,
    "d_orb_fade_fixed":        eval_orb_fade_fixed,
    "e_multi_day_breakout":    eval_multi_day_breakout,
    "f_eod_mean_reversion":    eval_eod_mean_reversion,
    "g_inside_bar_breakout":   eval_inside_bar_breakout,
}


def main():
    data_dir = ROOT / "data" / "historical"
    logger.info("[main] Loading pipeline (5 years)")
    pipeline = CSVEnrichmentPipeline(
        mnq_1m_csv=str(data_dir / "mnq_1min_databento.csv"),
        mnq_5m_csv=str(data_dir / "mnq_5min_databento.csv"),
        mes_1m_csv=str(data_dir / "mes_1min_databento.csv"),
        mes_5m_csv=str(data_dir / "mes_5min_databento.csv"),
        start="2021-05-17", end="2026-05-17",
    )

    mnq_1m_df = pipeline.mnq_1m_df.copy()

    state = LabState()
    trades: list[dict] = []
    active: dict[str, dict] = {}  # strategy -> {exit_ts: ...}
    cycle_count = 0
    signal_count: dict[str, int] = {k: 0 for k in STRATEGIES}
    t0 = time.time()

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1
        market["_bars_1m_ref"] = bars_1m  # for state helpers
        _update_overnight_and_rth_state(state, eval_ts, market)

        if cycle_count < 300:  # warmup
            continue

        for strat_name, fn in STRATEGIES.items():
            # If this strategy has an active trade still in progress, skip
            act = active.get(strat_name)
            if act is not None:
                if act.get("exit_ts") is not None and eval_ts >= act["exit_ts"]:
                    active[strat_name] = None
                else:
                    continue
            try:
                sig = fn(eval_ts, market, bars_1m, bars_5m, session_info, state)
            except Exception as e:
                logger.debug(f"{strat_name} err {eval_ts}: {e!r}")
                continue
            if sig is None:
                continue
            signal_count[strat_name] += 1
            tr = simulate_trade(
                signal_strategy=strat_name,
                signal_direction=sig.direction,
                entry_ts=eval_ts,
                entry_price=sig.entry_price,
                stop_price=sig.stop_price,
                target_price=sig.target_price,
                mnq_1m_df=mnq_1m_df,
            )
            active[strat_name] = {"exit_ts": tr.exit_ts}
            trades.append({
                "strategy": strat_name,
                "direction": sig.direction,
                "entry_ts": eval_ts,
                "entry_price": sig.entry_price,
                "stop_price": sig.stop_price,
                "target_price": sig.target_price,
                "exit_ts": tr.exit_ts,
                "exit_price": tr.exit_price,
                "exit_reason": tr.exit_reason,
                "pnl_dollars": tr.pnl_dollars,
                "pnl_ticks": tr.pnl_ticks,
                "hold_min": tr.hold_min,
                "year": eval_ts.year,
                "hour_ct": _ct(eval_ts).hour,
                "note": sig.note,
            })

    elapsed = time.time() - t0
    logger.info(f"[main] {cycle_count:,} cycles in {elapsed:.0f}s. "
                f"Total trades: {len(trades)}, by strat: {signal_count}")

    df = pd.DataFrame(trades)
    out_csv = ROOT / "backtest_results" / "phoenix_new_strategy_lab.csv"
    df.to_csv(out_csv, index=False)
    logger.info(f"[main] wrote {len(df)} trades to {out_csv}")

    # Summary
    print()
    print("=" * 100)
    print("PHASE 13 NEW STRATEGY LAB — 5 YEAR BACKTEST")
    print("=" * 100)
    print()
    if df.empty:
        print("(no trades generated)")
        return

    print("Total trades:", len(df))
    print("Total P&L:    $", round(df.pnl_dollars.sum(), 0))
    print()
    print("=== Per-strategy ===")
    agg = df.groupby("strategy").agg(
        n=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda s: (s > 0).sum()),
        total=("pnl_dollars", "sum"),
        avg=("pnl_dollars", "mean"),
        max_dd=("pnl_dollars", lambda s: (s.cumsum().cummax() - s.cumsum()).max()),
        avg_hold=("hold_min", "mean"),
    ).round(2)
    agg["wr_pct"] = (agg.wins / agg.n * 100).round(1)
    gross_win = df[df.pnl_dollars > 0].groupby("strategy").pnl_dollars.sum()
    gross_loss = -df[df.pnl_dollars < 0].groupby("strategy").pnl_dollars.sum()
    agg["pf"] = (gross_win / gross_loss).round(2)
    agg = agg.sort_values("total", ascending=False)
    print(agg[["n", "wr_pct", "total", "avg", "pf", "max_dd", "avg_hold"]].to_string())

    # Save summary CSV
    summary_csv = ROOT / "backtest_results" / "phoenix_new_strategy_summary.csv"
    agg.to_csv(summary_csv)
    logger.info(f"[main] wrote summary to {summary_csv}")

    print()
    print("=== Per-strategy × per-year ===")
    pivot = df.pivot_table(
        index="strategy", columns="year", values="pnl_dollars",
        aggfunc="sum", fill_value=0,
    ).round(0)
    print(pivot.to_string())


if __name__ == "__main__":
    main()
