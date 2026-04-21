"""
Phoenix Bot — Transaction Cost Analysis (TCA) Tracker

Measures execution quality: slippage, fill latency, commission impact.
Research shows TCA distinguishes TRUE alpha decay from execution issues —
if slippage is spiking but win rate is stable, the strategy's edge isn't
decaying, the execution pipeline is degrading.

Per-trade record:
  signal_price    = price when signal fired
  fill_price      = actual NT8 fill (from OIF outgoing)
  slippage_ticks  = fill - signal (direction-adjusted: negative = favorable)
  slippage_usd    = ticks × TICK_VALUE_USD
  time_to_fill_ms = time from OIF write to NT8 fill
  fill_type       = "LIMIT" or "MARKET"

Weekly report aggregates by strategy, regime, time-of-day.
Alerts if slippage > 2× rolling 7d median.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("TCA")

PHOENIX_ROOT = Path(__file__).parent.parent
TCA_LOG_FILE = PHOENIX_ROOT / "logs" / "tca_history.jsonl"

# MNQ constants
TICK_SIZE = 0.25
TICK_VALUE_USD = 0.50   # $0.50 per MNQ tick


@dataclass
class TCARecord:
    """One fill's TCA metrics."""
    trade_id: str
    ts: datetime
    strategy: str
    direction: str           # LONG or SHORT
    signal_price: float
    fill_price: float
    slippage_ticks: float    # Direction-adjusted: negative = favorable
    slippage_usd: float
    time_to_fill_ms: int
    fill_type: str           # LIMIT or MARKET
    regime: str
    time_of_day_bucket: str  # e.g., "08:30-09:30"

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "ts": self.ts.isoformat() if isinstance(self.ts, datetime) else self.ts,
            "strategy": self.strategy,
            "direction": self.direction,
            "signal_price": self.signal_price,
            "fill_price": self.fill_price,
            "slippage_ticks": self.slippage_ticks,
            "slippage_usd": self.slippage_usd,
            "time_to_fill_ms": self.time_to_fill_ms,
            "fill_type": self.fill_type,
            "regime": self.regime,
            "time_of_day_bucket": self.time_of_day_bucket,
        }


def _bucket_time(ts: datetime) -> str:
    """Bucket to hour-of-session string for aggregation."""
    hour = ts.hour
    # CDT session buckets
    if 8 <= hour < 9:
        return "08:30-09:30"
    elif 9 <= hour < 10:
        return "09:30-10:30"
    elif 10 <= hour < 11:
        return "10:30-11:30"
    elif 11 <= hour < 13:
        return "LUNCH"
    elif 13 <= hour < 14:
        return "13:00-14:00"
    elif 14 <= hour < 15:
        return "14:00-15:00"
    elif 15 <= hour < 17:
        return "15:00-17:00"
    elif 17 <= hour or hour < 2:
        return "ASIA"
    else:
        return "LONDON"


