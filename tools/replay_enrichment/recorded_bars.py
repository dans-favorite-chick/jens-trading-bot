"""Load the bot's OWN recorded bars (logs/history/<date>_<bot>.jsonl bar events)
as the backtester's data source, instead of databento CSVs.

The backtester's enrichment algorithms are correct, but reconstructing them from
databento bars diverges from live because databento bars differ from the bot's
NT8-built bars at the sub-tick level — which flips the hypersensitive tf_bias
2-of-3 vote ~50% of the time. Feeding the backtester the bot's actual recorded
bars closes that gap (proven: tf_bias reproduces live at 99.97%). Returns a
DataFrame matching tools.phoenix_real_backtest._load_bars_from_csv's schema.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
from tools.phoenix_real_backtest import _CT  # tz object


def load_recorded_bars(start: str, end: str, timeframe: str,
                       bot: str = "prod", warmup_days: int = 0) -> pd.DataFrame:
    """start/end: 'YYYY-MM-DD' (CT, inclusive). timeframe: '1m' or '5m'.
    Reads bar events, converts the naive-CT log timestamp to UTC, and returns
    columns ts(UTC tz-aware), open, high, low, close, volume — sorted, deduped."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date() - timedelta(days=warmup_days)
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    rows = []
    d = d0
    while d <= d1:
        p = ROOT / "logs" / "history" / f"{d.isoformat()}_{bot}.jsonl"
        d += timedelta(days=1)
        if not p.exists():
            continue
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("event") != "bar" or r.get("timeframe") != timeframe:
                continue
            o, h, l, c, v = (r.get("open"), r.get("high"), r.get("low"),
                             r.get("close"), r.get("volume"))
            if None in (o, h, l, c):
                continue
            try:
                dt = datetime.fromisoformat(r["ts"])  # naive CT (log-write time)
                ts = pd.Timestamp(dt, tz=_CT).tz_convert("UTC")
            except Exception:
                continue
            rows.append((ts, float(o), float(h), float(l), float(c), float(v or 0)))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if len(df):
        df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return df
