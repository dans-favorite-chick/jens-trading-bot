"""
Phoenix Bot — Noise Area Intraday Momentum

Source: Zarattini, Aziz & Barbon (2024) — "Beat the Market: An Effective
Intraday Momentum Strategy for the S&P500 ETF (SPY)." SSRN 4824172.

Published results (SPY, 2007-2024): 19.6% annual, Sharpe 1.33
NQ backtest (Quantitativo): 24.3% annual, Sharpe 1.67, 38% WR, payoff 2.25

Mechanics:
- Dynamic noise cone expanding through session, based on 14-day rolling
  mean of absolute intraday moves at each minute-of-day (sigma_open), shifted 1 day.
- Boundaries anchored to max(today_open, prev_close) for upper, min() for lower.
- Entry when price breaks outside cone AND on correct side of session VWAP (9:30 ET anchor).
- Signal check every 30 minutes (top/bottom of hour).
- Exit: dynamic — price returns inside cone OR signal-flip on VWAP OR EoD flat.
- Warmup: needs >= min_noise_history_days of sigma_open data before firing.

MNQ adaptations from published SPY spec:
- No dividend adjustment (futures don't pay)
- "Open" = 9:30 ET cash open (minute_of_day == 0)
- VWAP is session-anchored (Phoenix market['vwap'] already resets at session open)
- Prod mode uses 10:55 ET EoD flat; lab mode uses 15:55 ET (full-day Zarattini)
"""

from datetime import datetime, timedelta

from strategies.base_strategy import BaseStrategy, Signal
from config.settings import TICK_SIZE


# US Central → Eastern: ET = CT + 1 hour (standard, and during DST the offset
# stays 1 hour because both observe DST together).
_CT_TO_ET_HOURS = 1


