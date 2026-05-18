"""
Phoenix Trend-Pullback Strategy Lab
====================================

Tests Linda Raschke's 20-EMA pullback setup — the research-validated
intraday futures setup that trades WITH the trend back to the moving
average (NOT counter-trend mean-reversion).

Setup (canonical Raschke):
  1. TREND FILTER: ADX(14) > 30, or proxy: EMA21 vs EMA50 spread > N*ATR
  2. PULLBACK: price touches EMA reference in trend direction
  3. ENTRY: 5m close breaks high (LONG) / low (SHORT) of pullback bar
  4. STOP: pullback bar's opposite extreme (or 1*ATR)
  5. TARGET: 2R (or 1.5R/3R variants)

Why this should work where pure mean-rev failed:
  - Trades WITH NQ's structural trend bias (not against it)
  - Uses EMA as a "magnetic" pullback target, not a fade reference
  - Research-validated by Raschke, Macro-Ops, multiple sources

Variants tested (10 total):
  - raschke_baseline       (EMA21, ADX-proxy spread > 0.3*ATR, 2R)
  - raschke_strict_trend   (ADX-proxy spread > 0.5*ATR, 2R)
  - raschke_loose_trend    (just EMA9 > EMA21, 2R)
  - raschke_ema9_ref       (use EMA9 not EMA21 as pullback reference)
  - raschke_ema50_ref      (use EMA50, strict trend, 2R)
  - raschke_1.5r_target    (tighter target, expect higher WR)
  - raschke_3r_target      (wider target)
  - raschke_atr_stop       (1.0*ATR stop instead of pullback-bar)
  - raschke_short_only     (NQ trends down hard in bear regimes)
  - raschke_long_only      (NQ has long-bias drift)

Output:
  backtest_results/phoenix_trend_pullback_lab.csv
  backtest_results/phoenix_trend_pullback_summary.csv
"""
from __future__ import annotations

import logging
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import CSVEnrichmentPipeline, simulate_trade  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("trend_pullback_lab")
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
# MA state — tracks EMA9, EMA21, EMA50 from 5m bars
# ════════════════════════════════════════════════════════════════════
class MAState:
    def __init__(self):
        self.ema: dict[int, Optional[float]] = {9: None, 21: None, 50: None}
        self.alpha: dict[int, float] = {p: 2.0 / (p + 1) for p in (9, 21, 50)}
        self.last_5m_end_ts: Optional[float] = None
        self.bars_seen: int = 0

    def update(self, last_5m_bar) -> None:
        end_ts = float(getattr(last_5m_bar, "end_time", 0))
        if end_ts == 0 or end_ts == self.last_5m_end_ts:
            return
        self.last_5m_end_ts = end_ts
        close = float(getattr(last_5m_bar, "close", 0))
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
# Trend classifier (ADX proxy via EMA spread / ATR)
# ════════════════════════════════════════════════════════════════════
def trend_direction(ma: MAState, atr_5m: float, spread_threshold_atr: float,
                     long_only: bool = False, short_only: bool = False) -> Optional[str]:
    """Returns 'LONG' / 'SHORT' / None based on EMA21-EMA50 spread."""
    e21 = ma.get(21)
    e50 = ma.get(50)
    if e21 is None or e50 is None or atr_5m <= 0:
        return None
    spread = e21 - e50
    threshold = spread_threshold_atr * atr_5m
    if spread > threshold:
        return None if short_only else "LONG"
    if spread < -threshold:
        return None if long_only else "SHORT"
    return None  # In chop — no trend


def trend_direction_loose(ma: MAState, atr_5m: float) -> Optional[str]:
    """Just use EMA9 > EMA21 (loose — captures more setups)."""
    e9 = ma.get(9)
    e21 = ma.get(21)
    if e9 is None or e21 is None:
        return None
    if e9 > e21:
        return "LONG"
    if e9 < e21:
        return "SHORT"
    return None


