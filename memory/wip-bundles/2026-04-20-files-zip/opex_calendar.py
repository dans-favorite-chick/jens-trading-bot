"""
Phoenix Bot — OpEx Calendar

Detects options expiration days and applies appropriate trading rules.

KEY DATES (U.S. equity/index options):
  - Monthly OpEx: 3rd Friday of every month
  - Triple Witching: 3rd Friday of March/June/September/December
  - Weekly OpEx: every Friday
  - 0DTE OpEx: daily for SPX/SPY (creates mini-OpEx every day)

RESEARCH BASIS (all sources consistent):
  - MarketXLS: "OPEX days routinely see 30-50% higher trading volume.
    Quarterly expirations can see volume double."
  - QuantifiedStrategies: "final 30 minutes can be especially volatile"
  - MenthorQ OpEx guide: pinning effect near concentrated strikes
  - Bookmap futures analysis: "Price stalling or snapping back near round
    numbers or expiry times... due to options gamma futures"

USAGE:
    from core.opex_calendar import OpExCalendar

    opex = OpExCalendar()

    # Check current state
    state = opex.get_current_state(now_utc=datetime.now(timezone.utc))

    if state.is_opex_day and state.time_to_close_minutes < 30:
        # Skip new entries in pin zone during final 30 min of OpEx
        return None

    size_mult = state.size_multiplier  # 0.5 on quarterly, 0.7 on monthly, 1.0 normal
"""

from dataclasses import dataclass
from datetime import datetime, date, time, timezone, timedelta
from typing import Optional
from enum import Enum


class OpExType(Enum):
    NONE = "none"                    # Not an OpEx day
    WEEKLY = "weekly"                # Regular Friday
    MONTHLY = "monthly"              # 3rd Friday (non-quarterly)
    QUARTERLY = "quarterly"          # 3rd Friday of Mar/Jun/Sep/Dec (Triple Witching)


@dataclass
class OpExState:
    is_opex_day: bool
    opex_type: OpExType
    days_to_next_monthly_opex: int
    days_since_last_monthly_opex: int
    is_opex_week: bool               # Same week as monthly OpEx
    is_post_opex_week: bool          # Week AFTER monthly OpEx
    time_to_close_minutes: int       # Minutes until 4pm ET close (0 if after close)
    in_pin_zone_window: bool         # Last hour of OpEx day
    in_final_30min: bool             # Last 30 min of OpEx day (no new entries)

    # Derived trading rules
    size_multiplier: float           # Position size multiplier (1.0 = normal)
    allow_breakout_trades: bool      # False during pin zones
    allow_mean_reversion: bool       # Usually True on OpEx
    allow_new_entries: bool          # False in final 30 min of OpEx
    target_rr_multiplier: float      # 1.0 normal, 0.7 tight during pin, 1.3 post-OpEx
    reason: str


