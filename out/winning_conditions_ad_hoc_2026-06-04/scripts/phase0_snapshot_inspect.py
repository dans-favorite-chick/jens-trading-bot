"""
Inspect raw snapshot field names on recent bias_momentum + opening_session
trades to confirm or refute the Phase 0.6 zero-completeness verdict.
"""
from __future__ import annotations

import sys
import json
import datetime as dt
from collections import Counter
from pathlib import Path

REPO = Path(r"C:\Trading Project\phoenix_bot")
sys.path.insert(0, str(REPO))

from core.trade_memory import load_all_trades


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


def main() -> None:
    trades = load_all_trades()
    bm = [t for t in trades if (t.get("strategy") or "") == "bias_momentum"]
    # sort by entry_dt desc
    bm.sort(key=lambda t: _entry_dt(t) or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)

    print(f"bias_momentum trades: {len(bm)}")
    print()
    # show 3 recent trades' full top-level keys + snapshot keys
    for i, t in enumerate(bm[:3]):
        print(f"=== bias_momentum recent #{i+1} — entry={_entry_dt(t)} ===")
        print("  top-level keys:", sorted(t.keys()))
        snap = t.get("market_snapshot") or t.get("snapshot") or {}
        if not isinstance(snap, dict):
            print("  snapshot is not a dict:", type(snap).__name__)
            continue
        print(f"  snapshot keys ({len(snap)}):", sorted(snap.keys()))
        # show field values for the ones the audit was looking for
        for f in ["day_type","cr_verdict","cvd_health","regime","momentum_score",
                  "confluence_score","tf_bias_1m","tf_bias_5m","ema9","ema21",
                  "vwap","price","precision","atr_5m","cvd","cvd_session","bar_delta"]:
            print(f"    {f:24s} = {snap.get(f)!r}")
        print()

    # Aggregate which snapshot fields ARE populated across last 30 bm trades
    print("=== Snapshot field hit rates — last 30 bias_momentum trades ===")
    recent = bm[:30]
    field_hits = Counter()
    for t in recent:
        snap = t.get("market_snapshot") or t.get("snapshot") or {}
        if isinstance(snap, dict):
            for k, v in snap.items():
                if v is not None and v != "" and v != "UNKNOWN":
                    field_hits[k] += 1
    for k, n in sorted(field_hits.items(), key=lambda x: -x[1]):
        print(f"  {k:32s} {n}/30")


if __name__ == "__main__":
    main()