# ════════════════════════════════════════════════════════════════════
# Pullback detection (look at last 3 5m bars for EMA touch)
# ════════════════════════════════════════════════════════════════════
def find_pullback_bar(bars_5m, ema_ref: float, direction: str, touch_buffer_ticks: int = 2):
    """Find the most recent 5m bar (in last 3) that touched EMA in trend direction.
    For LONG: bar.low <= ema_ref + buffer AND bar.close > ema_ref
    For SHORT: bar.high >= ema_ref - buffer AND bar.close < ema_ref."""
    if len(bars_5m) < 3:
        return None, None
    last_n = list(bars_5m)[-4:-1]  # 3 bars BEFORE the current bar (so we can break it)
    buffer = touch_buffer_ticks * TICK
    for idx in range(len(last_n) - 1, -1, -1):
        b = last_n[idx]
        bl = float(getattr(b, "low", 0))
        bh = float(getattr(b, "high", 0))
        bc = float(getattr(b, "close", 0))
        if direction == "LONG" and bl <= ema_ref + buffer and bc > ema_ref:
            return b, idx
        if direction == "SHORT" and bh >= ema_ref - buffer and bc < ema_ref:
            return b, idx
    return None, None


# ════════════════════════════════════════════════════════════════════
# Raschke evaluator
# ════════════════════════════════════════════════════════════════════
def eval_raschke(eval_ts, market, bars_5m, ma_state,
                  trend_spread_atr: float,
                  ema_ref_period: int,
                  target_rr: float,
                  stop_kind: str,  # "pullback" or "atr"
                  long_only: bool,
                  short_only: bool,
                  loose_trend: bool,
                  last_fire_ts: Optional[pd.Timestamp]
                  ) -> Optional[VariantSignal]:
    now_ct = _ct(eval_ts)
    if not _in_rth(now_ct):
        return None
    # Per-bar dedup
    if last_fire_ts == eval_ts:
        return None
    # Need enough bars
    if len(bars_5m) < 4:
        return None
    # Only evaluate on 5m bar close boundary (every 5 min)
    if now_ct.minute % 5 != 0:
        return None

    atr = float(market.get("atr_5m") or 0)
    if atr <= 0:
        return None

    # Trend filter
    if loose_trend:
        direction = trend_direction_loose(ma_state, atr)
    else:
        direction = trend_direction(ma_state, atr, trend_spread_atr,
                                      long_only=long_only, short_only=short_only)
    if direction is None:
        return None
    # Apply long_only/short_only even in loose mode
    if direction == "LONG" and short_only:
        return None
    if direction == "SHORT" and long_only:
        return None

    # EMA reference for pullback
    ema_ref = ma_state.get(ema_ref_period)
    if ema_ref is None:
        return None

    # Find pullback bar in last 3 5m bars (NOT including current)
    pullback_bar, pb_idx = find_pullback_bar(bars_5m, ema_ref, direction)
    if pullback_bar is None:
        return None

    pb_high = float(getattr(pullback_bar, "high", 0))
    pb_low = float(getattr(pullback_bar, "low", 0))

    # Current 5m bar (last in bars_5m) — must break pullback bar high/low
    current = bars_5m[-1]
    cc = float(getattr(current, "close", 0))
    if direction == "LONG":
        if cc <= pb_high + TICK:
            return None
    else:
        if cc >= pb_low - TICK:
            return None

    price = float(market.get("price") or cc)

    # Stop
    if stop_kind == "pullback":
        if direction == "LONG":
            stop = pb_low - TICK
            stop_dist = price - stop
        else:
            stop = pb_high + TICK
            stop_dist = stop - price
    else:  # atr
        if direction == "LONG":
            stop = price - 1.0 * atr
            stop_dist = price - stop
        else:
            stop = price + 1.0 * atr
            stop_dist = stop - price

    if stop_dist < 6 * TICK or stop_dist > 40 * TICK:
        return None

    # Target
    if direction == "LONG":
        target = price + stop_dist * target_rr
    else:
        target = price - stop_dist * target_rr

    return VariantSignal(direction, price, stop, target,
                         note=f"raschke {direction} pb={pb_high if direction=='LONG' else pb_low:.2f} ema={ema_ref:.2f}")


# ════════════════════════════════════════════════════════════════════
# Variants
# ════════════════════════════════════════════════════════════════════
# (name, trend_spread_atr, ema_ref, target_rr, stop_kind, long_only, short_only, loose_trend)
VARIANTS = [
    ("raschke_baseline",      0.3, 21, 2.0, "pullback", False, False, False),
    ("raschke_strict_trend",  0.5, 21, 2.0, "pullback", False, False, False),
    ("raschke_loose_trend",   0.0, 21, 2.0, "pullback", False, False, True),
    ("raschke_ema9_ref",      0.3,  9, 2.0, "pullback", False, False, False),
    ("raschke_ema50_ref",     0.5, 50, 2.0, "pullback", False, False, False),
    ("raschke_1.5r_target",   0.3, 21, 1.5, "pullback", False, False, False),
    ("raschke_3r_target",     0.3, 21, 3.0, "pullback", False, False, False),
    ("raschke_atr_stop",      0.3, 21, 2.0, "atr",      False, False, False),
    ("raschke_long_only",     0.3, 21, 2.0, "pullback", True,  False, False),
    ("raschke_short_only",    0.3, 21, 2.0, "pullback", False, True,  False),
]


