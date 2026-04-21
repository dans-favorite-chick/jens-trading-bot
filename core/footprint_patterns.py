"""
Phoenix Bot — Footprint Pattern Detection

Detects institutional order flow patterns from FootprintBar output:
  - Stacked imbalance: 3+ consecutive price levels with 3:1 volume ratio same side
  - Absorption: high volume at price, minimal price movement
  - Exhaustion print: extreme volume at bar extreme, close reverses
  - Delta divergence: price makes new high/low but delta doesn't

Research basis (2026):
- Stacked imbalances reveal institutional sponsorship direction
- Absorption = someone defending a level (stop-hunt or accumulation)
- Exhaustion = final push with no follow-through (reversal warning)
- Delta divergence = weakening momentum, classic reversal signal

These are DETECTORS, not entry signals. strategies/*.py or structural_bias.py
consume them as confluence inputs.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bridge.footprint_builder import FootprintBar

logger = logging.getLogger("FootprintPatterns")

# Thresholds
STACKED_IMBALANCE_MIN_CONSECUTIVE = 3
STACKED_IMBALANCE_RATIO = 3.0          # 3:1 volume dominance per bucket
ABSORPTION_VOLUME_MULT = 2.0           # 2× bucket average
ABSORPTION_MAX_PRICE_MOVE_TICKS = 2
EXHAUSTION_VOL_MULT = 2.0              # Bar vol ≥ 2× 10-bar avg
EXHAUSTION_CLOSE_REVERSE_PCT = 0.4     # Close in bottom/top 40% opposite the extreme
DELTA_DIV_LOOKBACK_BARS = 5


@dataclass
class FootprintSignal:
    ts: datetime
    pattern: str            # "STACKED_IMBALANCE_BUY" | ... | "ABSORPTION_SUPPORT" | ...
    direction: str          # "BULLISH" | "BEARISH"
    price: float            # Relevant price level
    severity: float         # 0-1, higher = stronger
    reasons: list[str]

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat() if isinstance(self.ts, datetime) else self.ts,
            "pattern": self.pattern,
            "direction": self.direction,
            "price": round(self.price, 2),
            "severity": round(self.severity, 2),
            "reasons": self.reasons,
        }


def detect_stacked_imbalance(bar: FootprintBar) -> Optional[FootprintSignal]:
    """
    3+ consecutive price buckets where one side dominates 3:1+.
    Signals institutional sponsorship in that direction.
    """
    prices = bar.all_prices()
    if len(prices) < STACKED_IMBALANCE_MIN_CONSECUTIVE:
        return None

    # Walk adjacent buckets, count consecutive same-side imbalances
    current_streak = 0
    streak_side = ""
    streak_start = 0.0
    streak_end = 0.0
    max_streak = 0
    best_streak_info = None

    for p in prices:
        bid, ask, cls = bar.imbalance_at_price(p)
        if cls == "ASK_IMBALANCE":  # Buyer dominant
            if streak_side == "BUY":
                current_streak += 1
            else:
                current_streak = 1
                streak_side = "BUY"
                streak_start = p
            streak_end = p
        elif cls == "BID_IMBALANCE":  # Seller dominant
            if streak_side == "SELL":
                current_streak += 1
            else:
                current_streak = 1
                streak_side = "SELL"
                streak_start = p
            streak_end = p
        else:
            if current_streak > max_streak:
                max_streak = current_streak
                best_streak_info = (streak_side, streak_start, streak_end, current_streak)
            current_streak = 0
            streak_side = ""

    # Final check
    if current_streak > max_streak:
        max_streak = current_streak
        best_streak_info = (streak_side, streak_start, streak_end, current_streak)

    if max_streak < STACKED_IMBALANCE_MIN_CONSECUTIVE or best_streak_info is None:
        return None

    side, lo, hi, count = best_streak_info
    severity = min(1.0, count / 5.0)  # 5+ stack = max severity
    return FootprintSignal(
        ts=bar.ts_close,
        pattern=f"STACKED_IMBALANCE_{side}",
        direction="BULLISH" if side == "BUY" else "BEARISH",
        price=(lo + hi) / 2,
        severity=severity,
        reasons=[f"{count} consecutive {side} imbalances from {lo:.2f} to {hi:.2f}"],
    )


def detect_absorption(bar: FootprintBar, prior_bars: list[FootprintBar]) -> Optional[FootprintSignal]:
    """
    High volume at a specific price with minimal price advancement.
    Someone large is DEFENDING that level (either stop-hunt victim or institutional accumulation).
    """
    if len(prior_bars) < 5:
        return None
    # Average per-bucket volume across recent bars
    avg_bucket_vols: list[float] = []
    for pb in prior_bars[-10:]:
        for _price, vol in pb.bid_volume_at_price.items():
            avg_bucket_vols.append(vol)
        for _price, vol in pb.ask_volume_at_price.items():
            avg_bucket_vols.append(vol)
    if not avg_bucket_vols:
        return None
    baseline = sum(avg_bucket_vols) / len(avg_bucket_vols)

    # Find bucket with extreme volume in this bar
    max_price = None
    max_vol = 0.0
    max_side = ""
    for p, v in bar.bid_volume_at_price.items():
        if v > max_vol:
            max_vol = v
            max_price = p
            max_side = "SELL"  # sellers hit bid here
    for p, v in bar.ask_volume_at_price.items():
        if v > max_vol:
            max_vol = v
            max_price = p
            max_side = "BUY"

    if max_price is None or max_vol < baseline * ABSORPTION_VOLUME_MULT:
        return None

    # Check that price barely moved away from this bucket within the bar
    bar_range_ticks = (bar.high - bar.low) / 0.25
    if bar_range_ticks > ABSORPTION_MAX_PRICE_MOVE_TICKS * 4:  # Convert ticks to buckets
        # Price moved too much — not true absorption
        return None

    # If SELL-side volume dominated but price didn't break down → bullish absorption (support)
    # If BUY-side volume dominated but price didn't break up → bearish absorption (resistance)
    if max_side == "SELL":
        direction = "BULLISH"
        pattern = "ABSORPTION_SUPPORT"
        reason = f"sellers hit bid {max_vol:.0f} ({max_vol/baseline:.1f}× avg) at {max_price:.2f}, price held"
    else:
        direction = "BEARISH"
        pattern = "ABSORPTION_RESISTANCE"
        reason = f"buyers hit ask {max_vol:.0f} ({max_vol/baseline:.1f}× avg) at {max_price:.2f}, price capped"

    return FootprintSignal(
        ts=bar.ts_close, pattern=pattern, direction=direction,
        price=max_price, severity=min(1.0, max_vol / (baseline * 4)),
        reasons=[reason, f"bar range {bar_range_ticks:.1f}t minimal"],
    )


def detect_exhaustion(bar: FootprintBar, prior_bars: list[FootprintBar]) -> Optional[FootprintSignal]:
    """
    Extreme volume at bar extreme with close reversing = final push exhausted.
    """
    if len(prior_bars) < 5:
        return None
    avg_vol = sum(pb.total_volume for pb in prior_bars[-10:]) / max(1, len(prior_bars[-10:]))
    if bar.total_volume < avg_vol * EXHAUSTION_VOL_MULT:
        return None

    bar_range = bar.high - bar.low
    if bar_range <= 0:
        return None

    close_pos_from_low = (bar.close - bar.low) / bar_range  # 0..1

    # Bullish exhaustion = extreme sell bar, close in UPPER 40% (rejected the low)
    if close_pos_from_low >= (1 - EXHAUSTION_CLOSE_REVERSE_PCT):
        # Check that bulk of volume happened at the low
        low_bucket = round(bar.low / 0.25) * 0.25
        low_vol = (bar.bid_volume_at_price.get(low_bucket, 0) +
                   bar.ask_volume_at_price.get(low_bucket, 0) +
                   bar.ambiguous_volume_at_price.get(low_bucket, 0))
        if low_vol >= bar.total_volume * 0.15:  # ≥15% at the low
            return FootprintSignal(
                ts=bar.ts_close, pattern="EXHAUSTION_LOW", direction="BULLISH",
                price=bar.low,
                severity=min(1.0, bar.total_volume / (avg_vol * 3)),
                reasons=[
                    f"bar vol {bar.total_volume/avg_vol:.1f}× avg",
                    f"close {close_pos_from_low:.0%} from low",
                    f"{low_vol/bar.total_volume:.0%} vol concentrated at low",
                ],
            )

    # Bearish exhaustion = extreme buy bar, close in LOWER 40% (rejected the high)
    if close_pos_from_low <= EXHAUSTION_CLOSE_REVERSE_PCT:
        high_bucket = round(bar.high / 0.25) * 0.25
        high_vol = (bar.bid_volume_at_price.get(high_bucket, 0) +
                    bar.ask_volume_at_price.get(high_bucket, 0) +
                    bar.ambiguous_volume_at_price.get(high_bucket, 0))
        if high_vol >= bar.total_volume * 0.15:
            return FootprintSignal(
                ts=bar.ts_close, pattern="EXHAUSTION_HIGH", direction="BEARISH",
                price=bar.high,
                severity=min(1.0, bar.total_volume / (avg_vol * 3)),
                reasons=[
                    f"bar vol {bar.total_volume/avg_vol:.1f}× avg",
                    f"close {1-close_pos_from_low:.0%} from high",
                    f"{high_vol/bar.total_volume:.0%} vol concentrated at high",
                ],
            )

    return None


def detect_delta_divergence(bars: list[FootprintBar]) -> Optional[FootprintSignal]:
    """
    Price makes HH/LL but bar_delta doesn't confirm.
    Classic momentum weakening signal.
    """
    if len(bars) < DELTA_DIV_LOOKBACK_BARS:
        return None
    recent = bars[-DELTA_DIV_LOOKBACK_BARS:]
    latest = recent[-1]
    # Find previous high / low for comparison
    prior = recent[:-1]
    prior_highs = [b.high for b in prior]
    prior_lows = [b.low for b in prior]
    if not prior_highs or not prior_lows:
        return None

    prior_max_high = max(prior_highs)
    prior_min_low = min(prior_lows)
    prior_max_delta = max(b.bar_delta() for b in prior)
    prior_min_delta = min(b.bar_delta() for b in prior)

    latest_delta = latest.bar_delta()

    # Bearish divergence: latest high > prior max high, but latest delta < prior max delta
    if latest.high > prior_max_high and latest_delta < prior_max_delta * 0.6:
        return FootprintSignal(
            ts=latest.ts_close, pattern="DELTA_DIVERGENCE_BEARISH", direction="BEARISH",
            price=latest.high,
            severity=min(1.0, abs(prior_max_delta - latest_delta) / max(abs(prior_max_delta), 1)),
            reasons=[
                f"price HH {latest.high:.2f} > prior max {prior_max_high:.2f}",
                f"but delta {latest_delta:.0f} < prior peak delta {prior_max_delta:.0f}",
            ],
        )

    # Bullish divergence: latest low < prior min low, but latest delta > prior min delta
    if latest.low < prior_min_low and latest_delta > prior_min_delta * 0.6:
        return FootprintSignal(
            ts=latest.ts_close, pattern="DELTA_DIVERGENCE_BULLISH", direction="BULLISH",
            price=latest.low,
            severity=min(1.0, abs(latest_delta - prior_min_delta) / max(abs(prior_min_delta), 1)),
            reasons=[
                f"price LL {latest.low:.2f} < prior min {prior_min_low:.2f}",
                f"but delta {latest_delta:.0f} > prior trough delta {prior_min_delta:.0f}",
            ],
        )

    return None


def scan_bar(bar: FootprintBar, history: list[FootprintBar]) -> list[FootprintSignal]:
    """
    Run all detectors on one bar + history. Return all signals that fired.
    """
    signals = []
    for detector in (
        detect_stacked_imbalance,
        lambda b: detect_absorption(b, history),
        lambda b: detect_exhaustion(b, history),
    ):
        try:
            s = detector(bar)
            if s is not None:
                signals.append(s)
        except Exception as e:
            logger.debug(f"Detector failed: {e}")
    # Delta divergence needs full sequence
    try:
        s = detect_delta_divergence(history + [bar])
        if s is not None:
            signals.append(s)
    except Exception as e:
        logger.debug(f"Delta divergence failed: {e}")
    return signals
