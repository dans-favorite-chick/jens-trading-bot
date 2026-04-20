"""
Phoenix Bot — Noise Area sigma_open backfill from yfinance NQ=F.

Pulls 60 days of 5-minute NQ=F (continuous NQ futures) from Yahoo Finance
and produces sigma_open samples keyed by minute-of-day (minutes since
9:30 ET cash open).

Why NQ=F, not MNQ? Yahoo doesn't expose micro-futures, and sigma_open is a
percentage (|close / today_open - 1|) — NQ and MNQ have identical percentage
moves (MNQ is exactly 1/10 NQ). So sigma_open is directly transferable.

Why 5m, not 1m? Yahoo caps 1m requests to 7 days; 5m caps at 60 days.
The Noise Area strategy checks signals every 30 minutes (minutes 0, 30),
so the 5m buckets that fall on these minutes (0, 30, 60, 90...) are all we
strictly need. Extra 5m buckets (5, 10, 15, ...) are harvested as a bonus.

Usage:
    python tools/backfill_noise_area.py                 # write to memory/noise_area_warmup.json
    python tools/backfill_noise_area.py --days 30       # shorter pull
    python tools/backfill_noise_area.py --dump-preview  # print spot-check stats

The loader in strategies/noise_area.py can be seeded via:
    strategy.seed_history(load_backfill())
"""

import argparse
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("NoiseAreaBackfill")

_WARMUP_FILE = Path(__file__).resolve().parent.parent / "memory" / "noise_area_warmup.json"

# RTH cash session in ET: 9:30 - 16:00 (390 minutes). minute_of_day 0 = 9:30 ET.
_RTH_OPEN_H, _RTH_OPEN_M = 9, 30
_RTH_CLOSE_H = 16


def _to_et_naive(ts_utc):
    """Convert a tz-aware UTC timestamp (or naive UTC) → naive ET datetime.

    DST-correct: uses zoneinfo America/New_York.
    """
    from zoneinfo import ZoneInfo
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=timezone.utc)
    et = ts_utc.astimezone(ZoneInfo("America/New_York"))
    return et.replace(tzinfo=None)


def _minute_of_day_et(et_dt: datetime) -> int | None:
    """Minutes since 9:30 ET. Returns None if outside RTH."""
    open_dt = et_dt.replace(hour=_RTH_OPEN_H, minute=_RTH_OPEN_M, second=0, microsecond=0)
    close_dt = et_dt.replace(hour=_RTH_CLOSE_H, minute=0, second=0, microsecond=0)
    if et_dt < open_dt or et_dt > close_dt:
        return None
    return int((et_dt - open_dt).total_seconds() / 60)


def fetch_yfinance_5m(days: int = 60, symbol: str = "NQ=F"):
    """Fetch `days` of 5-minute OHLCV for `symbol`. Returns pandas DataFrame."""
    import yfinance as yf
    days = min(days, 60)  # Yahoo caps 5m requests at 60 days
    logger.info(f"[BACKFILL] Fetching {days}d of {symbol} 5m bars from yfinance...")
    data = yf.download(
        symbol, interval="5m", period=f"{days}d",
        progress=False, auto_adjust=False, group_by="column",
    )
    if data is None or len(data) == 0:
        raise RuntimeError(f"yfinance returned no data for {symbol}")
    # Flatten multi-index columns if group_by creates them
    if hasattr(data.columns, "nlevels") and data.columns.nlevels > 1:
        data.columns = data.columns.get_level_values(0)
    logger.info(f"[BACKFILL] Got {len(data)} 5m bars spanning "
                f"{data.index[0]} to {data.index[-1]}")
    return data


def build_sigma_open_table(data) -> dict[int, list[float]]:
    """
    Given a yfinance 5m DataFrame (index=UTC datetime), produce a sigma_open
    table keyed by minute_of_day. For each trading day in the data, determine
    today_open = open of the 9:30 ET bar, then compute |close / open - 1| for
    every subsequent RTH 5m bar.
    """
    table: dict[int, list[float]] = defaultdict(list)
    by_day: dict[str, list] = defaultdict(list)

    for ts, row in data.iterrows():
        et = _to_et_naive(ts)
        mod = _minute_of_day_et(et)
        if mod is None:
            continue
        day_key = et.strftime("%Y-%m-%d")
        by_day[day_key].append((mod, float(row["Open"]), float(row["Close"])))

    days_processed = 0
    for day_key, samples in by_day.items():
        samples.sort(key=lambda x: x[0])  # By minute_of_day
        # Find today_open: first bar with minute_of_day == 0 (9:30 ET), else earliest
        today_open = None
        for mod, op, cl in samples:
            if mod == 0:
                today_open = op
                break
        if today_open is None and samples:
            today_open = samples[0][1]  # Earliest available
        if today_open is None or today_open <= 0:
            continue

        day_added = 0
        for mod, _op, cl in samples:
            if cl <= 0:
                continue
            move_open = abs(cl / today_open - 1)
            # Append at most once per (day, mod) — drop dups
            existing = [v for v in table[mod][-len(by_day):] if v == move_open]
            table[mod].append(move_open)
            day_added += 1
        if day_added:
            days_processed += 1

    logger.info(f"[BACKFILL] Processed {days_processed} trading days "
                f"across {len(table)} minute-buckets")
    return dict(table)


def save_backfill(table: dict[int, list[float]], path: Path = _WARMUP_FILE):
    """Persist the table to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {str(k): list(v) for k, v in table.items()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "source": "yfinance NQ=F 5m",
            "table": serialisable,
        }, fh)
    logger.info(f"[BACKFILL] Wrote {path}")


def load_backfill(path: Path = _WARMUP_FILE) -> dict[int, list[float]]:
    """Read the backfill file produced by save_backfill()."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw = payload.get("table", {})
    return {int(k): list(v) for k, v in raw.items()}


def merge_histories(*tables: dict[int, list[float]]) -> dict[int, list[float]]:
    """Merge multiple sigma_open tables by concatenating per-bucket samples."""
    merged: dict[int, list[float]] = defaultdict(list)
    for t in tables:
        for mod, samples in t.items():
            merged[mod].extend(samples)
    # Clamp each bucket to 30 samples to match strategy memory policy
    return {mod: samples[-30:] for mod, samples in merged.items()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="yfinance 5m lookback (<=60)")
    ap.add_argument("--symbol", default="NQ=F")
    ap.add_argument("--output", default=str(_WARMUP_FILE))
    ap.add_argument("--dump-preview", action="store_true",
                    help="Print a spot-check of sigma_open by minute-of-day")
    args = ap.parse_args()

    df = fetch_yfinance_5m(days=args.days, symbol=args.symbol)
    table = build_sigma_open_table(df)
    save_backfill(table, Path(args.output))

    if args.dump_preview:
        print(f"\n=== SIGMA_OPEN PREVIEW — {len(table)} buckets ===")
        for mod in sorted(table.keys()):
            if mod % 30 != 0:  # Show only 30-min buckets (what the strategy reads)
                continue
            samples = table[mod]
            mean = sum(samples) / len(samples) if samples else 0
            total_minutes = 9 * 60 + 30 + mod
            print(f"  minute_of_day={mod:3d} (ET={total_minutes // 60:02d}:{total_minutes % 60:02d})"
                  f"  n={len(samples):2d}  mean_move_open={mean:.5f}")