class TCATracker:
    """Stateful TCA tracker. Appends to tca_history.jsonl."""

    def __init__(self):
        self.records: list[TCARecord] = []
        self._load_history()

    def _load_history(self) -> None:
        """Load last 30 days of TCA history from disk."""
        if not TCA_LOG_FILE.exists():
            return
        cutoff = datetime.now() - timedelta(days=30)
        try:
            with open(TCA_LOG_FILE) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        ts = datetime.fromisoformat(d["ts"].replace("Z", "+00:00"))
                        if ts >= cutoff:
                            rec = TCARecord(
                                trade_id=d["trade_id"], ts=ts,
                                strategy=d["strategy"], direction=d["direction"],
                                signal_price=d["signal_price"], fill_price=d["fill_price"],
                                slippage_ticks=d["slippage_ticks"], slippage_usd=d["slippage_usd"],
                                time_to_fill_ms=d["time_to_fill_ms"], fill_type=d["fill_type"],
                                regime=d.get("regime", "UNKNOWN"),
                                time_of_day_bucket=d.get("time_of_day_bucket", "UNKNOWN"),
                            )
                            self.records.append(rec)
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as e:
            logger.warning(f"TCA history load failed: {e}")

    def record_fill(self, trade_id: str, strategy: str, direction: str,
                    signal_price: float, fill_price: float,
                    time_to_fill_ms: int, fill_type: str = "LIMIT",
                    regime: str = "UNKNOWN", ts: datetime = None) -> TCARecord:
        """Compute slippage + append to log."""
        if ts is None:
            ts = datetime.now()
        # Direction-adjusted slippage
        # LONG: paying more than signal = positive slippage (unfavorable)
        # SHORT: selling for less than signal = positive slippage (unfavorable)
        if direction == "LONG":
            raw_slip = fill_price - signal_price
        else:
            raw_slip = signal_price - fill_price
        slippage_ticks = raw_slip / TICK_SIZE
        slippage_usd = slippage_ticks * TICK_VALUE_USD

        rec = TCARecord(
            trade_id=trade_id, ts=ts, strategy=strategy, direction=direction,
            signal_price=signal_price, fill_price=fill_price,
            slippage_ticks=round(slippage_ticks, 2),
            slippage_usd=round(slippage_usd, 2),
            time_to_fill_ms=time_to_fill_ms, fill_type=fill_type,
            regime=regime,
            time_of_day_bucket=_bucket_time(ts),
        )
        self.records.append(rec)

        # Append to jsonl
        try:
            TCA_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(TCA_LOG_FILE, "a") as f:
                f.write(json.dumps(rec.to_dict()) + "\n")
        except Exception as e:
            logger.warning(f"TCA write failed: {e}")

        return rec

    def rolling_median_slippage(self, days: int = 7) -> float:
        """Median slippage ticks over last N days. For alert threshold."""
        cutoff = datetime.now() - timedelta(days=days)
        recent = [r.slippage_ticks for r in self.records if r.ts >= cutoff]
        return statistics.median(recent) if recent else 0.0

    def check_recent_spike(self, last_n_trades: int = 3) -> Optional[str]:
        """
        Check if last N trades show slippage > 2× rolling median.
        Returns alert string or None.
        """
        if len(self.records) < last_n_trades + 10:
            return None  # Not enough baseline
        median = self.rolling_median_slippage(days=7)
        if median <= 0.5:
            return None  # Baseline too small to care
        recent_trades = self.records[-last_n_trades:]
        avg_recent = statistics.mean([r.slippage_ticks for r in recent_trades])
        if avg_recent > median * 2.0:
            return (f"Slippage spike: last {last_n_trades} trades avg "
                    f"{avg_recent:.1f}t > 2× median {median:.1f}t")
        return None

    def weekly_report(self) -> dict:
        """Aggregate last 7 days by strategy/regime/time-of-day."""
        cutoff = datetime.now() - timedelta(days=7)
        recent = [r for r in self.records if r.ts >= cutoff]
        if not recent:
            return {"trades": 0, "report": "No trades in last 7 days"}

        def agg(records):
            slips = [r.slippage_ticks for r in records]
            return {
                "count": len(records),
                "median_slip_ticks": round(statistics.median(slips), 2),
                "mean_slip_ticks": round(statistics.mean(slips), 2),
                "mean_slip_usd": round(statistics.mean([r.slippage_usd for r in records]), 2),
                "mean_fill_latency_ms": round(statistics.mean([r.time_to_fill_ms for r in records]), 0),
            }

        by_strategy = defaultdict(list)
        by_regime = defaultdict(list)
        by_tod = defaultdict(list)
        for r in recent:
            by_strategy[r.strategy].append(r)
            by_regime[r.regime].append(r)
            by_tod[r.time_of_day_bucket].append(r)

        return {
            "period_start": cutoff.isoformat(),
            "period_end": datetime.now().isoformat(),
            "trade_count": len(recent),
            "overall": agg(recent),
            "by_strategy": {k: agg(v) for k, v in by_strategy.items()},
            "by_regime": {k: agg(v) for k, v in by_regime.items()},
            "by_time_of_day": {k: agg(v) for k, v in by_tod.items()},
        }
