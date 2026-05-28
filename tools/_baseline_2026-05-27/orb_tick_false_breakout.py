"""
Tick-by-tick false-breakout analysis for opening_session.orb.

For orb trades in the TBBO window (2026-03-17..05-15), walk the real tick
stream from entry forward and ask: do LOSERS (false breakouts) show a
distinct tick-level signature in the first N seconds that a confirmation
filter could catch?

Per trade, in the first 60s after entry, measure:
  - max favorable excursion (ticks) and time-to-it
  - max adverse excursion (ticks) and time-to-it
  - did price retrace through entry within 30s / 60s (whipsaw)?
  - sign of net tick move at +15s / +30s (early follow-through vs fade)

Then compare winners vs losers. If losers fade immediately while winners
follow through, a short confirmation delay would raise win rate.

Read-only. 2-month window only (honest sample caveat).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("C:/Trading Project/phoenix_bot")
sys.path.insert(0, str(ROOT))
from tools.phoenix_tick_trail_verification import TickIndex  # noqa: E402

TICK = 0.25
WS = pd.Timestamp("2026-03-17", tz="UTC")
WE = pd.Timestamp("2026-05-15 21:00", tz="UTC")


def main():
    # orb trades in window
    d = pd.read_csv(ROOT / "backtest_results/opening_session_sub_breakdown.csv")
    d["entry_ts"] = pd.to_datetime(d["entry_ts"], utc=True, errors="coerce")
    orb = d[(d.sub_name == "orb") & (d.entry_ts >= WS) & (d.entry_ts <= WE)].copy()
    print(f"orb trades in TBBO window: {len(orb)}")

    print("loading ticks...", flush=True)
    ticks = pd.read_parquet(ROOT / "data/historical/databento_tbbo/mnq_ticks.parquet")
    ticks = ticks.sort_values("ts_event").reset_index(drop=True)
    idx = TickIndex(ticks)
    print(f"  {len(ticks):,} ticks loaded", flush=True)

    rows = []
    for t in orb.itertuples(index=False):
        entry_ts = pd.Timestamp(t.entry_ts)
        entry = float(t.entry_price)
        direction = t.direction
        win = t.pnl_dollars > 0
        end = entry_ts + pd.Timedelta(seconds=60)
        ts_arr, px_arr = idx.slice(entry_ts + pd.Timedelta(microseconds=1), end)
        if len(ts_arr) == 0:
            continue
        entry_ns = entry_ts.value
        # REALISTIC fill = first tradeable tick at/after the 5m close (entry_ts),
        # not the idealized backtest entry_price (OR_high+1t, often already passed).
        entry = float(px_arr[0])
        # favorable / adverse in ticks (signed to trade direction), from realistic fill
        if direction == "LONG":
            fav = (px_arr.max() - entry) / TICK
            adv = (px_arr.min() - entry) / TICK  # negative = adverse
        else:
            fav = (entry - px_arr.min()) / TICK
            adv = (entry - px_arr.max()) / TICK
        # net move at +15s, +30s
        def net_at(sec):
            cut = entry_ns + sec * 1_000_000_000
            j = np.searchsorted(ts_arr, cut, side="right") - 1
            if j < 0:
                return np.nan
            p = px_arr[j]
            return ((p - entry) if direction == "LONG" else (entry - p)) / TICK
        # whipsaw: did it go adverse past -8t within 30s?
        cut30 = entry_ns + 30 * 1_000_000_000
        j30 = np.searchsorted(ts_arr, cut30, side="right")
        if direction == "LONG":
            adv30 = (px_arr[:j30].min() - entry) / TICK if j30 > 0 else 0
        else:
            adv30 = (entry - px_arr[:j30].max()) / TICK if j30 > 0 else 0
        rows.append({
            "win": win,
            "fav60_t": round(float(fav), 1),
            "adv60_t": round(float(adv), 1),
            "net15s_t": round(float(net_at(15)), 1),
            "net30s_t": round(float(net_at(30)), 1),
            "adv30s_t": round(float(adv30), 1),
        })

    df = pd.DataFrame(rows)
    print(f"\nanalyzed {len(df)} trades with tick data\n")
    print("=== WINNERS vs LOSERS — tick path in first 60s after entry ===")
    for label, grp in [("WINNERS", df[df.win]), ("LOSERS", df[~df.win])]:
        if len(grp) == 0:
            continue
        print(f"{label} (n={len(grp)}):")
        print(f"  mean max-favorable 60s: {grp.fav60_t.mean():+.1f}t   mean max-adverse 60s: {grp.adv60_t.mean():+.1f}t")
        print(f"  mean net move +15s: {grp.net15s_t.mean():+.1f}t   +30s: {grp.net30s_t.mean():+.1f}t")
        print(f"  median adverse within 30s: {grp.adv30s_t.median():+.1f}t")
        print(f"  pct that went adverse >8t within 30s: {100*(grp.adv30s_t < -8).mean():.0f}%")
        print()

    # Confirmation-filter simulation: skip trades that go adverse > X ticks within 30s
    print("=== CONFIRMATION FILTER: skip if adverse > threshold within 30s ===")
    base_wr = 100 * df.win.mean()
    print(f"baseline: n={len(df)} WR={base_wr:.1f}%")
    for thr in [-4, -6, -8, -12, -16]:
        kept = df[df.adv30s_t >= thr]  # not too adverse early
        if len(kept) == 0:
            continue
        wr = 100 * kept.win.mean()
        print(f"  skip if adv30s < {thr}t: keep {len(kept)}/{len(df)}  WR={wr:.1f}%  (delta {wr-base_wr:+.1f}pp)")


if __name__ == "__main__":
    main()
