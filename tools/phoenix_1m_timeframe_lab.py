"""
Phoenix 1-Minute Timeframe Lab
================================

Tests whether running strategies on 1m bars (instead of 5m) improves
results via faster entries/exits. Builds 1m-native versions of the
top performers and compares against the 5m baseline.

Strategies tested (all 1m timeframe):
  - raschke_1m_baseline       (EMA21 on 1m, pullback last 5 1m bars)
  - raschke_1m_ema9_ref       (EMA9 on 1m)
  - raschke_1m_loose          (just EMA9 > EMA21 trend on 1m)
  - inside_bar_1m             (inside bar pattern on 1m bars)
  - multi_day_breakout_1m     (1m close vs 3-day RTH H/L)
  - asian_continuation_1m     (1m close vs overnight range, sub-RTH)

Comparison baseline: same strategies on 5m (from prior labs).

Hypothesis:
  - TREND strategies (Raschke, multi_day, asian) might benefit from
    1m granularity — faster re-entries on trend resumption
  - MOMENTUM patterns (inside bar) might suffer from 1m noise
  - Expect WR to drop slightly but fire rate to rise; net unclear
"""
from __future__ import annotations

import logging
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import CSVEnrichmentPipeline, simulate_trade  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("tf_1m_lab")
logger.setLevel(logging.INFO)

TICK = 0.25


def _ct(ts: pd.Timestamp) -> datetime:
    return ts.tz_convert("America/Chicago").to_pydatetime()


def _in_rth(now_ct: datetime) -> bool:
    t = now_ct.time()
    return dtime(8, 30) <= t < dtime(15, 0)


@dataclass
class VariantSignal:
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    note: str


# ════════════════════════════════════════════════════════════════════
# 1m MA state — incremental EMA computation from 1m bars
# ════════════════════════════════════════════════════════════════════
class MA1mState:
    def __init__(self):
        self.ema: dict[int, Optional[float]] = {9: None, 21: None, 50: None}
        self.alpha: dict[int, float] = {p: 2.0 / (p + 1) for p in (9, 21, 50)}
        self.last_1m_end_ts: Optional[float] = None
        self.bars_seen: int = 0

    def update(self, last_1m_bar) -> None:
        end_ts = float(getattr(last_1m_bar, "end_time", 0))
        if end_ts == 0 or end_ts == self.last_1m_end_ts:
            return
        self.last_1m_end_ts = end_ts
        close = float(getattr(last_1m_bar, "close", 0))
        if close <= 0:
            return
        self.bars_seen += 1
        for period, current in self.ema.items():
            a = self.alpha[period]
            self.ema[period] = close if current is None else a * close + (1 - a) * current

    def get(self, period: int) -> Optional[float]:
        v = self.ema.get(period)
        return v if (v is not None and self.bars_seen >= period) else None


# ════════════════════════════════════════════════════════════════════
# Shared state — 3-day RTH H/L for multi_day; overnight range for asian
# ════════════════════════════════════════════════════════════════════
@dataclass
class SharedState:
    cur_rth_high: Optional[float] = None
    cur_rth_low: Optional[float] = None
    cur_rth_date: Optional[str] = None
    rth_highs: list = field(default_factory=list)  # (date, high)
    rth_lows: list = field(default_factory=list)
    on_high: Optional[float] = None
    on_low: Optional[float] = None
    on_day: Optional[str] = None
    fired_today: dict = field(default_factory=dict)


def _update_shared(state: SharedState, eval_ts: pd.Timestamp, bars_1m) -> None:
    now_ct = _ct(eval_ts)
    date_str = now_ct.strftime("%Y-%m-%d")
    if not bars_1m:
        return
    last = bars_1m[-1]
    high = float(getattr(last, "high", 0))
    low = float(getattr(last, "low", 0))
    hh = now_ct.hour
    mm = now_ct.minute
    in_overnight = (hh >= 17) or (hh < 8) or (hh == 8 and mm < 30)
    if in_overnight:
        on_date = (now_ct + timedelta(days=1)).strftime("%Y-%m-%d") if hh >= 17 else date_str
        if state.on_day != on_date:
            state.on_day = on_date
            state.on_high = high
            state.on_low = low
        else:
            state.on_high = max(state.on_high, high) if state.on_high else high
            state.on_low = min(state.on_low, low) if state.on_low else low
    in_rth = (hh == 8 and mm >= 30) or (9 <= hh < 15)
    if in_rth:
        if state.cur_rth_date != date_str:
            if state.cur_rth_date is not None and state.cur_rth_high is not None:
                state.rth_highs.append((state.cur_rth_date, state.cur_rth_high))
                state.rth_lows.append((state.cur_rth_date, state.cur_rth_low))
                state.rth_highs = state.rth_highs[-10:]
                state.rth_lows = state.rth_lows[-10:]
            state.cur_rth_date = date_str
            state.cur_rth_high = high
            state.cur_rth_low = low
        else:
            state.cur_rth_high = max(state.cur_rth_high, high)
            state.cur_rth_low = min(state.cur_rth_low, low)


