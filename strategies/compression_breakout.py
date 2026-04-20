"""
Phoenix Bot — Compression Breakout Strategy (Research-Validated v2)

Based on convergent research from:
  - Mark Minervini's VCP (Volatility Contraction Pattern)
  - John Bollinger / Linda Bradford Raschke's "Squeeze" methodology
  - John Carter's TTM Squeeze
  - ATR Volatility Compression (LeafAlgo, Coding Nexus)
  - 18-year backtest on 30Y Treasury futures (Volatility Box)

CORE THESIS: Periods of low volatility are ALWAYS followed by periods of high
volatility. When BOTH price range contracts AND volume dries up (genuine
"coiling"), the eventual breakout has structural energy.

This strategy detects TRUE compression (4 conditions) and enters on a CONFIRMED
breakout (4 conditions). It does NOT enter on every quiet bar. It does NOT
chase. It waits for the spring to release.

Validated win-rate range from research: 55-65% with 1.5:1 to 3:1 R:R when
parameters are correct and walk-forward validated.
"""

from dataclasses import dataclass
from typing import Optional
import math

import logging

from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)


@dataclass
class CompressionState:
    """Track compression detection across bars (for "must hold N bars" rule)."""
    consecutive_squeeze_bars: int = 0
    last_squeeze_high: float = 0.0
    last_squeeze_low: float = 0.0
    squeeze_avg_volume: float = 0.0
    in_squeeze: bool = False


