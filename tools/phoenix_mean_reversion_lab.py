"""
Phoenix Mean-Reversion Strategy Lab
====================================

Parameter-sweep backtest for two new candidate strategy families:

  1. ATR REVERSAL — fade extreme z-score (price-VWAP)/ATR moves
     Variants: 3 timeframes (1m / 5m / 15m) × 3 z-thresholds (2.0/2.5/3.0)
              = 9 variants

  2. EMA REVERSION — fade extreme distance from moving average
     Variants: 4 MAs (EMA9, EMA21, EMA50, SMA20 — all 5m) × 2 ATR
              thresholds (1.5 / 2.0) = 8 variants

Total: 17 standalone variants run in a single pipeline pass.

Output:
  backtest_results/phoenix_mean_reversion_lab.csv         — every trade
  backtest_results/phoenix_mean_reversion_summary.csv     — per-variant summary
  stdout — formatted summary + ranking
"""
from __future__ import annotations

import logging
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.phoenix_real_backtest import CSVEnrichmentPipeline, simulate_trade  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("mean_rev_lab")
logger.setLevel(logging.INFO)

TICK = 0.25


def _ct(ts: pd.Timestamp) -> datetime:
    return ts.tz_convert("America/Chicago").to_pydatetime()


def _in_rth(now_ct: datetime) -> bool:
    """RTH window 08:30-15:00 CT. Mean-rev mostly fires during RTH."""
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
# Variant config
# ════════════════════════════════════════════════════════════════════
# ATR reversal: (name, atr_field, z_threshold)
# Z-score = (price - vwap) / atr  — measures how many ATRs price is from VWAP
ATR_REV_VARIANTS = [
    ("atr_rev_1m_z2.0",  "atr_1m",  2.0),
    ("atr_rev_1m_z2.5",  "atr_1m",  2.5),
    ("atr_rev_1m_z3.0",  "atr_1m",  3.0),
    ("atr_rev_5m_z2.0",  "atr_5m",  2.0),
    ("atr_rev_5m_z2.5",  "atr_5m",  2.5),
    ("atr_rev_5m_z3.0",  "atr_5m",  3.0),
    ("atr_rev_15m_z2.0", "atr_15m", 2.0),
    ("atr_rev_15m_z2.5", "atr_15m", 2.5),
    ("atr_rev_15m_z3.0", "atr_15m", 3.0),
]

# EMA reversion: (name, ma_kind, ma_period, atr_threshold)
# ma_kind: "ema" or "sma" — computed from 5m bars
EMA_REV_VARIANTS = [
    ("ema_rev_ema9_1.5atr",  "ema",  9,  1.5),
    ("ema_rev_ema9_2.0atr",  "ema",  9,  2.0),
    ("ema_rev_ema21_1.5atr", "ema", 21,  1.5),
    ("ema_rev_ema21_2.0atr", "ema", 21,  2.0),
    ("ema_rev_ema50_1.5atr", "ema", 50,  1.5),
    ("ema_rev_ema50_2.0atr", "ema", 50,  2.0),
    ("ema_rev_sma20_1.5atr", "sma", 20,  1.5),
    ("ema_rev_sma20_2.0atr", "sma", 20,  2.0),
]


# ════════════════════════════════════════════════════════════════════
# MA state tracker (incremental computation across bars)
# ════════════════════════════════════════════════════════════════════
class MAState:
    """Tracks EMA + SMA values for required periods, updated per 5m bar."""

    def __init__(self):
        # EMA state
        self.ema_values: dict[int, Optional[float]] = {9: None, 21: None, 50: None}
        self.ema_alphas: dict[int, float] = {p: 2.0 / (p + 1) for p in (9, 21, 50)}
        # SMA state — rolling window of last N closes
        self.sma_windows: dict[int, deque] = {20: deque(maxlen=20)}
        # Track which 5m bar end_time we've last consumed (avoid double-update)
        self.last_5m_end_ts: Optional[float] = None
        # Track total 5m bars seen — used for warmup
        self.bars_seen: int = 0

    def update(self, last_5m_bar) -> None:
        """Update MAs from the most recent 5m bar — but ONLY if this is a
        new bar (its end_time differs from last seen)."""
        end_ts = float(getattr(last_5m_bar, "end_time", 0))
        if end_ts == 0 or end_ts == self.last_5m_end_ts:
            return
        self.last_5m_end_ts = end_ts
        close = float(getattr(last_5m_bar, "close", 0))
        if close <= 0:
            return
        self.bars_seen += 1
        # EMA update
        for period, current in self.ema_values.items():
            alpha = self.ema_alphas[period]
            if current is None:
                self.ema_values[period] = close
            else:
                self.ema_values[period] = alpha * close + (1 - alpha) * current
        # SMA update
        for period, window in self.sma_windows.items():
            window.append(close)

    def get(self, kind: str, period: int) -> Optional[float]:
        if kind == "ema":
            val = self.ema_values.get(period)
            return val if (val is not None and self.bars_seen >= period) else None
        if kind == "sma":
            window = self.sma_windows.get(period)
            if window is None or len(window) < period:
                return None
            return sum(window) / len(window)
        return None