# ════════════════════════════════════════════════════════════════════
# 1m Raschke evaluator
# ════════════════════════════════════════════════════════════════════
def find_pullback_bar_1m(bars_1m, ema_ref: float, direction: str,
                           lookback: int = 5):
    """Look at last N 1m bars (excluding current) for EMA touch."""
    if len(bars_1m) < lookback + 1:
        return None
    last_n = list(bars_1m)[-(lookback + 1):-1]
    buffer = 2 * TICK
    for idx in range(len(last_n) - 1, -1, -1):
        b = last_n[idx]
        bl = float(getattr(b, "low", 0))
        bh = float(getattr(b, "high", 0))
        bc = float(getattr(b, "close", 0))
        if direction == "LONG" and bl <= ema_ref + buffer and bc > ema_ref:
            return b
        if direction == "SHORT" and bh >= ema_ref - buffer and bc < ema_ref:
            return b
    return None


def eval_raschke_1m(eval_ts, market, bars_1m, ma: MA1mState,
                     ema_ref_period: int, trend_spread_atr: float,
                     loose_trend: bool, last_fire_ts) -> Optional[VariantSignal]:
    now_ct = _ct(eval_ts)
    if not _in_rth(now_ct):
        return None
    if last_fire_ts == eval_ts:
        return None
    if len(bars_1m) < 8:
        return None
    atr = float(market.get("atr_1m") or 0)
    if atr <= 0:
        return None
    # Trend direction (using 1m EMAs)
    e21 = ma.get(21)
    e50 = ma.get(50)
    if e21 is None or e50 is None:
        return None
    if loose_trend:
        e9 = ma.get(9)
        if e9 is None:
            return None
        if e9 > e21:
            direction = "LONG"
        elif e9 < e21:
            direction = "SHORT"
        else:
            return None
    else:
        spread = e21 - e50
        threshold = trend_spread_atr * atr * 4  # adjust for 1m ATR being ~1/4 of 5m
        if spread > threshold:
            direction = "LONG"
        elif spread < -threshold:
            direction = "SHORT"
        else:
            return None
    ema_ref = ma.get(ema_ref_period)
    if ema_ref is None:
        return None
    pb = find_pullback_bar_1m(bars_1m, ema_ref, direction, lookback=5)
    if pb is None:
        return None
    current = bars_1m[-1]
    cc = float(getattr(current, "close", 0))
    pb_high = float(getattr(pb, "high", 0))
    pb_low = float(getattr(pb, "low", 0))
    if direction == "LONG" and cc <= pb_high + TICK:
        return None
    if direction == "SHORT" and cc >= pb_low - TICK:
        return None
    price = float(market.get("price") or cc)
    if direction == "LONG":
        stop = pb_low - TICK
        stop_dist = price - stop
        target = price + stop_dist * 2.0
    else:
        stop = pb_high + TICK
        stop_dist = stop - price
        target = price - stop_dist * 2.0
    if stop_dist < 4 * TICK or stop_dist > 25 * TICK:
        return None
    return VariantSignal(direction, price, stop, target, note=f"1m raschke {direction}")


# ════════════════════════════════════════════════════════════════════
# 1m Inside-bar breakout
# ════════════════════════════════════════════════════════════════════
def eval_inside_bar_1m(eval_ts, market, bars_1m, last_fire_ts) -> Optional[VariantSignal]:
    now_ct = _ct(eval_ts)
    if not (dtime(8, 45) <= now_ct.time() < dtime(14, 0)):
        return None
    if last_fire_ts == eval_ts:
        return None
    if len(bars_1m) < 4:
        return None
    parent = bars_1m[-3]
    inside = bars_1m[-2]
    current = bars_1m[-1]
    ph = float(getattr(parent, "high", 0))
    pl = float(getattr(parent, "low", 0))
    ih = float(getattr(inside, "high", 0))
    il = float(getattr(inside, "low", 0))
    cc = float(getattr(current, "close", 0))
    irng = ih - il
    prng = ph - pl
    if not (ih <= ph and il >= pl):
        return None
    if irng < 2 * TICK:  # 1m bars tighter — allow smaller inside
        return None
    if irng > 0.85 * prng:
        return None
    price = float(market.get("price") or cc)
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
    if stop_dist < 4 * TICK or stop_dist > 20 * TICK:
        return None
    if direction == "LONG":
        target = price + stop_dist * 2.0
    else:
        target = price - stop_dist * 2.0
    return VariantSignal(direction, price, stop, target, note=f"1m_IB_{direction}")