class CompressionBreakout(BaseStrategy):
    """
    Compression breakout — detects volatility contraction and enters on the
    confirmed expansion.

    INPUTS:
        market: tick aggregator snapshot
        bars_5m: completed 5m bars (we use this as primary execution TF)
        bars_15m: completed 15m bars (for compression detection)
        bars_60m: completed 60m bars (for HTF context)
        session_info: regime + time-of-day context

    OUTPUTS:
        Signal if compression breakout fires, None otherwise
    """

    name = "compression_breakout"

    # ── Class-level state (shared across calls) ──────────────────────
    # In production, this should live in a per-instrument state object.
    # For now, single-instrument bot keeps it as class state.
    _state: CompressionState = CompressionState()

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:

        # ── PARAMETER LOAD ─────────────────────────────────────────────
        # All from config/strategies.py — overridable by dashboard/sliders
        compression_tf = self.config.get("compression_timeframe", "15m")
        atr_period = self.config.get("atr_period", 14)
        atr_smoothing = self.config.get("atr_smoothing", 50)
        atr_compression_ratio = self.config.get("atr_compression_ratio", 0.5)
        bb_period = self.config.get("bb_period", 20)
        bb_std = self.config.get("bb_std", 2.0)
        kc_period = self.config.get("kc_period", 20)
        kc_atr_mult = self.config.get("kc_atr_mult", 1.5)
        min_squeeze_bars = self.config.get("min_squeeze_bars", 5)
        breakout_volume_mult = self.config.get("breakout_volume_mult", 1.5)
        range_atr_ratio = self.config.get("range_atr_ratio", 1.5)
        stop_atr_mult = self.config.get("stop_atr_mult", 1.5)
        target_rr = self.config.get("target_rr", 2.0)
        require_htf_alignment = self.config.get("require_htf_alignment", True)

        # ── DATA AVAILABILITY CHECK ────────────────────────────────────
        # We need at minimum 50 bars on the compression TF for ATR smoothing
        bars_compression = self._select_compression_bars(
            compression_tf, bars_5m, bars_1m, market
        )
        if bars_compression is None or len(bars_compression) < 50:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None

        # Most recent CLOSED bar is our reference
        ref_bar = bars_compression[-1]

        # ── STAGE 1: COMPRESSION DETECTION (4 conditions, ALL must be true) ──

        # Condition 1: Bollinger Bands inside Keltner Channels (TTM Squeeze)
        bb_width, kc_width, bb_upper, bb_lower = self._calculate_bb_kc(
            bars_compression, bb_period, bb_std, kc_period, kc_atr_mult
        )
        if bb_width is None or kc_width is None:
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None
        in_ttm_squeeze = bb_width < kc_width

        # Condition 2: ATR(14) ≤ 50% of ATR(14) 50-bar avg
        current_atr = self._calculate_atr(bars_compression[-atr_period:], atr_period)
        atr_history = [
            self._calculate_atr(bars_compression[-(atr_period+i):-i if i > 0 else None], atr_period)
            for i in range(atr_smoothing)
        ]
        atr_history = [a for a in atr_history if a is not None and a > 0]
        if len(atr_history) < atr_smoothing // 2:  # Need most of the lookback
            logger.debug(f"[EVAL] {self.name}: SKIP warmup_incomplete")
            return None
        avg_atr = sum(atr_history) / len(atr_history)
        atr_compressed = current_atr <= (avg_atr * atr_compression_ratio)

        # Condition 3: Recent volume < 75% of 50-bar avg volume
        recent_avg_vol = sum(b.volume for b in bars_compression[-5:]) / 5
        long_avg_vol = sum(b.volume for b in bars_compression[-50:]) / 50
        volume_dried = recent_avg_vol < (long_avg_vol * 0.75)

        # Condition 4: Range compression — last 20 bars high-low < 1.5x current ATR
        range_high = max(b.high for b in bars_compression[-20:])
        range_low = min(b.low for b in bars_compression[-20:])
        range_size = range_high - range_low
        range_compressed = range_size < (current_atr * range_atr_ratio)

        # Update squeeze state
        all_compressed = in_ttm_squeeze and atr_compressed and volume_dried and range_compressed
        if all_compressed:
            self._state.consecutive_squeeze_bars += 1
            self._state.last_squeeze_high = range_high
            self._state.last_squeeze_low = range_low
            self._state.squeeze_avg_volume = long_avg_vol
            self._state.in_squeeze = True
        else:
            # If we WERE in a squeeze and now we're not, this is a potential breakout
            # but we ONLY count it if we were in squeeze for min_squeeze_bars
            if (self._state.in_squeeze and
                    self._state.consecutive_squeeze_bars >= min_squeeze_bars):
                # Continue to STAGE 2 for breakout detection
                pass
            else:
                # Reset
                self._state.consecutive_squeeze_bars = 0
                self._state.in_squeeze = False
                logger.debug(f"[EVAL] {self.name}: NO_SIGNAL squeeze_not_held_min_bars")
                return None

        # ── STAGE 2: BREAKOUT DETECTION (4 conditions, ALL must be true) ──

        # Need a fully closed reference bar that just broke out
        squeeze_high = self._state.last_squeeze_high
        squeeze_low = self._state.last_squeeze_low
        squeeze_avg_vol = self._state.squeeze_avg_volume

        # Direction: which side did it break?
        direction = None
        breakout_level = None

        # Condition 1: Close beyond the squeeze range (NOT just wick)
        if ref_bar.close > squeeze_high:
            direction = "LONG"
            breakout_level = squeeze_high
        elif ref_bar.close < squeeze_low:
            direction = "SHORT"
            breakout_level = squeeze_low
        else:
            # Still inside the range, no breakout yet
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL price_inside_squeeze_range")
            return None

        # Condition 2: Breakout bar volume ≥ 1.5x squeeze avg volume
        if ref_bar.volume < (squeeze_avg_vol * breakout_volume_mult):
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:breakout_volume_insufficient")
            return None  # Weak breakout — likely fake

        # Condition 3: BB has re-expanded outside KC (squeeze released)
        if in_ttm_squeeze:  # If still in squeeze, breakout isn't confirmed
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:still_in_ttm_squeeze")
            return None

        # Condition 4: Close is solidly past breakout level (not just a tick over)
        breakout_distance = abs(ref_bar.close - breakout_level)
        min_breakout_distance = current_atr * 0.25  # At least 25% of ATR past level
        if breakout_distance < min_breakout_distance:
            logger.debug(f"[EVAL] {self.name}: BLOCKED gate:breakout_distance_too_small")
            return None  # Marginal breakout — wait for confirmation

        # ── STAGE 3: HIGHER TIMEFRAME ALIGNMENT (optional but recommended) ──

        if require_htf_alignment:
            htf_aligned = self._check_htf_alignment(direction, market, session_info)
            if not htf_aligned:
                logger.debug(f"[EVAL] {self.name}: BLOCKED gate:htf_not_aligned")
                return None

        # ── STAGE 4: BUILD THE SIGNAL ──────────────────────────────────

        # Stop: 1.5x ATR below entry (LONG) or above entry (SHORT)
        stop_distance_price = current_atr * stop_atr_mult
        # Convert to ticks (MNQ tick size = 0.25)
        tick_size = 0.25
        stop_ticks = int(stop_distance_price / tick_size)
        # NQ research clamps (Fix 7, 2026-04-20) — 40t floor / 120t ceiling
        min_stop = self.config.get("min_stop_ticks", 40)
        max_stop = self.config.get("max_stop_ticks", 120)
        stop_ticks = max(min_stop, min(max_stop, stop_ticks))

        # Confluences for trade journal
        confluences = [
            f"Squeeze: {self._state.consecutive_squeeze_bars} bars",
            f"ATR ratio: {(current_atr/avg_atr):.2f} (target ≤ {atr_compression_ratio})",
            f"Volume dried to {(recent_avg_vol/long_avg_vol):.0%} of avg",
            f"Range: {range_size:.2f} (vs ATR {current_atr:.2f})",
            f"Breakout vol: {(ref_bar.volume/squeeze_avg_vol):.1f}x avg",
            f"Direction: {direction} at {ref_bar.close:.2f}",
            f"Squeeze range: [{squeeze_low:.2f}, {squeeze_high:.2f}]",
            f"Regime: {session_info.get('regime', '?')}",
        ]

        # Reset state — squeeze has resolved
        self._state = CompressionState()

        # Entry type = STOPMARKET per roadmap v4 matrix (range break triggers entry).
        # Entry price = the squeeze boundary we just broke past (one tick beyond it).
        if direction == "LONG":
            entry_price_sm = round(squeeze_high + 0.25, 2)
        else:
            entry_price_sm = round(squeeze_low - 0.25, 2)

        logger.info(f"[EVAL] {self.name}: SIGNAL {direction} entry={ref_bar.close:.2f}")
        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=75.0,  # Compression breakouts are high-conviction setups
            entry_score=55.0,  # Score 55/60 = "B" tier risk
            strategy=self.name,
            reason=(
                f"Compression breakout {direction} after "
                f"{self._state.consecutive_squeeze_bars}-bar squeeze, "
                f"vol {(ref_bar.volume/squeeze_avg_vol):.1f}x"
            ),
            confluences=confluences,
            entry_type="STOPMARKET",
            entry_price=entry_price_sm,
            metadata={"squeeze_high": squeeze_high, "squeeze_low": squeeze_low},
        )

    # ── Helper methods ─────────────────────────────────────────────────

    def _select_compression_bars(self, tf: str, bars_5m, bars_1m, market):
        """Select the bar series to use for compression detection."""
        if tf == "5m":
            return bars_5m
        elif tf == "15m":
            # We need to either get 15m bars from market or aggregate 5m → 15m
            # For now: aggregate 5m bars into 15m groups of 3
            if len(bars_5m) < 3:
                return None
            return self._aggregate_bars(bars_5m, 3)
        elif tf == "60m":
            if len(bars_5m) < 12:
                return None
            return self._aggregate_bars(bars_5m, 12)
        return bars_5m

    def _aggregate_bars(self, bars: list, group_size: int) -> list:
        """Aggregate N consecutive bars into 1 larger bar (OHLCV)."""
        result = []
        for i in range(0, len(bars) - group_size + 1, group_size):
            group = bars[i:i + group_size]
            if len(group) < group_size:
                break
            # Use Bar dataclass from tick_aggregator
            from core.tick_aggregator import Bar
            agg = Bar(
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum(b.volume for b in group),
                tick_count=sum(b.tick_count for b in group),
                start_time=group[0].start_time,
                end_time=group[-1].end_time,
            )
            result.append(agg)
        return result

    def _calculate_atr(self, bars: list, period: int) -> Optional[float]:
        """True Range avg over `period` bars."""
        if len(bars) < period:
            return None
        tr_values = []
        for i in range(1, len(bars)):
            curr = bars[i]
            prev = bars[i - 1]
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close),
            )
            tr_values.append(tr)
        if not tr_values:
            return None
        return sum(tr_values[-period:]) / min(len(tr_values), period)

    def _calculate_bb_kc(self, bars: list, bb_period: int, bb_std: float,
                          kc_period: int, kc_atr_mult: float):
        """Calculate Bollinger Band width and Keltner Channel width."""
        if len(bars) < max(bb_period, kc_period):
            return None, None, None, None

        # Bollinger Bands
        closes = [b.close for b in bars[-bb_period:]]
        bb_mean = sum(closes) / len(closes)
        bb_variance = sum((c - bb_mean) ** 2 for c in closes) / len(closes)
        bb_stdev = math.sqrt(bb_variance)
        bb_upper = bb_mean + bb_std * bb_stdev
        bb_lower = bb_mean - bb_std * bb_stdev
        bb_width = bb_upper - bb_lower

        # Keltner Channels (EMA-based, simplified to SMA for clarity)
        kc_atr = self._calculate_atr(bars[-kc_period:], kc_period - 1)
        if kc_atr is None:
            return None, None, None, None
        kc_mean = sum(b.close for b in bars[-kc_period:]) / kc_period
        kc_upper = kc_mean + kc_atr_mult * kc_atr
        kc_lower = kc_mean - kc_atr_mult * kc_atr
        kc_width = kc_upper - kc_lower

        return bb_width, kc_width, bb_upper, bb_lower

    def _check_htf_alignment(self, direction: str, market: dict,
                              session_info: dict) -> bool:
        """
        Verify HTF (1-hour) trend aligns with breakout direction.
        Use EMA50 slope as proxy if available.
        """
        # Use 60m bias from tick_aggregator if available
        tf_bias = market.get("tf_bias", {})
        bias_60m = tf_bias.get("60m", "NEUTRAL")

        if direction == "LONG" and bias_60m == "BEARISH":
            return False  # Don't long against 1h downtrend
        if direction == "SHORT" and bias_60m == "BULLISH":
            return False  # Don't short against 1h uptrend

        # Also check MenthorQ HVL regime if available
        # If price below HVL in negative gamma regime: momentum mode (good for breakouts)
        # If price above HVL in positive gamma regime: mean-reversion mode (bad for breakouts)
        regime = session_info.get("regime", "")
        if "AFTERNOON_CHOP" in regime or "CLOSE_CHOP" in regime:
            return False  # Avoid chop windows

        return True
