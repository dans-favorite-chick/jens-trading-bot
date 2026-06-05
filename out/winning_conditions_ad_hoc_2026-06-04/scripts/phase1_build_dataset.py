"""
Phase 1 — Build the winning-conditions analysis dataset.

One row per closed non-RECONCILED bias_momentum or opening_session trade.
Columns: identity + entry-time market state (from snapshot) + tick-window
features (from TBBO, where available) + entry-timing features +
regime-details.

Output: /c/tmp/winning_conditions/wc_dataset.parquet
        /c/tmp/winning_conditions/wc_dataset.json  (also; for inspection)
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(r"C:\Trading Project\phoenix_bot")
sys.path.insert(0, str(REPO))

from core.trade_memory import load_all_trades  # canonical reader
from tools.tbbo_cache_builder import load_clean_ticks

OUT_DIR = Path(r"C:\tmp\winning_conditions")
OUT_DIR.mkdir(parents=True, exist_ok=True)
DATASET_PATH = OUT_DIR / "wc_dataset.parquet"

# Strategy scope
TARGET_STRATEGIES = ("bias_momentum", "opening_session")

# TBBO window (from filename and the cache builder)
TBBO_START = dt.datetime(2026, 3, 17, tzinfo=dt.timezone.utc)
TBBO_END = dt.datetime(2026, 5, 17, tzinfo=dt.timezone.utc)

# Holdout split (Phase 0.7)
TODAY = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc).date()
DERIVATION_START = TODAY - dt.timedelta(days=90)
DERIVATION_END = TODAY - dt.timedelta(days=30)
HOLDOUT_START = TODAY - dt.timedelta(days=30)
HOLDOUT_END = TODAY

TICK_SIZE = 0.25  # MNQ tick size in points; $0.50/tick dollar value


def _entry_dt(t: dict) -> dt.datetime | None:
    for key in ("entry_time", "entry_ts", "entry_iso", "entry_datetime", "open_time"):
        v = t.get(key)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc)
            if isinstance(v, str):
                return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def _exit_dt(t: dict) -> dt.datetime | None:
    for key in ("exit_time", "exit_ts", "exit_iso", "exit_datetime", "close_time"):
        v = t.get(key)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc)
            if isinstance(v, str):
                return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def _is_closed_non_reconciled(t: dict) -> bool:
    status = (t.get("status") or t.get("state") or "").upper()
    result = (t.get("result") or "").upper()
    if t.get("exit_price") is None and t.get("exit_time") is None:
        return False
    if status == "RECONCILED" or t.get("reconciled") is True:
        return False
    src = (t.get("source") or "").lower()
    if "reconcil" in src:
        return False
    orig = (t.get("origin") or "").lower()
    if "reconcil" in orig:
        return False
    tid = str(t.get("trade_id") or "")
    if tid.startswith("RECONCILED"):
        return False
    # Exclude NO_FILL records — phantom-NT8 silent rejections that have no
    # entry_time and no realized PnL. They're operationally distinct from
    # closed trades and must not contaminate winner/loser stats.
    if result == "NO_FILL":
        return False
    if t.get("entry_time") is None:
        return False
    return True


def _net_pnl(t: dict) -> float | None:
    """Robust net-pnl extraction across the legacy and modern trade_memory
    formats. Priority: pnl_dollars_net (modern), pnl_dollars (canonical
    100%-populated field), gross_pnl - commission (older), price derivation
    fallback."""
    for k in ("pnl_dollars_net", "pnl_dollars"):
        v = t.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                continue
    # Fallback: gross minus commission
    gp = t.get("pnl_dollars_gross") or t.get("gross_pnl")
    cm = t.get("commission_dollars") or t.get("commission")
    try:
        if gp is not None:
            return float(gp) - (float(cm) if cm is not None else 0.0)
    except Exception:
        pass
    # Last resort: derive from prices
    try:
        ep = float(t.get("entry_price"))
        xp = float(t.get("exit_price"))
        contracts = float(t.get("contracts") or 1)
        direction = (t.get("direction") or "").upper()
        sign = 1.0 if direction == "LONG" else -1.0
        # MNQ: $2.00 per point per contract (micro)
        gross = (xp - ep) * sign * contracts * 2.0
        comm = float(t.get("commission_dollars") or t.get("commission") or 0.0)
        return gross - comm
    except Exception:
        return None


SNAPSHOT_FLAT_FIELDS = [
    "regime", "day_type", "day_type_reason", "cr_verdict", "cr_direction",
    "cr_mom_score", "cr_confidence", "cr_at_resistance", "cr_at_support",
    "mq_direction_bias",
    "cvd_health", "cvd_health_short", "cvd_method", "cvd",
    "bar_delta", "bar_buy_vol", "bar_sell_vol",
    "vol_climax_ratio", "vsa_signal_5m",
    "tf_bias", "tf_bias_tick", "tf_votes_bullish", "tf_votes_bearish",
    "ema9", "ema21", "ema5", "ema9_15m", "ema21_15m",
    "vwap", "vwap_std", "vwap_upper1", "vwap_lower1",
    "avwap_pd_high", "avwap_pd_low", "avwap_pd_close",
    "atr_1m", "atr_5m", "atr_15m", "atr_60m", "atr_tick",
    "macd_line", "macd_signal", "macd_histogram", "macd_histogram_prev", "macd_warm",
    "microstructure",
    "dom_imbalance", "dom_bid_heavy", "dom_ask_heavy", "dom_depth", "dom_signal",
    "es_nq_rs",
    "prior_day_high", "prior_day_low", "prior_day_close",
    "prior_day_poc", "prior_day_vah", "prior_day_val",
    "pivot_pp", "pivot_r1", "pivot_r2", "pivot_s1", "pivot_s2",
    "pmh", "pml", "opening_holds_outside_at_845", "opening_type",
    "orb_first_break_direction",
    "rth_open_price", "rth_5min_high", "rth_5min_low",
    "rth_15min_high", "rth_15min_low",
    "rth_60min_high", "rth_60min_low",
    "price", "signal_price",
    "fill_latency_ms", "now_ct",
]


def _extract_snapshot(snap: dict) -> dict:
    """Flatten snapshot fields into row columns."""
    out: dict[str, Any] = {}
    if not isinstance(snap, dict):
        return out

    for f in SNAPSHOT_FLAT_FIELDS:
        v = snap.get(f)
        # Don't try to flatten the nested cvd_health dict — keep just the veto bool
        if f == "cvd_health":
            if isinstance(v, dict):
                out["cvd_health_veto"] = v.get("veto")
                out["cvd_health_agreement"] = v.get("agreement")
                out["cvd_health_reason"] = v.get("reason")
            continue
        if f == "microstructure":
            # microstructure is a dict; extract a few useful keys
            if isinstance(v, dict):
                out["ms_bid_size_avg"] = v.get("bid_size_avg")
                out["ms_ask_size_avg"] = v.get("ask_size_avg")
                out["ms_avg_spread"] = v.get("avg_spread")
            continue
        if isinstance(v, (dict, list)):
            # skip non-scalar
            continue
        out[f] = v
    return out


def _session_phase_ct(ts_utc: dt.datetime | None) -> str:
    if ts_utc is None:
        return "UNKNOWN"
    # Convert UTC to America/Chicago (CT). Naive offset OK for direction.
    # CT is UTC-5 (CDT) in this window (Mar–Jun = DST active).
    ct = ts_utc - dt.timedelta(hours=5)
    h = ct.hour
    if 6 <= h < 8:
        return "PREMARKET"
    if 8 <= h < 10:
        return "OPEN"
    if 10 <= h < 12:
        return "MORNING"
    if 12 <= h < 13:
        return "LUNCH"
    if 13 <= h < 14:
        return "AFTERNOON"
    if 14 <= h < 15:
        return "CLOSE"
    return "OVERNIGHT"


# --------------------------------------------------------------------
# Tick-window features (TBBO-derived)
# --------------------------------------------------------------------


def compute_tick_window_features(
    ticks: pd.DataFrame,
    entry_time: dt.datetime,
    entry_price: float,
    direction: str,
    atr_5m: float | None,
) -> dict[str, Any]:
    """
    ticks: pre-sliced DataFrame with ts_event index ∈ [entry-5m, entry+5m].
    Returns the tick-window + entry-timing feature dict.
    """
    out: dict[str, Any] = {}
    if ticks is None or ticks.empty:
        return out

    et_ts = pd.Timestamp(entry_time)
    before = ticks.loc[:et_ts]
    after = ticks.loc[et_ts:]

    # tick counts
    out["tick_count_5m_before"] = int(len(before))
    out["tick_count_5m_after"] = int(len(after))

    # side='A' = aggressor=buyer (ask hit). side='B' = aggressor=seller (bid hit).
    if not before.empty:
        side = before["side"]
        size = before["size"]
        ask_vol_before = int(size[side == "A"].sum())
        bid_vol_before = int(size[side == "B"].sum())
        out["ask_volume_5m_before"] = ask_vol_before
        out["bid_volume_5m_before"] = bid_vol_before
        total_vol = ask_vol_before + bid_vol_before
        if total_vol > 0:
            # delta_aligned_ratio_5m: fraction of aggressor volume aligned with trade direction
            if direction == "LONG":
                aligned = ask_vol_before  # buyers aggressing
            else:
                aligned = bid_vol_before  # sellers aggressing
            out["delta_aligned_ratio_5m"] = aligned / total_vol
        # spread
        spread = before["ask_px_00"] - before["bid_px_00"]
        out["spread_avg_5m"] = float(spread.mean()) if not spread.empty else None
        out["spread_max_5m"] = float(spread.max()) if not spread.empty else None
        # price range
        hi = float(before["price"].max())
        lo = float(before["price"].min())
        out["price_5m_high"] = hi
        out["price_5m_low"] = lo
        out["price_range_5m_ticks"] = (hi - lo) / TICK_SIZE
        # position of entry in 5m bar (0=low, 1=high)
        if hi > lo:
            out["price_position_in_5m_bar"] = (entry_price - lo) / (hi - lo)
        # distance from 5m extremes
        out["distance_from_5m_high_ticks"] = (hi - entry_price) / TICK_SIZE
        out["distance_from_5m_low_ticks"] = (entry_price - lo) / TICK_SIZE
        # CVD slope before entry (signed delta vol / minute over preceding 5m)
        cvd_delta_5m = ask_vol_before - bid_vol_before
        out["cvd_slope_5m_per_min"] = cvd_delta_5m / 5.0
        # 1m slope = last minute of the before-window
        last_1m = before.loc[et_ts - pd.Timedelta(minutes=1) :]
        if not last_1m.empty:
            side1 = last_1m["side"]
            size1 = last_1m["size"]
            ask1 = int(size1[side1 == "A"].sum())
            bid1 = int(size1[side1 == "B"].sum())
            out["cvd_slope_1m"] = ask1 - bid1
        # cvd acceleration: slope_1m vs slope_5m_per_min
        if "cvd_slope_5m_per_min" in out and "cvd_slope_1m" in out:
            out["cvd_acceleration"] = out["cvd_slope_1m"] - out["cvd_slope_5m_per_min"]

    # pullback_flag: TRUE if price moved AGAINST direction by > 0.5 * atr_5m
    # in the 2–5 minute window preceding entry.
    if atr_5m and not before.empty:
        pre2 = before.loc[
            et_ts - pd.Timedelta(minutes=5) : et_ts - pd.Timedelta(minutes=2)
        ]
        if not pre2.empty:
            p_start = float(pre2["price"].iloc[0])
            p_end_pre = float(pre2["price"].iloc[-1])
            adverse_move = (p_end_pre - p_start) if direction == "SHORT" else (p_start - p_end_pre)
            out["adverse_pre_move_ticks"] = adverse_move / TICK_SIZE
            out["pullback_flag"] = bool(adverse_move > 0.5 * atr_5m)

    return out


# --------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    print("Loading all trades via core.trade_memory.load_all_trades() ...")
    trades = load_all_trades()
    print(f"  total trades: {len(trades)}")

    rows: list[dict[str, Any]] = []
    for t in trades:
        if not _is_closed_non_reconciled(t):
            continue
        strat = t.get("strategy") or ""
        if strat not in TARGET_STRATEGIES:
            continue
        ed = _entry_dt(t)
        xd = _exit_dt(t)
        if ed is None:
            continue

        row: dict[str, Any] = {
            "trade_id": t.get("trade_id"),
            "strategy": strat,
            "sub_strategy": t.get("sub_strategy"),
            "bot_id": t.get("bot_id"),
            "account": t.get("account"),
            "entry_time_iso": ed.isoformat(),
            "entry_time_epoch": ed.timestamp(),
            "exit_time_iso": xd.isoformat() if xd else None,
            "exit_time_epoch": xd.timestamp() if xd else None,
            "direction": t.get("direction"),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "stop_price": t.get("stop_price"),
            "target_price": t.get("target_price"),
            "entry_reason": t.get("entry_reason"),
            "exit_reason": t.get("exit_reason"),
            "result": t.get("result"),
            "tier": t.get("tier"),
            "contracts": t.get("contracts"),
            "commission_dollars": t.get("commission_dollars"),
            "exchange_fees_dollars": t.get("exchange_fees_dollars"),
            "slippage_dollars": t.get("slippage_dollars"),
            "pnl_dollars_gross": t.get("pnl_dollars_gross") or t.get("gross_pnl"),
            "pnl_dollars_net": _net_pnl(t),
            "pnl_dollars_raw": t.get("pnl_dollars"),
            "pnl_ticks": t.get("pnl_ticks"),
            "r_distance": t.get("r_distance"),
            "r_multiple": t.get("r_multiple"),
            "mae_price": t.get("mae_price"),
            "mae_ticks": t.get("mae_ticks"),
            "mfe_price": t.get("mfe_price"),
            "mfe_ticks": t.get("mfe_ticks"),
            "mfe_capture_pct": t.get("mfe_capture_pct"),
            "hold_time_s": t.get("hold_time_s"),
        }
        # Derived: WIN flag — prefer canonical `result` (100% populated),
        # fall back to pnl sign.
        res = (t.get("result") or "").upper()
        if res == "WIN":
            row["win"] = True
        elif res == "LOSS":
            row["win"] = False
        else:
            pnl = row.get("pnl_dollars_net")
            try:
                row["win"] = bool(float(pnl) > 0) if pnl is not None else None
            except Exception:
                row["win"] = None
        row["result_label"] = res or None

        ed_date = ed.date()
        if DERIVATION_START <= ed_date < DERIVATION_END:
            row["split"] = "DERIVATION"
        elif HOLDOUT_START <= ed_date <= HOLDOUT_END:
            row["split"] = "HOLDOUT"
        elif ed_date < DERIVATION_START:
            row["split"] = "PRE_WINDOW"
        else:
            row["split"] = "POST_WINDOW"

        row["entry_in_tbbo_window"] = TBBO_START <= ed <= TBBO_END
        row["session_phase_ct"] = _session_phase_ct(ed)

        # Flat snapshot
        snap = t.get("market_snapshot") or {}
        row.update(_extract_snapshot(snap))

        # R-multiple recompute when missing.  r_multiple = pnl_dollars_net /
        # |entry_price - stop_price| * 2.0  (MNQ $2/point)
        try:
            if row.get("r_multiple") is None:
                ep = float(row["entry_price"]) if row["entry_price"] is not None else None
                sp = float(row["stop_price"]) if row["stop_price"] is not None else None
                pnl_n = row.get("pnl_dollars_net")
                contracts = float(row.get("contracts") or 1)
                if (
                    ep is not None and sp is not None and ep != sp
                    and pnl_n is not None
                ):
                    r_distance_dollars = abs(ep - sp) * 2.0 * contracts  # risk in $
                    if r_distance_dollars > 0:
                        row["r_multiple_computed"] = float(pnl_n) / r_distance_dollars
                        row["r_distance_dollars"] = r_distance_dollars
        except Exception:
            pass

        rows.append(row)

    print(f"  filtered to {len(rows)} target-strategy closed non-RECONCILED trades")
    df = pd.DataFrame(rows)
    print(f"  base dataset shape: {df.shape}")
    print(f"  per-strategy counts:")
    print(df["strategy"].value_counts().to_string())
    print(f"  per-split counts:")
    print(df.groupby(["strategy", "split"]).size().to_string())

    # --- TBBO enrichment ---
    print(f"\n[{time.time()-t0:5.1f}s] Loading TBBO clean parquet ...")
    ticks = load_clean_ticks()
    print(f"  TBBO shape: {ticks.shape}")
    print(f"  TBBO ts range: {ticks.index.min()} -> {ticks.index.max()}")

    # Iterate trades within TBBO window; for each, slice ±5m and compute features.
    in_window_mask = df["entry_in_tbbo_window"]
    n_enriched = 0
    enrichment_records: list[dict[str, Any]] = []
    for idx, row in df[in_window_mask].iterrows():
        et_iso = row["entry_time_iso"]
        et = dt.datetime.fromisoformat(et_iso.replace("Z", "+00:00"))
        et_ts = pd.Timestamp(et)
        slice_start = et_ts - pd.Timedelta(minutes=5)
        slice_end = et_ts + pd.Timedelta(minutes=5)
        try:
            sub = ticks.loc[slice_start:slice_end]
        except Exception:
            sub = None
        if sub is None or sub.empty:
            continue
        feats = compute_tick_window_features(
            ticks=sub,
            entry_time=et,
            entry_price=float(row["entry_price"]) if row["entry_price"] is not None else 0.0,
            direction=row.get("direction") or "",
            atr_5m=row.get("atr_5m"),
        )
        feats["trade_id"] = row["trade_id"]
        enrichment_records.append(feats)
        n_enriched += 1
        if n_enriched % 25 == 0:
            print(f"    [{time.time()-t0:5.1f}s] enriched {n_enriched} trades...")

    print(f"  TBBO enrichment complete: {n_enriched} trades enriched")
    if enrichment_records:
        enr = pd.DataFrame(enrichment_records).set_index("trade_id")
        df = df.merge(enr, how="left", left_on="trade_id", right_index=True)
        print(f"  post-enrichment dataset shape: {df.shape}")

    # Add derived entry-timing features from snapshot (always available)
    if "ema9" in df.columns and "entry_price" in df.columns:
        df["distance_from_ema9_ticks"] = (
            (pd.to_numeric(df["entry_price"], errors="coerce")
             - pd.to_numeric(df["ema9"], errors="coerce"))
            / TICK_SIZE
        )
    if "vwap" in df.columns and "entry_price" in df.columns:
        df["distance_from_vwap_ticks"] = (
            (pd.to_numeric(df["entry_price"], errors="coerce")
             - pd.to_numeric(df["vwap"], errors="coerce"))
            / TICK_SIZE
        )

    # Save
    print(f"\n[{time.time()-t0:5.1f}s] Saving dataset to {DATASET_PATH}")
    # Coerce object columns to keep parquet writer happy
    df_to_save = df.copy()
    for c in df_to_save.columns:
        if df_to_save[c].dtype == "object":
            df_to_save[c] = df_to_save[c].astype("string")
    df_to_save.to_parquet(DATASET_PATH, index=False)
    print(f"  saved {len(df_to_save)} rows, {len(df_to_save.columns)} columns")
    print(f"  total elapsed: {time.time()-t0:.1f}s")

    # Brief computability summary per feature
    print("\n=== Feature computability rates (non-null fraction) ===")
    nn = df.notna().mean().sort_values(ascending=False)
    for col, frac in nn.items():
        print(f"  {col:36s} {frac*100:5.1f}%")


if __name__ == "__main__":
    main()
