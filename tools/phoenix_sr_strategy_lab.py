"""
Phoenix S/R Strategy Lab
========================

Tests whether SUPPORT/RESISTANCE zones detected by core/sr_zones.py
provide a tradeable edge on 5 years of MNQ data.

Six variants tested:
  sr_bounce_strict      strength >= 0.7, n_tests >= 3, any source
  sr_bounce_moderate    strength >= 0.5, n_tests >= 2, any source
  sr_bounce_loose       strength >= 0.3, n_tests >= 2, any source
  sr_bounce_round_only  source = "round" only, n_tests >= 1
  sr_bounce_vwap_dev    source = vwap_band_*, any tests
  sr_bounce_swing_only  source = "swing" only, n_tests >= 2

Entry logic (per variant):
  At each 5m bar close during 08:45-14:30 CT:
    - Detect S/R zones from rolling 5m window (~300 bars = ~25 hours)
    - Find nearest qualifying zone within 4-12 ticks of current price
    - Require a REJECTION CANDLE confirmation:
        SUPPORT bounce LONG: bar.low <= zone + 4t, bar.close >= zone + 1t,
                              lower wick >= 30% of range
        RESISTANCE rejection SHORT: bar.high >= zone - 4t, bar.close <= zone - 1t,
                                     upper wick >= 30% of range
    - Stop: 2-4 ticks beyond the zone (zone.width_ticks + 2)
    - Target: 2R fixed (per Phase 13 Section U findings)
  Once per zone per day. Max 4 trades per day.

Output files:
  backtest_results/phoenix_sr_strategy_lab.csv         — every trade
  backtest_results/phoenix_sr_strategy_summary.csv     — per-variant aggregates
  stdout — formatted summary
"""
from __future__ import annotations

import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sr_zones import detect_sr_zones, SRZone, TICK  # noqa: E402
from tools.phoenix_real_backtest import CSVEnrichmentPipeline, simulate_trade  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("sr_lab")
logger.setLevel(logging.INFO)

_CT = ZoneInfo("America/Chicago")
TICK_VALUE = 0.50

# Run S/R zone redetection every N 5m bars (every 5min × 1 = 5min;
# every 5min × 3 = 15min). Recomputing every bar is expensive; every 3 bars
# is a good balance.
ZONE_RECOMPUTE_EVERY_N_5M_BARS = 3

MAX_TRADES_PER_DAY = 4


# ════════════════════════════════════════════════════════════════════
# Variant configs
# ════════════════════════════════════════════════════════════════════

@dataclass
class VariantConfig:
    name: str
    min_strength: float
    min_tests: int
    source_filter: Optional[set] = None  # if set, only fire on these sources

VARIANTS: list[VariantConfig] = [
    VariantConfig("sr_bounce_strict",    min_strength=0.70, min_tests=3),
    VariantConfig("sr_bounce_moderate",  min_strength=0.50, min_tests=2),
    VariantConfig("sr_bounce_loose",     min_strength=0.30, min_tests=2),
    VariantConfig("sr_bounce_round_only", min_strength=0.0,  min_tests=1,
                  source_filter={"round"}),
    VariantConfig("sr_bounce_vwap_dev",  min_strength=0.0,  min_tests=0,
                  source_filter={"vwap_band_upper", "vwap_band_lower"}),
    VariantConfig("sr_bounce_swing_only", min_strength=0.30, min_tests=2,
                  source_filter={"swing"}),
]


# ════════════════════════════════════════════════════════════════════
# State
# ════════════════════════════════════════════════════════════════════

@dataclass
class LabState:
    # Per-variant tracking
    active: dict = field(default_factory=dict)               # variant_name -> exit_ts
    trades_today: dict = field(default_factory=lambda: defaultdict(int))  # (variant, date) -> count
    fired_zone: dict = field(default_factory=dict)           # (variant, date, zone_bucket_price) -> True

    # Cached zones (recomputed every N 5m bars).
    # Track by the LAST BAR's end_time (epoch seconds) — using deque length
    # is BROKEN because bars_5m is a maxlen=200 deque (saturates).
    cached_zones: list = field(default_factory=list)
    last_zone_compute_bar_ts: float = -1.0


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _ct(ts: pd.Timestamp) -> datetime:
    return ts.tz_convert(_CT).to_pydatetime()