# ════════════════════════════════════════════════════════════════════
# Main runner
# ════════════════════════════════════════════════════════════════════
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

    ma_state = MAState()
    active: dict[str, Optional[dict]] = {v[0]: None for v in VARIANTS}
    last_fire: dict[str, Optional[pd.Timestamp]] = {v[0]: None for v in VARIANTS}
    signal_count: dict[str, int] = {v[0]: 0 for v in VARIANTS}
    trades: list[dict] = []
    cycle_count = 0
    t0 = time.time()

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1
        if bars_5m:
            ma_state.update(bars_5m[-1])
        if cycle_count < 300:
            continue

        for v in VARIANTS:
            name = v[0]
            act = active[name]
            if act is not None:
                if act.get("exit_ts") is not None and eval_ts >= act["exit_ts"]:
                    active[name] = None
                else:
                    continue
            try:
                sig = eval_raschke(eval_ts, market, bars_5m, ma_state,
                                    trend_spread_atr=v[1], ema_ref_period=v[2],
                                    target_rr=v[3], stop_kind=v[4],
                                    long_only=v[5], short_only=v[6],
                                    loose_trend=v[7],
                                    last_fire_ts=last_fire[name])
            except Exception as e:
                logger.debug(f"{name} err {eval_ts}: {e!r}")
                continue
            if sig is None:
                continue
            signal_count[name] += 1
            last_fire[name] = eval_ts
            tr = simulate_trade(
                signal_strategy=name,
                signal_direction=sig.direction,
                entry_ts=eval_ts,
                entry_price=sig.entry_price,
                stop_price=sig.stop_price,
                target_price=sig.target_price,
                mnq_1m_df=mnq_1m_df,
            )
            active[name] = {"exit_ts": tr.exit_ts}
            trades.append({
                "strategy": name,
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
                f"Total trades: {len(trades)}, by variant: {signal_count}")

    df = pd.DataFrame(trades)
    out_csv = ROOT / "backtest_results" / "phoenix_trend_pullback_lab.csv"
    df.to_csv(out_csv, index=False)
    logger.info(f"[main] wrote {len(df)} trades to {out_csv}")

    if df.empty:
        print("(no trades)")
        return

    print()
    print("=" * 100)
    print("RASCHKE TREND-PULLBACK STRATEGY LAB — 5 YEAR BACKTEST")
    print("=" * 100)
    print()
    print(f"Total trades: {len(df):,}")
    print(f"Total P&L:    ${df.pnl_dollars.sum():,.0f}")
    print()
    print("=== Per-variant summary (sorted by total) ===")
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
    gross_win = df[df.pnl_dollars > 0].groupby("strategy").pnl_dollars.sum()
    gross_loss = -df[df.pnl_dollars < 0].groupby("strategy").pnl_dollars.sum()
    pf = (gross_win / gross_loss).round(2)
    agg["pf"] = pf
    agg = agg.sort_values("total", ascending=False)
    print(agg[["n", "wr_pct", "total", "avg", "pf", "max_dd", "avg_hold"]].to_string())

    summary_csv = ROOT / "backtest_results" / "phoenix_trend_pullback_summary.csv"
    agg.to_csv(summary_csv)
    logger.info(f"[main] wrote summary to {summary_csv}")

    print()
    print("=== Per-variant × per-year ===")
    pivot = df.pivot_table(index="strategy", columns="year",
                            values="pnl_dollars", aggfunc="sum",
                            fill_value=0).round(0)
    print(pivot.to_string())

    print()
    print("=== Per-variant × LONG vs SHORT ===")
    dir_pivot = df.pivot_table(index="strategy", columns="direction",
                                values="pnl_dollars", aggfunc=["count", "sum"],
                                fill_value=0).round(0)
    print(dir_pivot.to_string())


if __name__ == "__main__":
    main()
