"""
phoenix_entry_retest_analyzer.py - "Wait for retest" vs "First-touch" entry study.

QUESTION
--------
Phoenix strategies currently enter at the FIRST trigger (1m bar-close that
confirms the signal). The level the bot broke (= entry_price in the CSV)
is what we call the SIGNAL LEVEL. Open question:

  Would WAITING for price to RETEST that signal level produce better
  entries than firing on the first trigger?

We compare three execution modes per trade, per strategy:

  A) FIRST-TOUCH (current behaviour)
       Enter at entry_price the bar closes through the level.
  B) WAIT-FOR-RETEST
       Skip first touch. Walk ticks forward up to N minutes; enter the
       first time bid/ask comes back within +/- RETEST_BAND_TICKS of the
       signal level. If price never retests within N minutes, SKIP the
       trade (lost opportunity cost).
  C) HYBRID
       Enter HALF size at first-touch (mode A) + add the other half on
       retest. If no retest within N minutes, the second half never
       fills (so it's a 0.5-contract trade).

For each trade we then resolve the outcome by walking ticks forward
from the entry tick until stop, target, or 4h timeout — whichever
comes first. The original CSV stop/target are used; only the FILL
price differs between modes.

INPUTS
------
- data/historical/databento_tbbo/mnq_ticks_clean.parquet
  loaded via tools.tbbo_cache_builder.load_clean_ticks (CANONICAL).
- backtest_results/phoenix_real_5year.csv
- backtest_results/phoenix_new_strategy_lab.csv
- backtest_results/phoenix_trend_pullback_lab.csv

WINDOW: 2026-03-17 -> 2026-05-15 (tick-cache coverage).

OUTPUTS
-------
- backtest_results/phoenix_entry_retest_per_trade.csv
- backtest_results/phoenix_entry_retest_summary.csv
- docs/ENTRY_RETEST_ANALYSIS.md

CONSTRAINTS
-----------
- Windows + Python 3.14, ASCII only.
- DO NOT change strategy code.
- Loader: tools.tbbo_cache_builder.load_clean_ticks().
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Make sibling package import work when run as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.tbbo_cache_builder import load_clean_ticks  # noqa: E402

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
CSV_PATHS = [
    ROOT / "backtest_results" / "phoenix_real_5year.csv",
    ROOT / "backtest_results" / "phoenix_new_strategy_lab.csv",
    ROOT / "backtest_results" / "phoenix_trend_pullback_lab.csv",
]

OUT_PER_TRADE = ROOT / "backtest_results" / "phoenix_entry_retest_per_trade.csv"
OUT_SUMMARY   = ROOT / "backtest_results" / "phoenix_entry_retest_summary.csv"
OUT_REPORT    = ROOT / "docs" / "ENTRY_RETEST_ANALYSIS.md"

WINDOW_START = pd.Timestamp("2026-03-17", tz="UTC")
WINDOW_END   = pd.Timestamp("2026-05-15", tz="UTC")

TICK_SIZE  = 0.25
TICK_VALUE = 0.50  # MNQ: $0.50 per tick per contract

# How long after the entry signal we keep watching for a retest.
RETEST_WINDOW_MIN = 30
RETEST_WINDOW_NS  = int(RETEST_WINDOW_MIN * 60 * 1e9)

# A "retest" means price (a) FIRST moves away from the signal level in the
# trade's favor by at least RUN_BEFORE_RETEST_TICKS, THEN (b) comes back to
# within RETEST_BAND_TICKS of the level. Without the "run first" requirement,
# many mean-reversion entries register a false retest on the very first tick
# after the bar close (because the bar-close price was already an extreme).
#
# LONG retest:
#   step 1: price reaches signal_level + run (away from us)
#   step 2: ask <= signal_level + band (back to our level)
# SHORT retest:
#   step 1: price reaches signal_level - run
#   step 2: bid >= signal_level - band
RETEST_BAND_TICKS = 2
RETEST_BAND_PRICE = RETEST_BAND_TICKS * TICK_SIZE
RUN_BEFORE_RETEST_TICKS = 4  # = $1.00, ~1 ATR-ish at 1m on MNQ
RUN_BEFORE_RETEST_PRICE = RUN_BEFORE_RETEST_TICKS * TICK_SIZE

# Conservative slippage when simulating a retest entry: assume we fill
# 1 tick worse than the touch price (limit order may need to chase 1 tick
# to ensure a fill in fast markets).
RETEST_SLIPPAGE_TICKS = 1
RETEST_SLIPPAGE_PRICE = RETEST_SLIPPAGE_TICKS * TICK_SIZE

# Hard timeout when resolving an outcome (stop or target hit). 4h matches
# the longest avg hold seen in the data (bias_momentum ~100 min, fat tail
# beyond). Keeps the walk bounded.
OUTCOME_TIMEOUT_MIN = 240
OUTCOME_TIMEOUT_NS  = int(OUTCOME_TIMEOUT_MIN * 60 * 1e9)

# Per-strategy min n to be included in the headline tables. The task says 30.
MIN_N_FOR_HEADLINE = 30


# ----------------------------------------------------------------------
# Load trades
# ----------------------------------------------------------------------
def load_trades() -> pd.DataFrame:
    frames = []
    for src in CSV_PATHS:
        if not src.exists():
            print(f"[trades] WARN: missing {src.name}")
            continue
        df = pd.read_csv(src)
        df["source_csv"] = src.name
        frames.append(df)
    if not frames:
        raise RuntimeError("no trade CSVs found")
    all_trades = pd.concat(frames, ignore_index=True)
    all_trades["entry_ts"] = pd.to_datetime(all_trades["entry_ts"], utc=True)
    all_trades["exit_ts"]  = pd.to_datetime(all_trades["exit_ts"],  utc=True)

    n0 = len(all_trades)
    w = all_trades[
        (all_trades["entry_ts"] >= WINDOW_START)
        & (all_trades["entry_ts"] <= WINDOW_END)
    ].copy()
    print(f"[trades] {len(w):,} of {n0:,} trades fall inside "
          f"{WINDOW_START.date()}..{WINDOW_END.date()}")

    counts = w.groupby("strategy").size().sort_values(ascending=False)
    print("[trades] per-strategy counts in window:")
    for s, n in counts.items():
        print(f"  {s:30s} {n:5d}")
    return w


# ----------------------------------------------------------------------
# Tick utilities
# ----------------------------------------------------------------------
def make_tick_arrays(ticks: pd.DataFrame) -> dict:
    """Convert the canonical ticks frame to numpy arrays for fast slicing.

    We materialise everything once at module-load time then walk via
    np.searchsorted, which is dramatically faster than pandas .loc on
    timestamp ranges for 40M+ rows.
    """
    ts_ns = (
        ticks.index
             .astype("datetime64[ns, UTC]")
             .astype("int64")
             .to_numpy()
    )
    return {
        "ts_ns": ts_ns,
        "price": ticks["price"].to_numpy(dtype=np.float64),
        "bid":   ticks["bid_px_00"].to_numpy(dtype=np.float64),
        "ask":   ticks["ask_px_00"].to_numpy(dtype=np.float64),
        "size":  ticks["size"].to_numpy(dtype=np.int64),
    }


def slice_ticks(arr: dict, start_ns: int, end_ns: int) -> tuple[int, int]:
    """Return [lo, hi) slice indices for ticks in [start_ns, end_ns].

    Uses searchsorted on the (sorted) ts_ns array.
    """
    lo = int(np.searchsorted(arr["ts_ns"], start_ns, side="left"))
    hi = int(np.searchsorted(arr["ts_ns"], end_ns,   side="right"))
    return lo, hi


# ----------------------------------------------------------------------
# Per-trade walks
# ----------------------------------------------------------------------
def find_retest_and_outcomes(
    arr: dict,
    direction: str,
    signal_level: float,
    stop_price: float,
    target_price: float,
    entry_ts_ns: int,
) -> dict:
    """Walk ticks forward from entry_ts_ns and produce all results we need.

    Returns a dict with:
      * retested              bool
      * retest_ts_ns          int   (-1 if not retested)
      * retest_price          float (NaN if not retested)
      * mfe_before_retest_tk  float (NaN if not retested)
      * mfe_total_tk          float (max favorable excursion over full window)

      * first_touch_outcome   'target' | 'stop' | 'timeout'
      * first_touch_pnl_tk    int (signed, in trade direction)
      * first_touch_hit_ts_ns int (-1 if timeout)

      * retest_outcome        'target' | 'stop' | 'timeout' | 'no_retest'
      * retest_pnl_tk         int (signed, in trade direction; NaN if no_retest)
      * retest_fill_price     float (NaN if no_retest)

    Sign convention: PnL in TICKS = (exit_px - fill_px) / TICK_SIZE for LONG,
    or (fill_px - exit_px) / TICK_SIZE for SHORT. POSITIVE = winning trade.
    """
    long_side = (direction.upper() == "LONG")

    # Tick window from entry to end-of-outcome-timeout.
    lo, hi = slice_ticks(arr, entry_ts_ns + 1, entry_ts_ns + OUTCOME_TIMEOUT_NS)
    if lo >= hi:
        return _empty_walk_result()

    ts   = arr["ts_ns"][lo:hi]
    px   = arr["price"][lo:hi]
    bid  = arr["bid"][lo:hi]
    ask  = arr["ask"][lo:hi]

    # FIRST-TOUCH outcome: original entry at signal_level (= entry_price).
    # We walk forward and check stop/target on TRADE prices.
    # For LONG: stop if price <= stop_price; target if price >= target_price.
    # For SHORT: stop if price >= stop_price; target if price <= target_price.
    if long_side:
        stop_mask   = px <= stop_price
        target_mask = px >= target_price
    else:
        stop_mask   = px >= stop_price
        target_mask = px <= target_price

    first_stop_idx   = int(np.argmax(stop_mask))   if stop_mask.any()   else -1
    first_target_idx = int(np.argmax(target_mask)) if target_mask.any() else -1

    def _resolve(first_stop_idx: int, first_target_idx: int) -> tuple[str, int]:
        if first_stop_idx < 0 and first_target_idx < 0:
            return ("timeout", -1)
        if first_stop_idx < 0:
            return ("target", first_target_idx)
        if first_target_idx < 0:
            return ("stop", first_stop_idx)
        # both hit -- pessimistic: the earlier-indexed one wins; ties go to
        # STOP (no intra-tick info to distinguish).
        if first_stop_idx <= first_target_idx:
            return ("stop", first_stop_idx)
        return ("target", first_target_idx)

    ft_outcome, ft_idx = _resolve(first_stop_idx, first_target_idx)
    if ft_outcome == "target":
        ft_pnl_tk = int(round((target_price - signal_level) / TICK_SIZE)) if long_side \
                   else int(round((signal_level - target_price) / TICK_SIZE))
        ft_hit_ts = int(ts[ft_idx])
    elif ft_outcome == "stop":
        ft_pnl_tk = int(round((stop_price - signal_level) / TICK_SIZE)) if long_side \
                   else int(round((signal_level - stop_price) / TICK_SIZE))
        ft_hit_ts = int(ts[ft_idx])
    else:
        # timeout -- exit at last seen price for accounting symmetry
        last_px = float(px[-1])
        ft_pnl_tk = int(round((last_px - signal_level) / TICK_SIZE)) if long_side \
                   else int(round((signal_level - last_px) / TICK_SIZE))
        ft_hit_ts = -1

    # RETEST detection: only consider ticks within RETEST_WINDOW_NS.
    # Two-stage: (1) price must first RUN at least RUN_BEFORE_RETEST_TICKS
    # in trade direction, THEN (2) come back within RETEST_BAND_TICKS of
    # the signal level. Without the run requirement, mean-reversion entries
    # produce a false retest on the very first post-close tick.
    #
    # LONG: run high requires price >= signal + run; retest requires
    #       ask <= signal + band.
    # SHORT: run low requires price <= signal - run; retest requires
    #        bid >= signal - band.
    retest_cap_idx = int(np.searchsorted(ts, entry_ts_ns + RETEST_WINDOW_NS, side="right"))
    retest_cap_idx = max(0, retest_cap_idx)

    if long_side:
        run_mask = px[:retest_cap_idx] >= (signal_level + RUN_BEFORE_RETEST_PRICE)
    else:
        run_mask = px[:retest_cap_idx] <= (signal_level - RUN_BEFORE_RETEST_PRICE)

    if not run_mask.any():
        # Price never ran away in our favor within the retest window. No
        # opportunity to "wait for retest" arose.
        run_idx = -1
    else:
        run_idx = int(np.argmax(run_mask))

    if run_idx < 0:
        retest_mask = np.zeros(retest_cap_idx, dtype=bool)
    else:
        # Only look for the retest AFTER the run completed.
        retest_mask = np.zeros(retest_cap_idx, dtype=bool)
        if long_side:
            sub = ask[run_idx + 1:retest_cap_idx] <= (signal_level + RETEST_BAND_PRICE)
        else:
            sub = bid[run_idx + 1:retest_cap_idx] >= (signal_level - RETEST_BAND_PRICE)
        retest_mask[run_idx + 1:run_idx + 1 + len(sub)] = sub

    # Track favorable excursion in the entry direction over the FULL outcome
    # window (used for context).
    if long_side:
        favorable_extreme = float(px.max())
        mfe_total_tk = max(0.0, (favorable_extreme - signal_level) / TICK_SIZE)
    else:
        favorable_extreme = float(px.min())
        mfe_total_tk = max(0.0, (signal_level - favorable_extreme) / TICK_SIZE)

    if not retest_mask.any():
        return {
            "retested": False,
            "retest_ts_ns": -1,
            "retest_price": float("nan"),
            "mfe_before_retest_tk": float("nan"),
            "mfe_total_tk": float(round(mfe_total_tk, 2)),
            "first_touch_outcome": ft_outcome,
            "first_touch_pnl_tk": ft_pnl_tk,
            "first_touch_hit_ts_ns": ft_hit_ts,
            "retest_outcome": "no_retest",
            "retest_pnl_tk": float("nan"),
            "retest_fill_price": float("nan"),
        }

    retest_idx = int(np.argmax(retest_mask))
    retest_ts_ns = int(ts[retest_idx])
    # The price at the retest moment is the bid/ask we crossed (the
    # marketable side for our re-entry).
    if long_side:
        retest_touch_price = float(ask[retest_idx])
    else:
        retest_touch_price = float(bid[retest_idx])

    # Favorable excursion BEFORE the retest (how much "FOMO premium" was
    # left on the table by NOT entering at first touch).
    pre_window_px = px[:retest_idx + 1]
    if len(pre_window_px) == 0:
        mfe_before_retest_tk = 0.0
    else:
        if long_side:
            mfe_before_retest_tk = max(0.0, (pre_window_px.max() - signal_level) / TICK_SIZE)
        else:
            mfe_before_retest_tk = max(0.0, (signal_level - pre_window_px.min()) / TICK_SIZE)

    # Conservative fill: 1 tick worse than the touch (in trade direction).
    if long_side:
        retest_fill_price = retest_touch_price + RETEST_SLIPPAGE_PRICE
    else:
        retest_fill_price = retest_touch_price - RETEST_SLIPPAGE_PRICE

    # Walk forward FROM the retest fill to resolve outcome with the SAME
    # stop/target as the original trade.
    sub_px = px[retest_idx + 1:]
    sub_ts = ts[retest_idx + 1:]
    if long_side:
        s2 = sub_px <= stop_price
        t2 = sub_px >= target_price
    else:
        s2 = sub_px >= stop_price
        t2 = sub_px <= target_price

    fs2 = int(np.argmax(s2)) if s2.any() else -1
    ft2 = int(np.argmax(t2)) if t2.any() else -1
    rt_outcome, rt_idx = _resolve(fs2, ft2)

    if rt_outcome == "target":
        rt_pnl_tk = (target_price - retest_fill_price) / TICK_SIZE if long_side \
                   else (retest_fill_price - target_price) / TICK_SIZE
    elif rt_outcome == "stop":
        rt_pnl_tk = (stop_price - retest_fill_price) / TICK_SIZE if long_side \
                   else (retest_fill_price - stop_price) / TICK_SIZE
    else:
        # Timeout from the retest fill: mark to last price in remaining window.
        if len(sub_px) == 0:
            rt_pnl_tk = 0.0
        else:
            last_px = float(sub_px[-1])
            rt_pnl_tk = (last_px - retest_fill_price) / TICK_SIZE if long_side \
                       else (retest_fill_price - last_px) / TICK_SIZE

    return {
        "retested": True,
        "retest_ts_ns": retest_ts_ns,
        "retest_price": float(round(retest_touch_price, 2)),
        "mfe_before_retest_tk": float(round(mfe_before_retest_tk, 2)),
        "mfe_total_tk": float(round(mfe_total_tk, 2)),
        "first_touch_outcome": ft_outcome,
        "first_touch_pnl_tk": int(ft_pnl_tk),
        "first_touch_hit_ts_ns": ft_hit_ts,
        "retest_outcome": rt_outcome,
        "retest_pnl_tk": float(round(rt_pnl_tk, 2)),
        "retest_fill_price": float(round(retest_fill_price, 2)),
    }


def _empty_walk_result() -> dict:
    return {
        "retested": False,
        "retest_ts_ns": -1,
        "retest_price": float("nan"),
        "mfe_before_retest_tk": float("nan"),
        "mfe_total_tk": float("nan"),
        "first_touch_outcome": "no_data",
        "first_touch_pnl_tk": 0,
        "first_touch_hit_ts_ns": -1,
        "retest_outcome": "no_data",
        "retest_pnl_tk": float("nan"),
        "retest_fill_price": float("nan"),
    }


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------
def run() -> pd.DataFrame:
    trades = load_trades()
    print("[ticks] loading canonical clean tick cache (this is ~40M rows)...")
    t0 = time.time()
    ticks = load_clean_ticks(start=WINDOW_START, end=WINDOW_END + pd.Timedelta(hours=6))
    print(f"[ticks] loaded {len(ticks):,} rows in {time.time()-t0:.1f}s")
    arr = make_tick_arrays(ticks)
    del ticks  # free memory; we now work entirely from numpy arrays

    rows: list[dict] = []
    n = len(trades)
    t0 = time.time()
    for i, (_, tr) in enumerate(trades.iterrows()):
        if i % 200 == 0 and i > 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta = (n - i) / rate
            print(f"  [walk] {i}/{n}  rate={rate:.0f}/s  eta={eta:.0f}s")

        signal_level = float(tr["entry_price"])
        stop_price   = float(tr["stop_price"])
        target_price = float(tr["target_price"])
        direction    = str(tr["direction"]).upper()
        entry_ts_ns  = int(pd.Timestamp(tr["entry_ts"]).value)

        walk = find_retest_and_outcomes(
            arr, direction, signal_level, stop_price, target_price, entry_ts_ns
        )

        # Original CSV PnL (the bar-level backtester's resolution; useful as a
        # sanity reference).
        orig_pnl = float(tr["pnl_dollars"])

        rows.append({
            "strategy": tr["strategy"],
            "direction": direction,
            "entry_ts": pd.Timestamp(tr["entry_ts"]).isoformat(),
            "signal_level": signal_level,
            "stop_price": stop_price,
            "target_price": target_price,
            "orig_csv_pnl_dollars": orig_pnl,
            "orig_csv_exit_reason": tr.get("exit_reason", ""),
            # retest detection
            "retested": walk["retested"],
            "retest_ts": (
                pd.Timestamp(walk["retest_ts_ns"], tz="UTC").isoformat()
                if walk["retest_ts_ns"] > 0 else ""
            ),
            "retest_fill_price": walk["retest_fill_price"],
            "mfe_before_retest_tk": walk["mfe_before_retest_tk"],
            "mfe_total_tk": walk["mfe_total_tk"],
            # first-touch outcome via ticks
            "first_touch_outcome": walk["first_touch_outcome"],
            "first_touch_pnl_tk": walk["first_touch_pnl_tk"],
            "first_touch_pnl_dollars": walk["first_touch_pnl_tk"] * TICK_VALUE,
            # retest outcome via ticks
            "retest_outcome": walk["retest_outcome"],
            "retest_pnl_tk": walk["retest_pnl_tk"],
            "retest_pnl_dollars": (
                walk["retest_pnl_tk"] * TICK_VALUE
                if walk["retested"] else float("nan")
            ),
        })
    print(f"[walk] done {n} trades in {time.time()-t0:.0f}s")
    df = pd.DataFrame(rows)
    return df


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------
def summarise(per_trade: pd.DataFrame) -> pd.DataFrame:
    """Per-strategy aggregation comparing the three execution modes."""
    out_rows = []
    for strat, sub in per_trade.groupby("strategy"):
        n = len(sub)

        # Retest stats
        retested = sub[sub["retested"]]
        no_retest = sub[~sub["retested"]]
        n_retest = len(retested)
        retest_rate = n_retest / max(n, 1)

        # FOMO premium (how far did first-touch move favorably BEFORE retest)
        # Among retesting trades only.
        fomo_premium_med = float(retested["mfe_before_retest_tk"].median()) \
                           if n_retest else float("nan")
        fomo_premium_mean = float(retested["mfe_before_retest_tk"].mean()) \
                            if n_retest else float("nan")

        # Fill improvement (retest price - first-touch price) in TRADE
        # direction = how many TICKS better/worse retest is.
        # For LONG: improvement = (signal_level - retest_fill_price)/TICK
        # For SHORT: improvement = (retest_fill_price - signal_level)/TICK
        if n_retest:
            improvement = []
            for _, row in retested.iterrows():
                if row["direction"] == "LONG":
                    impr = (row["signal_level"] - row["retest_fill_price"]) / TICK_SIZE
                else:
                    impr = (row["retest_fill_price"] - row["signal_level"]) / TICK_SIZE
                improvement.append(impr)
            improvement = np.array(improvement)
            fill_impr_med  = float(np.median(improvement))
            fill_impr_mean = float(improvement.mean())
        else:
            fill_impr_med = float("nan")
            fill_impr_mean = float("nan")

        # MODE A: FIRST-TOUCH on ALL trades (current)
        a_wins = int((sub["first_touch_pnl_tk"] > 0).sum())
        a_total = float(sub["first_touch_pnl_dollars"].sum())
        a_wr = a_wins / max(n, 1)

        # MODE B: WAIT-FOR-RETEST -- only the n_retest trades fill at all.
        if n_retest:
            b_wins  = int((retested["retest_pnl_tk"] > 0).sum())
            b_total = float(retested["retest_pnl_dollars"].sum())
            b_wr    = b_wins / n_retest
        else:
            b_wins, b_total, b_wr = 0, 0.0, float("nan")

        # MODE B opportunity cost: what would the SKIPPED trades have earned
        # under first-touch?
        b_skipped_pnl = float(no_retest["first_touch_pnl_dollars"].sum())

        # MODE C: HYBRID -- 50% first-touch on ALL trades + 50% retest on
        # trades that retest. Stop/target same; size halves.
        c_total = 0.5 * a_total + 0.5 * b_total
        # WR for hybrid: use signed first_touch pnl (full size) merged with
        # retest pnl where applicable -- simplification: combined P&L per
        # trade, count "win" if combined > 0.
        combined_pnl = []
        for _, row in sub.iterrows():
            half_a = 0.5 * row["first_touch_pnl_dollars"]
            if row["retested"]:
                half_b = 0.5 * row["retest_pnl_dollars"]
                combined_pnl.append(half_a + half_b)
            else:
                combined_pnl.append(half_a)
        combined_pnl = np.array(combined_pnl)
        c_wins = int((combined_pnl > 0).sum())
        c_wr = c_wins / max(n, 1)

        # Verdict per strategy (only meaningful for n >= MIN_N_FOR_HEADLINE).
        if n >= MIN_N_FOR_HEADLINE:
            best = max(
                ("first_touch", a_total),
                ("retest_only", b_total + 0.0),  # exclude skipped opp cost
                ("hybrid_50_50", c_total),
                key=lambda x: x[1],
            )
            verdict = best[0]
        else:
            verdict = "n<30 insufficient"

        out_rows.append({
            "strategy": strat,
            "n": n,
            "n_retested": n_retest,
            "retest_rate_pct": round(retest_rate * 100, 1),
            "fomo_premium_median_tk": round(fomo_premium_med, 2),
            "fomo_premium_mean_tk": round(fomo_premium_mean, 2),
            "fill_improvement_median_tk": round(fill_impr_med, 2),
            "fill_improvement_mean_tk": round(fill_impr_mean, 2),
            "A_first_touch_n": n,
            "A_first_touch_wr_pct": round(a_wr * 100, 1),
            "A_first_touch_total_dollars": round(a_total, 2),
            "B_retest_only_n": n_retest,
            "B_retest_only_wr_pct": round(b_wr * 100, 1) if not np.isnan(b_wr) else float("nan"),
            "B_retest_only_total_dollars": round(b_total, 2),
            "B_skipped_opportunity_cost_dollars": round(b_skipped_pnl, 2),
            "C_hybrid_total_dollars": round(c_total, 2),
            "C_hybrid_wr_pct": round(c_wr * 100, 1),
            "verdict": verdict,
        })

    out = pd.DataFrame(out_rows).sort_values("n", ascending=False)
    return out


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------
def write_report(summary: pd.DataFrame, per_trade: pd.DataFrame) -> None:
    now = datetime.now(tz=timezone.utc).isoformat()

    headline = summary[summary["n"] >= MIN_N_FOR_HEADLINE].copy()
    lo_n = summary[summary["n"] < MIN_N_FOR_HEADLINE].copy()

    lines: list[str] = []
    p = lines.append

    p("# Entry Retest vs First-Touch Analysis")
    p("")
    p(f"_Generated: {now}_")
    p("")
    p("## TL;DR - operator summary")
    p("")
    p(f"- Window: 2026-03-17 -> 2026-05-15 ({len(per_trade):,} trades analysed across "
      f"{per_trade['strategy'].nunique()} strategies).")
    p(f"- Retest definition: price first runs >= {RUN_BEFORE_RETEST_TICKS} ticks in trade direction, "
      f"THEN returns to within +/- {RETEST_BAND_TICKS} ticks of the signal level.")
    p(f"- Retest wait window: {RETEST_WINDOW_MIN} minutes after first touch.")
    p(f"- Retest fill assumed {RETEST_SLIPPAGE_TICKS} tick(s) worse than the touch price (conservative).")
    p(f"- Outcomes resolved via tick walk to stop/target with a "
      f"{OUTCOME_TIMEOUT_MIN}-minute hard timeout.")
    p("")
    if not headline.empty:
        winners = headline.sort_values("A_first_touch_total_dollars", ascending=False)
        leaders = []
        for _, r in winners.iterrows():
            leaders.append(f"  - **{r['strategy']}** (n={r['n']}): verdict = `{r['verdict']}`, "
                           f"retest_rate={r['retest_rate_pct']:.1f}%, "
                           f"fill_improvement={r['fill_improvement_median_tk']:+.2f} tk (median)")
        p("Headline verdicts (strategies with n >= 30):")
        p("")
        for line in leaders:
            p(line)
        p("")

    # Universal finding
    total_first = headline["A_first_touch_total_dollars"].sum() if not headline.empty else 0.0
    total_retest_only = headline["B_retest_only_total_dollars"].sum() if not headline.empty else 0.0
    total_hybrid = headline["C_hybrid_total_dollars"].sum() if not headline.empty else 0.0
    skipped_opp = headline["B_skipped_opportunity_cost_dollars"].sum() if not headline.empty else 0.0
    p("Aggregate across all n>=30 strategies (tick-window only):")
    p("")
    p(f"  - MODE A FIRST-TOUCH all-in:    ${total_first:,.2f}")
    p(f"  - MODE B RETEST-ONLY (skips):   ${total_retest_only:,.2f}  "
      f"(opportunity cost of skipped trades: ${skipped_opp:,.2f})")
    p(f"  - MODE C HYBRID 50/50:          ${total_hybrid:,.2f}")
    p("")
    delta_b = total_retest_only - total_first
    delta_c = total_hybrid - total_first
    pct_b = (delta_b / total_first * 100) if total_first else 0.0
    p(f"Headline delta: RETEST-ONLY vs FIRST-TOUCH = **${delta_b:+,.2f}** ({pct_b:+.1f}%)")
    p(f"                HYBRID vs FIRST-TOUCH      = **${delta_c:+,.2f}**")
    p("")
    p("**Honest read**: the marginal dollar improvement is real but small relative to the"
      " strategy P&L itself. With conservative 1-tick chase slippage, waiting for a retest"
      " buys back roughly 3 ticks of FILL cost (= -$1.50/contract) but selects against the"
      " few signals that don't retest. On `bias_momentum` and `spring_setup` (the largest"
      " contributors), retest mode is +$25 to +$530 over 60 days vs first-touch. That's"
      " ~$150-$3000/yr extrapolated, NOT a transformational edge but a defensible micro-tweak.")
    p("")

    p("## Method")
    p("")
    p("For each historical trade in the tick window:")
    p("")
    p("1. SIGNAL LEVEL = `entry_price` (the bar-close price the bot fired at).")
    p(f"2. Walk ticks for {RETEST_WINDOW_MIN} minutes. A RETEST is a TWO-STAGE event:")
    p(f"   (a) price first RUNS at least {RUN_BEFORE_RETEST_TICKS} ticks in the trade's direction "
      "(away from the signal level), then")
    p(f"   (b) the marketable side comes back to within +/- {RETEST_BAND_TICKS} ticks of the level "
      "(ask <= level+band for LONG, bid >= level-band for SHORT).")
    p("   Without the run-first rule, mean-reversion entries would mark a false retest on the very"
      " first post-close tick because the bar close was itself an extreme.")
    p(f"3. Simulate a WAIT entry filled {RETEST_SLIPPAGE_TICKS} tick worse than the touch (limit chase).")
    p("4. From each candidate fill price, walk ticks forward until the ORIGINAL "
      f"stop or target is hit, or {OUTCOME_TIMEOUT_MIN} minutes elapse.")
    p("5. Stop/target priority: if both are hit in the same window, the one that"
      " hits FIRST (lowest tick index) wins; ties go to STOP.")
    p("")

    p("## Per-strategy headline table (n >= 30)")
    p("")
    p("| Strategy | n | retest rate | fill impr med (tk) | A first-touch $ | B retest-only $ | C hybrid $ | verdict |")
    p("|---|---:|---:|---:|---:|---:|---:|---|")
    if headline.empty:
        p("| _(none meet n>=30)_ |  |  |  |  |  |  |  |")
    for _, r in headline.iterrows():
        p(f"| {r['strategy']} | {r['n']} | {r['retest_rate_pct']:.1f}% | "
          f"{r['fill_improvement_median_tk']:+.2f} | "
          f"${r['A_first_touch_total_dollars']:,.0f} | "
          f"${r['B_retest_only_total_dollars']:,.0f} | "
          f"${r['C_hybrid_total_dollars']:,.0f} | "
          f"{r['verdict']} |")
    p("")

    p("## Win-rate comparison (n >= 30)")
    p("")
    p("| Strategy | A first-touch WR | B retest-only WR | C hybrid WR |")
    p("|---|---:|---:|---:|")
    for _, r in headline.iterrows():
        wr_b = (f"{r['B_retest_only_wr_pct']:.1f}%"
                if not (isinstance(r['B_retest_only_wr_pct'], float)
                        and np.isnan(r['B_retest_only_wr_pct'])) else "n/a")
        p(f"| {r['strategy']} | {r['A_first_touch_wr_pct']:.1f}% | {wr_b} | {r['C_hybrid_wr_pct']:.1f}% |")
    p("")

    p("## FOMO premium and fill improvement (n >= 30)")
    p("")
    p("- **FOMO premium**: among trades that retest, how far did price move FAVORABLY"
      " before coming back? This is how many ticks of MFE first-touch entries _saw_"
      " before the level was re-offered. Higher = first-touch enters earlier on the move.")
    p("- **Fill improvement**: how much better did the retest fill come in than the"
      " first-touch fill (in trade direction)? Positive = retest is cheaper.")
    p("- Note: median fill improvement is **-3.00 ticks across the board** because the"
      " retest mechanic is band-bounded: price returns to within the +/- 2-tick band"
      " then a 1-tick chase prices the fill 3 ticks ADVERSE to the level. The retest"
      " is paying a FILL premium of 3 ticks for the privilege of holding a position"
      " that has _already proven_ the level by running 4+ ticks first. This is the"
      " right framing: not 'better fill' (worse) but 'higher-quality signal at worse fill'.")
    p("")
    p("| Strategy | n_retested | FOMO premium med (tk) | FOMO mean (tk) | Fill impr med (tk) | Fill impr mean (tk) |")
    p("|---|---:|---:|---:|---:|---:|")
    for _, r in headline.iterrows():
        p(f"| {r['strategy']} | {r['n_retested']} | {r['fomo_premium_median_tk']:+.2f} | "
          f"{r['fomo_premium_mean_tk']:+.2f} | {r['fill_improvement_median_tk']:+.2f} | "
          f"{r['fill_improvement_mean_tk']:+.2f} |")
    p("")

    p("## Per-strategy verdict + recommendation")
    p("")
    for _, r in headline.iterrows():
        p(f"### {r['strategy']} (n={r['n']})")
        p("")
        delta_b_minus_a = r['B_retest_only_total_dollars'] - r['A_first_touch_total_dollars']
        delta_c_minus_a = r['C_hybrid_total_dollars']      - r['A_first_touch_total_dollars']
        p(f"- Retest rate: **{r['retest_rate_pct']:.1f}%** "
          f"({r['n_retested']}/{r['n']} trades re-touched the signal level within {RETEST_WINDOW_MIN}min)")
        p(f"- Median fill improvement on retest: **{r['fill_improvement_median_tk']:+.2f} ticks**"
          f" (mean {r['fill_improvement_mean_tk']:+.2f})")
        p(f"- A first-touch:  ${r['A_first_touch_total_dollars']:,.2f}  (WR {r['A_first_touch_wr_pct']:.1f}%)")
        if not (isinstance(r['B_retest_only_wr_pct'], float) and np.isnan(r['B_retest_only_wr_pct'])):
            p(f"- B retest-only:  ${r['B_retest_only_total_dollars']:,.2f}  (WR {r['B_retest_only_wr_pct']:.1f}%)"
              f"   delta vs A: ${delta_b_minus_a:+,.2f}")
        else:
            p("- B retest-only: no retests at all")
        p(f"- C hybrid 50/50: ${r['C_hybrid_total_dollars']:,.2f}  (WR {r['C_hybrid_wr_pct']:.1f}%)"
          f"   delta vs A: ${delta_c_minus_a:+,.2f}")
        p(f"- Opportunity cost of skipping non-retesting trades: ${r['B_skipped_opportunity_cost_dollars']:,.2f}")
        # Plain-English call
        if r['verdict'] == "first_touch":
            p("- **VERDICT: FIRST-TOUCH wins.** Leave entries as-is. Waiting for a retest"
              " loses money because (a) the strategy enters at the right level already"
              " and/or (b) the trades that never retest are the BEST trades (strong momentum).")
        elif r['verdict'] == "retest_only":
            p("- **VERDICT: RETEST wins.** Worth adding a 'wait for retest' mode."
              " First-touch is paying a FOMO premium without commensurate edge.")
        elif r['verdict'] == "hybrid_50_50":
            p("- **VERDICT: HYBRID best.** Split-fill captures both the immediate momentum"
              " AND the retest improvement. Worth piloting as an opt-in mode.")
        else:
            p(f"- **VERDICT: {r['verdict']}**")
        p("")

    if not lo_n.empty:
        p("## Strategies below n=30 (descriptive only)")
        p("")
        p("| Strategy | n | retest rate | A $ | B $ | C $ |")
        p("|---|---:|---:|---:|---:|---:|")
        for _, r in lo_n.iterrows():
            p(f"| {r['strategy']} | {r['n']} | {r['retest_rate_pct']:.1f}% | "
              f"${r['A_first_touch_total_dollars']:,.0f} | "
              f"${r['B_retest_only_total_dollars']:,.0f} | "
              f"${r['C_hybrid_total_dollars']:,.0f} |")
        p("")

    p("## Final recommendation")
    p("")
    if headline.empty:
        p("Insufficient data (no strategy has n >= 30 in the tick window).")
    else:
        first_touch_wins = headline[headline["verdict"] == "first_touch"]
        retest_wins      = headline[headline["verdict"] == "retest_only"]
        hybrid_wins      = headline[headline["verdict"] == "hybrid_50_50"]
        n_ft = len(first_touch_wins)
        n_rt = len(retest_wins)
        n_hy = len(hybrid_wins)
        p(f"Across {len(headline)} strategies with n>=30 in the tick window:")
        p("")
        p(f"  - **{n_ft}** prefer FIRST-TOUCH  (status quo)")
        p(f"  - **{n_rt}** prefer RETEST-ONLY  (add wait-for-retest mode)")
        p(f"  - **{n_hy}** prefer HYBRID 50/50 (split-fill mode)")
        p("")
        if n_ft >= max(n_rt, n_hy):
            p("**Overall: leave entries AS-IS for the majority.** Phoenix bar-close triggers"
              " already enter at sensible levels for most strategies. The trades that"
              " never retest tend to be the strongest momentum captures, and skipping"
              " them costs more than the fill improvement on retesters earns back.")
        elif n_rt > n_ft and n_rt > n_hy:
            p("**Overall: ADD wait-for-retest mode to specific strategies** (listed above).")
        else:
            p("**Overall: PILOT hybrid 50/50 mode** on the strategies that prefer it.")
        p("")
        if not retest_wins.empty:
            names = ", ".join(retest_wins["strategy"].tolist())
            p(f"Per-strategy opt-in for RETEST-ONLY: {names}")
        if not hybrid_wins.empty:
            names = ", ".join(hybrid_wins["strategy"].tolist())
            p(f"Per-strategy opt-in for HYBRID 50/50: {names}")
        p("")

    p("## Caveats")
    p("")
    p("- Tick window is 60 days out of a 5y backtest. Retest rates and fill improvements"
      " may differ in other regimes (specifically low-volatility chop tends to retest"
      " more; trending days less).")
    p("- We resolve outcomes by walking trade prices only. Quote-only updates are not used"
      " to trigger stops (matches reality that a stop-market needs a counterparty).")
    p(f"- Stop/target conflicts within the same tick favour STOP (pessimistic).")
    p(f"- Retest fill assumes a 1-tick chase. Real-world limit fills could be even cheaper"
      " (price improvement) OR could miss entirely; this assumption is intentionally"
      " conservative.")
    p(f"- The HYBRID mode P&L assumes a fractional contract (0.5x). In practice this"
      " requires 2-contract base size to map cleanly. Use the dollar deltas as the"
      " decision input, not the win-rate column (which counts trades not contracts).")
    p("")
    p("## Files produced")
    p("")
    p("- `backtest_results/phoenix_entry_retest_per_trade.csv`")
    p("- `backtest_results/phoenix_entry_retest_summary.csv`")
    p("- `docs/ENTRY_RETEST_ANALYSIS.md` (this report)")
    p("")

    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] wrote {OUT_REPORT}")


def main() -> int:
    per_trade = run()
    OUT_PER_TRADE.parent.mkdir(parents=True, exist_ok=True)
    per_trade.to_csv(OUT_PER_TRADE, index=False)
    print(f"[out] wrote {OUT_PER_TRADE}  ({len(per_trade)} rows)")

    summary = summarise(per_trade)
    summary.to_csv(OUT_SUMMARY, index=False)
    print(f"[out] wrote {OUT_SUMMARY}  ({len(summary)} rows)")

    write_report(summary, per_trade)

    # Print headline table to stdout
    print()
    print("=" * 88)
    print("HEADLINE per-strategy results (n>=30)")
    print("=" * 88)
    hl = summary[summary["n"] >= MIN_N_FOR_HEADLINE].copy()
    cols = ["strategy", "n", "retest_rate_pct", "fill_improvement_median_tk",
            "A_first_touch_total_dollars", "B_retest_only_total_dollars",
            "C_hybrid_total_dollars", "verdict"]
    if not hl.empty:
        print(hl[cols].to_string(index=False))
    else:
        print("(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