# ════════════════════════════════════════════════════════════════════
# 1m Multi-day breakout
# ════════════════════════════════════════════════════════════════════
def eval_multi_day_1m(eval_ts, market, bars_1m, state: SharedState,
                       last_fire_ts) -> Optional[VariantSignal]:
    now_ct = _ct(eval_ts)
    if not (dtime(8, 45) <= now_ct.time() < dtime(13, 0)):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if state.fired_today.get(("multi_day_1m", date_str)):
        return None
    if len(state.rth_highs) < 3:
        return None
    three_high = max(h for _, h in state.rth_highs[-3:])
    three_low = min(l for _, l in state.rth_lows[-3:])
    if not bars_1m:
        return None
    last = bars_1m[-1]
    cc = float(getattr(last, "close", 0))
    hh = float(getattr(last, "high", 0))
    ll = float(getattr(last, "low", 0))
    price = float(market.get("price") or cc)
    if cc > three_high + TICK:
        direction = "LONG"
        stop = ll - 2 * TICK
        stop_dist = price - stop
    elif cc < three_low - TICK:
        direction = "SHORT"
        stop = hh + 2 * TICK
        stop_dist = stop - price
    else:
        return None
    if stop_dist < 4 * TICK or stop_dist > 25 * TICK:
        return None
    if direction == "LONG":
        target = price + stop_dist * 2.0
    else:
        target = price - stop_dist * 2.0
    state.fired_today[("multi_day_1m", date_str)] = True
    return VariantSignal(direction, price, stop, target, note=f"1m_MDB_{direction}")


# ════════════════════════════════════════════════════════════════════
# 1m Asian continuation
# ════════════════════════════════════════════════════════════════════
def eval_asian_1m(eval_ts, market, bars_1m, state: SharedState,
                   last_fire_ts) -> Optional[VariantSignal]:
    now_ct = _ct(eval_ts)
    if not (dtime(3, 0) <= now_ct.time() < dtime(8, 0)):
        return None
    date_str = now_ct.strftime("%Y-%m-%d")
    if state.fired_today.get(("asian_1m", date_str)):
        return None
    if state.on_high is None or state.on_low is None or state.on_day != date_str:
        return None
    atr = float(market.get("atr_1m") or 0) or 2.0
    on_range = state.on_high - state.on_low
    if on_range < TICK * 6:
        return None
    if not bars_1m:
        return None
    last = bars_1m[-1]
    cc = float(getattr(last, "close", 0))
    price = float(market.get("price") or cc)
    direction = None
    if cc > state.on_high + 0.5 * atr:
        direction = "LONG"
    elif cc < state.on_low - 0.5 * atr:
        direction = "SHORT"
    if direction is None:
        return None
    if direction == "LONG":
        stop_dist = min(price - state.on_low, 12 * TICK)
        stop_dist = max(stop_dist, 4 * TICK)
        stop = price - stop_dist
        target = price + stop_dist * 2.0
    else:
        stop_dist = min(state.on_high - price, 12 * TICK)
        stop_dist = max(stop_dist, 4 * TICK)
        stop = price + stop_dist
        target = price - stop_dist * 2.0
    state.fired_today[("asian_1m", date_str)] = True
    return VariantSignal(direction, price, stop, target, note=f"1m_asian_{direction}")


