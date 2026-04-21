"""
Phoenix Bot — Opening Session Strategy

Family of 6 opening-window sub-strategies dispatched by time + opening type:

  1. _evaluate_open_drive         (OPEN_DRIVE; 08:35-09:00 CT)
  2. _evaluate_open_test_drive    (OPEN_TEST_DRIVE; 08:30-09:00 CT)
  3. _evaluate_open_auction_in    (OPEN_AUCTION_IN; 09:30-12:30 CT)
  4. _evaluate_open_auction_out   (OPEN_AUCTION_OUT; 08:45-11:00 CT)
  5. _evaluate_premarket_breakout (any type; 08:30-08:45 CT)
  6. _evaluate_orb                (any type; 08:45-14:30 CT)

Lab-only (validated=False). 1-contract exits with BE-on-milestone and
time exit. Multi-leg is stubbed for future multi-contract accounts.

Universal guards (all sub-evaluators):
  - News blackout (+/-5 min around high-impact releases)
  - Gamma gate (core.menthorq_gamma.is_entry_into_wall)
  - Max 2 trades/day
  - Day flat by 14:30 CT
  - Volume confirmation on the entry bar (each sub-evaluator)

Stop math (Fix 6, locked 2026-04-20):
  - MIN 40 ticks (NQ noise floor)
  - MAX 100 ticks (daily-cap protection)
  - Structural stop computed per setup, then clamped/rejected.

Phase 3 scope: strategy file + config block + tests.
Phase 4 (not in scope here): snapshot enrichment in base_bot, registration
in bot strategy_classes, multi-account routing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Any, Callable, Optional

from config.settings import TICK_SIZE
from core.session_levels import (
    classify_opening_type,
    is_in_window,
    is_news_blackout,
)
from strategies.base_strategy import BaseStrategy, Signal

logger = logging.getLogger(__name__)


# ─── Exit planning ──────────────────────────────────────────────────
@dataclass
class ExitPlan:
    """Resolved exit plan for a single trade."""
    primary_target: float
    be_move_at: Optional[float]
    time_exit_ct: dtime
    invalidation_check: Optional[Callable[..., bool]] = None
    metadata: dict = field(default_factory=dict)


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


# ─── Strategy ───────────────────────────────────────────────────────
class OpeningSessionStrategy(BaseStrategy):
    """Single strategy dispatching to 6 opening-window sub-evaluators."""

    name = "opening_session"

    def __init__(self, config: dict):
        super().__init__(config)
        # Per-day state
        self._daily_trades_today: int = 0
        self._trade_date: Optional[str] = None
        # ORB: one-direction-per-day rule
        self._orb_first_break_direction: Optional[str] = None

    # ── Daily reset ────────────────────────────────────────────────
    def _maybe_reset_daily(self, now_ct: datetime) -> None:
        today = now_ct.strftime("%Y-%m-%d")
        if self._trade_date != today:
            self._trade_date = today
            self._daily_trades_today = 0
            self._orb_first_break_direction = None

    # ── Logging helper ─────────────────────────────────────────────
    def _log_eval(self, msg: str) -> None:
        logger.debug(f"[EVAL] {self.name}: {msg}")

    # ── Dispatcher ─────────────────────────────────────────────────
    def evaluate(self, market: dict, bars_5m: list = None, bars_1m: list = None,
                 session_info: dict = None) -> Signal | None:
        now_ct = market.get("now_ct")
        if not isinstance(now_ct, datetime):
            self._log_eval("SKIP missing_now_ct")
            return None

        self._maybe_reset_daily(now_ct)

        # ── Universal guards ───────────────────────────────────────
        if self._daily_trades_today >= int(self.config.get("max_trades_per_day", 2)):
            self._log_eval("BLOCKED daily_max")
            return None

        if is_news_blackout(now_ct, market.get("news_calendar", [])):
            self._log_eval("BLOCKED news_blackout")
            return None

        day_flat = _parse_hhmm(self.config.get("day_flat_time_ct", "14:30"))
        if now_ct.time() > day_flat:
            self._log_eval("BLOCKED past_day_flat_time")
            return None

        opening_type = market.get("opening_type")
        if opening_type is None:
            opening_type = classify_opening_type(market)

        # Order of dispatch below is intentional: breakout/drive windows
        # run first because their 08:30-09:00 windows overlap other types.

        def _try(sub_callable) -> Signal | None:
            sig = sub_callable(market)
            if sig is None:
                return None
            gated = self._apply_gamma_gate(sig, market)
            if gated is not None:
                self._daily_trades_today += 1
            return gated

        # 1. Premarket Breakout (any opening type)
        if is_in_window(now_ct, "08:30", "08:45"):
            out = _try(self._evaluate_premarket_breakout)
            if out is not None:
                return out

        # 2. Open Drive
        if opening_type == "OPEN_DRIVE" and is_in_window(now_ct, "08:35", "09:00"):
            out = _try(self._evaluate_open_drive)
            if out is not None:
                return out

        # 3. Open Test Drive
        if opening_type == "OPEN_TEST_DRIVE" and is_in_window(now_ct, "08:30", "09:00"):
            out = _try(self._evaluate_open_test_drive)
            if out is not None:
                return out

        # 4. ORB (any opening type)
        if is_in_window(now_ct, "08:45", "14:30"):
            out = _try(self._evaluate_orb)
            if out is not None:
                return out

        # 5. Open Auction Out
        if opening_type == "OPEN_AUCTION_OUT" and is_in_window(now_ct, "08:45", "11:00"):
            out = _try(self._evaluate_open_auction_out)
            if out is not None:
                return out

        # 6. Open Auction In
        if opening_type == "OPEN_AUCTION_IN" and is_in_window(now_ct, "09:30", "12:30"):
            out = _try(self._evaluate_open_auction_in)
            if out is not None:
                return out

        return None

    # ── Gamma gate ─────────────────────────────────────────────────
    def _apply_gamma_gate(self, signal: Signal, market: dict) -> Signal | None:
        levels = market.get("gamma_levels")
        if levels is None:
            return signal
        try:
            from core.menthorq_gamma import is_entry_into_wall
        except ImportError:
            return signal
        entry_price = signal.entry_price if signal.entry_price is not None else market.get("price")
        if entry_price is None:
            return signal
        wall = is_entry_into_wall(float(entry_price), signal.direction, levels)
        if wall:
            self._log_eval(f"BLOCKED gamma_gate wall={wall}")
            return None
        return signal

    # ── Universal stop math ────────────────────────────────────────
    def _finalize_stop(
        self,
        entry_price: float,
        structural_stop: float,
        direction: str,
        sub_name: str,
    ) -> Optional[tuple[float, int]]:
        """
        Apply the MIN/MAX tick clamp. Returns (stop_price, stop_ticks)
        or None if the structural stop exceeds MAX and the signal must
        be rejected.
        """
        min_ticks = int(self.config.get("min_stop_ticks", 40))
        max_ticks = int(self.config.get("max_stop_ticks", 100))

        dist_ticks = abs(entry_price - structural_stop) / TICK_SIZE

        if dist_ticks > max_ticks:
            self._log_eval(
                f"BLOCKED {sub_name} stop_too_wide={dist_ticks:.1f}t>max={max_ticks}t"
            )
            return None

        if dist_ticks < min_ticks:
            # Widen to noise floor.
            if direction == "LONG":
                stop_price = entry_price - (min_ticks * TICK_SIZE)
            else:
                stop_price = entry_price + (min_ticks * TICK_SIZE)
            return (round(stop_price, 2), min_ticks)

        return (round(structural_stop, 2), int(round(dist_ticks)))

    # ── Exit planner ───────────────────────────────────────────────
    def determine_exits(
        self, signal: Signal, snapshot: dict, contract_count: int = 1
    ) -> ExitPlan:
        targets = self._get_strategy_targets(signal, snapshot)

        if contract_count == 1:
            return ExitPlan(
                primary_target=targets["t1"],
                be_move_at=targets.get("be_milestone"),
                time_exit_ct=targets["time_exit"],
                invalidation_check=targets.get("invalidation"),
                metadata=targets.get("metadata", {}),
            )

        # Multi-leg scaling stubbed for future contract_count > 1.
        raise NotImplementedError("Multi-leg scaling not yet implemented")

    def _get_strategy_targets(self, signal: Signal, snapshot: dict) -> dict:
        """Pull T1 / BE-milestone / time-exit from the signal's metadata."""
        md = signal.metadata or {}
        return {
            "t1": md.get("t1"),
            "be_milestone": md.get("be_milestone"),
            "time_exit": md.get("time_exit_ct"),
            "invalidation": md.get("invalidation"),
            "metadata": md,
        }

    # ── Signal builder ─────────────────────────────────────────────
    def _build_signal(
        self,
        *,
        direction: str,
        entry_price: float,
        stop_price: float,
        stop_ticks: int,
        t1: float,
        be_milestone: Optional[float],
        time_exit_ct: dtime,
        sub_name: str,
        reason: str,
        confluences: list[str],
        extra_metadata: Optional[dict] = None,
        invalidation: Optional[str] = None,
        confidence: float = 60.0,
        entry_score: float = 50.0,
    ) -> Signal:
        # Target RR is computed from T1 and structural stop distance so
        # the dashboard/logs still show a numeric RR even though the
        # actual exit is driven by ExitPlan.primary_target.
        stop_dist = abs(entry_price - stop_price)
        if stop_dist > 0:
            target_dist = abs(t1 - entry_price)
            target_rr = round(target_dist / stop_dist, 2)
        else:
            target_rr = 1.0

        metadata = {
            "sub_name": sub_name,
            "t1": t1,
            "be_milestone": be_milestone,
            "time_exit_ct": time_exit_ct,
            "invalidation": invalidation,
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=confidence,
            entry_score=entry_score,
            strategy=self.name,
            reason=reason,
            confluences=confluences,
            atr_stop_override=True,
            entry_type="MARKET",
            entry_price=round(entry_price, 2),
            stop_price=round(stop_price, 2),
            target_price=round(t1, 2),
            metadata=metadata,
        )

    # ═══════════════════════════════════════════════════════════════
    # Sub-evaluator 1: Open Drive
    # ═══════════════════════════════════════════════════════════════
    def _evaluate_open_drive(self, market: dict) -> Signal | None:
        sub = "open_drive"
        rth_open = market.get("rth_open_price")
        h5 = market.get("rth_5min_high")
        l5 = market.get("rth_5min_low")
        c5 = market.get("rth_5min_close")
        price = market.get("price")
        v1 = market.get("rth_1min_volume")
        avg_v1 = market.get("avg_1min_volume")
        pivot_pp = market.get("pivot_pp")

        required = (rth_open, h5, l5, c5, price, v1, avg_v1, pivot_pp)
        if any(v is None for v in required):
            self._log_eval(f"SKIP {sub} missing_fields")
            return None

        direction = "LONG" if c5 > rth_open else "SHORT"

        entry_vol_ratio = float(self.config.get("open_drive_entry_volume_ratio", 1.2))
        if avg_v1 <= 0 or v1 < entry_vol_ratio * avg_v1:
            self._log_eval(f"NO_SIGNAL {sub} low_volume")
            return None

        # Entry trigger: 1-min close beyond 5-min OR extreme.
        if direction == "LONG" and price <= h5:
            self._log_eval(f"NO_SIGNAL {sub} no_break_above_or")
            return None
        if direction == "SHORT" and price >= l5:
            self._log_eval(f"NO_SIGNAL {sub} no_break_below_or")
            return None

        # Structural stop = midpoint of 5-min OR.
        structural_stop = (h5 + l5) / 2.0
        finalized = self._finalize_stop(price, structural_stop, direction, sub)
        if finalized is None:
            return None
        stop_price, stop_ticks = finalized

        # 1R distance (points) for BE milestone.
        one_r = abs(price - stop_price)
        be_milestone = price + one_r if direction == "LONG" else price - one_r

        return self._build_signal(
            direction=direction,
            entry_price=price,
            stop_price=stop_price,
            stop_ticks=stop_ticks,
            t1=pivot_pp,
            be_milestone=be_milestone,
            time_exit_ct=_parse_hhmm("14:30"),
            sub_name=sub,
            reason=f"Open Drive {direction} — break of 5-min OR with volume",
            confluences=[
                f"OR break {'>' if direction == 'LONG' else '<'} "
                f"{h5 if direction == 'LONG' else l5:.2f}",
                f"1m vol {v1:.0f} > {entry_vol_ratio:.1f}x avg {avg_v1:.0f}",
            ],
            invalidation="price_re_enters_5min_or",
            extra_metadata={
                "or_high": h5,
                "or_low": l5,
                "trail_ticks_after_t1": int(self.config.get("open_drive_trail_ticks", 20)),
            },
        )

    # ═══════════════════════════════════════════════════════════════
    # Sub-evaluator 2: Open Test Drive
    # ═══════════════════════════════════════════════════════════════
    def _evaluate_open_test_drive(self, market: dict) -> Signal | None:
        sub = "open_test_drive"
        rth_open = market.get("rth_open_price")
        h5 = market.get("rth_5min_high")
        l5 = market.get("rth_5min_low")
        c5 = market.get("rth_5min_close")
        price = market.get("price")
        v1 = market.get("rth_1min_volume")
        avg_v1 = market.get("avg_1min_volume")
        pd_high = market.get("prior_day_high")
        pd_low = market.get("prior_day_low")
        pd_poc = market.get("prior_day_poc")

        required = (rth_open, h5, l5, c5, price, v1, avg_v1, pd_high, pd_low, pd_poc)
        if any(v is None for v in required):
            self._log_eval(f"SKIP {sub} missing_fields")
            return None

        # Direction: opposite of the failed test extreme.
        if h5 > pd_high:
            direction = "SHORT"
        elif l5 < pd_low:
            direction = "LONG"
        else:
            self._log_eval(f"NO_SIGNAL {sub} no_failed_test")
            return None

        rev_vol_ratio = float(self.config.get("open_test_drive_reversal_volume_ratio", 1.3))
        if avg_v1 <= 0 or v1 < rev_vol_ratio * avg_v1:
            self._log_eval(f"NO_SIGNAL {sub} low_reversal_volume")
            return None

        # Reversal through RTH open in trade direction.
        if direction == "SHORT" and price >= rth_open:
            self._log_eval(f"NO_SIGNAL {sub} no_close_through_open")
            return None
        if direction == "LONG" and price <= rth_open:
            self._log_eval(f"NO_SIGNAL {sub} no_close_through_open")
            return None

        buffer_ticks = int(self.config.get("open_test_drive_stop_buffer_ticks", 4))
        buf = buffer_ticks * TICK_SIZE
        structural_stop = (h5 + buf) if direction == "SHORT" else (l5 - buf)

        finalized = self._finalize_stop(price, structural_stop, direction, sub)
        if finalized is None:
            return None
        stop_price, stop_ticks = finalized

        one_r = abs(price - stop_price)
        be_milestone = price + one_r if direction == "LONG" else price - one_r

        # T1: prior_day_poc.
        t1 = pd_poc

        # time_exit: entry_time + 75 minutes (encoded in metadata; resolved by determine_exits consumer).
        time_exit_min = int(self.config.get("open_test_drive_time_exit_min", 75))
        now_ct = market.get("now_ct")
        time_exit = (now_ct + timedelta(minutes=time_exit_min)).time() if isinstance(now_ct, datetime) \
            else _parse_hhmm("14:30")

        return self._build_signal(
            direction=direction,
            entry_price=price,
            stop_price=stop_price,
            stop_ticks=stop_ticks,
            t1=t1,
            be_milestone=be_milestone,
            time_exit_ct=time_exit,
            sub_name=sub,
            reason=f"Open Test Drive {direction} — failed test reversal through RTH open",
            confluences=[
                f"Failed test of prior-day {'high' if direction == 'SHORT' else 'low'}",
                f"Reversal close through RTH open {rth_open:.2f}",
                f"1m vol {v1:.0f} > {rev_vol_ratio:.1f}x avg",
            ],
            extra_metadata={"time_exit_minutes": time_exit_min},
        )

    # ═══════════════════════════════════════════════════════════════
    # Sub-evaluator 3: Open Auction In
    # ═══════════════════════════════════════════════════════════════
    def _evaluate_open_auction_in(self, market: dict) -> Signal | None:
        sub = "open_auction_in"
        ib_high = market.get("rth_60min_high")
        ib_low = market.get("rth_60min_low")
        bar_open = market.get("rth_1min_open")
        bar_high = market.get("rth_1min_high")
        bar_low = market.get("rth_1min_low")
        bar_close = market.get("rth_1min_close")
        v1 = market.get("rth_1min_volume")
        avg_v1 = market.get("avg_1min_volume")
        pd_poc = market.get("prior_day_poc")
        price = market.get("price")

        required = (ib_high, ib_low, bar_open, bar_high, bar_low, bar_close,
                    v1, avg_v1, pd_poc, price)
        if any(v is None for v in required):
            self._log_eval(f"SKIP {sub} missing_fields")
            return None

        bar_range = bar_high - bar_low
        if bar_range <= 0:
            self._log_eval(f"NO_SIGNAL {sub} zero_range_bar")
            return None

        vol_ratio = float(self.config.get("open_auction_in_volume_ratio", 1.2))
        if avg_v1 <= 0 or v1 < vol_ratio * avg_v1:
            self._log_eval(f"NO_SIGNAL {sub} low_volume")
            return None

        wick_min = float(self.config.get("open_auction_in_wick_pct_min", 0.60))
        upper_wick = bar_high - max(bar_open, bar_close)
        lower_wick = min(bar_open, bar_close) - bar_low

        direction: Optional[str] = None
        if bar_high >= ib_high and (upper_wick / bar_range) >= wick_min and bar_close < ib_high:
            direction = "SHORT"
        elif bar_low <= ib_low and (lower_wick / bar_range) >= wick_min and bar_close > ib_low:
            direction = "LONG"

        if direction is None:
            self._log_eval(f"NO_SIGNAL {sub} no_ib_rejection")
            return None

        buffer_ticks = int(self.config.get("open_auction_in_stop_buffer_ticks", 8))
        buf = buffer_ticks * TICK_SIZE
        structural_stop = (ib_high + buf) if direction == "SHORT" else (ib_low - buf)

        finalized = self._finalize_stop(price, structural_stop, direction, sub)
        if finalized is None:
            return None
        stop_price, stop_ticks = finalized

        t1 = pd_poc
        time_exit = _parse_hhmm(self.config.get("open_auction_in_time_exit_ct", "12:30"))

        return self._build_signal(
            direction=direction,
            entry_price=price,
            stop_price=stop_price,
            stop_ticks=stop_ticks,
            t1=t1,
            be_milestone=None,  # Mean-revert does not pair well with BE.
            time_exit_ct=time_exit,
            sub_name=sub,
            reason=f"Open Auction In {direction} — IB {'high' if direction == 'SHORT' else 'low'} wick rejection",
            confluences=[
                f"IB {'high' if direction == 'SHORT' else 'low'} rejection wick",
                f"Wick {(upper_wick if direction == 'SHORT' else lower_wick) / bar_range:.0%} of range",
                f"1m vol {v1:.0f} > {vol_ratio:.1f}x avg",
            ],
            extra_metadata={"ib_high": ib_high, "ib_low": ib_low},
        )

    # ═══════════════════════════════════════════════════════════════
    # Sub-evaluator 4: Open Auction Out
    # ═══════════════════════════════════════════════════════════════
    def _evaluate_open_auction_out(self, market: dict) -> Signal | None:
        sub = "open_auction_out"
        rth_open = market.get("rth_open_price")
        pd_high = market.get("prior_day_high")
        pd_low = market.get("prior_day_low")
        pd_poc = market.get("prior_day_poc")
        price = market.get("price")
        v1 = market.get("rth_1min_volume")
        avg_v1 = market.get("avg_1min_volume")
        r1 = market.get("pivot_r1")
        s1 = market.get("pivot_s1")

        required = (rth_open, pd_high, pd_low, pd_poc, price, v1, avg_v1, r1, s1)
        if any(v is None for v in required):
            self._log_eval(f"SKIP {sub} missing_fields")
            return None

        # Volume confirmation (universal).
        if avg_v1 <= 0 or v1 < 1.2 * avg_v1:
            self._log_eval(f"NO_SIGNAL {sub} low_volume")
            return None

        # Gap direction established at RTH open.
        if rth_open > pd_high:
            gap = "UP"
        elif rth_open < pd_low:
            gap = "DOWN"
        else:
            self._log_eval(f"NO_SIGNAL {sub} no_gap")
            return None

        # Acceptance vs rejection at 8:45 check time.
        if gap == "UP":
            acceptance = price >= pd_high
        else:
            acceptance = price <= pd_low

        buffer_ticks = int(self.config.get("open_auction_out_stop_buffer_ticks", 8))
        buf = buffer_ticks * TICK_SIZE

        if acceptance:
            # Entry: pullback to prior-range edge, direction = with the gap.
            if gap == "UP":
                direction = "LONG"
                # Trigger: price within 4 ticks of prior_day_high (pullback zone).
                if abs(price - pd_high) > 4 * TICK_SIZE:
                    self._log_eval(f"NO_SIGNAL {sub} no_pullback_to_pdh")
                    return None
                structural_stop = pd_high - buf
                t1 = r1
            else:
                direction = "SHORT"
                if abs(price - pd_low) > 4 * TICK_SIZE:
                    self._log_eval(f"NO_SIGNAL {sub} no_pullback_to_pdl")
                    return None
                structural_stop = pd_low + buf
                t1 = s1
            scenario = "ACCEPTANCE"
        else:
            # Rejection: price back inside prior range → fade the gap.
            if gap == "UP":
                direction = "SHORT"
                structural_stop = rth_open + buf  # beyond gap extreme
            else:
                direction = "LONG"
                structural_stop = rth_open - buf
            t1 = pd_poc
            scenario = "REJECTION"

        finalized = self._finalize_stop(price, structural_stop, direction, sub)
        if finalized is None:
            return None
        stop_price, stop_ticks = finalized

        one_r = abs(price - stop_price)
        be_milestone = price + one_r if direction == "LONG" else price - one_r

        time_exit = _parse_hhmm(self.config.get("open_auction_out_time_exit_ct", "11:00"))

        return self._build_signal(
            direction=direction,
            entry_price=price,
            stop_price=stop_price,
            stop_ticks=stop_ticks,
            t1=t1,
            be_milestone=be_milestone,
            time_exit_ct=time_exit,
            sub_name=sub,
            reason=f"Open Auction Out {scenario} {direction} — gap {gap.lower()} {'held' if acceptance else 'filled'}",
            confluences=[
                f"Gap {gap.lower()} at RTH open",
                scenario,
                f"1m vol {v1:.0f} > 1.2x avg",
            ],
            extra_metadata={"scenario": scenario, "gap": gap},
        )

    # ═══════════════════════════════════════════════════════════════
    # Sub-evaluator 5: Premarket Breakout
    # ═══════════════════════════════════════════════════════════════
    def _evaluate_premarket_breakout(self, market: dict) -> Signal | None:
        sub = "premarket_breakout"
        pmh = market.get("pmh")
        pml = market.get("pml")
        price = market.get("price")
        v1 = market.get("rth_1min_volume")
        avg_v1 = market.get("avg_1min_volume")
        pivot_pp = market.get("pivot_pp")

        required = (pmh, pml, price, v1, avg_v1, pivot_pp)
        if any(v is None for v in required):
            self._log_eval(f"SKIP {sub} missing_fields")
            return None

        min_range_pts = float(self.config.get("premarket_breakout_min_range_pts", 10))
        if (pmh - pml) < min_range_pts:
            self._log_eval(f"NO_SIGNAL {sub} pm_range_too_small")
            return None

        vol_ratio = float(self.config.get("premarket_breakout_volume_ratio", 1.4))
        if avg_v1 <= 0 or v1 < vol_ratio * avg_v1:
            self._log_eval(f"NO_SIGNAL {sub} low_breakout_volume")
            return None

        buffer_ticks = int(self.config.get("premarket_breakout_buffer_ticks", 2))
        stop_buf_ticks = int(self.config.get("premarket_breakout_stop_buffer_ticks", 8))
        buf = buffer_ticks * TICK_SIZE
        stop_buf = stop_buf_ticks * TICK_SIZE

        direction: Optional[str] = None
        if price > pmh + buf:
            direction = "LONG"
            structural_stop = pmh - stop_buf
        elif price < pml - buf:
            direction = "SHORT"
            structural_stop = pml + stop_buf

        if direction is None:
            self._log_eval(f"NO_SIGNAL {sub} no_pm_break")
            return None

        finalized = self._finalize_stop(price, structural_stop, direction, sub)
        if finalized is None:
            return None
        stop_price, stop_ticks = finalized

        # BE milestone = halfway to pivot_pp.
        dist_to_pp = pivot_pp - price if direction == "LONG" else price - pivot_pp
        be_milestone = price + dist_to_pp / 2.0 if direction == "LONG" else price - dist_to_pp / 2.0

        time_exit = _parse_hhmm(self.config.get("premarket_breakout_time_exit_ct", "10:30"))

        return self._build_signal(
            direction=direction,
            entry_price=price,
            stop_price=stop_price,
            stop_ticks=stop_ticks,
            t1=pivot_pp,
            be_milestone=be_milestone,
            time_exit_ct=time_exit,
            sub_name=sub,
            reason=f"Premarket Breakout {direction} — break of {'PMH' if direction == 'LONG' else 'PML'}",
            confluences=[
                f"PM range {pmh - pml:.2f}pts >= {min_range_pts}pts",
                f"Break {'>' if direction == 'LONG' else '<'} {'PMH' if direction == 'LONG' else 'PML'}",
                f"1m vol {v1:.0f} > {vol_ratio:.1f}x avg",
            ],
            extra_metadata={"pmh": pmh, "pml": pml},
        )

    # ═══════════════════════════════════════════════════════════════
    # Sub-evaluator 6: ORB (15-min opening range breakout)
    # ═══════════════════════════════════════════════════════════════
    def _evaluate_orb(self, market: dict) -> Signal | None:
        sub = "orb"
        or_high = market.get("rth_15min_high")
        or_low = market.get("rth_15min_low")
        rth_open = market.get("rth_open_price")
        last_5m_close = market.get("rth_5min_close_last")
        price = market.get("price")

        required = (or_high, or_low, rth_open, last_5m_close, price)
        if any(v is None for v in required):
            self._log_eval(f"SKIP {sub} missing_fields")
            return None

        or_size = or_high - or_low
        if or_size <= 0:
            self._log_eval(f"NO_SIGNAL {sub} non_positive_or")
            return None

        max_pct = float(self.config.get("orb_max_range_pct", 0.008))
        if rth_open > 0 and (or_size / rth_open) > max_pct:
            self._log_eval(f"NO_SIGNAL {sub} or_too_wide")
            return None

        direction: Optional[str] = None
        if last_5m_close > or_high:
            direction = "LONG"
        elif last_5m_close < or_low:
            direction = "SHORT"
        if direction is None:
            self._log_eval(f"NO_SIGNAL {sub} no_5m_break")
            return None

        # One-trade-per-day: reject if first break was the opposite direction.
        if self._orb_first_break_direction is None:
            self._orb_first_break_direction = direction
        elif self._orb_first_break_direction != direction:
            self._log_eval(f"NO_SIGNAL {sub} opposite_of_first_break")
            return None

        # Structural stop = opposite side of OR.
        structural_stop = or_low if direction == "LONG" else or_high
        finalized = self._finalize_stop(price, structural_stop, direction, sub)
        if finalized is None:
            return None
        stop_price, stop_ticks = finalized

        target_pct = float(self.config.get("orb_target_pct_of_or", 0.50))
        be_pct = float(self.config.get("orb_be_pct_of_or", 0.25))
        t1_dist = or_size * target_pct
        be_dist = or_size * be_pct

        if direction == "LONG":
            t1 = price + t1_dist
            be_milestone = price + be_dist
        else:
            t1 = price - t1_dist
            be_milestone = price - be_dist

        time_exit = _parse_hhmm(self.config.get("orb_time_exit_ct", "14:30"))

        return self._build_signal(
            direction=direction,
            entry_price=price,
            stop_price=stop_price,
            stop_ticks=stop_ticks,
            t1=t1,
            be_milestone=be_milestone,
            time_exit_ct=time_exit,
            sub_name=sub,
            reason=f"ORB {direction} — 5-min close beyond 15-min OR",
            confluences=[
                f"OR size {or_size:.2f}pts ({or_size / rth_open:.2%} of open)",
                f"5m close {last_5m_close:.2f} {'>' if direction == 'LONG' else '<'} OR {'high' if direction == 'LONG' else 'low'}",
            ],
            extra_metadata={"or_high": or_high, "or_low": or_low, "or_size": or_size},
        )
