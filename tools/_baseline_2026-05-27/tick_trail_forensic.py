"""
Forensic tick-level validation of the 2026-05-27 bar-level optimizer picks.

OPERATOR ASK (2026-05-27 pivot):
  "Verify that tick_trail_4_post_1r and tick_trail_8_post_05r survive REAL
  market microstructure, not bar-level interpolation artifacts. Sweep to find
  the true volatility floor where the trail stops getting taken out by
  micro-noise."

WHAT THIS DOES:
  1. Filter the fresh per-trade CSV (and lab CSVs) to the TBBO window
     (2026-03-17 -> 2026-05-15, 60 days). Trades outside have no tick data.
  2. For each trade, run the trail policy GRID (trail_ticks x activate_r) on:
     - True TBBO tick stream (tick-level reality)
     - 1m OHLCV bars (bar-level promise) -- using the same code path the
       optimizer uses.
  3. Aggregate per (strategy, policy):
        tick_total$, bar_total$, phantom_$ = bar - tick,
        phantom_% = phantom_$ / bar_$,
        n_trades, tick_wr_pct, bar_wr_pct,
        n_tick_earlier_exit, avg_extra_seconds_held_in_bar.
  4. Volatility floor: for each strategy, identify the smallest trail_ticks
     where tick_total >= 80% of bar_total (i.e. trail survives microstructure).
  5. Specifically rank tick_trail_4_post_1r and tick_trail_8_post_05r among
     all swept variants, by tick-level $ and PF.

POLICY GRID:
  trail_ticks ∈ {2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 48}
  activate_r  ∈ {0.5, 1.0, 1.5}
  + fixed_2r, fixed_3r as reference

  -> 36 trail variants + 2 fixed = 38 policies per trade.

PARALLELISM:
  4 workers via spawn (memory-conservative: each loads 1.4 GB tick stream)
  Each worker loads ticks once in initializer; processes all assigned
  strategies sequentially.

OUTPUTS:
  out/_baseline_2026-05-27/tick_validation/per_trade.csv         (every trade x policy x level)
  out/_baseline_2026-05-27/tick_validation/per_policy_summary.csv (aggregated)
  out/_baseline_2026-05-27/tick_validation/volatility_floor.csv  (per-strategy minimum tight trail)
  out/_baseline_2026-05-27/tick_validation/headline.md           (human-readable verdict)
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import the proven tick-walk implementations from the existing tool
from tools.phoenix_tick_trail_verification import (  # noqa: E402
    TICK_SIZE, TICK_VALUE, MAX_HOLD_MIN,
    TickIndex, policy_tick_trail, policy_fixed_rr,
    policy_tick_trail_BAR,
)

TICK_PARQUET = ROOT / "data" / "historical" / "databento_tbbo" / "mnq_ticks.parquet"
BARS_1M_CSV  = ROOT / "data" / "historical" / "mnq_1min_databento.csv"

WINDOW_START = pd.Timestamp("2026-03-17", tz="UTC")
WINDOW_END   = pd.Timestamp("2026-05-15 21:00", tz="UTC")

# Policy grid -- expanded to find true volatility floor
TRAIL_TICKS_GRID = [2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 48]
ACTIVATE_R_GRID  = [0.5, 1.0, 1.5]


def build_policy_battery() -> list[tuple[str, str, dict]]:
    """Returns list of (name, kind, kwargs) tuples covering the full sweep."""
    out = []
    for trail in TRAIL_TICKS_GRID:
        for act in ACTIVATE_R_GRID:
            act_label = "05r" if act == 0.5 else ("1r" if act == 1.0 else "15r")
            name = f"tick_trail_{trail}_post_{act_label}"
            out.append((name, "tick", {"trail_ticks": trail, "activate_r": act}))
    out.append(("fixed_2r", "fixed", {"rr": 2.0}))
    out.append(("fixed_3r", "fixed", {"rr": 3.0}))
    return out


POLICY_BATTERY = build_policy_battery()


# Strategies to validate -- the picks from the fresh bar-level optimizer
TARGET_STRATEGIES = [
    # Picked tick_trail_4_post_1r (operator's primary forensic target):
    "bias_momentum",
    "spring_setup",
    "vwap_pullback_v2",
    # Picked tick_trail_8_post_05r (operator's secondary forensic target):
    "noise_area",
    # Picked tick_trail_8_post_15r:
    "opening_session",
    "ib_breakout",
    "vwap_band_reversion",
    "compression_breakout_v2",
    # Phase 13 winners (already chandelier/time-exit per optimizer, but
    # include for full coverage):
    "a_asian_continuation",
    "e_multi_day_breakout",
    "g_inside_bar_breakout",
    "raschke_baseline",
]


# ─────────────────────────────────────────────────────────────────────
# Worker globals
# ─────────────────────────────────────────────────────────────────────

_WORKER_TICK_IDX: TickIndex | None = None
_WORKER_BARS_1M: pd.DataFrame | None = None


def _worker_init(tick_parquet: str, bars_csv: str) -> None:
    global _WORKER_TICK_IDX, _WORKER_BARS_1M
    pid = mp.current_process().pid
    print(f"[worker-{pid}] loading ticks {tick_parquet}", flush=True)
    t0 = time.time()
    df = pd.read_parquet(tick_parquet)
    df = df.sort_values("ts_event").reset_index(drop=True)
    _WORKER_TICK_IDX = TickIndex(df)
    print(f"[worker-{pid}] ticks loaded: {len(df):,} in {time.time() - t0:.1f}s", flush=True)
    del df  # TickIndex retained the arrays; free the DataFrame

    t0 = time.time()
    bars = pd.read_csv(bars_csv)
    bars["ts"] = pd.to_datetime(bars["ts_utc"], utc=True)
    bars = bars.set_index("ts").sort_index()
    bars = bars.loc[(bars.index >= WINDOW_START) & (bars.index <= WINDOW_END)]
    _WORKER_BARS_1M = bars[["open", "high", "low", "close", "volume"]]
    print(f"[worker-{pid}] bars loaded: {len(_WORKER_BARS_1M):,} in {time.time() - t0:.1f}s", flush=True)


# ─────────────────────────────────────────────────────────────────────
# Per-trade walk
# ─────────────────────────────────────────────────────────────────────

def _walk_trade(trade: dict) -> list[dict]:
    """Walk one trade through every policy at BOTH tick and bar level.
    Returns one row per (policy, level)."""
    global _WORKER_TICK_IDX, _WORKER_BARS_1M
    entry_ts = pd.Timestamp(trade["entry_ts"])
    if entry_ts.tz is None:
        entry_ts = entry_ts.tz_localize("UTC")
    entry_ns = entry_ts.value
    end_ts = entry_ts + pd.Timedelta(minutes=MAX_HOLD_MIN)

    ts_arr, px_arr = _WORKER_TICK_IDX.slice(
        entry_ts + pd.Timedelta(microseconds=1), end_ts
    )
    if len(ts_arr) == 0:
        return []

    bars_window = _WORKER_BARS_1M.loc[
        (_WORKER_BARS_1M.index > entry_ts) & (_WORKER_BARS_1M.index <= end_ts)
    ]

    direction = trade["direction"]
    entry = float(trade["entry_price"])
    stop = float(trade["stop_price"])

    out = []
    for pname, kind, kwargs in POLICY_BATTERY:
        # Tick-level
        if kind == "tick":
            r_tick = policy_tick_trail(
                direction, entry, stop, ts_arr, px_arr, entry_ns, **kwargs
            )
        else:
            r_tick = policy_fixed_rr(
                direction, entry, stop, ts_arr, px_arr, entry_ns, **kwargs
            )
        out.append({
            "strategy": trade["strategy"],
            "direction": direction,
            "entry_ts": entry_ts,
            "entry_price": entry,
            "stop_price": stop,
            "policy": pname,
            "level": "tick",
            "exit_ts_ns": r_tick.exit_ts_ns,
            "exit_price": r_tick.exit_price,
            "pnl_ticks": r_tick.pnl_ticks,
            "pnl_dollars": r_tick.pnl_ticks * TICK_VALUE,
            "exit_reason": r_tick.exit_reason,
            "hold_sec": r_tick.hold_sec,
        })

        # Bar-level mirror (only for tick policies; fixed_rr is the same logic on bars)
        if kind == "tick":
            r_bar = policy_tick_trail_BAR(
                direction, entry, stop, bars_window, entry_ts, **kwargs
            )
            out.append({
                "strategy": trade["strategy"],
                "direction": direction,
                "entry_ts": entry_ts,
                "entry_price": entry,
                "stop_price": stop,
                "policy": pname,
                "level": "bar",
                "exit_ts_ns": r_bar.exit_ts_ns,
                "exit_price": r_bar.exit_price,
                "pnl_ticks": r_bar.pnl_ticks,
                "pnl_dollars": r_bar.pnl_ticks * TICK_VALUE,
                "exit_reason": r_bar.exit_reason,
                "hold_sec": r_bar.hold_sec,
            })
    return out


def _worker_strategy(args: tuple) -> tuple[str, list[dict]]:
    strat_name, trades_pickled = args
    trades = pickle.loads(trades_pickled)
    t0 = time.time()
    all_rows: list[dict] = []
    for _, tr in trades.iterrows():
        rows = _walk_trade(tr.to_dict())
        all_rows.extend(rows)
    elapsed = time.time() - t0
    print(f"[done] {strat_name}: n={len(trades)} trades, "
          f"{len(all_rows)} rows in {elapsed:.1f}s", flush=True)
    return strat_name, all_rows


# ─────────────────────────────────────────────────────────────────────
# Main driver
# ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv")
    ap.add_argument("--extra-trades", nargs="*",
                    default=["backtest_results/phoenix_new_strategy_lab.csv",
                             "backtest_results/phoenix_trend_pullback_lab.csv"])
    ap.add_argument("--out-dir", default="out/_baseline_2026-05-27/tick_validation")
    ap.add_argument("--workers", type=int, default=4,
                    help="Pool size (default 4 -- conservative for 1.4 GB tick stream per worker)")
    ap.add_argument("--strategies",
                    default=",".join(TARGET_STRATEGIES),
                    help="Comma-separated strategies to validate")
    args = ap.parse_args()

    out_dir = ROOT / args.out_dir if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load trades
    paths = [args.trades] + list(args.extra_trades)
    frames = []
    for p in paths:
        full = ROOT / p if not Path(p).is_absolute() else Path(p)
        if not full.exists():
            print(f"[WARN] missing: {full}")
            continue
        df = pd.read_csv(full)
        df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
        if "exit_ts" in df.columns:
            df["exit_ts"] = pd.to_datetime(df["exit_ts"], utc=True, errors="coerce")
        keep = ["strategy", "direction", "entry_ts", "entry_price", "stop_price",
                "target_price", "pnl_dollars", "pnl_ticks"]
        for c in keep:
            if c not in df.columns:
                df[c] = None
        frames.append(df[keep])
    df_all = pd.concat(frames, ignore_index=True)

    # Filter to TBBO window
    df_all = df_all[
        (df_all["entry_ts"] >= WINDOW_START)
        & (df_all["entry_ts"] <= WINDOW_END - pd.Timedelta(minutes=MAX_HOLD_MIN + 5))
    ]

    # Filter to target strategies
    target = [s.strip() for s in args.strategies.split(",") if s.strip()]
    df_all = df_all[df_all["strategy"].isin(target)].sort_values("entry_ts").reset_index(drop=True)

    print(f"[scope] {len(df_all):,} trades in TBBO window across {df_all['strategy'].nunique()} strategies")
    print(f"[scope] per-strategy counts: {df_all.groupby('strategy').size().to_dict()}")
    print(f"[scope] policies in battery: {len(POLICY_BATTERY)}")

    # Per-strategy work items
    work_items = []
    for s in target:
        sdf = df_all[df_all["strategy"] == s]
        if len(sdf) == 0:
            print(f"[skip] {s}: 0 trades in window")
            continue
        work_items.append((s, pickle.dumps(sdf)))

    if not work_items:
        print("[ERROR] no work items")
        sys.exit(1)

    # Run pool
    ctx = mp.get_context("spawn")
    print(f"[pool] {args.workers} workers (spawn) for {len(work_items)} strategies")
    t_pool_start = time.time()
    all_rows: list[dict] = []
    with ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=ctx,
        initializer=_worker_init,
        initargs=(str(TICK_PARQUET), str(BARS_1M_CSV)),
    ) as ex:
        futures = {ex.submit(_worker_strategy, item): item[0] for item in work_items}
        for fut in as_completed(futures):
            sname = futures[fut]
            try:
                _, rows = fut.result()
                all_rows.extend(rows)
            except Exception as e:
                print(f"[ERROR] {sname}: {e!r}")
    print(f"[pool] all done in {time.time() - t_pool_start:.1f}s")

    if not all_rows:
        print("[ERROR] no result rows")
        sys.exit(1)

    # ── Per-trade CSV ────────────────────────────────────────────────
    per_trade = pd.DataFrame(all_rows)
    per_trade.to_csv(out_dir / "per_trade.csv", index=False)
    print(f"[write] per_trade.csv: {len(per_trade):,} rows")

    # ── Per-policy aggregate (split by level) ────────────────────────
    agg_rows = []
    for (s, p, lvl), gdf in per_trade.groupby(["strategy", "policy", "level"]):
        n = len(gdf)
        total = float(gdf["pnl_dollars"].sum())
        wins = int((gdf["pnl_dollars"] > 0).sum())
        wr = wins / n if n else 0
        gross_w = float(gdf.loc[gdf["pnl_dollars"] > 0, "pnl_dollars"].sum())
        gross_l = -float(gdf.loc[gdf["pnl_dollars"] < 0, "pnl_dollars"].sum())
        pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
        agg_rows.append({
            "strategy": s,
            "policy": p,
            "level": lvl,
            "n": n,
            "total_dollars": round(total, 2),
            "wr_pct": round(100 * wr, 2),
            "avg_dollars": round(total / n, 2) if n else 0,
            "pf": round(pf, 2) if not math.isinf(pf) else 99.0,
            "avg_hold_sec": round(float(gdf["hold_sec"].mean()), 1),
        })
    per_policy_long = pd.DataFrame(agg_rows)

    # ── Bar-vs-tick phantom analysis ────────────────────────────────
    pivot = per_policy_long.pivot_table(
        index=["strategy", "policy"],
        columns="level",
        values=["total_dollars", "wr_pct", "pf", "n"],
        aggfunc="first"
    )
    pivot.columns = [f"{c[0]}_{c[1]}" for c in pivot.columns]
    pivot = pivot.reset_index()

    if "total_dollars_bar" in pivot.columns and "total_dollars_tick" in pivot.columns:
        pivot["phantom_dollars"] = pivot["total_dollars_bar"] - pivot["total_dollars_tick"]
        pivot["phantom_pct"] = np.where(
            pivot["total_dollars_bar"].abs() > 0,
            100 * pivot["phantom_dollars"] / pivot["total_dollars_bar"].abs(),
            0.0
        ).round(2)
        # Tick survives bar promise if tick_total >= 0.8 * bar_total (and both >0)
        pivot["tick_survives_80pct"] = (
            (pivot["total_dollars_tick"] >= 0.8 * pivot["total_dollars_bar"])
            & (pivot["total_dollars_tick"] > 0)
            & (pivot["total_dollars_bar"] > 0)
        )

    pivot.to_csv(out_dir / "per_policy_summary.csv", index=False)
    print(f"[write] per_policy_summary.csv: {len(pivot)} rows")

    # ── Volatility floor per strategy ──────────────────────────────
    # For each strategy, find the smallest trail_ticks (at activate_r=1.0) where
    # tick_total >= 0.8 * bar_total. That's the "true volatility floor".
    floor_rows = []
    for s in target:
        sdf = pivot[pivot["strategy"] == s].copy()
        if sdf.empty:
            continue
        # Parse policy name -> (trail_ticks, activate_r)
        def parse_pol(p):
            if not p.startswith("tick_trail_"):
                return (None, None)
            try:
                parts = p[len("tick_trail_"):].split("_post_")
                trail = int(parts[0])
                act_label = parts[1]
                act = 0.5 if act_label == "05r" else (1.0 if act_label == "1r" else 1.5)
                return (trail, act)
            except Exception:
                return (None, None)

        sdf["trail_ticks"] = sdf["policy"].apply(lambda p: parse_pol(p)[0])
        sdf["activate_r"] = sdf["policy"].apply(lambda p: parse_pol(p)[1])
        sdf = sdf.dropna(subset=["trail_ticks"])

        for act in ACTIVATE_R_GRID:
            sub = sdf[sdf["activate_r"] == act].sort_values("trail_ticks")
            if sub.empty:
                continue
            # Find smallest trail where tick_survives_80pct
            survivor = sub[sub["tick_survives_80pct"]]
            min_floor = float(survivor["trail_ticks"].min()) if not survivor.empty else None
            # Best tick total in this activate_r row
            best_idx = sub["total_dollars_tick"].idxmax() if not sub["total_dollars_tick"].isna().all() else None
            best_row = sub.loc[best_idx] if best_idx is not None else None
            floor_rows.append({
                "strategy": s,
                "activate_r": act,
                "min_trail_ticks_surviving_80pct_of_bar": min_floor,
                "best_trail_ticks_by_tick_pnl": float(best_row["trail_ticks"]) if best_row is not None else None,
                "best_tick_total_dollars": float(best_row["total_dollars_tick"]) if best_row is not None else None,
                "best_bar_total_dollars": float(best_row["total_dollars_bar"]) if best_row is not None else None,
                "best_phantom_pct": float(best_row["phantom_pct"]) if best_row is not None else None,
            })
    floor_df = pd.DataFrame(floor_rows)
    floor_df.to_csv(out_dir / "volatility_floor.csv", index=False)
    print(f"[write] volatility_floor.csv: {len(floor_df)} rows")

    # ── Headline markdown ──────────────────────────────────────────
    headline = ["# Tick-Level Forensic Validation — 2026-05-27",
                "",
                f"Window: 2026-03-17 → 2026-05-15 ({len(df_all):,} trades, "
                f"{df_all['strategy'].nunique()} strategies)",
                f"Policy battery: {len(POLICY_BATTERY)} variants "
                f"(trail × activate_r grid + 2 fixed RR refs)",
                "",
                "## Verdict on the named picks",
                ""]

    for s in target:
        sdf = pivot[pivot["strategy"] == s]
        if sdf.empty:
            continue
        headline.append(f"### {s}")
        # Specifically called-out picks:
        for named in ["tick_trail_4_post_1r", "tick_trail_8_post_05r"]:
            row = sdf[sdf["policy"] == named]
            if row.empty:
                continue
            r = row.iloc[0]
            bar_t = r.get("total_dollars_bar")
            tick_t = r.get("total_dollars_tick")
            ph_pct = r.get("phantom_pct")
            survives = r.get("tick_survives_80pct")
            verdict = "✅ SURVIVES" if survives else "❌ FAILS"
            headline.append(f"- **{named}**: bar=${bar_t:,.0f}, tick=${tick_t:,.0f}, phantom={ph_pct:+.1f}% → {verdict}")
        # Volatility floor at activate_r=1.0
        fr = floor_df[(floor_df["strategy"] == s) & (floor_df["activate_r"] == 1.0)]
        if not fr.empty:
            min_tr = fr.iloc[0]["min_trail_ticks_surviving_80pct_of_bar"]
            best_tr = fr.iloc[0]["best_trail_ticks_by_tick_pnl"]
            best_tk = fr.iloc[0]["best_tick_total_dollars"]
            headline.append(f"- Volatility floor (activate_r=1.0): "
                            f"min trail = {min_tr if min_tr is not None else 'NONE PASSES'}t; "
                            f"best by tick $ = {best_tr}t at ${best_tk:,.0f}")
        headline.append("")

    headline.append("## Methodology")
    headline.append("- Trail policies activate after +activate_r favorable, then trail trail_ticks behind high_water.")
    headline.append("- TICK level: every TBBO trade event walked. Adverse tick after activation kills the trade.")
    headline.append("- BAR level: 1m OHLC walked exactly as the bar-level optimizer does it.")
    headline.append("- Phantom % = (bar_total - tick_total) / |bar_total| × 100. Positive = bar lies upward.")
    headline.append("- Survives criterion: tick_total >= 0.8 × bar_total AND both > 0.")

    (out_dir / "headline.md").write_text("\n".join(headline), encoding="utf-8")
    print(f"[write] headline.md")

    print("[done] tick_trail_forensic complete")


if __name__ == "__main__":
    mp.freeze_support()
    main()
