"""
Phase 0.6 + 0.7 — Snapshot completeness + holdout window split.

Counts non-RECONCILED closed trades for bias_momentum + opening_session.
Computes fraction of winners with all required market_snapshot fields.
Determines DERIVATION (oldest 60d) vs HOLDOUT (most recent 30d) split.
"""
from __future__ import annotations

import sys
import datetime as dt
from collections import Counter
from pathlib import Path

# Ensure we can import phoenix_bot.* from the repo root.
REPO = Path(r"C:\Trading Project\phoenix_bot")
sys.path.insert(0, str(REPO))

from core.trade_memory import load_all_trades  # canonical reader

REQUIRED_SNAPSHOT_FIELDS = [
    "day_type", "cr_verdict", "cvd_health", "regime",
    "momentum_score", "confluence_score",
    "tf_bias_1m", "tf_bias_5m",
    "ema9", "ema21", "vwap", "price",
]


def _is_closed_non_reconciled(t: dict) -> bool:
    status = (t.get("status") or t.get("state") or "").upper()
    # Closed means we have a fill + exit
    if t.get("exit_price") is None and t.get("exit_time") is None:
        return False
    # Filter out RECONCILED test/reconciliation-generated records
    if status == "RECONCILED" or t.get("reconciled") is True:
        return False
    if (t.get("source") or "").lower() == "reconciliation":
        return False
    if (t.get("origin") or "").lower() == "reconciliation":
        return False
    return True


def _entry_dt(t: dict) -> dt.datetime | None:
    for key in ("entry_time", "entry_ts", "entry_iso", "entry_datetime", "open_time"):
        v = t.get(key)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                # epoch seconds
                return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc)
            if isinstance(v, str):
                # ISO
                return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def _pnl(t: dict) -> float | None:
    for key in ("pnl_dollars_net", "pnl_net", "pnl_dollars", "pnl"):
        v = t.get(key)
        if v is not None:
            try:
                return float(v)
            except Exception:
                continue
    return None


def _snapshot(t: dict) -> dict:
    snap = t.get("market_snapshot") or t.get("snapshot") or {}
    if not isinstance(snap, dict):
        return {}
    return snap


def _has_all_snapshot_fields(snap: dict) -> bool:
    for f in REQUIRED_SNAPSHOT_FIELDS:
        v = snap.get(f)
        if v is None or v == "" or v == "UNKNOWN" or v == "unknown":
            return False
    return True


def main() -> None:
    trades = load_all_trades()
    print(f"Total trades loaded: {len(trades)}")

    per_strategy = Counter()
    by_strategy: dict[str, list[dict]] = {}

    for t in trades:
        if not _is_closed_non_reconciled(t):
            continue
        strat = t.get("strategy") or t.get("strategy_name") or "UNKNOWN"
        per_strategy[strat] += 1
        by_strategy.setdefault(strat, []).append(t)

    print("\n=== Closed non-RECONCILED trades per strategy ===")
    for strat, n in sorted(per_strategy.items(), key=lambda x: -x[1]):
        print(f"  {strat:30s} {n}")

    print()
    for target_strat, target_min in [("bias_momentum", 100), ("opening_session", 500)]:
        n = per_strategy.get(target_strat, 0)
        verdict = "OK" if n >= target_min else f"BELOW TARGET ({target_min}) — downgrade conclusions"
        print(f"  {target_strat:30s} {n}  target>={target_min}  {verdict}")

    # ----- Snapshot completeness (winners in last 12 months) -----
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)

    print("\n=== Snapshot completeness (winners, last 12 months) ===")
    for strat in ("bias_momentum", "opening_session"):
        trades_s = by_strategy.get(strat, [])
        winners = []
        for t in trades_s:
            pnl = _pnl(t)
            ed = _entry_dt(t)
            if pnl is None or ed is None:
                continue
            if pnl <= 0:
                continue
            if ed < cutoff:
                continue
            winners.append(t)
        n_winners = len(winners)
        if n_winners == 0:
            print(f"  {strat:30s} 0 winners in last 12mo — N/A")
            continue
        n_complete = sum(1 for w in winners if _has_all_snapshot_fields(_snapshot(w)))
        pct = 100.0 * n_complete / n_winners
        if pct >= 80:
            tier = "FULL (>=80%)"
        elif pct >= 60:
            tier = "DIRECTIONAL (60-80%)"
        else:
            tier = "DEFER PHASE 6 (<60%)"
        print(f"  {strat:30s} winners={n_winners}  complete={n_complete}  {pct:5.1f}%  -> {tier}")

        # field-by-field hit rate to see WHERE we're missing
        miss = Counter()
        for w in winners:
            snap = _snapshot(w)
            for f in REQUIRED_SNAPSHOT_FIELDS:
                v = snap.get(f)
                if v is None or v == "" or v == "UNKNOWN" or v == "unknown":
                    miss[f] += 1
        if miss:
            print(f"    missing fields (count of winners missing each):")
            for f in REQUIRED_SNAPSHOT_FIELDS:
                if miss[f]:
                    print(f"      {f:24s} {miss[f]}/{n_winners}")

    # ----- Holdout window split (last 90 days rich-data window) -----
    print("\n=== Holdout window split (Phase 0.7) ===")
    today = dt.datetime.now(dt.timezone.utc).date()
    rich_window_start = today - dt.timedelta(days=90)
    derivation_start = today - dt.timedelta(days=90)
    derivation_end = today - dt.timedelta(days=30)  # oldest 60d
    holdout_start = today - dt.timedelta(days=30)
    holdout_end = today
    print(f"  Today (UTC date): {today}")
    print(f"  Rich-data window: {rich_window_start} -> {today} (90d)")
    print(f"  DERIVATION SET:   {derivation_start} -> {derivation_end} (oldest 60d)")
    print(f"  HOLDOUT SET:      {holdout_start} -> {holdout_end} (most recent 30d)")

    # Trades per split per strategy
    for strat in ("bias_momentum", "opening_session"):
        trades_s = by_strategy.get(strat, [])
        n_deriv = 0
        n_hold = 0
        n_pre = 0
        for t in trades_s:
            ed = _entry_dt(t)
            if ed is None:
                continue
            d = ed.date()
            if d < derivation_start:
                n_pre += 1
            elif d < derivation_end:
                n_deriv += 1
            elif d <= holdout_end:
                n_hold += 1
        print(f"  {strat:30s} pre_window={n_pre} derivation={n_deriv} holdout={n_hold}")


if __name__ == "__main__":
    main()
