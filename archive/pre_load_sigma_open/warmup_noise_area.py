"""
Phoenix Bot — Noise Area sigma_open warmup loader.

Reads logs/history/YYYY-MM-DD_{prod|lab}.jsonl (1m bar events) and builds
the sigma_open_table required by strategies/noise_area.py. Without this
warmup the strategy sits dormant until 10+ days of live data accumulate.

Output: dict keyed by minute_of_day (0 = 9:30 ET, 30 = 10:00 ET, ... 390 = 16:00 ET)
        with value = list of |close/open_of_day - 1| samples per day.

Usage:
    from tools.warmup_noise_area import load_sigma_open_history
    history = load_sigma_open_history(days=14)
    strategy.seed_history(history)

Session boundaries:
- RTH cash open = 9:30 ET = 8:30 CT (historical bars are stored in local CT time)
- Session close = 16:00 ET = 15:00 CT → minute_of_day 390
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("NoiseAreaWarmup")

_HISTORY_DIR = Path(__file__).resolve().parent.parent / "logs" / "history"


def _parse_bar_event(line: str) -> dict | None:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    if rec.get("event") != "bar":
        return None
    if rec.get("timeframe") != "1m":
        return None
    return rec


def _minute_of_day_ct(bar_ts_iso: str) -> int | None:
    """
    Return minutes since 8:30 CT (= 9:30 ET RTH open).
    Returns None if the bar is outside RTH or the timestamp is unparseable.
    """
    try:
        dt = datetime.fromisoformat(bar_ts_iso)
    except ValueError:
        return None
    open_dt = dt.replace(hour=8, minute=30, second=0, microsecond=0)
    mod = int((dt - open_dt).total_seconds() / 60)
    if mod < 0 or mod > 390:
        return None
    return mod


def load_sigma_open_history(days: int = 14, bot: str = "lab") -> dict[int, list[float]]:
    """
    Load the last N days of 1m bars and return sigma_open samples keyed by minute_of_day.

    Args:
        days: How many trading days of history to load (default 14).
        bot: Which bot log to read ("prod" or "lab"). lab is preferred because it
             runs the full session (prod is restricted to 8:30-11:00 CT).

    Returns:
        dict[minute_of_day] -> list[|close/open_of_day - 1|], newest last.
    """
    if not _HISTORY_DIR.exists():
        logger.warning(f"History dir not found: {_HISTORY_DIR}")
        return {}

    # Pick the N most recent history files for this bot
    files = sorted(_HISTORY_DIR.glob(f"*_{bot}.jsonl"))
    if not files:
        logger.warning(f"No {bot} history files under {_HISTORY_DIR}")
        return {}
    files = files[-days:]

    sigma_open_table: dict[int, list[float]] = defaultdict(list)
    days_processed = 0

    for f in files:
        today_open_price: float | None = None
        samples_for_day: dict[int, float] = {}
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    rec = _parse_bar_event(line)
                    if rec is None:
                        continue
                    ts = rec.get("ts", "")
                    mod = _minute_of_day_ct(ts)
                    if mod is None:
                        continue
                    if today_open_price is None:
                        today_open_price = float(rec.get("open", 0) or 0)
                        if today_open_price <= 0:
                            continue
                    close = float(rec.get("close", 0) or 0)
                    if close <= 0:
                        continue
                    move_open = abs(close / today_open_price - 1)
                    # Keep the last sample per minute-of-day (end-of-minute value)
                    samples_for_day[mod] = move_open
        except OSError as e:
            logger.warning(f"Could not read {f.name}: {e}")
            continue

        if not samples_for_day:
            continue

        for mod, sample in samples_for_day.items():
            sigma_open_table[mod].append(sample)
        days_processed += 1

    logger.info(
        f"[NOISE_AREA_WARMUP] Loaded {days_processed} days "
        f"across {len(sigma_open_table)} minute-buckets from bot='{bot}'"
    )
    return dict(sigma_open_table)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--bot", choices=["lab", "prod"], default="lab")
    args = ap.parse_args()

    hist = load_sigma_open_history(days=args.days, bot=args.bot)
    print(f"Loaded {len(hist)} minute-buckets")
    if hist:
        # Spot check a few buckets
        for mod in sorted(hist.keys())[:5]:
            samples = hist[mod]
            avg = sum(samples) / len(samples) if samples else 0
            print(f"  minute_of_day={mod:3d}: n={len(samples)} mean_move_open={avg:.5f}")
        if len(hist) > 5:
            print("  ...")
        # Sample late session too
        late = [m for m in sorted(hist.keys()) if m >= 300]
        for mod in late[:3]:
            samples = hist[mod]
            avg = sum(samples) / len(samples) if samples else 0
            print(f"  minute_of_day={mod:3d}: n={len(samples)} mean_move_open={avg:.5f}")
