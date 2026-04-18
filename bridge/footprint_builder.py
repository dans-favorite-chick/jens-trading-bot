"""
Phoenix Bot — Footprint Builder (bridge-side)

Consumes the existing tick stream (price, bid, ask, vol, ts) and builds
aggressor-classified per-bar footprint bars. No NT8 changes required.

Research basis (2026):
- Aggressor classification: trade at ≥ ask = aggressive BUY, at ≤ bid = aggressive SELL
- Trade between bid/ask = ambiguous → assign by tick rule (uptick = buy, downtick = sell)
- Per-price-per-bar aggregation reveals stacked imbalances and absorption
- Footprint patterns are ~85% of Order Flow+ volumetric value without NT8 C# changes

Integration:
- base_bot subscribes to tick stream, forwards ticks to FootprintAccumulator
- On 1m/5m bar close, get the completed footprint bar for pattern detection
- core/footprint_patterns.py consumes the footprint bars
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("Footprint")

# MNQ tick size — aggregate volume per bucket
DEFAULT_PRICE_BUCKET = 0.25

# Stale quote threshold — if bid/ask older than this, use tick rule fallback
STALE_QUOTE_MS = 100


@dataclass
class FootprintBar:
    """A single completed footprint bar."""
    ts_open: datetime
    ts_close: datetime
    bar_length_s: int        # 60 = 1m, 300 = 5m
    open_price: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    total_volume: float = 0.0

    # Aggressor volumes per bucketed price
    # {price_bucket: volume_hitting_ask}  → buy-side (aggressive buyers)
    bid_volume_at_price: dict[float, float] = field(default_factory=dict)  # Volume hitting bid (sellers)
    ask_volume_at_price: dict[float, float] = field(default_factory=dict)  # Volume hitting ask (buyers)
    ambiguous_volume_at_price: dict[float, float] = field(default_factory=dict)

    def bar_delta(self) -> float:
        """Sum of ask_vol - bid_vol across all prices. Positive = buyer aggression dominant."""
        ask_total = sum(self.ask_volume_at_price.values())
        bid_total = sum(self.bid_volume_at_price.values())
        return ask_total - bid_total

    def buy_volume(self) -> float:
        return sum(self.ask_volume_at_price.values())

    def sell_volume(self) -> float:
        return sum(self.bid_volume_at_price.values())

    def delta_ratio(self) -> float:
        """Buy volume as fraction of total classified volume. 0.5 = balanced."""
        buy = self.buy_volume()
        sell = self.sell_volume()
        total = buy + sell
        if total <= 0:
            return 0.5
        return buy / total

    def all_prices(self) -> list[float]:
        keys = set(self.bid_volume_at_price.keys())
        keys.update(self.ask_volume_at_price.keys())
        keys.update(self.ambiguous_volume_at_price.keys())
        return sorted(keys)

    def imbalance_at_price(self, price: float) -> tuple[float, float, str]:
        """
        Return (bid_vol, ask_vol, classification) at a specific price.
        classification ∈ "BID_IMBALANCE" (sellers dominating) | "ASK_IMBALANCE" | "BALANCED"
        Imbalance defined as ratio ≥ 3:1 in one direction.
        """
        bid = self.bid_volume_at_price.get(price, 0.0)
        ask = self.ask_volume_at_price.get(price, 0.0)
        if bid > 0 and ask / max(bid, 1e-9) >= 3.0:
            return (bid, ask, "ASK_IMBALANCE")
        if ask > 0 and bid / max(ask, 1e-9) >= 3.0:
            return (bid, ask, "BID_IMBALANCE")
        return (bid, ask, "BALANCED")


class FootprintAccumulator:
    """
    Stateful tick → footprint-bar builder.
    Call process_tick() for each tick, close_bar() at each bar boundary.
    """

    def __init__(self, bar_length_s: int = 60, bucket_size: float = DEFAULT_PRICE_BUCKET):
        self.bar_length_s = bar_length_s
        self.bucket_size = bucket_size
        self._current: Optional[FootprintBar] = None
        self._last_price: float = 0.0
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        self._last_quote_ts_ms: int = 0
        self.completed_bars: list[FootprintBar] = []  # Keep last 100 bars

    def _bucket(self, price: float) -> float:
        return round(price / self.bucket_size) * self.bucket_size

    def _classify_tick(self, price: float, bid: float, ask: float,
                       last_price: float, quote_age_ms: int) -> str:
        """
        Classify a trade as BUY / SELL / AMBIGUOUS.

        Primary: use bid/ask if quote is fresh.
          price >= ask  → BUY (aggressive buyer hit ask)
          price <= bid  → SELL (aggressive seller hit bid)
          inside spread → AMBIGUOUS (resolved by tick rule)

        Fallback: if quote is stale, use tick rule.
          price > last_price → BUY
          price < last_price → SELL
          price == last_price → carry forward prior classification (assumed AMBIGUOUS)
        """
        if quote_age_ms < STALE_QUOTE_MS and bid > 0 and ask > 0:
            if price >= ask:
                return "BUY"
            if price <= bid:
                return "SELL"
            # Inside spread → tick rule
        # Tick rule fallback
        if last_price > 0:
            if price > last_price:
                return "BUY"
            if price < last_price:
                return "SELL"
        return "AMBIGUOUS"

    def process_tick(self, tick: dict) -> None:
        """
        Ingest a single tick dict: {price, bid, ask, vol, ts}.
        Expected to be called from base_bot's tick handler.
        """
        price = float(tick.get("price", 0) or 0)
        vol = float(tick.get("vol", 0) or 0)
        if price <= 0 or vol <= 0:
            return
        bid = float(tick.get("bid", 0) or 0)
        ask = float(tick.get("ask", 0) or 0)
        ts_str = tick.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now()

        # Update quote freshness
        now_ms = int(datetime.now().timestamp() * 1000)
        if bid > 0 and ask > 0 and bid < ask:
            self._last_bid = bid
            self._last_ask = ask
            self._last_quote_ts_ms = now_ms
        quote_age_ms = now_ms - self._last_quote_ts_ms if self._last_quote_ts_ms else 99999

        # Classify
        classification = self._classify_tick(
            price, self._last_bid, self._last_ask, self._last_price, quote_age_ms
        )

        # Ensure current bar
        if self._current is None:
            self._current = FootprintBar(
                ts_open=ts, ts_close=ts, bar_length_s=self.bar_length_s,
                open_price=price, high=price, low=price, close=price,
            )

        # Update bar OHLC
        cur = self._current
        cur.ts_close = ts
        if price > cur.high:
            cur.high = price
        if price < cur.low:
            cur.low = price
        cur.close = price
        cur.total_volume += vol

        # Aggregate volume into price bucket by classification
        bucket = self._bucket(price)
        if classification == "BUY":
            cur.ask_volume_at_price[bucket] = cur.ask_volume_at_price.get(bucket, 0.0) + vol
        elif classification == "SELL":
            cur.bid_volume_at_price[bucket] = cur.bid_volume_at_price.get(bucket, 0.0) + vol
        else:
            cur.ambiguous_volume_at_price[bucket] = cur.ambiguous_volume_at_price.get(bucket, 0.0) + vol

        self._last_price = price

    def close_bar(self) -> Optional[FootprintBar]:
        """
        Close the current bar and start a new one. Returns the completed bar.
        Called by base_bot on 1m (or 5m, depending on accumulator instance) bar boundary.
        """
        if self._current is None:
            return None
        if self._current.low == float("inf"):
            self._current.low = self._current.high  # No low recorded
        completed = self._current
        self.completed_bars.append(completed)
        if len(self.completed_bars) > 100:
            self.completed_bars = self.completed_bars[-100:]
        self._current = None
        return completed

    def current_bar(self) -> Optional[FootprintBar]:
        """The bar currently being accumulated. Don't mutate — read-only snapshot."""
        return self._current

    def last_completed(self) -> Optional[FootprintBar]:
        return self.completed_bars[-1] if self.completed_bars else None