# ════════════════════════════════════════════════════════════════════
# ATR reversal evaluator
# ════════════════════════════════════════════════════════════════════
def eval_atr_reversal(eval_ts, market, atr_field: str, z_threshold: float,
                       active_dedup_ts: Optional[pd.Timestamp]) -> Optional[VariantSignal]:
    """Fire when |z| = |(price - vwap) / atr| > threshold during RTH.
    Direction = revert toward VWAP. Stop: 0.6 * atr beyond entry.
    Target: VWAP (the mean)."""
    now_ct = _ct(eval_ts)
    if not _in_rth(now_ct):
        return None
    if active_dedup_ts == eval_ts:
        return None
    price = float(market.get("price") or 0)
    vwap = float(market.get("vwap") or 0)
    atr = float(market.get(atr_field) or 0)
    if price <= 0 or vwap <= 0 or atr <= 0:
        return None
    distance = price - vwap
    z = distance / atr
    if abs(z) < z_threshold:
        return None
    # Direction: revert TOWARD vwap
    if z > 0:
        direction = "SHORT"
        stop_dist = atr * 0.6
        stop = price + stop_dist
        target = vwap
    else:
        direction = "LONG"
        stop_dist = atr * 0.6
        stop = price - stop_dist
        target = vwap
    # Sanity: stop must be 4-30 ticks
    if stop_dist < 4 * TICK or stop_dist > 30 * TICK:
        return None
    # Target dist must be > 0.5 * stop_dist (need positive RR)
    target_dist = abs(target - price)
    if target_dist < 0.5 * stop_dist:
        return None
    return VariantSignal(direction, price, stop, target,
                         note=f"atr_rev z={z:+.2f} atr={atr:.2f}")