def _in_window(now_ct: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = map(int, start_hhmm.split(":"))
    eh, em = map(int, end_hhmm.split(":"))
    t = now_ct.time()
    return dtime(sh, sm) <= t < dtime(eh, em)


def _is_5m_close(now_ct: datetime) -> bool:
    """5m bars close at :00, :05, :10, ..., :55."""
    return now_ct.minute % 5 == 0


def _zone_bucket(price: float, bucket_ticks: int = 8) -> int:
    """Round price into a bucket so that close-by zones share a key
    (prevents re-firing the 'same' zone twice in a day)."""
    return int(round(price / (bucket_ticks * TICK)))


def _qualifies(z: SRZone, cfg: VariantConfig) -> bool:
    if z.strength < cfg.min_strength:
        return False
    if z.n_tests < cfg.min_tests:
        return False
    if cfg.source_filter and z.source not in cfg.source_filter:
        return False
    return True


def _evaluate_variant(
    cfg: VariantConfig,
    eval_ts: pd.Timestamp,
    now_ct: datetime,
    current_bar,
    current_price: float,
    zones: list,
    state: LabState,
):
    """Return a trade signal dict if this variant fires, else None.

    Signal dict: {direction, entry_price, stop_price, target_price, note}
    """
    # Window
    if not _in_window(now_ct, "08:45", "14:30"):
        return None
    # Daily cap
    date_str = now_ct.strftime("%Y-%m-%d")
    if state.trades_today[(cfg.name, date_str)] >= MAX_TRADES_PER_DAY:
        return None

    # Need a real candle to evaluate rejection
    o = float(getattr(current_bar, "open", 0))
    h = float(getattr(current_bar, "high", 0))
    l = float(getattr(current_bar, "low", 0))
    c = float(getattr(current_bar, "close", 0))
    rng = h - l
    if rng < 2 * TICK:
        return None

    # Find nearest qualifying support BELOW price + resistance ABOVE
    proximity = 8 * TICK

    # SUPPORT: price came down to a zone and bounced
    best_sup = None
    best_sup_dist = float("inf")
    for z in zones:
        if not _qualifies(z, cfg):
            continue
        if z.type != "support":
            continue
        # Must be at or below current price (we're testing it)
        if z.price > current_price + proximity:
            continue
        d = abs(current_price - z.price)
        if d <= 12 * TICK and d < best_sup_dist:
            best_sup = z
            best_sup_dist = d

    best_res = None
    best_res_dist = float("inf")
    for z in zones:
        if not _qualifies(z, cfg):
            continue
        if z.type != "resistance":
            continue
        if z.price < current_price - proximity:
            continue
        d = abs(z.price - current_price)
        if d <= 12 * TICK and d < best_res_dist:
            best_res = z
            best_res_dist = d

    signal = None

    # LONG: bounce off support
    if best_sup is not None:
        zone_price = best_sup.price
        # Dedup per zone per day
        zb = _zone_bucket(zone_price)
        if state.fired_zone.get((cfg.name, date_str, zb)) is True:
            best_sup = None

    if best_sup is not None:
        zone_price = best_sup.price
        # Rejection candle: bar dipped to/below zone, closed above zone,
        # lower wick >= 30% of range
        dipped = l <= zone_price + 4 * TICK
        closed_above = c >= zone_price + TICK
        lower_wick = (min(o, c) - l) / rng
        if dipped and closed_above and lower_wick >= 0.30:
            # Stop: 2-4 ticks below zone (zone width + 2)
            stop_buffer = max(2, best_sup.width_ticks + 2)
            stop_price = zone_price - stop_buffer * TICK
            stop_dist = current_price - stop_price
            if 4 * TICK <= stop_dist <= 30 * TICK:
                target_price = current_price + stop_dist * 2.0
                signal = {
                    "direction": "LONG",
                    "entry_price": current_price,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "note": (f"SUP {best_sup.source} px={zone_price:.2f} "
                              f"s={best_sup.strength:.2f} n={best_sup.n_tests} "
                              f"wick={lower_wick:.0%}"),
                    "zone_price": zone_price,
                }

    # SHORT: rejection at resistance (only if no LONG fired)
    if signal is None and best_res is not None:
        zone_price = best_res.price
        zb = _zone_bucket(zone_price)
        if state.fired_zone.get((cfg.name, date_str, zb)) is True:
            best_res = None

    if signal is None and best_res is not None:
        zone_price = best_res.price
        broke_into = h >= zone_price - 4 * TICK
        closed_below = c <= zone_price - TICK
        upper_wick = (h - max(o, c)) / rng
        if broke_into and closed_below and upper_wick >= 0.30:
            stop_buffer = max(2, best_res.width_ticks + 2)
            stop_price = zone_price + stop_buffer * TICK
            stop_dist = stop_price - current_price
            if 4 * TICK <= stop_dist <= 30 * TICK:
                target_price = current_price - stop_dist * 2.0
                signal = {
                    "direction": "SHORT",
                    "entry_price": current_price,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "note": (f"RES {best_res.source} px={zone_price:.2f} "
                              f"s={best_res.strength:.2f} n={best_res.n_tests} "
                              f"wick={upper_wick:.0%}"),
                    "zone_price": zone_price,
                }

    return signal


# ════════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════════

def main():
    data_dir = ROOT / "data" / "historical"
    logger.info("[main] Loading pipeline (5 years)")
    pipeline = CSVEnrichmentPipeline(
        mnq_1m_csv=str(data_dir / "mnq_1min_databento.csv"),
        mnq_5m_csv=str(data_dir / "mnq_5min_databento.csv"),
        mes_1m_csv=None,
        mes_5m_csv=None,
        start="2021-05-17", end="2026-05-17",
    )

    mnq_1m_df = pipeline.mnq_1m_df.copy()
    state = LabState()
    trades: list[dict] = []
    cycle_count = 0
    signal_count: dict[str, int] = {v.name: 0 for v in VARIANTS}
    eval_count = 0  # only counts 5m close evals
    t0 = time.time()

    for eval_ts, market, bars_1m, bars_5m, session_info in pipeline.iter_eval_cycles():
        cycle_count += 1
        if cycle_count < 300:
            continue

        now_ct = _ct(eval_ts)
        # Only evaluate at 5m bar closes
        if not _is_5m_close(now_ct):
            # Still need to clear active trades
            for v in VARIANTS:
                act = state.active.get(v.name)
                if act is not None and act.get("exit_ts") is not None \
                        and eval_ts >= act["exit_ts"]:
                    state.active[v.name] = None
            continue

        # Time gate (do this BEFORE expensive zone recompute)
        if not _in_window(now_ct, "08:45", "14:30"):
            for v in VARIANTS:
                act = state.active.get(v.name)
                if act is not None and act.get("exit_ts") is not None \
                        and eval_ts >= act["exit_ts"]:
                    state.active[v.name] = None
            continue

        # Recompute zones occasionally (expensive).
        # Use the last 5m bar's end_time as the freshness key — `len(bars_5m)`
        # saturates at 200 (deque maxlen) and stops growing after warmup.
        last_5m_end_time = (
            float(getattr(bars_5m[-1], "end_time", 0.0)) if bars_5m else 0.0
        )
        # Recompute every N*300s = 15 minutes when ZONE_RECOMPUTE_EVERY_N_5M_BARS=3
        recompute_interval_s = ZONE_RECOMPUTE_EVERY_N_5M_BARS * 300.0
        if last_5m_end_time - state.last_zone_compute_bar_ts >= recompute_interval_s:
            current_price = float(market.get("price") or 0)
            state.cached_zones = detect_sr_zones(
                bars_5m=bars_5m,
                current_price=current_price,
                lookback_bars=300,
                prior_day_high=market.get("prior_day_high"),
                prior_day_low=market.get("prior_day_low"),
                prior_day_poc=market.get("prior_day_poc"),
                vwap=market.get("vwap"),
                vwap_std=market.get("vwap_std"),
            )
            state.last_zone_compute_bar_ts = last_5m_end_time

        zones = state.cached_zones
        if not zones:
            continue

        current_bar = bars_5m[-1] if bars_5m else None
        if current_bar is None:
            continue
        current_price = float(getattr(current_bar, "close", market.get("price") or 0))
        date_str = now_ct.strftime("%Y-%m-%d")
        eval_count += 1

        for v in VARIANTS:
            # Clear stale active position first
            act = state.active.get(v.name)
            if act is not None and act.get("exit_ts") is not None \
                    and eval_ts >= act["exit_ts"]:
                state.active[v.name] = None
            if state.active.get(v.name) is not None:
                continue

            sig = _evaluate_variant(v, eval_ts, now_ct, current_bar,
                                     current_price, zones, state)
            if sig is None:
                continue

            signal_count[v.name] += 1
            tr = simulate_trade(
                signal_strategy=v.name,
                signal_direction=sig["direction"],
                entry_ts=eval_ts,
                entry_price=sig["entry_price"],
                stop_price=sig["stop_price"],
                target_price=sig["target_price"],
                mnq_1m_df=mnq_1m_df,
            )
            state.active[v.name] = {"exit_ts": tr.exit_ts}
            state.trades_today[(v.name, date_str)] += 1
            zb = _zone_bucket(sig["zone_price"])
            state.fired_zone[(v.name, date_str, zb)] = True

            trades.append({
                "strategy": v.name,
                "direction": sig["direction"],
                "entry_ts": eval_ts,
                "entry_price": sig["entry_price"],
                "stop_price": sig["stop_price"],
                "target_price": sig["target_price"],
                "exit_ts": tr.exit_ts,
                "exit_price": tr.exit_price,
                "exit_reason": tr.exit_reason,
                "pnl_dollars": tr.pnl_dollars,
                "pnl_ticks": tr.pnl_ticks,
                "hold_min": tr.hold_min,
                "year": eval_ts.year,
                "hour_ct": now_ct.hour,
                "note": sig["note"],
            })

        if cycle_count % 100_000 == 0:
            elapsed = time.time() - t0
            logger.info(
                f"[runner] cycle={cycle_count:,} ts={eval_ts} "
                f"trades={len(trades)} elapsed={elapsed:.0f}s"
            )

    elapsed = time.time() - t0
    logger.info(
        f"[main] {cycle_count:,} cycles ({eval_count:,} 5m evals in window) "
        f"in {elapsed:.0f}s. Total trades: {len(trades)}, by variant: {signal_count}"
    )

    if not trades:
        print("(no trades generated)")
        return

    df = pd.DataFrame(trades)
    out_csv = ROOT / "backtest_results" / "phoenix_sr_strategy_lab.csv"
    out_csv.parent.mkdir(exist_ok=True)
    df.to_csv(out_csv, index=False)
    logger.info(f"[main] wrote {len(df)} trades to {out_csv}")

    # ── Summary ─────────────────────────────────────────────────
    print()
    print("=" * 110)
    print("PHOENIX S/R STRATEGY LAB — 5 YEAR BACKTEST  (2021-05-17 -> 2026-05-17)")
    print("=" * 110)
    print()
    print(f"Total trades:        {len(df)}")
    print(f"Total P&L:           ${df.pnl_dollars.sum():+,.0f}")
    print(f"Distinct years:      {sorted(df.year.unique())}")
    print()

    print("=== Per-variant ===")
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

    # Wilson 95% CI for win rate
    def wilson_lower(wins, n, z=1.96):
        if n == 0:
            return 0.0
        p = wins / n
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
        return round((center - margin) * 100, 1)

    agg["wr_wilson_low"] = [
        wilson_lower(int(row.wins), int(row.n))
        for row in agg.itertuples()
    ]
    agg = agg.sort_values("total", ascending=False)
    print(agg[["n", "wr_pct", "wr_wilson_low", "total", "avg", "pf", "max_dd",
               "avg_hold"]].to_string())

    summary_csv = ROOT / "backtest_results" / "phoenix_sr_strategy_summary.csv"
    agg.to_csv(summary_csv)
    logger.info(f"[main] wrote summary to {summary_csv}")

    print()
    print("=== Per-variant x per-year (P&L $) ===")
    pivot = df.pivot_table(
        index="strategy", columns="year", values="pnl_dollars",
        aggfunc="sum", fill_value=0,
    ).round(0).astype(int)
    print(pivot.to_string())

    print()
    print("=== Trade counts per year ===")
    pivot_n = df.pivot_table(
        index="strategy", columns="year", values="pnl_dollars",
        aggfunc="count", fill_value=0,
    ).astype(int)
    print(pivot_n.to_string())


if __name__ == "__main__":
    main()