class OpExCalendar:
    """
    Computes OpEx state and trading rules for a given moment.

    All internal date math uses U.S. Eastern Time because OpEx is a
    U.S. market concept. Input `now_utc` is converted accordingly.
    """

    MARKET_CLOSE_ET = time(16, 0, 0)        # 4:00 PM ET
    PIN_ZONE_MINUTES_BEFORE_CLOSE = 60      # Pin zone = last 60 min
    NO_ENTRY_MINUTES_BEFORE_CLOSE = 30      # No new entries in last 30 min of OpEx

    # ET is UTC-5 (EST) or UTC-4 (EDT). We use a simple fixed offset of -5 for
    # the date-level math (good enough for "what day is it?" — doesn't matter
    # if we're off by an hour near midnight). For the intraday time-to-close
    # calculation we use a more careful DST-aware conversion.
    ET_FIXED_OFFSET_HOURS = -5

    def get_current_state(self, now_utc: datetime) -> OpExState:
        """Compute the full OpEx state for the given UTC timestamp."""

        et_now = self._utc_to_et(now_utc)
        today_et = et_now.date()

        # Classify today
        opex_type = self._classify_opex_day(today_et)
        is_opex_day = opex_type != OpExType.NONE

        # Calendar math
        next_monthly = self._next_monthly_opex(today_et)
        last_monthly = self._last_monthly_opex(today_et)
        days_to_next = (next_monthly - today_et).days
        days_since_last = (today_et - last_monthly).days

        # "OpEx week" = same week as monthly OpEx (Mon-Fri containing 3rd Fri)
        is_opex_week = 0 <= days_to_next <= 5 and self._same_week(today_et, next_monthly)
        is_post_opex_week = 0 < days_since_last <= 5 and self._same_week(today_et, last_monthly + timedelta(days=3))

        # Intraday timing
        mins_to_close = self._minutes_to_market_close(et_now)
        in_pin_zone = is_opex_day and 0 < mins_to_close <= self.PIN_ZONE_MINUTES_BEFORE_CLOSE
        in_final_30 = is_opex_day and 0 < mins_to_close <= self.NO_ENTRY_MINUTES_BEFORE_CLOSE

        # Apply trading rules
        return self._apply_rules(
            is_opex_day=is_opex_day,
            opex_type=opex_type,
            days_to_next=days_to_next,
            days_since_last=days_since_last,
            is_opex_week=is_opex_week,
            is_post_opex_week=is_post_opex_week,
            mins_to_close=mins_to_close,
            in_pin_zone=in_pin_zone,
            in_final_30=in_final_30,
        )

    # ─── DATE CLASSIFICATION ───────────────────────────────────────────

    def _classify_opex_day(self, d: date) -> OpExType:
        """Is this date a Weekly/Monthly/Quarterly OpEx Friday?"""
        # OpEx only on Fridays (weekday 4)
        if d.weekday() != 4:
            return OpExType.NONE

        # Check if this is the 3rd Friday of the month
        if self._is_third_friday(d):
            # Triple witching months
            if d.month in (3, 6, 9, 12):
                return OpExType.QUARTERLY
            return OpExType.MONTHLY

        return OpExType.WEEKLY

    def _is_third_friday(self, d: date) -> bool:
        """True if date is the 3rd Friday of its month."""
        if d.weekday() != 4:
            return False
        # 3rd Friday is between the 15th and 21st inclusive
        return 15 <= d.day <= 21

    def _next_monthly_opex(self, from_date: date) -> date:
        """Return the next monthly (3rd Friday) OpEx on/after from_date."""
        d = from_date
        # Check current month first
        for candidate_month_offset in range(3):
            year = d.year
            month = d.month + candidate_month_offset
            while month > 12:
                month -= 12
                year += 1
            third_fri = self._third_friday_of(year, month)
            if third_fri >= from_date:
                return third_fri
        # Fallback (shouldn't reach here in practice)
        return self._third_friday_of(d.year + 1, d.month)

    def _last_monthly_opex(self, from_date: date) -> date:
        """Return the most recent monthly OpEx before or equal to from_date."""
        # Check current month first
        third_fri = self._third_friday_of(from_date.year, from_date.month)
        if third_fri <= from_date:
            return third_fri
        # Otherwise it was last month
        if from_date.month == 1:
            return self._third_friday_of(from_date.year - 1, 12)
        return self._third_friday_of(from_date.year, from_date.month - 1)

    def _third_friday_of(self, year: int, month: int) -> date:
        """Compute the 3rd Friday of (year, month)."""
        d = date(year, month, 1)
        # Days until first Friday
        offset_to_first_fri = (4 - d.weekday()) % 7
        first_friday = d + timedelta(days=offset_to_first_fri)
        return first_friday + timedelta(days=14)

    def _same_week(self, a: date, b: date) -> bool:
        """True if a and b are in the same ISO week."""
        iso_a = a.isocalendar()
        iso_b = b.isocalendar()
        return iso_a[0] == iso_b[0] and iso_a[1] == iso_b[1]

    # ─── TIME OF DAY ───────────────────────────────────────────────────

    def _utc_to_et(self, now_utc: datetime) -> datetime:
        """Convert UTC to U.S. Eastern (DST-aware)."""
        # Use zoneinfo if available (Python 3.9+), fall back to fixed offset
        try:
            from zoneinfo import ZoneInfo
            return now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            return now_utc + timedelta(hours=self.ET_FIXED_OFFSET_HOURS)

    def _minutes_to_market_close(self, et_now: datetime) -> int:
        """Minutes from et_now until 4pm ET market close. 0 if after close."""
        close_dt = datetime.combine(
            et_now.date(),
            self.MARKET_CLOSE_ET,
            tzinfo=et_now.tzinfo,
        )
        delta = close_dt - et_now
        return max(0, int(delta.total_seconds() / 60))

    # ─── TRADING RULES APPLICATION ─────────────────────────────────────

    def _apply_rules(
        self,
        is_opex_day: bool,
        opex_type: OpExType,
        days_to_next: int,
        days_since_last: int,
        is_opex_week: bool,
        is_post_opex_week: bool,
        mins_to_close: int,
        in_pin_zone: bool,
        in_final_30: bool,
    ) -> OpExState:
        """Apply trading rules based on OpEx state."""

        size_mult = 1.0
        target_rr_mult = 1.0
        allow_breakout = True
        allow_mean_rev = True
        allow_entries = True
        reasons = []

        # RULE 1: Quarterly (Triple Witching) day
        if opex_type == OpExType.QUARTERLY:
            size_mult *= 0.5
            allow_breakout = False
            target_rr_mult = 0.8
            reasons.append("Triple Witching: -50% size, no breakouts")

        # RULE 2: Monthly OpEx day (non-quarterly)
        elif opex_type == OpExType.MONTHLY:
            size_mult *= 0.7
            allow_breakout = False
            target_rr_mult = 0.8
            reasons.append("Monthly OpEx: -30% size, no breakouts")

        # RULE 3: Weekly OpEx (regular Friday)
        elif opex_type == OpExType.WEEKLY:
            # Less restrictive but still elevated
            size_mult *= 0.85
            reasons.append("Weekly Friday OpEx: -15% size")

        # RULE 4: Pin zone (last hour of OpEx)
        if in_pin_zone:
            allow_breakout = False
            target_rr_mult *= 0.7
            reasons.append(f"Pin zone ({mins_to_close} min to close): no breakouts, tight RR")

        # RULE 5: Final 30 min of OpEx — no new entries
        if in_final_30:
            allow_entries = False
            reasons.append(f"OpEx final 30 min: no new entries")

        # RULE 6: OpEx week (day before or day of monthly/quarterly)
        if is_opex_week and not is_opex_day:
            # Dealers pinning price reduces trend quality
            target_rr_mult *= 0.85
            reasons.append("OpEx week: reduced RR targets (trends suppressed)")

        # RULE 7: Post-OpEx week (week after) — gamma unclenching
        if is_post_opex_week:
            # Vol expansion expected; larger targets justified
            target_rr_mult *= 1.15
            reasons.append("Post-OpEx week: larger RR targets (vol expansion)")

        reason_str = " | ".join(reasons) if reasons else "Normal trading day"

        return OpExState(
            is_opex_day=is_opex_day,
            opex_type=opex_type,
            days_to_next_monthly_opex=days_to_next,
            days_since_last_monthly_opex=days_since_last,
            is_opex_week=is_opex_week,
            is_post_opex_week=is_post_opex_week,
            time_to_close_minutes=mins_to_close,
            in_pin_zone_window=in_pin_zone,
            in_final_30min=in_final_30,
            size_multiplier=size_mult,
            allow_breakout_trades=allow_breakout,
            allow_mean_reversion=allow_mean_rev,
            allow_new_entries=allow_entries,
            target_rr_multiplier=target_rr_mult,
            reason=reason_str,
        )

    # ─── DASHBOARD SUPPORT ─────────────────────────────────────────────

    def snapshot(self, now_utc: datetime) -> dict:
        """Return state as dict for dashboard."""
        s = self.get_current_state(now_utc)
        return {
            "is_opex_day": s.is_opex_day,
            "opex_type": s.opex_type.value,
            "days_to_next_opex": s.days_to_next_monthly_opex,
            "is_opex_week": s.is_opex_week,
            "is_post_opex_week": s.is_post_opex_week,
            "time_to_close_min": s.time_to_close_minutes,
            "in_pin_zone": s.in_pin_zone_window,
            "allow_new_entries": s.allow_new_entries,
            "size_multiplier": round(s.size_multiplier, 2),
            "target_rr_multiplier": round(s.target_rr_multiplier, 2),
            "reason": s.reason,
        }
