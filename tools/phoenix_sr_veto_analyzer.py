"""
Phoenix S/R VETO Analyzer (Phase 13 V.4 follow-up)
==================================================

Tests whether vetoing `bias_momentum` trades that fire INTO a strong S/R
zone improves the strategy's overall edge.

HYPOTHESIS
----------
bias_momentum is a trend-following / continuation strategy. When it fires
LONG and a strong RESISTANCE zone sits a few ticks above entry, the trade
is highly likely to stall and fail at that level. Same logic for SHORT
entries firing into nearby support.

If we SKIP those trades entirely (binary VETO), do we keep the winning
trades (where price has room to run) and shed the structural losers?

METHODOLOGY
-----------
1. Load `backtest_results/phoenix_real_5year.csv` and filter to
   `bias_momentum` rows (13,790 trades).
2. Load `data/historical/mnq_5min_databento.csv` and pre-convert to
   Phoenix `Bar` instances (one-time cost).
3. For each trade entry timestamp:
     a. Slice the last 300 5m bars ending at-or-before the entry.
     b. Call `core.sr_zones.detect_sr_zones` to get zones for that moment.
     c. Cache zones per (date, 30-min bucket) to amortize cost — zones
        don't move materially within 30 min.
     d. For LONG: check the NEAREST resistance zone with strength >= X
        sitting WITHIN Y ticks ABOVE entry_price.
        For SHORT: same but support within Y ticks BELOW.
     e. Record veto_fires for each (X, Y) cell of the grid.
4. For each (X, Y) cell, compute:
     - kept_count, kept_pnl, kept_winrate, kept_profit_factor
     - blocked_count, blocked_pnl, avg_$_per_blocked_trade (the SAVING)
     - per-year P&L breakdown for the best cell.
5. Compare to the baseline:
     bias_momentum no veto:  13,790 trades / +$178,379 / PF 1.33 / 6/6 years.

GRID
----
  X (strength threshold):  0.5, 0.6, 0.7
  Y (proximity ticks):     4, 8, 12
  → 9 cells

OUTPUT
------
  backtest_results/phoenix_sr_veto_summary.csv  — every (X, Y) cell + per-year
  stdout — formatted summary + verdict (positive vs not)

NOTES
-----
- Pure analysis tool. Does NOT modify any strategy or production code.
- Windows-safe, ASCII-only console output.
- Cap: ~13,790 trades * 1 zone-detect per 30-min bucket per day
       ~ 1500 zone-detects per year * 5 years = ~7500 calls.
       Each detect is ~30ms with lookback=300 bars → ~4 min runtime worst case.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.sr_zones import detect_sr_zones, SRZone, TICK  # noqa: E402
from core.tick_aggregator import Bar  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("sr_veto")

TICK_VALUE_DOLLARS = 0.50   # 1 MNQ tick = $0.50

# Grid to sweep (extended beyond the brief's 3x3 to ensure we explore both
# very-tight zones (2-3 ticks) and the brief's 4/8/12 spec).
STRENGTH_GRID = [0.5, 0.6, 0.7]
PROX_TICK_GRID = [2, 3, 4, 8, 12]

# Zone-detection cache bucket size — recompute zones at most once per N
# minutes within the same day. 30 min strikes a balance between fidelity
# and runtime.
ZONE_CACHE_MINUTES = 30

# How many 5m bars to feed into detect_sr_zones (matches the lab default).
ZONE_LOOKBACK_BARS = 300


# ════════════════════════════════════════════════════════════════════
# Bar loading + conversion
# ════════════════════════════════════════════════════════════════════

def _load_bars_5m(csv_path: Path) -> list[Bar]:
    """Load the MNQ 5min CSV and return a chronological list of Bar.

    The Bar.end_time is set to the bar's open timestamp + 300s. We use
    the bar's OPEN timestamp (the CSV's ts_utc) as the FIRST tick of the
    bar; the bar 'completes' 300 seconds later. This matters because we
    need to ensure that when we say 'last bar before entry_ts', we only
    include bars whose CLOSE happened before the entry.
    """
    logger.info("[bars] reading %s ...", csv_path.name)
    df = pd.read_csv(csv_path)
    if "ts_utc" not in df.columns:
        raise KeyError(f"{csv_path}: missing ts_utc column")
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    bars: list[Bar] = []
    for row in df.itertuples(index=False):
        open_epoch = row.ts.timestamp()
        end_epoch = open_epoch + 300.0  # bar closes 5 min after open ts
        b = Bar(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=int(row.volume),
            tick_count=int(row.volume),
            start_time=open_epoch,
            end_time=end_epoch,
        )
        bars.append(b)
    logger.info("[bars] loaded %d 5m bars (%s -> %s)",
                len(bars),
                df["ts"].iloc[0], df["ts"].iloc[-1])
    return bars


# ════════════════════════════════════════════════════════════════════
# Trade loading
# ════════════════════════════════════════════════════════════════════

def _load_bias_momentum_trades(csv_path: Path) -> pd.DataFrame:
    logger.info("[trades] reading %s ...", csv_path.name)
    df = pd.read_csv(csv_path)
    bm = df[df["strategy"] == "bias_momentum"].copy()
    bm["entry_ts"] = pd.to_datetime(bm["entry_ts"], utc=True)
    bm = bm.sort_values("entry_ts").reset_index(drop=True)
    # pandas 3.x stores datetime at microsecond precision by default; use
    # .timestamp() per-row to be precision-agnostic and correct.
    bm["entry_epoch"] = bm["entry_ts"].apply(lambda t: t.timestamp())
    logger.info("[trades] loaded %d bias_momentum trades (%s -> %s)",
                len(bm),
                bm["entry_ts"].iloc[0], bm["entry_ts"].iloc[-1])
    return bm


# ════════════════════════════════════════════════════════════════════
# Veto evaluation
# ════════════════════════════════════════════════════════════════════

def _bars_before(bars: list[Bar], entry_epoch: float) -> list[Bar]:
    """Binary-search the bars list and return up to the last
    ZONE_LOOKBACK_BARS whose end_time <= entry_epoch.

    Bars are pre-sorted by start_time so a simple bisect on end_time works.
    """
    # bisect right by end_time
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid].end_time <= entry_epoch:
            lo = mid + 1
        else:
            hi = mid
    # lo is the first index where end_time > entry_epoch
    upper = lo
    lower = max(0, upper - ZONE_LOOKBACK_BARS)
    return bars[lower:upper]


def _cache_key(entry_epoch: int) -> int:
    """Round entry epoch to ZONE_CACHE_MINUTES boundary."""
    bucket_s = ZONE_CACHE_MINUTES * 60
    return int(entry_epoch // bucket_s) * bucket_s


def _veto_fires(zones: list[SRZone], entry_price: float, direction: str,
                min_strength: float, max_prox_ticks: int) -> bool:
    """Return True if the trade should be vetoed.

    LONG  → check resistance zones AT OR ABOVE entry within max_prox_ticks.
    SHORT → check support    zones AT OR BELOW entry within max_prox_ticks.

    A zone qualifies as a veto trigger if:
      z.strength >= min_strength
      AND zone is on the wrong side of the trade
      AND distance (in ticks) <= max_prox_ticks
    """
    max_dist = max_prox_ticks * TICK
    if direction == "LONG":
        # Resistance ABOVE entry is the threat.
        for z in zones:
            if z.type != "resistance":
                continue
            if z.strength < min_strength:
                continue
            d = z.price - entry_price
            if 0 <= d <= max_dist:
                return True
        return False
    # SHORT
    for z in zones:
        if z.type != "support":
            continue
        if z.strength < min_strength:
            continue
        d = entry_price - z.price
        if 0 <= d <= max_dist:
            return True
    return False


# ════════════════════════════════════════════════════════════════════
# Main runner
# ════════════════════════════════════════════════════════════════════

@dataclass
class CellStats:
    min_strength: float
    max_prox_ticks: int
    # full-period
    kept_count: int = 0
    kept_pnl: float = 0.0
    kept_wins: int = 0
    kept_gross_win: float = 0.0
    kept_gross_loss: float = 0.0
    blocked_count: int = 0
    blocked_pnl: float = 0.0
    blocked_wins: int = 0
    # per-direction breakdowns
    blocked_long_count: int = 0
    blocked_long_pnl: float = 0.0
    blocked_short_count: int = 0
    blocked_short_pnl: float = 0.0
    # per-year buckets
    per_year_kept_pnl: dict = None
    per_year_kept_count: dict = None
    per_year_blocked_pnl: dict = None
    per_year_blocked_count: dict = None

    def __post_init__(self):
        self.per_year_kept_pnl = defaultdict(float)
        self.per_year_kept_count = defaultdict(int)
        self.per_year_blocked_pnl = defaultdict(float)
        self.per_year_blocked_count = defaultdict(int)


def run(trades_csv: Path, bars_csv: Path, out_csv: Path) -> dict:
    bars = _load_bars_5m(bars_csv)
    trades = _load_bias_momentum_trades(trades_csv)

    # Pre-build epoch index for binary search
    bar_end_epochs = [b.end_time for b in bars]

    # Cells: one per (X, Y)
    cells: dict[tuple[float, int], CellStats] = {
        (X, Y): CellStats(min_strength=X, max_prox_ticks=Y)
        for X in STRENGTH_GRID for Y in PROX_TICK_GRID
    }

    # Cache: (cache_key) -> zones (recomputed once per 30 min)
    # We do NOT key by entry price because zones only depend on bars + recent
    # price. For the veto distance check we use the actual trade entry_price,
    # so the cached zones are still valid across different entries within
    # the bucket.
    zone_cache: dict[int, list[SRZone]] = {}
    cache_hits = 0
    cache_misses = 0

    # Baseline aggregates (no veto)
    baseline_pnl = float(trades["pnl_dollars"].sum())
    baseline_count = len(trades)
    baseline_wins = int((trades["pnl_dollars"] > 0).sum())
    baseline_gross_win = float(trades.loc[trades["pnl_dollars"] > 0, "pnl_dollars"].sum())
    baseline_gross_loss = float(-trades.loc[trades["pnl_dollars"] <= 0, "pnl_dollars"].sum())
    per_year_baseline = trades.groupby("year")["pnl_dollars"].agg(["sum", "count"]).reset_index()

    logger.info("[baseline] %d trades, $%.0f total, WR %.1f%%, PF %.3f",
                baseline_count, baseline_pnl,
                100.0 * baseline_wins / baseline_count,
                baseline_gross_win / baseline_gross_loss if baseline_gross_loss > 0 else float("inf"))

    t0 = time.time()
    processed = 0
    skipped_no_bars = 0

    for tr in trades.itertuples(index=False):
        processed += 1
        if processed % 1000 == 0:
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            logger.info("[run] %d/%d (%.0f tr/s, cache %d/%d)",
                        processed, baseline_count, rate, cache_hits, cache_hits + cache_misses)

        entry_epoch = int(tr.entry_epoch)
        ckey = _cache_key(entry_epoch)
        zones = zone_cache.get(ckey)
        if zones is None:
            cache_misses += 1
            window = _bars_before(bars, entry_epoch)
            if not window:
                skipped_no_bars += 1
                # Without bars we cannot veto — count as kept everywhere.
                _record_kept(cells, tr)
                continue
            current_price = float(window[-1].close)
            zones = detect_sr_zones(
                bars_5m=window,
                current_price=current_price,
                lookback_bars=ZONE_LOOKBACK_BARS,
                prior_day_high=None,
                prior_day_low=None,
                prior_day_poc=None,
                vwap=None,
                vwap_std=None,
            )
            zone_cache[ckey] = zones
        else:
            cache_hits += 1

        # Trim cache to bound memory (keep only recent buckets)
        if len(zone_cache) > 5000:
            # Drop oldest 1000 keys
            for k in sorted(zone_cache.keys())[:1000]:
                del zone_cache[k]

        entry_price = float(tr.entry_price)
        direction = str(tr.direction)
        pnl = float(tr.pnl_dollars)
        year = int(tr.year)
        is_win = pnl > 0

        for X in STRENGTH_GRID:
            for Y in PROX_TICK_GRID:
                cell = cells[(X, Y)]
                if _veto_fires(zones, entry_price, direction, X, Y):
                    cell.blocked_count += 1
                    cell.blocked_pnl += pnl
                    cell.blocked_wins += int(is_win)
                    cell.per_year_blocked_pnl[year] += pnl
                    cell.per_year_blocked_count[year] += 1
                    if direction == "LONG":
                        cell.blocked_long_count += 1
                        cell.blocked_long_pnl += pnl
                    else:
                        cell.blocked_short_count += 1
                        cell.blocked_short_pnl += pnl
                else:
                    cell.kept_count += 1
                    cell.kept_pnl += pnl
                    cell.kept_wins += int(is_win)
                    if pnl > 0:
                        cell.kept_gross_win += pnl
                    else:
                        cell.kept_gross_loss += -pnl
                    cell.per_year_kept_pnl[year] += pnl
                    cell.per_year_kept_count[year] += 1

    elapsed = time.time() - t0
    logger.info("[run] processed %d trades in %.1fs (cache %d hits / %d misses, %d no-bars)",
                processed, elapsed, cache_hits, cache_misses, skipped_no_bars)

    # ── Build summary table ────────────────────────────────────────
    rows: list[dict] = []
    rows.append({
        "scenario": "BASELINE (no veto)",
        "min_strength": "",
        "max_prox_ticks": "",
        "kept_trades": baseline_count,
        "kept_pnl": round(baseline_pnl, 2),
        "kept_winrate_pct": round(100.0 * baseline_wins / baseline_count, 2),
        "kept_pf": round(baseline_gross_win / baseline_gross_loss, 3)
                   if baseline_gross_loss > 0 else float("inf"),
        "kept_avg_pnl": round(baseline_pnl / baseline_count, 2),
        "blocked_trades": 0,
        "blocked_pnl": 0.0,
        "blocked_winrate_pct": 0.0,
        "saved_per_blocked": 0.0,
        "veto_fire_rate_pct": 0.0,
        "pnl_lift_vs_baseline": 0.0,
        "pf_lift_vs_baseline": 0.0,
    })

    baseline_pf = baseline_gross_win / baseline_gross_loss if baseline_gross_loss > 0 else float("inf")

    for (X, Y), cell in sorted(cells.items()):
        kept_winrate = (100.0 * cell.kept_wins / cell.kept_count) if cell.kept_count else 0.0
        kept_pf = (cell.kept_gross_win / cell.kept_gross_loss) if cell.kept_gross_loss > 0 else float("inf")
        kept_avg = (cell.kept_pnl / cell.kept_count) if cell.kept_count else 0.0
        blk_wr = (100.0 * cell.blocked_wins / cell.blocked_count) if cell.blocked_count else 0.0
        saved_each = (-cell.blocked_pnl / cell.blocked_count) if cell.blocked_count else 0.0
        veto_rate = 100.0 * cell.blocked_count / (cell.blocked_count + cell.kept_count)
        rows.append({
            "scenario": f"VETO_X{X}_Y{Y}",
            "min_strength": X,
            "max_prox_ticks": Y,
            "kept_trades": cell.kept_count,
            "kept_pnl": round(cell.kept_pnl, 2),
            "kept_winrate_pct": round(kept_winrate, 2),
            "kept_pf": round(kept_pf, 3) if kept_pf != float("inf") else "inf",
            "kept_avg_pnl": round(kept_avg, 2),
            "blocked_trades": cell.blocked_count,
            "blocked_pnl": round(cell.blocked_pnl, 2),
            "blocked_winrate_pct": round(blk_wr, 2),
            "saved_per_blocked": round(saved_each, 2),
            "veto_fire_rate_pct": round(veto_rate, 2),
            "pnl_lift_vs_baseline": round(cell.kept_pnl - baseline_pnl, 2),
            "pf_lift_vs_baseline": round(kept_pf - baseline_pf, 3) if kept_pf != float("inf") else "inf",
            "blocked_long_n": cell.blocked_long_count,
            "blocked_long_pnl": round(cell.blocked_long_pnl, 2),
            "blocked_short_n": cell.blocked_short_count,
            "blocked_short_pnl": round(cell.blocked_short_pnl, 2),
        })

    summary_df = pd.DataFrame(rows)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    summary_df.to_csv(out_csv, index=False)
    logger.info("[out] wrote %s", out_csv)

    # ── Per-year breakdown for the best cell (max kept_pnl with PF >= baseline) ──
    best_key = None
    best_score = None
    for (X, Y), cell in cells.items():
        if cell.kept_count == 0:
            continue
        kept_pf = (cell.kept_gross_win / cell.kept_gross_loss) if cell.kept_gross_loss > 0 else float("inf")
        # Score: must improve total $ AND not crash PF below baseline
        if cell.kept_pnl < baseline_pnl:
            continue
        if kept_pf < baseline_pf:
            continue
        score = (cell.kept_pnl - baseline_pnl) + (kept_pf - baseline_pf) * 10000
        if best_score is None or score > best_score:
            best_score = score
            best_key = (X, Y)

    per_year_rows = []
    if best_key is not None:
        X, Y = best_key
        cell = cells[best_key]
        for y in sorted(cell.per_year_kept_pnl.keys() | cell.per_year_blocked_pnl.keys()):
            base_row = per_year_baseline[per_year_baseline["year"] == y]
            base_pnl_y = float(base_row["sum"].iloc[0]) if len(base_row) else 0.0
            base_n_y = int(base_row["count"].iloc[0]) if len(base_row) else 0
            per_year_rows.append({
                "year": y,
                "baseline_count": base_n_y,
                "baseline_pnl": round(base_pnl_y, 2),
                "veto_kept_count": cell.per_year_kept_count[y],
                "veto_kept_pnl": round(cell.per_year_kept_pnl[y], 2),
                "veto_blocked_count": cell.per_year_blocked_count[y],
                "veto_blocked_pnl": round(cell.per_year_blocked_pnl[y], 2),
                "year_lift_dollars": round(cell.per_year_kept_pnl[y] - base_pnl_y, 2),
            })

    return {
        "baseline_count": baseline_count,
        "baseline_pnl": baseline_pnl,
        "baseline_pf": baseline_pf,
        "baseline_winrate_pct": 100.0 * baseline_wins / baseline_count,
        "best_key": best_key,
        "cells": cells,
        "per_year": per_year_rows,
        "summary_df": summary_df,
        "elapsed_s": elapsed,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "skipped_no_bars": skipped_no_bars,
    }


def _record_kept(cells, tr):
    """Record a trade as 'kept' in every cell (used when zone detection
    cannot be performed — we cannot veto without zones)."""
    pnl = float(tr.pnl_dollars)
    year = int(tr.year)
    is_win = pnl > 0
    for cell in cells.values():
        cell.kept_count += 1
        cell.kept_pnl += pnl
        cell.kept_wins += int(is_win)
        if pnl > 0:
            cell.kept_gross_win += pnl
        else:
            cell.kept_gross_loss += -pnl
        cell.per_year_kept_pnl[year] += pnl
        cell.per_year_kept_count[year] += 1


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def _print_summary(result: dict) -> None:
    print("")
    print("=" * 78)
    print("PHOENIX S/R VETO ANALYZER -- BIAS_MOMENTUM")
    print("=" * 78)
    print(f"Baseline: {result['baseline_count']} trades / "
          f"${result['baseline_pnl']:.2f} / "
          f"WR {result['baseline_winrate_pct']:.1f}% / "
          f"PF {result['baseline_pf']:.3f}")
    print("")
    print("Grid: strength X in {0.5, 0.6, 0.7} x proximity Y in {4, 8, 12} ticks")
    print("")
    print(f"{'cell':<14} {'kept_n':>7} {'kept_$':>10} {'kept_WR':>8} {'kept_PF':>8} "
          f"{'blk_n':>6} {'blk_$':>10} {'save/t':>8} {'fire%':>6} {'$lift':>10}")
    print("-" * 100)
    baseline_pf = result["baseline_pf"]
    for (X, Y), cell in sorted(result["cells"].items()):
        kept_winrate = (100.0 * cell.kept_wins / cell.kept_count) if cell.kept_count else 0.0
        kept_pf = (cell.kept_gross_win / cell.kept_gross_loss) if cell.kept_gross_loss > 0 else 0.0
        saved_each = (-cell.blocked_pnl / cell.blocked_count) if cell.blocked_count else 0.0
        veto_rate = 100.0 * cell.blocked_count / (cell.blocked_count + cell.kept_count)
        lift = cell.kept_pnl - result["baseline_pnl"]
        print(f"X{X}_Y{Y:<2}        {cell.kept_count:>7} ${cell.kept_pnl:>9,.0f} "
              f"{kept_winrate:>7.1f}% {kept_pf:>8.3f} "
              f"{cell.blocked_count:>6} ${cell.blocked_pnl:>9,.0f} "
              f"${saved_each:>6.2f} {veto_rate:>5.1f}% ${lift:>+9,.0f}")
    print("")
    if result["best_key"] is None:
        print(">>> VERDICT: NO cell beats baseline on BOTH total $ AND PF.")
        print(">>> Veto filter does not improve bias_momentum. Skip.")
    else:
        X, Y = result["best_key"]
        cell = result["cells"][(X, Y)]
        kept_pf = (cell.kept_gross_win / cell.kept_gross_loss) if cell.kept_gross_loss > 0 else 0.0
        kept_winrate = (100.0 * cell.kept_wins / cell.kept_count) if cell.kept_count else 0.0
        print(f">>> BEST cell: X={X}, Y={Y}ticks")
        print(f">>>   kept {cell.kept_count} trades / ${cell.kept_pnl:,.2f} / "
              f"WR {kept_winrate:.1f}% / PF {kept_pf:.3f}")
        print(f">>>   blocked {cell.blocked_count} trades / ${cell.blocked_pnl:,.2f} "
              f"(avg ${-cell.blocked_pnl / max(1, cell.blocked_count):.2f} saved each)")
        print(f">>>   net $ lift vs baseline: ${cell.kept_pnl - result['baseline_pnl']:+,.2f}")
        print(f">>>   PF lift vs baseline: {kept_pf - baseline_pf:+.3f}")
        print("")
        print(">>> Per-year breakdown (best cell):")
        print(f"{'year':<6} {'base_n':>7} {'base_$':>10} {'veto_n':>7} {'veto_$':>10} "
              f"{'blk_n':>6} {'blk_$':>10} {'$lift':>10}")
        for r in result["per_year"]:
            print(f"{r['year']:<6} {r['baseline_count']:>7} ${r['baseline_pnl']:>9,.0f} "
                  f"{r['veto_kept_count']:>7} ${r['veto_kept_pnl']:>9,.0f} "
                  f"{r['veto_blocked_count']:>6} ${r['veto_blocked_pnl']:>9,.0f} "
                  f"${r['year_lift_dollars']:>+9,.0f}")
    print("")
    print(f"runtime: {result['elapsed_s']:.1f}s "
          f"(cache hits {result['cache_hits']}, misses {result['cache_misses']}, "
          f"no-bars {result['skipped_no_bars']})")
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", default=str(ROOT / "backtest_results" / "phoenix_real_5year.csv"))
    parser.add_argument("--bars-csv", default=str(ROOT / "data" / "historical" / "mnq_5min_databento.csv"))
    parser.add_argument("--out-csv", default=str(ROOT / "backtest_results" / "phoenix_sr_veto_summary.csv"))
    parser.add_argument("--quick", action="store_true",
                        help="Run only the first 500 trades for sanity testing")
    args = parser.parse_args()

    trades_csv = Path(args.trades_csv)
    bars_csv = Path(args.bars_csv)
    out_csv = Path(args.out_csv)

    if args.quick:
        # Monkey-patch the loader to clip to 500 trades.
        global _load_bias_momentum_trades
        _orig = _load_bias_momentum_trades
        def _quick_load(p):
            df = _orig(p)
            return df.iloc[:500].copy()
        _load_bias_momentum_trades = _quick_load

    result = run(trades_csv, bars_csv, out_csv)
    _print_summary(result)


if __name__ == "__main__":
    main()