# ════════════════════════════════════════════════════════════════════
# Variant dispatch
# ════════════════════════════════════════════════════════════════════
VARIANTS_RASCHKE = [
    ("raschke_1m_baseline",   21, 0.3, False),
    ("raschke_1m_ema9_ref",    9, 0.3, False),
    ("raschke_1m_loose",      21, 0.0, True),
]
OTHER_VARIANTS = [
    "inside_bar_1m",
    "multi_day_breakout_1m",
    "asian_continuation_1m",
]


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

    ma = MA1mState()
    shared = SharedState()
    all_names = [v[0] for v in VARIANTS_RASCHKE] + OTHER_VARIANTS
    active = {n: None for n in all_names}
    last_fire = {n: None for n in all_names}
    signal_count = {n: 0 for n in all_names}
    trades = []
    cycle_count = 0
    t0 = time.time()

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1
        if bars_1m:
            ma.update(bars_1m[-1])
        _update_shared(shared, eval_ts, bars_1m)
        if cycle_count < 300:
            continue

        # Each variant: skip if active trade open
        for name, ema_p, spread, loose in VARIANTS_RASCHKE:
            if active[name] is not None:
                if active[name].get("exit_ts") is not None and eval_ts >= active[name]["exit_ts"]:
                    active[name] = None
                else:
                    continue
            sig = eval_raschke_1m(eval_ts, market, bars_1m, ma, ema_p, spread,
                                    loose, last_fire[name])
            if sig is None:
                continue
            signal_count[name] += 1
            last_fire[name] = eval_ts
            tr = simulate_trade(name, sig.direction, eval_ts, sig.entry_price,
                                 sig.stop_price, sig.target_price, mnq_1m_df)
            active[name] = {"exit_ts": tr.exit_ts}
            trades.append({"strategy": name, "direction": sig.direction,
                            "entry_ts": eval_ts, "entry_price": sig.entry_price,
                            "stop_price": sig.stop_price, "target_price": sig.target_price,
                            "exit_ts": tr.exit_ts, "exit_price": tr.exit_price,
                            "exit_reason": tr.exit_reason, "pnl_dollars": tr.pnl_dollars,
                            "pnl_ticks": tr.pnl_ticks, "hold_min": tr.hold_min,
                            "year": eval_ts.year})

        for name in OTHER_VARIANTS:
            if active[name] is not None:
                if active[name].get("exit_ts") is not None and eval_ts >= active[name]["exit_ts"]:
                    active[name] = None
                else:
                    continue
            if name == "inside_bar_1m":
                sig = eval_inside_bar_1m(eval_ts, market, bars_1m, last_fire[name])
            elif name == "multi_day_breakout_1m":
                sig = eval_multi_day_1m(eval_ts, market, bars_1m, shared, last_fire[name])
            else:
                sig = eval_asian_1m(eval_ts, market, bars_1m, shared, last_fire[name])
            if sig is None:
                continue
            signal_count[name] += 1
            last_fire[name] = eval_ts
            tr = simulate_trade(name, sig.direction, eval_ts, sig.entry_price,
                                 sig.stop_price, sig.target_price, mnq_1m_df)
            active[name] = {"exit_ts": tr.exit_ts}
            trades.append({"strategy": name, "direction": sig.direction,
                            "entry_ts": eval_ts, "entry_price": sig.entry_price,
                            "stop_price": sig.stop_price, "target_price": sig.target_price,
                            "exit_ts": tr.exit_ts, "exit_price": tr.exit_price,
                            "exit_reason": tr.exit_reason, "pnl_dollars": tr.pnl_dollars,
                            "pnl_ticks": tr.pnl_ticks, "hold_min": tr.hold_min,
                            "year": eval_ts.year})

    elapsed = time.time() - t0
    logger.info(f"[main] {cycle_count:,} cycles in {elapsed:.0f}s. "
                f"Total trades: {len(trades)}, by variant: {signal_count}")

    df = pd.DataFrame(trades)
    out_csv = ROOT / "backtest_results" / "phoenix_1m_timeframe_lab.csv"
    df.to_csv(out_csv, index=False)
    logger.info(f"[main] wrote {len(df)} trades to {out_csv}")

    if df.empty:
        print("(no trades)")
        return

    print()
    print("=" * 100)
    print("1-MINUTE TIMEFRAME LAB — 5 YEAR BACKTEST")
    print("=" * 100)
    print()
    print(f"Total trades: {len(df):,}  Total P&L: ${df.pnl_dollars.sum():,.0f}")
    print()
    agg = df.groupby("strategy").agg(
        n=("pnl_dollars", "count"),
        wins=("pnl_dollars", lambda s: (s > 0).sum()),
        total=("pnl_dollars", "sum"),
        avg=("pnl_dollars", "mean"),
        max_dd=("pnl_dollars",
                lambda s: (s.cumsum().cummax() - s.cumsum()).max()),
        avg_hold=("hold_min", "mean"),
    ).round(2)
    agg["wr_pct"] = (agg.wins / agg.n * 100).round(1)
    gw = df[df.pnl_dollars > 0].groupby("strategy").pnl_dollars.sum()
    gl = -df[df.pnl_dollars < 0].groupby("strategy").pnl_dollars.sum()
    agg["pf"] = (gw / gl).round(2)
    agg = agg.sort_values("total", ascending=False)
    print(agg[["n", "wr_pct", "total", "avg", "pf", "max_dd", "avg_hold"]].to_string())

    summary_csv = ROOT / "backtest_results" / "phoenix_1m_timeframe_summary.csv"
    agg.to_csv(summary_csv)

    print()
    print("=== Per-year ===")
    pivot = df.pivot_table(index="strategy", columns="year",
                            values="pnl_dollars", aggfunc="sum",
                            fill_value=0).round(0)
    print(pivot.to_string())


if __name__ == "__main__":
    main()
