"""
Phoenix Bot — Options Expiration (OpEx) Calendar Handler

Detects 3rd Friday of each month (monthly OpEx) and applies special
afternoon handling: size reduction, higher conviction threshold, veto
continuation breakouts in the last hour (pinning dominates).

Research basis (2026):
- Monthly OpEx (3rd Fri): quarterly expiration if it's also quarter-end (Mar/Jun/Sep/Dec)
  = "Triple Witching" — highest gamma unwinding
- Intraday pattern: drift toward max pain strike until settlement
- Afternoon pinning dominates; continuation breakouts fail more often
- Morning often more directional as delta hedges roll
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional


# Quarterly months (Triple Witching)
QUARTERLY_MONTHS = {3, 6, 9, 12}


@dataclass
class OpExStatus:
    is_opex_day: bool
    is_triple_witching: bool
    in_afternoon_window: bool      # 14:00-15:00 CDT
    in_last_hour_window: bool       # 14:30-15:00 CDT (power hour / close)
    size_reduction_factor: float    # 1.0 = full size, 0.7 = 30% reduction
    conviction_threshold_bonus: int # Extra points required on composite
    veto_continuation_patterns: bool
    reasoning: list[str]


def third_friday_of_month(year: int, month: int) -> date:
    """Compute 3rd Friday of given month."""
    # First day of month
    cal = calendar.monthcalendar(year, month)
    # Fridays in each week (index 4 = Friday, 0 = Monday)
    fridays = [week[4] for week in cal if week[4] != 0]
    if len(fridays) < 3:
        # Shouldn't happen for any real month but guard it
        raise ValueError(f"Month {year}-{month:02d} has fewer than 3 Fridays (?)")
    return date(year, month, fridays[2])


def is_opex_day(today: date) -> bool:
    """True if today is the 3rd Friday of its month."""
    third_fri = third_friday_of_month(today.year, today.month)
    return today == third_fri


def is_triple_witching(today: date) -> bool:
    """True if today is 3rd Friday of a quarterly month (Mar/Jun/Sep/Dec)."""
    return is_opex_day(today) and today.month in QUARTERLY_MONTHS


def get_opex_status(now: datetime = None) -> OpExStatus:
    """Return full OpEx status for the given datetime."""
    if now is None:
        now = datetime.now()
    today = now.date()
    t = now.time()

    is_opex = is_opex_day(today)
    is_triple = is_triple_witching(today)
    in_afternoon = is_opex and time(14, 0) <= t < time(15, 0)
    in_last_hour = is_opex and time(14, 30) <= t < time(15, 0)

    if not is_opex:
        return OpExStatus(
            is_opex_day=False, is_triple_witching=False,
            in_afternoon_window=False, in_last_hour_window=False,
            size_reduction_factor=1.0,
            conviction_threshold_bonus=0,
            veto_continuation_patterns=False,
            reasoning=["not an OpEx day"],
        )

    reasoning = []
    if is_triple:
        reasoning.append(f"TRIPLE WITCHING ({today}) — max gamma unwinding")
    else:
        reasoning.append(f"Monthly OpEx ({today})")

    size_mult = 1.0
    threshold_bonus = 0
    veto_continuation = False

    if in_last_hour:
        # Most restrictive: last hour, pinning dominant
        size_mult = 0.5 if is_triple else 0.6
        threshold_bonus = 15
        veto_continuation = True
        reasoning.append("last hour OpEx — pinning dominant, continuation vetoed")
    elif in_afternoon:
        # Afternoon: reduce but don't veto
        size_mult = 0.7
        threshold_bonus = 10
        reasoning.append("OpEx afternoon — size reduced 30%")

    if is_triple and not in_last_hour and not in_afternoon:
        # Morning Triple Witching — still elevated volatility, slight caution
        size_mult = 0.9
        threshold_bonus = 5
        reasoning.append("Triple Witching morning — mild caution")

    return OpExStatus(
        is_opex_day=True, is_triple_witching=is_triple,
        in_afternoon_window=in_afternoon, in_last_hour_window=in_last_hour,
        size_reduction_factor=size_mult,
        conviction_threshold_bonus=threshold_bonus,
        veto_continuation_patterns=veto_continuation,
        reasoning=reasoning,
    )