class NoiseAreaMomentum(BaseStrategy):
    """Zarattini 2024 noise-cone intraday momentum."""

    name = "noise_area"

    def __init__(self, config: dict):
        super().__init__(config)
        # {minute_of_day: [|move_open| per day, up to 30 days]}
        self.sigma_open_table: dict[int, list[float]] = {}
        self._last_seen_1m_ts: float = 0
        self._last_30min_fired_ts: float = 0
        self._last_daily_reset_date: str | None = None
        self._today_open_price: float | None = None
        self.is_prod_bot: bool = config.get("is_prod_bot", False)

    # ─── sigma_open_table management ────────────────────────────────────

    def seed_history(self, history: dict[int, list[float]]):
        """
        Inject pre-computed 14+ days of history (called by warmup loader
        at bot startup). Key = minute_of_day (0 = 9:30 ET, 30 = 10:00 ET, ...).
        Value = list of |move_open| samples, newest last.
        """
        for minute_of_day, samples in history.items():
            self.sigma_open_table[minute_of_day] = list(samples)[-30:]

    def _update_sigma_open_from_bar(self, bar, today_open_price: float):
        """Called once per completed 1m bar. Records |close/open - 1|."""
        if today_open_price <= 0:
            return
        bar_dt_et = datetime.fromtimestamp(bar.end_time) + timedelta(hours=_CT_TO_ET_HOURS)
        minute_of_day = self._minute_of_day(bar_dt_et)
        if minute_of_day < 0 or minute_of_day > 390:  # 6.5h session = 390 min
            return
        move_open = abs(bar.close / today_open_price - 1)
        self.sigma_open_table.setdefault(minute_of_day, []).append(move_open)
        # Cap memory per minute-of-day
        if len(self.sigma_open_table[minute_of_day]) > 30:
            self.sigma_open_table[minute_of_day] = self.sigma_open_table[minute_of_day][-30:]

    def _get_sigma_open(self, minute_of_day: int) -> float | None:
        """Rolling 14-day mean of |move_open|, shifted 1 day (exclude today)."""
        history = self.sigma_open_table.get(minute_of_day, [])
        if len(history) < 13:
            return None
        # Exclude the most recent entry (today's partial if already written) and
        # average the prior 14 days. If only 13 days available, use those.
        window = history[-15:-1] if len(history) >= 15 else history[:-1]
        if not window:
            return None
        return sum(window) / len(window)

    @staticmethod
    def _minute_of_day(now_et: datetime) -> int:
        """Minutes since 9:30 ET. 9:30 = 0, 10:00 = 30, 15:30 = 360."""
        open_time = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        return int((now_et - open_time).total_seconds() / 60)

    # ─── Main evaluation ────────────────────────────────────────────────

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Signal | None:
        if len(bars_1m) < 1:
            return None

        # ── Config ──────────────────────────────────────────────────
        lookback_days = int(self.config.get("lookback_days", 14))
        band_mult = float(self.config.get("band_mult", 1.0))
        trade_freq_min = int(self.config.get("trade_freq_minutes", 30))
        require_vwap = bool(self.config.get("require_vwap_confluence", True))
        min_history = int(self.config.get("min_noise_history_days", 10))
        eod_time_et = self.config.get(
            "prod_eod_flat_time_et" if self.is_prod_bot else "eod_flat_time_et",
            "10:55" if self.is_prod_bot else "15:55",
        )

        # ── Resolve current time (bars are CT; convert to ET) ────────
        last_bar = bars_1m[-1]
        try:
            bar_dt_ct = datetime.fromtimestamp(last_bar.end_time)
        except (OSError, ValueError, TypeError):
            return None
        bar_dt_et = bar_dt_ct + timedelta(hours=_CT_TO_ET_HOURS)
        today_str = bar_dt_et.strftime("%Y-%m-%d")

        # Daily reset (clears today_open_price for fresh detection)
        if self._last_daily_reset_date != today_str:
            self._last_daily_reset_date = today_str
            self._today_open_price = None
            self._last_30min_fired_ts = 0

        # ── Determine today's open (first bar at/after 9:30 ET) ──────
        if self._today_open_price is None:
            for b in bars_1m:
                b_dt_et = datetime.fromtimestamp(b.end_time) + timedelta(hours=_CT_TO_ET_HOURS)
                if b_dt_et.strftime("%Y-%m-%d") != today_str:
                    continue
                if self._minute_of_day(b_dt_et) >= 0:
                    self._today_open_price = b.open
                    break
            if self._today_open_price is None:
                return None  # Session hasn't opened yet

        # ── Update sigma_open_table with new 1m bars only once each ──
        if last_bar.end_time > self._last_seen_1m_ts:
            # Process every bar newer than last seen (handles gaps)
            for b in bars_1m:
                if b.end_time > self._last_seen_1m_ts:
                    self._update_sigma_open_from_bar(b, self._today_open_price)
            self._last_seen_1m_ts = last_bar.end_time

        # ── Warmup gate ─────────────────────────────────────────────
        minutes_with_history = sum(
            1 for samples in self.sigma_open_table.values() if len(samples) >= min_history
        )
        if minutes_with_history < 30:  # Need at least 30 minute-buckets populated
            return None

        # ── 30-minute signal cadence ────────────────────────────────
        minute_of_hour = bar_dt_et.minute
        if minute_of_hour % trade_freq_min != 0:
            return None
        # Dedup: one signal per 30-min window
        window_key = bar_dt_et.replace(second=0, microsecond=0).timestamp()
        if window_key == self._last_30min_fired_ts:
            return None
        self._last_30min_fired_ts = window_key

        # ── EoD cutoff ──────────────────────────────────────────────
        if bar_dt_et.strftime("%H:%M") >= eod_time_et:
            return None

        # ── Compute noise cone ──────────────────────────────────────
        minute_of_day = self._minute_of_day(bar_dt_et)
        if minute_of_day < 0:
            return None
        sigma_open = self._get_sigma_open(minute_of_day)
        if sigma_open is None:
            return None

        today_open = self._today_open_price
        prev_close = market.get("avwap_pd_close", 0) or 0
        if prev_close <= 0:
            # Fallback: use today_open if prev_close not available
            prev_close = today_open

        ub = max(today_open, prev_close) * (1 + band_mult * sigma_open)
        lb = min(today_open, prev_close) * (1 - band_mult * sigma_open)

        # ── Signal logic ────────────────────────────────────────────
        price = market.get("price", 0) or 0
        if price <= 0:
            return None
        vwap = market.get("vwap", 0) or 0
        if require_vwap and vwap <= 0:
            return None  # VWAP not ready — don't fire without dual confirm

        direction = None
        if price > ub and (not require_vwap or price > vwap):
            direction = "LONG"
        elif price < lb and (not require_vwap or price < vwap):
            direction = "SHORT"
        if direction is None:
            return None

        # ── Price construction ──────────────────────────────────────
        # Entry: LIMIT at current price + 1 tick wiggle (per spec).
        # Stop: opposite noise boundary + 2-tick buffer.
        # Target: None — dynamic managed exit; base_bot handles EoD flat + stop.
        buf = 2 * TICK_SIZE
        if direction == "LONG":
            entry_price = round(price + TICK_SIZE, 2)
            stop_price = round(lb - buf, 2)
            stop_distance = entry_price - stop_price
        else:
            entry_price = round(price - TICK_SIZE, 2)
            stop_price = round(ub + buf, 2)
            stop_distance = stop_price - entry_price

        if stop_distance <= 0:
            return None
        stop_ticks = max(4, int(stop_distance / TICK_SIZE))

        confluences = [
            f"minute_of_day={minute_of_day}",
            f"sigma_open={sigma_open:.5f}",
            f"UB={ub:.2f} LB={lb:.2f}",
            f"today_open={today_open:.2f} prev_close={prev_close:.2f}",
            f"VWAP={vwap:.2f}" if vwap else "VWAP=n/a",
        ]

        broken_boundary = ub if direction == "LONG" else lb
        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=0.0,  # Dynamic exit; target_rr unused
            confidence=60.0,
            entry_score=50.0,
            strategy=self.name,
            reason=(
                f"NoiseArea {direction} — price {price:.2f} "
                f"{'broke UB' if direction == 'LONG' else 'broke LB'} "
                f"{broken_boundary:.2f}; VWAP-confirmed at {vwap:.2f}"
            ),
            confluences=confluences,
            atr_stop_override=True,
            entry_type="LIMIT",
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=None,  # Managed exit — no bracket target
            exit_trigger="price_returns_inside_noise_area",
            eod_flat_time_et=eod_time_et,
            metadata={
                "UB": ub, "LB": lb, "vwap": vwap,
                "sigma_open": sigma_open,
                "today_open": today_open, "prev_close": prev_close,
            },
        )

    # ─── Managed-exit check (called every bar by base_bot) ──────────────

    def check_exit(self, position, market: dict, bars_1m: list,
                   session_info: dict) -> tuple[bool, str]:
        """
        Dynamic exit logic — called every bar for active noise_area positions.
        Exits when price returns inside the noise cone OR crosses VWAP.
        """
        if not bars_1m:
            return (False, "")
        last_bar = bars_1m[-1]
        try:
            bar_dt_et = datetime.fromtimestamp(last_bar.end_time) + timedelta(hours=_CT_TO_ET_HOURS)
        except (OSError, ValueError, TypeError):
            return (False, "")

        # 1. EoD flat
        eod_time_et = self.config.get(
            "prod_eod_flat_time_et" if self.is_prod_bot else "eod_flat_time_et",
            "10:55" if self.is_prod_bot else "15:55",
        )
        if bar_dt_et.strftime("%H:%M") >= eod_time_et:
            return (True, "eod_flat")

        # 2. Signal flip — price back inside cone or wrong side of VWAP
        price = market.get("price", 0) or 0
        vwap = market.get("vwap", 0) or 0
        meta = getattr(position, "metadata", None) or {}
        # Pull the cone captured at entry (base_bot stashes signal.metadata into position.metadata)
        ub_entry = meta.get("UB")
        lb_entry = meta.get("LB")
        if price <= 0:
            return (False, "")

        direction = getattr(position, "direction", "")
        if direction == "LONG":
            if ub_entry is not None and price < ub_entry:
                return (True, "signal_flip_returned_below_UB")
            if vwap > 0 and price < vwap:
                return (True, "signal_flip_below_vwap")
        elif direction == "SHORT":
            if lb_entry is not None and price > lb_entry:
                return (True, "signal_flip_returned_above_LB")
            if vwap > 0 and price > vwap:
                return (True, "signal_flip_above_vwap")

        return (False, "")