# ════════════════════════════════════════════════════════════════════
# EMA reversion evaluator
# ════════════════════════════════════════════════════════════════════
def eval_ema_reversion(eval_ts, market, ma_state: MAState, ma_kind: str, ma_period: int,
                        atr_threshold: float,
                        active_dedup_ts: Optional[pd.Timestamp]) -> Optional[VariantSignal]:
    """Fire when |price - ma| > atr_threshold * atr_5m during RTH.
    Direction = revert toward MA. Stop: 0.5 * atr beyond entry.
    Target: MA itself."""
    now_ct = _ct(eval_ts)
    if not _in_rth(now_ct):
        return None
    if active_dedup_ts == eval_ts:
        return None
    price = float(market.get("price") or 0)
    atr = float(market.get("atr_5m") or 0)
    ma_val = ma_state.get(ma_kind, ma_period)
    if price <= 0 or atr <= 0 or ma_val is None:
        return None
    distance = price - ma_val
    threshold = atr_threshold * atr
    if abs(distance) < threshold:
        return None
    # Direction: revert toward MA
    if distance > 0:
        direction = "SHORT"
        stop_dist = atr * 0.5
        stop = price + stop_dist
        target = ma_val
    else:
        direction = "LONG"
        stop_dist = atr * 0.5
        stop = price - stop_dist
        target = ma_val
    if stop_dist < 4 * TICK or stop_dist > 30 * TICK:
        return None
    target_dist = abs(target - price)
    if target_dist < 0.5 * stop_dist:
        return None
    return VariantSignal(direction, price, stop, target,
                         note=f"ema_rev d={distance:+.2f} ma={ma_val:.2f}")


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

    # Per-variant state
    ma_state = MAState()  # shared across EMA variants
    active: dict[str, Optional[dict]] = {}      # variant -> {exit_ts, entry_ts}
    last_signal_ts: dict[str, Optional[pd.Timestamp]] = {}
    signal_count: dict[str, int] = {}
    trades: list[dict] = []

    all_variants = (
        [("atr_rev", name, atr_field, z) for name, atr_field, z in ATR_REV_VARIANTS]
        + [("ema_rev", name, ma_kind, ma_period, atr_thresh)
           for name, ma_kind, ma_period, atr_thresh in EMA_REV_VARIANTS]
    )
    for v in all_variants:
        name = v[1]
        active[name] = None
        last_signal_ts[name] = None
        signal_count[name] = 0

    cycle_count = 0
    t0 = time.time()

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1

        # Update MA state from the latest 5m bar (incremental, only on new bar)
        if bars_5m:
            ma_state.update(bars_5m[-1])

        if cycle_count < 300:  # warmup
            continue

        # Evaluate each variant
        for v in all_variants:
            kind = v[0]
            name = v[1]
            act = active[name]
            if act is not None:
                if act.get("exit_ts") is not None and eval_ts >= act["exit_ts"]:
                    active[name] = None
                else:
                    continue

            try:
                if kind == "atr_rev":
                    _, _, atr_field, z = v
                    sig = eval_atr_reversal(eval_ts, market, atr_field, z,
                                             last_signal_ts[name])
                else:  # ema_rev
                    _, _, ma_kind, ma_period, atr_thresh = v
                    sig = eval_ema_reversion(eval_ts, market, ma_state, ma_kind,
                                              ma_period, atr_thresh,
                                              last_signal_ts[name])
            except Exception as e:
                logger.debug(f"{name} err {eval_ts}: {e!r}")
                continue

            if sig is None:
                continue

            signal_count[name] += 1
            last_signal_ts[name] = eval_ts

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
                "kind": kind,
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
    out_csv = ROOT / "backtest_results" / "phoenix_mean_reversion_lab.csv"
    df.to_csv(out_csv, index=False)
    logger.info(f"[main] wrote {len(df)} trades to {out_csv}")

    if df.empty:
        print("(no trades)")
        return

    print()
    print("=" * 100)
    print("MEAN-REVERSION STRATEGY LAB — 5 YEAR BACKTEST")
    print("=" * 100)
    print()
    print(f"Total trades: {len(df):,}")
    print(f"Total P&L:    ${df.pnl_dollars.sum():,.0f}")
    print()
    print("=== Per-variant summary ===")
    agg = df.groupby(["kind", "strategy"]).agg(
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
    agg = agg.reset_index()
    agg["pf"] = agg.strategy.map(pf).fillna(0)
    agg = agg.sort_values("total", ascending=False)
    print(agg[["kind", "strategy", "n", "wr_pct", "total", "avg",
                "pf", "max_dd", "avg_hold"]].to_string(index=False))

    summary_csv = ROOT / "backtest_results" / "phoenix_mean_reversion_summary.csv"
    agg.to_csv(summary_csv, index=False)
    logger.info(f"[main] wrote summary to {summary_csv}")

    print()
    print("=== Per-variant × per-year ===")
    pivot = df.pivot_table(index="strategy", columns="year",
                           values="pnl_dollars", aggfunc="sum",
                           fill_value=0).round(0)
    print(pivot.to_string())

    print()
    print("=== TOP 5 by Total $ ===")
    top5 = agg.nlargest(5, "total")
    print(top5[["kind", "strategy", "n", "wr_pct", "total",
                 "pf", "max_dd"]].to_string(index=False))

    print()
    print("=== TOP 5 by PF (risk-adjusted) ===")
    top5_pf = agg[agg.n >= 30].nlargest(5, "pf")
    print(top5_pf[["kind", "strategy", "n", "wr_pct", "total",
                    "pf", "max_dd"]].to_string(index=False))


if __name__ == "__main__":
    main()
