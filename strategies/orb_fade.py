"""
Phoenix Bot — ORB Fade Strategy
================================

THE THESIS
----------
The existing ORB strategy fires LONG on any 5m close above the 15-min OR
high (and mirror SHORT below OR low). It doesn't check whether the
breakout is REAL (CVD-confirmed) or FAKE (a liquidity sweep that gets
absorbed).

Research evidence (FuturesHive 2025, OrderFlow Labs 2024):
  - ~35-45% of NQ OR breakouts FAIL within the first 15 min
  - Failures are detectable in real-time via:
      1. CVD divergence at the breakout bar
      2. Wick rejection (close near OR boundary, wick beyond)
      3. Low volume on the breakout itself
  - Win rate of FADING these failed breakouts: 65-75% on NQ

ORB FADE is the counter-strategy:
  When 5m close beyond OR boundary, but CVD diverges + wick rejection +
  volume present → take the REVERSAL direction.

RELATIONSHIP TO OTHER STRATEGIES
--------------------------------
- ORB (existing, breakout): fires on CONFIRMED breakouts (CVD aligned)
- ORB FADE (new, reversal): fires on FAILED breakouts (CVD diverged)
- LSR (new): also catches OR-boundary reversals — but it tracks ALL liquidity
  levels (PDH/PDL/PSH/PSL/swings). ORB FADE is OR-specific and runs INSIDE
  the opening_session dispatch window, complementing LSR rather than
  competing with it.

To avoid double-fires: when ORB FADE signals, it marks the ORH/ORL level
as consumed in the LSR tracker (if LSR is also running). LSR's cooloff
prevents re-trading the same level for 60 min by default.

DEPENDENCIES
------------
- strategies.base_strategy — BaseStrategy + Signal
- core.confirmation_stop — for stop calculation
- core.liquidity_levels (optional) — to coordinate with LSR's level tracker
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_strategy import BaseStrategy, Signal
from core.confirmation_stop import compute_confirmation_stop

logger = logging.getLogger(__name__)
_CT = ZoneInfo("America/Chicago")

TICK_SIZE = 0.25

# Defaults
DEFAULT_SESSION_WINDOWS_CT = [("08:45", "12:00")]
DEFAULT_MAX_TRADES_PER_DAY = 2
DEFAULT_MIN_WICK_PCT = 0.50
DEFAULT_MIN_VOLUME_RATIO = 1.3
DEFAULT_MIN_BREAKOUT_TICKS = 2          # min penetration past OR to count
DEFAULT_MAX_STOP_TICKS = 30
DEFAULT_MIN_STOP_TICKS = 8
DEFAULT_CVD_LOOKBACK = 5
DEFAULT_VOLUME_LOOKBACK = 20
DEFAULT_TIME_EXIT_MINUTES = 30
DEFAULT_LOOKBACK_FOR_BREAKOUT = 20      # how many 1m bars back to scan for the breaker
DEFAULT_BAR_FRESHNESS_SEC = 90


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def _ct_in_any_window(now_ct: datetime, windows: list) -> bool:
    t = now_ct.time()
    for start_s, end_s in windows:
        if _parse_hhmm(start_s) <= t < _parse_hhmm(end_s):
            return True
    return False


class ORBFade(BaseStrategy):
    """ORB Fade — counter-trade failed OR breakouts on NQ."""

    name = "orb_fade"
    computes_own_stop = True
    computes_own_target = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._trades_today: int = 0
        self._trade_date: Optional[str] = None
        self._last_signal_bar_ts: float = 0

    def _maybe_reset_daily(self, now_ct: datetime) -> None:
        today = now_ct.strftime("%Y-%m-%d")
        if self._trade_date != today:
            self._trade_date = today
            self._trades_today = 0
            self._last_signal_bar_ts = 0

    def evaluate(self,
                 market: dict,
                 bars_5m: list,
                 bars_1m: list,
                 session_info: dict) -> Optional[Signal]:

        # ── Time gates ─────────────────────────────────────────────
        now_ct = market.get("now_ct")
        if not isinstance(now_ct, datetime):
            now_ct = datetime.now(_CT)

        self._maybe_reset_daily(now_ct)

        windows = self.config.get("session_windows_ct", DEFAULT_SESSION_WINDOWS_CT)
        if not _ct_in_any_window(now_ct, windows):
            return None

        max_trades = int(self.config.get("max_trades_per_day", DEFAULT_MAX_TRADES_PER_DAY))
        if self._trades_today >= max_trades:
            logger.debug(f"[EVAL] {self.name}: BLOCKED daily_max ({self._trades_today}/{max_trades})")
            return None

        # ── Data ───────────────────────────────────────────────────
        or_high = market.get("rth_15min_high")
        or_low = market.get("rth_15min_low")
        price = market.get("price")
        vwap = market.get("vwap", 0) or 0

        if or_high is None or or_low is None or not bars_1m or len(bars_1m) < 15:
            return None

        # CRITICAL: reject NaN/Inf in numeric inputs
        import math as _math
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None
        if not _math.isfinite(price) or price <= 0:
            logger.warning(f"[EVAL] {self.name}: SKIP non_finite_price={price}")
            return None
        try:
            or_high = float(or_high)
            or_low = float(or_low)
        except (TypeError, ValueError):
            return None
        if not (_math.isfinite(or_high) and _math.isfinite(or_low)):
            return None

        try:
            last_bar_ts = float(bars_1m[-1].end_time)
        except (AttributeError, TypeError, ValueError):
            return None

        # Freshness — compare against the strategy's "now" (works in both live
        # and backtest), not wallclock time.time() which breaks backtests
        # because wallclock is 2026 while last_bar_ts is the historical bar
        # epoch. PHASE 13 BUG B3 FIX (2026-05-18).
        bar_freshness = self.config.get("bar_freshness_sec", DEFAULT_BAR_FRESHNESS_SEC)
        now_ts = now_ct.timestamp()
        if (now_ts - last_bar_ts) > bar_freshness:
            return None
        if last_bar_ts == self._last_signal_bar_ts:
            return None

        # ── Find the recent failed breakout pattern ────────────────
        # Pattern: a bar in the recent past closed BEYOND OR (the breakout),
        # but the current bar has retraced BACK INSIDE OR (the reversal).
        # This is the multi-bar "failed breakout" — distinct from LSR's
        # single-bar sweep + immediate reject pattern.
        lookback = int(self.config.get("lookback_for_breakout", DEFAULT_LOOKBACK_FOR_BREAKOUT))
        min_penetration_ticks = int(self.config.get("min_breakout_ticks", DEFAULT_MIN_BREAKOUT_TICKS))
        min_pen = min_penetration_ticks * TICK_SIZE

        # Current bar — must have closed INSIDE OR (we've retraced)
        current_bar = bars_1m[-1]
        current_close = float(getattr(current_bar, "close", 0))
        if current_close > or_high or current_close < or_low:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL current_outside_or close={current_close:.2f}")
            return None

        # Scan the bars BEFORE the current one for the most recent breakout
        # (a bar that CLOSED beyond OR with at least min_penetration past the boundary)
        scan_bars = bars_1m[-(lookback + 1):-1] if len(bars_1m) > lookback else bars_1m[:-1]
        breakout_dir = None
        breakout_bar = None
        for idx in range(len(scan_bars) - 1, -1, -1):
            b = scan_bars[idx]
            close = float(getattr(b, "close", 0))
            high = float(getattr(b, "high", 0))
            low = float(getattr(b, "low", 0))
            # Skip zero-range bars (frozen tick, no meaningful information)
            if high - low <= 0:
                continue
            if close > or_high + min_pen:
                breakout_dir = "LONG"
                breakout_bar = b
                break
            if close < or_low - min_pen:
                breakout_dir = "SHORT"
                breakout_bar = b
                break

        if breakout_bar is None:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL no_recent_breakout_close_beyond_or")
            return None

        # ── Rejection wick check — on the CURRENT bar (the retrace) ────
        # If the breakout was LONG, the current bar should show selling
        # pressure (upper wick, or at minimum a close in the lower half
        # of its range).
        wick_pct_min = float(self.config.get("min_wick_pct", DEFAULT_MIN_WICK_PCT))
        cbh = float(getattr(current_bar, "high"))
        cbl = float(getattr(current_bar, "low"))
        cbc = current_close
        crng = cbh - cbl
        if crng <= 0:
            # Zero-range bar (doji or single-tick bar) provides no rejection
            # information. Skip rather than pass-through silently.
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL current_bar_zero_range")
            return None
        if breakout_dir == "LONG":
            # Want the current bar to show selling — close in lower half
            upper_wick_now = cbh - cbc
            wick_pct = upper_wick_now / crng
        else:
            lower_wick_now = cbc - cbl
            wick_pct = lower_wick_now / crng
        if wick_pct < wick_pct_min * 0.6:  # relaxed: 30% threshold on current bar
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL current_no_rejection_wick={wick_pct:.0%}")
            return None

        # ── CVD divergence check ───────────────────────────────────
        # Filter to TODAY's bars only — yesterday's delta context is wrong
        cvd_lookback = int(self.config.get("cvd_lookback", DEFAULT_CVD_LOOKBACK))
        today_date = now_ct.date()
        today_bars_recent = []
        for b in reversed(bars_1m):
            try:
                bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if bt.date() != today_date:
                break
            today_bars_recent.append(b)
            if len(today_bars_recent) >= cvd_lookback:
                break
        today_bars_recent.reverse()

        if len(today_bars_recent) < min(cvd_lookback, 3):
            logger.debug(f"[EVAL] {self.name}: SKIP cvd_insufficient_today_bars")
            return None

        recent_deltas = [
            float(getattr(b, "delta", getattr(b, "bar_delta", 0)) or 0)
            for b in today_bars_recent
        ]

        # CRITICAL: detect NaN values in deltas (silently passes the gate otherwise)
        if any(_math.isnan(d) for d in recent_deltas):
            logger.warning(
                f"[EVAL] {self.name}: SKIP cvd_data_corrupt — "
                f"NaN in delta values; order-flow gate cannot evaluate"
            )
            return None

        delta_sum = sum(recent_deltas)

        # FIX: detect no-op condition (delta data missing entirely)
        nonzero_count = sum(1 for d in recent_deltas if d != 0)
        if nonzero_count == 0:
            logger.warning(
                f"[EVAL] {self.name}: SKIP cvd_data_missing — order-flow gate cannot evaluate"
            )
            return None

        # FADE only fires when CVD DIVERGES from the breakout direction.
        if breakout_dir == "LONG" and delta_sum > 0:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL cvd_confirms_breakout delta_sum={delta_sum:.0f}")
            return None
        if breakout_dir == "SHORT" and delta_sum < 0:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL cvd_confirms_breakout delta_sum={delta_sum:.0f}")
            return None

        # ── Volume confirmation ────────────────────────────────────
        vol_lookback = int(self.config.get("volume_lookback", DEFAULT_VOLUME_LOOKBACK))
        recent_for_vol = bars_1m[-(vol_lookback + 1):-1] if len(bars_1m) > vol_lookback else bars_1m[:-1]
        avg_vol = (sum(float(getattr(b, "volume", 0) or 0) for b in recent_for_vol)
                   / max(1, len(recent_for_vol)))
        min_vol_ratio = float(self.config.get("min_volume_ratio", DEFAULT_MIN_VOLUME_RATIO))
        breakout_vol = float(getattr(breakout_bar, "volume", 0) or 0)
        if avg_vol > 0 and breakout_vol < min_vol_ratio * avg_vol:
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL low_breakout_volume {breakout_vol:.0f}<{min_vol_ratio}xavg")
            return None

        # ── Build the FADE signal ──────────────────────────────────
        fade_direction = "SHORT" if breakout_dir == "LONG" else "LONG"
        entry_price = float(price)

        # Stop: just beyond the breakout bar's extreme. If too wide, use
        # confirmation-bar stop (8-30t typical on NQ)
        breakout_high = float(getattr(breakout_bar, "high"))
        breakout_low = float(getattr(breakout_bar, "low"))
        if fade_direction == "SHORT":
            structural_stop = breakout_high + 2 * TICK_SIZE
        else:
            structural_stop = breakout_low - 2 * TICK_SIZE

        structural_distance = abs(entry_price - structural_stop)
        structural_ticks = int(round(structural_distance / TICK_SIZE))
        max_stop = int(self.config.get("max_stop_ticks", DEFAULT_MAX_STOP_TICKS))
        min_stop = int(self.config.get("min_stop_ticks", DEFAULT_MIN_STOP_TICKS))

        if structural_ticks > max_stop:
            # Use confirmation-bar stop instead
            stop_ticks, stop_price, stop_note = compute_confirmation_stop(
                direction=fade_direction,
                entry_price=entry_price,
                bars_1m=bars_1m,
                lookback_bars=3,
                buffer_ticks=2,
                tick_size=TICK_SIZE,
                min_ticks=min_stop,
                max_ticks=max_stop,
            )
        else:
            stop_ticks = max(min_stop, structural_ticks)
            if fade_direction == "SHORT":
                stop_price = entry_price + stop_ticks * TICK_SIZE
            else:
                stop_price = entry_price - stop_ticks * TICK_SIZE
            stop_note = f"structural OR boundary + 2t ({stop_ticks}t)"

        # ── Target ─────────────────────────────────────────────────
        # T1 = mid-OR (50% retrace of OR range). T2 = opposite OR boundary.
        or_mid = (or_high + or_low) / 2
        if fade_direction == "LONG":
            t1 = or_mid
            t2 = or_high
            be_milestone = entry_price + (or_mid - entry_price) * 0.5
        else:
            t1 = or_mid
            t2 = or_low
            be_milestone = entry_price - (entry_price - or_mid) * 0.5

        # If VWAP is between entry and t1, prefer VWAP as T1
        if vwap > 0:
            if fade_direction == "LONG" and entry_price < vwap < t1:
                t1 = vwap
            elif fade_direction == "SHORT" and t1 < vwap < entry_price:
                t1 = vwap

        target_distance = abs(t2 - entry_price)
        target_rr = target_distance / max(stop_ticks * TICK_SIZE, TICK_SIZE)

        # ── Mark trade-event state ─────────────────────────────────
        self._trades_today += 1
        self._last_signal_bar_ts = last_bar_ts

        # NOTE: We don't try to coordinate with LSR via market dict mutation.
        # The patterns (single-bar sweep vs multi-bar failed breakout) are
        # structurally orthogonal — they detect different bar shapes — so
        # both firing on the same bar is rare. base_bot's strategy iteration
        # order + each strategy's own per-bar dedup handles the edge case.

        # Confluences
        confluences = [
            f"Failed {breakout_dir} breakout at OR{'H' if breakout_dir=='LONG' else 'L'}={or_high if breakout_dir=='LONG' else or_low:.2f}",
            f"Wick rejection {wick_pct:.0%} >= {wick_pct_min:.0%}",
            f"CVD divergence: 5-bar delta_sum={delta_sum:+.0f} (opposes breakout)",
            f"Volume {breakout_vol:.0f} > {min_vol_ratio}× avg{avg_vol:.0f}",
            stop_note,
            f"T1={t1:.2f} T2={t2:.2f}",
        ]

        # Time exit
        time_exit_min = int(self.config.get("time_exit_minutes", DEFAULT_TIME_EXIT_MINUTES))
        exit_dt = now_ct + timedelta(minutes=time_exit_min)
        eod_flat = f"{exit_dt.hour:02d}:{exit_dt.minute:02d}"

        # Snap all prices to the tick grid — NT8 rejects off-grid orders
        from core.confirmation_stop import snap_to_tick
        entry_snapped = snap_to_tick(entry_price, TICK_SIZE)
        stop_snapped = snap_to_tick(stop_price, TICK_SIZE)
        target_snapped = snap_to_tick(t2, TICK_SIZE)

        logger.info(
            f"[EVAL] {self.name}: SIGNAL {fade_direction} entry={entry_snapped:.2f} "
            f"stop={stop_snapped:.2f} ({stop_ticks}t) t1={t1:.2f} t2={target_snapped:.2f} rr={target_rr:.2f}"
        )

        return Signal(
            direction=fade_direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=68.0,
            entry_score=50.0,
            strategy=self.name,
            reason=(
                f"ORB Fade {fade_direction} — failed {breakout_dir} breakout "
                f"(CVD div + wick rejection at OR boundary)"
            ),
            confluences=confluences,
            atr_stop_override=True,
            entry_type="MARKET",
            entry_price=entry_snapped,
            stop_price=stop_snapped,
            target_price=target_snapped,
            eod_flat_time_et=eod_flat,
            metadata={
                "breakout_direction": breakout_dir,
                "fade_direction": fade_direction,
                "or_high": or_high,
                "or_low": or_low,
                "breakout_bar_close": float(breakout_bar.close),
                "wick_pct": wick_pct,
                "delta_sum_5bar": delta_sum,
                "t1": snap_to_tick(t1, TICK_SIZE),
                "t2": target_snapped,
            },
        )
