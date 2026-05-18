"""
Phoenix Bot — Compression Breakout V2 (NQ-Tuned)
==================================================

DROP-IN ALTERNATIVE to strategies/compression_breakout.py. Same trade
thesis (TTM squeeze release on NQ), three architectural fixes that
address the 5,476 squeeze_not_held events/day pattern.

THREE FIXES vs the original
---------------------------

**FIX A: Per-bar dedup on state updates.**
Original increments `consecutive_squeeze_bars` on EVERY evaluate() call
(every ~15s), then resets to 0 on any non-compressed eval. With sub-bar
evaluation frequency, this is unstable.

V2 tracks `_last_processed_bar_ts`. State only updates when a NEW closed
bar appears. Within-bar evaluations don't change state.

**FIX B: Window-based compression instead of consecutive bars.**
Original requires ≥5 consecutive bars compressed. One bar of noise resets
the streak. NQ produces transient non-compression bars constantly.

V2 tracks a rolling 8-bar window. Signal fires when ≥5 of last 8 bars
were compressed AND current bar is the BREAKOUT bar. This tolerates
1-2 noise bars without losing the regime.

**FIX C: NQ-tuned Bollinger Band std.**
Original uses Carter 2010 settings: bb_std=2.0, kc_atr_mult=1.5.
On NQ 2026, BB at 2.0σ is too wide — captures fewer "BB inside KC" states.

V2 default: bb_std=1.5, kc_atr_mult=1.5. The TTM squeeze condition
(BB tighter than KC) is met more often, which is appropriate because
NQ's intraday volatility distribution is fatter-tailed than equities.

ENTRY LOGIC (unchanged from original)
-------------------------------------
After window-based compression detected:
1. Direction by close beyond range high (LONG) or low (SHORT)
2. Volume on breakout bar ≥ 1.5× squeeze-window avg volume
3. BB now expanded outside KC (squeeze released)
4. Close at least 25% of current ATR past breakout level
5. Optional HTF alignment check

STOPS
-----
Uses confirmation-bar fallback when natural ATR stop exceeds max_stop_ticks.
This is the same fix applied to all the other strategies — sits comfortably
in the $50/trade budget on NQ.

NAME
----
This strategy's `name = "compression_breakout_v2"`. The original
`compression_breakout` can keep running alongside for comparison until
you validate v2.

DEPENDENCIES
------------
- strategies.base_strategy — BaseStrategy + Signal
- core.confirmation_stop — for stop fallback
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

# Defaults (NQ 2026 tuned)
DEFAULT_BB_PERIOD = 20
DEFAULT_BB_STD = 1.5                # was 2.0 in original (NQ tuning)
DEFAULT_KC_PERIOD = 20
DEFAULT_KC_ATR_MULT = 1.5
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_SMOOTHING = 50
DEFAULT_ATR_COMPRESSION_RATIO = 0.60   # was 0.50; slightly looser for NQ
DEFAULT_WINDOW_BARS = 8                # rolling window size
DEFAULT_MIN_COMPRESSED_IN_WINDOW = 5   # 5 of last 8 must be compressed
DEFAULT_BREAKOUT_VOLUME_MULT = 1.5
DEFAULT_RANGE_ATR_RATIO = 1.5
DEFAULT_MIN_BREAKOUT_DIST_ATR = 0.25
DEFAULT_MAX_STOP_TICKS = 60         # tighter than 120; uses confirmation fallback
DEFAULT_MIN_STOP_TICKS = 12
DEFAULT_TARGET_RR = 2.0


@dataclass
class _CompressionWindow:
    """Rolling window state for window-based compression detection."""
    bar_history: list = field(default_factory=list)  # list of (ts, compressed: bool, range_high, range_low, atr)
    last_processed_ts: float = 0
    max_window: int = DEFAULT_WINDOW_BARS

    def update(self, ts: float, compressed: bool, range_high: float, range_low: float, atr: float) -> bool:
        """Returns True if this is a NEW bar (state was actually updated)."""
        if ts <= self.last_processed_ts:
            return False  # already processed this bar
        self.bar_history.append((ts, compressed, range_high, range_low, atr))
        if len(self.bar_history) > self.max_window:
            self.bar_history.pop(0)
        self.last_processed_ts = ts
        return True

    def compressed_count(self) -> int:
        return sum(1 for entry in self.bar_history if entry[1])

    def window_range(self) -> tuple[float, float, float]:
        """Returns (high, low, avg_atr) over the window."""
        if not self.bar_history:
            return 0.0, 0.0, 0.0
        h = max(e[2] for e in self.bar_history)
        l = min(e[3] for e in self.bar_history)
        a = sum(e[4] for e in self.bar_history) / len(self.bar_history)
        return h, l, a

    def reset(self):
        self.bar_history.clear()
        self.last_processed_ts = 0


class CompressionBreakoutV2(BaseStrategy):
    """NQ-tuned compression breakout with window-based detection."""

    name = "compression_breakout_v2"
    computes_own_stop = True

    # Instance-level state (not class-level — multi-instance safe)
    def __init__(self, config: dict):
        super().__init__(config)
        self._window = _CompressionWindow(
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
            # NQ futures have daily session boundaries — reset compression
            # window so yesterday's bars don't pollute today's regime detection.
            # (The original v2 design left the window persistent, but that
            # caused yesterday's compressed bars to count toward today's
            # 5-of-8 threshold — false positive risk.)
            self._window.reset()

    # ── Indicator helpers ──────────────────────────────────────────
    @staticmethod
    def _calculate_atr(bars: list, period: int) -> Optional[float]:
        """Simple TR-based ATR over the last `period` bars."""
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
    def _calculate_bb(bars: list, period: int, std_mult: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Returns (sma, upper_band, lower_band)."""
        if len(bars) < period:
            return None, None, None
        closes = [float(b.close) for b in bars[-period:]]
        sma = sum(closes) / period
        variance = sum((c - sma) ** 2 for c in closes) / period
        std = variance ** 0.5
        return sma, sma + std_mult * std, sma - std_mult * std

    def _calculate_kc(self, bars: list, period: int, atr_mult: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Returns (sma, upper_band, lower_band) for Keltner Channels."""
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

        max_trades = int(self.config.get("max_trades_per_day", 3))
        if self._trades_today >= max_trades:
            return None

        # Use 5m bars as primary execution TF (matches original)
        if not bars_5m or len(bars_5m) < 50:
            return None

        ref_bar = bars_5m[-1]
        try:
            bar_ts = float(ref_bar.end_time)
        except (AttributeError, TypeError, ValueError):
            return None

        # Per-bar dedup
        if bar_ts == self._last_signal_bar_ts:
            return None

        # ── Compute compression conditions on the current closed bar ──
        bb_period = int(self.config.get("bb_period", DEFAULT_BB_PERIOD))
        bb_std = float(self.config.get("bb_std", DEFAULT_BB_STD))
        kc_period = int(self.config.get("kc_period", DEFAULT_KC_PERIOD))
        kc_atr_mult = float(self.config.get("kc_atr_mult", DEFAULT_KC_ATR_MULT))
        atr_period = int(self.config.get("atr_period", DEFAULT_ATR_PERIOD))
        atr_smoothing = int(self.config.get("atr_smoothing", DEFAULT_ATR_SMOOTHING))
        atr_compression_ratio = float(self.config.get("atr_compression_ratio", DEFAULT_ATR_COMPRESSION_RATIO))
        range_atr_ratio = float(self.config.get("range_atr_ratio", DEFAULT_RANGE_ATR_RATIO))

        bb_sma, bb_upper, bb_lower = self._calculate_bb(bars_5m, bb_period, bb_std)
        kc_sma, kc_upper, kc_lower = self._calculate_kc(bars_5m, kc_period, kc_atr_mult)
        if bb_upper is None or kc_upper is None:
            return None

        bb_width = bb_upper - bb_lower
        kc_width = kc_upper - kc_lower
        in_ttm_squeeze = bb_width < kc_width

        current_atr = self._calculate_atr(bars_5m, atr_period)
        if current_atr is None or current_atr <= 0:
            return None

        # ATR rolling average (skip if not enough history)
        atr_history = []
        for i in range(min(atr_smoothing, len(bars_5m) - atr_period)):
            slice_end = len(bars_5m) - i
            atr_i = self._calculate_atr(bars_5m[:slice_end], atr_period)
            if atr_i is not None:
                atr_history.append(atr_i)
        if len(atr_history) < atr_smoothing // 2:
            return None
        avg_atr = sum(atr_history) / len(atr_history)
        atr_compressed = current_atr <= avg_atr * atr_compression_ratio

        # Range compression
        recent_high = max(float(b.high) for b in bars_5m[-20:])
        recent_low = min(float(b.low) for b in bars_5m[-20:])
        range_size = recent_high - recent_low
        range_compressed = range_size < current_atr * range_atr_ratio

        # Volume dryness
        avg_v5 = sum(float(b.volume) for b in bars_5m[-5:]) / 5
        avg_v50 = sum(float(b.volume) for b in bars_5m[-50:]) / 50
        volume_dried = avg_v50 > 0 and avg_v5 < 0.75 * avg_v50

        # 3 of 4 must be compressed (matches original 2026-05-15 fix)
        compressed_flags = [in_ttm_squeeze, atr_compressed, volume_dried, range_compressed]
        compressed_count = sum(compressed_flags)
        min_conditions = int(self.config.get("min_compression_conditions", 3))
        bar_compressed = compressed_count >= min_conditions

        # ── FIX A: per-bar dedup state update ─────────────────────
        is_new_bar = self._window.update(
            ts=bar_ts,
            compressed=bar_compressed,
            range_high=recent_high,
            range_low=recent_low,
            atr=current_atr,
        )

        # ── FIX B: window-based compression check ─────────────────
        min_compressed = int(self.config.get("min_compressed_in_window", DEFAULT_MIN_COMPRESSED_IN_WINDOW))
        window_size = self._window.max_window
        compressed_in_window = self._window.compressed_count()

        # Need full window to evaluate
        if len(self._window.bar_history) < window_size:
            logger.debug(
                f"[EVAL] {self.name}: SKIP window_filling "
                f"({len(self._window.bar_history)}/{window_size})"
            )
            return None

        # Window check: enough of recent bars were compressed
        if compressed_in_window < min_compressed:
            logger.debug(
                f"[EVAL] {self.name}: NO_SIGNAL not_enough_compression "
                f"({compressed_in_window}/{window_size} compressed; need {min_compressed})"
            )
            return None

        # Current bar must be the BREAKOUT (NOT compressed)
        if bar_compressed:
            logger.debug(
                f"[EVAL] {self.name}: NO_SIGNAL still_compressed "
                f"({compressed_in_window}/{window_size} compressed)"
            )
            return None

        # ── Breakout direction ─────────────────────────────────────
        window_high, window_low, window_avg_atr = self._window.window_range()
        # Use the prior bar's window range — don't include current breakout bar
        # in the range calc (it would auto-include itself)
        prior_bars = self._window.bar_history[:-1] if len(self._window.bar_history) > 0 else []
        if prior_bars:
            range_high_squeeze = max(e[2] for e in prior_bars)
            range_low_squeeze = min(e[3] for e in prior_bars)
        else:
            range_high_squeeze, range_low_squeeze = window_high, window_low

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
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL price_inside_squeeze_range")
            return None

        # ── Breakout volume confirmation ───────────────────────────
        breakout_volume_mult = float(self.config.get("breakout_volume_mult", DEFAULT_BREAKOUT_VOLUME_MULT))
        ref_vol = float(ref_bar.volume)
        # Compare to squeeze-window average
        squeeze_bars_volumes = []
        for i in range(min(window_size, len(bars_5m))):
            squeeze_bars_volumes.append(float(bars_5m[-(i + 1)].volume))
        squeeze_avg_vol = sum(squeeze_bars_volumes) / max(1, len(squeeze_bars_volumes))
        if squeeze_avg_vol > 0 and ref_vol < squeeze_avg_vol * breakout_volume_mult:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED breakout_volume_low "
                f"({ref_vol:.0f} < {breakout_volume_mult}× {squeeze_avg_vol:.0f})"
            )
            return None

        # ── Breakout distance check ────────────────────────────────
        min_breakout_dist = current_atr * float(
            self.config.get("min_breakout_dist_atr", DEFAULT_MIN_BREAKOUT_DIST_ATR)
        )
        breakout_distance = abs(ref_close - breakout_level)
        if breakout_distance < min_breakout_dist:
            logger.debug(
                f"[EVAL] {self.name}: BLOCKED breakout_too_marginal "
                f"({breakout_distance:.2f} < {min_breakout_dist:.2f})"
            )
            return None

        # ── BB has re-expanded outside KC? (squeeze released) ──────
        if in_ttm_squeeze:
            logger.debug(f"[EVAL] {self.name}: BLOCKED still_in_ttm_squeeze")
            return None

        # ── Build signal with confirmation-bar fallback stop ───────
        entry_price = ref_close

        # First try ATR-based stop
        stop_atr_mult = float(self.config.get("stop_atr_mult", 1.5))
        atr_stop_dist = current_atr * stop_atr_mult
        atr_stop_ticks = int(atr_stop_dist / TICK_SIZE)
        max_stop = int(self.config.get("max_stop_ticks", DEFAULT_MAX_STOP_TICKS))
        min_stop = int(self.config.get("min_stop_ticks", DEFAULT_MIN_STOP_TICKS))

        if atr_stop_ticks > max_stop:
            # FALLBACK: confirmation-bar stop
            stop_ticks, stop_price, stop_note = compute_confirmation_stop(
                direction=direction,
                entry_price=entry_price,
                bars_1m=bars_1m if bars_1m else bars_5m,
                lookback_bars=5,
                buffer_ticks=2,
                tick_size=TICK_SIZE,
                min_ticks=min_stop,
                max_ticks=max_stop,
            )
        else:
            stop_ticks = max(min_stop, atr_stop_ticks)
            if direction == "LONG":
                stop_price = snap_to_tick(entry_price - stop_ticks * TICK_SIZE, TICK_SIZE)
            else:
                stop_price = snap_to_tick(entry_price + stop_ticks * TICK_SIZE, TICK_SIZE)
            stop_note = f"ATR-based {stop_ticks}t ({stop_atr_mult}× ATR)"

        # ── Target ─────────────────────────────────────────────────
        target_rr = float(self.config.get("target_rr", DEFAULT_TARGET_RR))
        import math as _math
        if target_rr <= 0 or not _math.isfinite(target_rr):
            target_rr = DEFAULT_TARGET_RR
        stop_distance = abs(entry_price - stop_price)
        if direction == "LONG":
            target_price = snap_to_tick(entry_price + stop_distance * target_rr, TICK_SIZE)
        else:
            target_price = snap_to_tick(entry_price - stop_distance * target_rr, TICK_SIZE)

        # Snap entry to tick
        entry_price_snapped = snap_to_tick(entry_price, TICK_SIZE)

        # Update trade-event state
        self._trades_today += 1
        self._last_signal_bar_ts = bar_ts

        confluences = [
            f"Window compression: {compressed_in_window}/{window_size} bars compressed",
            f"Squeeze range: {range_low_squeeze:.2f}-{range_high_squeeze:.2f} ({range_high_squeeze - range_low_squeeze:.2f}pt)",
            f"Breakout volume {ref_vol:.0f} > {breakout_volume_mult}× squeeze-avg {squeeze_avg_vol:.0f}",
            f"Breakout distance {breakout_distance:.2f}pt >= {min_breakout_dist:.2f}pt",
            f"ATR={current_atr:.2f} (compressed vs avg {avg_atr:.2f})",
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
            confidence=70.0,
            entry_score=50.0,
            strategy=self.name,
            reason=f"Compression V2 {direction} — window-detected squeeze release",
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
                "current_atr": current_atr,
                "avg_atr": avg_atr,
                "stop_note": stop_note,
            },
        )
