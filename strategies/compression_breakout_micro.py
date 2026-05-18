"""
Phoenix Bot — Compression Breakout MICRO (1m TF, NQ-Tuned)
============================================================

A FAST-SCALE variant of compression_breakout_v2, sized for 1m bars.
Runs in PARALLEL with compression_breakout_v2 to catch the full size
spectrum of compression-release setups on NQ.

POSITIONING vs V2
-----------------
                       V2 (5m)              MICRO (1m)
Compression window:    25 min (5-of-8)      6 min (6-of-10)
Move size caught:      15-40 pt             5-15 pt
Fire frequency:        2-5 per week         5-15 per week
Stop size:             12-60 ticks          8-30 ticks
Target distance:       30-120 ticks @ 2R    12-45 ticks @ 1.5R
Time to target:        15-60 min            5-15 min

The TWO are complementary — a big breakout starts as a small breakout.
MICRO enters EARLIER in the move (after 6 min compression) at TIGHTER risk.
V2 enters LATER (after 25 min compression) at wider risk but catches bigger
moves with stronger context.

You can hold both signals simultaneously: MICRO scalps the initial release,
V2 rides the continuation if the compression was a "real" big setup.

NQ-SPECIFIC TUNING
------------------
NQ 1m bar ATR is typically 2-5 points (8-20 ticks).
A "compressed" 1m bar is 1-2 points (4-8 ticks).
A "broken" 1m bar is 4-8 points (16-32 ticks) with volume.

This variant uses:
- bb_std = 1.4 (very tight Bollinger — 1m is naturally tighter than 5m)
- kc_atr_mult = 1.5 (same as V2)
- atr_smoothing = 30 (30 min context — half of V2's 50)
- atr_compression_ratio = 0.65 (slightly looser — 1m bars are more variable)
- window_bars = 10 (10 min look-back)
- min_compressed_in_window = 6 (tolerate 4 noise bars in 10 = 40% noise tolerance)
- breakout_volume_mult = 1.4 (slightly lower — 1m volume is choppier)
- min_breakout_dist_atr = 0.30 (need clearer break on 1m)

DEPENDENCIES
------------
- strategies.base_strategy
- core.confirmation_stop
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal
from core.confirmation_stop import compute_confirmation_stop, snap_to_tick

logger = logging.getLogger(__name__)
_CT = ZoneInfo("America/Chicago")

TICK_SIZE = 0.25

# Defaults — NQ 1m-bar tuned
DEFAULT_BB_PERIOD = 20
DEFAULT_BB_STD = 1.4               # tight (1m volatility lower than 5m baseline)
DEFAULT_KC_PERIOD = 20
DEFAULT_KC_ATR_MULT = 1.5
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_SMOOTHING = 30
DEFAULT_ATR_COMPRESSION_RATIO = 0.65
DEFAULT_WINDOW_BARS = 10           # 10 min look-back
DEFAULT_MIN_COMPRESSED_IN_WINDOW = 6   # 6 of 10 — tolerate 40% noise
DEFAULT_BREAKOUT_VOLUME_MULT = 1.4
DEFAULT_RANGE_ATR_RATIO = 1.5
DEFAULT_MIN_BREAKOUT_DIST_ATR = 0.30   # clearer break on 1m
DEFAULT_MAX_STOP_TICKS = 30        # scalp range
DEFAULT_MIN_STOP_TICKS = 8
DEFAULT_TARGET_RR = 1.5            # scalp R:R (vs 2.0 on V2)
DEFAULT_MAX_TRADES_PER_DAY = 5     # higher cap — more fires expected


@dataclass
class _MicroCompressionWindow:
    bar_history: list = field(default_factory=list)
    last_processed_ts: float = 0
    max_window: int = DEFAULT_WINDOW_BARS

    def update(self, ts: float, compressed: bool, range_high: float, range_low: float, atr: float) -> bool:
        if ts <= self.last_processed_ts:
            return False
        self.bar_history.append((ts, compressed, range_high, range_low, atr))
        if len(self.bar_history) > self.max_window:
            self.bar_history.pop(0)
        self.last_processed_ts = ts
        return True

    def compressed_count(self) -> int:
        return sum(1 for entry in self.bar_history if entry[1])

    def reset(self):
        self.bar_history.clear()
        self.last_processed_ts = 0


class CompressionBreakoutMicro(BaseStrategy):
    """1m-bar compression breakout — fast scalp on micro-compression releases."""

    name = "compression_breakout_micro"
    computes_own_stop = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._window = _MicroCompressionWindow(
            max_window=int(config.get("window_bars", DEFAULT_WINDOW_BARS))
        )
        self._trades_today: int = 0
        self._trade_date: Optional[str] = None
        self._last_signal_bar_ts: float = 0

    def _maybe_reset_daily(self, now_ct: datetime):
        today = now_ct.strftime("%Y-%m-%d")
        if self._trade_date != today:
            self._trade_date = today
            self._trades_today = 0
            self._last_signal_bar_ts = 0
            # Reset window on day boundary — NQ has daily session breaks
            self._window.reset()

    # ── Indicator helpers ──────────────────────────────────────────
    @staticmethod
    def _calculate_atr(bars: list, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        trs = []
        for i in range(1, period + 1):
            b = bars[-i]
            prev = bars[-i - 1]
            high = float(getattr(b, "high"))
            low = float(getattr(b, "low"))
            prev_close = float(getattr(prev, "close"))
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        return sum(trs) / period

    @staticmethod
    def _calculate_bb(bars: list, period: int, std_mult: float):
        if len(bars) < period:
            return None, None, None
        closes = [float(b.close) for b in bars[-period:]]
        sma = sum(closes) / period
        variance = sum((c - sma) ** 2 for c in closes) / period
        std = variance ** 0.5
        return sma, sma + std_mult * std, sma - std_mult * std

    def _calculate_kc(self, bars: list, period: int, atr_mult: float):
        if len(bars) < period:
            return None, None, None
        closes = [float(b.close) for b in bars[-period:]]
        sma = sum(closes) / period
        atr = self._calculate_atr(bars, period)
        if atr is None:
            return None, None, None
        return sma, sma + atr_mult * atr, sma - atr_mult * atr

    # ── Main evaluate ──────────────────────────────────────────────
    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:

        now_ct = market.get("now_ct")
        if not isinstance(now_ct, datetime):
            now_ct = datetime.now(_CT)
        self._maybe_reset_daily(now_ct)

        max_trades = int(self.config.get("max_trades_per_day", DEFAULT_MAX_TRADES_PER_DAY))
        if self._trades_today >= max_trades:
            return None

        # Use 1m bars as primary TF (key difference from V2)
        if not bars_1m or len(bars_1m) < 50:
            return None

        ref_bar = bars_1m[-1]
        try:
            bar_ts = float(ref_bar.end_time)
        except (AttributeError, TypeError, ValueError):
            return None

        if bar_ts == self._last_signal_bar_ts:
            return None

        # ── Config ────────────────────────────────────────────────
        bb_period = int(self.config.get("bb_period", DEFAULT_BB_PERIOD))
        bb_std = float(self.config.get("bb_std", DEFAULT_BB_STD))
        kc_period = int(self.config.get("kc_period", DEFAULT_KC_PERIOD))
        kc_atr_mult = float(self.config.get("kc_atr_mult", DEFAULT_KC_ATR_MULT))
        atr_period = int(self.config.get("atr_period", DEFAULT_ATR_PERIOD))
        atr_smoothing = int(self.config.get("atr_smoothing", DEFAULT_ATR_SMOOTHING))
        atr_compression_ratio = float(self.config.get("atr_compression_ratio", DEFAULT_ATR_COMPRESSION_RATIO))
        range_atr_ratio = float(self.config.get("range_atr_ratio", DEFAULT_RANGE_ATR_RATIO))

        # ── Compute compression conditions ────────────────────────
        bb_sma, bb_upper, bb_lower = self._calculate_bb(bars_1m, bb_period, bb_std)
        kc_sma, kc_upper, kc_lower = self._calculate_kc(bars_1m, kc_period, kc_atr_mult)
        if bb_upper is None or kc_upper is None:
            return None

        bb_width = bb_upper - bb_lower
        kc_width = kc_upper - kc_lower
        in_ttm_squeeze = bb_width < kc_width

        current_atr = self._calculate_atr(bars_1m, atr_period)
        if current_atr is None or current_atr <= 0:
            return None

        # ATR rolling average
        atr_history = []
        for i in range(min(atr_smoothing, len(bars_1m) - atr_period)):
            slice_end = len(bars_1m) - i
            atr_i = self._calculate_atr(bars_1m[:slice_end], atr_period)
            if atr_i is not None:
                atr_history.append(atr_i)
        if len(atr_history) < atr_smoothing // 2:
            return None
        avg_atr = sum(atr_history) / len(atr_history)
        atr_compressed = current_atr <= avg_atr * atr_compression_ratio

        # Range compression (over recent 15 bars = 15 min)
        recent_high = max(float(b.high) for b in bars_1m[-15:])
        recent_low = min(float(b.low) for b in bars_1m[-15:])
        range_size = recent_high - recent_low
        range_compressed = range_size < current_atr * range_atr_ratio

        # Volume dryness (3 vs 30 bar avg)
        avg_v3 = sum(float(b.volume) for b in bars_1m[-3:]) / 3
        avg_v30 = sum(float(b.volume) for b in bars_1m[-30:]) / 30
        volume_dried = avg_v30 > 0 and avg_v3 < 0.80 * avg_v30  # 80% threshold (looser for 1m)

        compressed_flags = [in_ttm_squeeze, atr_compressed, volume_dried, range_compressed]
        compressed_count = sum(compressed_flags)
        min_conditions = int(self.config.get("min_compression_conditions", 3))
        bar_compressed = compressed_count >= min_conditions

        # Per-bar dedup state update
        is_new_bar = self._window.update(
            ts=bar_ts, compressed=bar_compressed,
            range_high=recent_high, range_low=recent_low, atr=current_atr,
        )

        # Window-based compression check
        min_compressed = int(self.config.get("min_compressed_in_window", DEFAULT_MIN_COMPRESSED_IN_WINDOW))
        window_size = self._window.max_window
        compressed_in_window = self._window.compressed_count()

        if len(self._window.bar_history) < window_size:
            return None  # warmup

        if compressed_in_window < min_compressed:
            logger.debug(
                f"[EVAL] {self.name}: NO_SIGNAL not_enough_compression "
                f"({compressed_in_window}/{window_size}; need {min_compressed})"
            )
            return None

        # Current bar must be the BREAKOUT
        if bar_compressed:
            return None

        # ── Direction ──────────────────────────────────────────────
        prior_bars = self._window.bar_history[:-1] if len(self._window.bar_history) > 0 else []
        if prior_bars:
            range_high_squeeze = max(e[2] for e in prior_bars)
            range_low_squeeze = min(e[3] for e in prior_bars)
        else:
            range_high_squeeze, range_low_squeeze = recent_high, recent_low

        ref_close = float(ref_bar.close)
        direction = None
        breakout_level = None
        if ref_close > range_high_squeeze:
            direction = "LONG"
            breakout_level = range_high_squeeze
        elif ref_close < range_low_squeeze:
            direction = "SHORT"
            breakout_level = range_low_squeeze
        else:
            return None

        # ── Breakout volume confirmation ───────────────────────────
        breakout_volume_mult = float(self.config.get("breakout_volume_mult", DEFAULT_BREAKOUT_VOLUME_MULT))
        ref_vol = float(ref_bar.volume)
        squeeze_avg_vol = sum(float(bars_1m[-(i + 1)].volume) for i in range(window_size)) / window_size
        if squeeze_avg_vol > 0 and ref_vol < squeeze_avg_vol * breakout_volume_mult:
            logger.debug(f"[EVAL] {self.name}: BLOCKED breakout_volume_low")
            return None

        # ── Breakout distance check ────────────────────────────────
        min_breakout_dist = current_atr * float(
            self.config.get("min_breakout_dist_atr", DEFAULT_MIN_BREAKOUT_DIST_ATR)
        )
        breakout_distance = abs(ref_close - breakout_level)
        if breakout_distance < min_breakout_dist:
            return None

        if in_ttm_squeeze:
            return None  # still squeezed

        # ── Build signal ───────────────────────────────────────────
        entry_price = ref_close

        # Validate target_rr
        target_rr = float(self.config.get("target_rr", DEFAULT_TARGET_RR))
        import math as _math
        if target_rr <= 0 or not _math.isfinite(target_rr):
            target_rr = DEFAULT_TARGET_RR

        # ATR-based stop with confirmation fallback
        stop_atr_mult = float(self.config.get("stop_atr_mult", 1.5))
        atr_stop_dist = current_atr * stop_atr_mult
        atr_stop_ticks = int(atr_stop_dist / TICK_SIZE)
        max_stop = int(self.config.get("max_stop_ticks", DEFAULT_MAX_STOP_TICKS))
        min_stop = int(self.config.get("min_stop_ticks", DEFAULT_MIN_STOP_TICKS))

        if atr_stop_ticks > max_stop:
            stop_ticks, stop_price, stop_note = compute_confirmation_stop(
                direction=direction, entry_price=entry_price,
                bars_1m=bars_1m, lookback_bars=5, buffer_ticks=2,
                tick_size=TICK_SIZE, min_ticks=min_stop, max_ticks=max_stop,
            )
        else:
            stop_ticks = max(min_stop, atr_stop_ticks)
            if direction == "LONG":
                stop_price = snap_to_tick(entry_price - stop_ticks * TICK_SIZE, TICK_SIZE)
            else:
                stop_price = snap_to_tick(entry_price + stop_ticks * TICK_SIZE, TICK_SIZE)
            stop_note = f"ATR-based {stop_ticks}t (1m micro)"

        stop_distance = abs(entry_price - stop_price)
        if direction == "LONG":
            target_price = snap_to_tick(entry_price + stop_distance * target_rr, TICK_SIZE)
        else:
            target_price = snap_to_tick(entry_price - stop_distance * target_rr, TICK_SIZE)

        entry_price_snapped = snap_to_tick(entry_price, TICK_SIZE)

        self._trades_today += 1
        self._last_signal_bar_ts = bar_ts

        confluences = [
            f"MICRO compression: {compressed_in_window}/{window_size} bars (1m)",
            f"Squeeze range: {range_low_squeeze:.2f}-{range_high_squeeze:.2f} ({range_high_squeeze - range_low_squeeze:.2f}pt)",
            f"Breakout vol {ref_vol:.0f} > {breakout_volume_mult}× avg{squeeze_avg_vol:.0f}",
            f"Breakout dist {breakout_distance:.2f}pt >= {min_breakout_dist:.2f}pt",
            f"ATR_1m={current_atr:.2f} (compressed vs avg {avg_atr:.2f})",
            stop_note,
        ]

        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} entry={entry_price_snapped:.2f} "
            f"stop={stop_price:.2f} ({stop_ticks}t) target={target_price:.2f}"
        )

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=65.0,
            entry_score=45.0,
            strategy=self.name,
            reason=f"Compression MICRO {direction} — 1m squeeze release",
            confluences=confluences,
            atr_stop_override=True,
            entry_type="MARKET",
            entry_price=entry_price_snapped,
            stop_price=stop_price,
            target_price=target_price,
            eod_flat_time_et="14:30",
            metadata={
                "compressed_in_window": compressed_in_window,
                "window_size": window_size,
                "squeeze_high": range_high_squeeze,
                "squeeze_low": range_low_squeeze,
                "current_atr_1m": current_atr,
                "avg_atr_1m": avg_atr,
                "stop_note": stop_note,
                "scale": "micro",
            },
        )
