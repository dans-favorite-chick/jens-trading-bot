"""P0-3 regression guard: WEEKLY_LOSS_LIMIT must dominate DAILY_LOSS_LIMIT.

Synthesis F-02 (docs/audits/SYNTHESIS_2026-05-24.md): the original config had
WEEKLY_LOSS_LIMIT=$150 < DAILY_LOSS_LIMIT=$200, meaning a single $150 day
closed the bot for the rest of the week. This test refuses to let that
regress, regardless of how the values evolve.
"""
from __future__ import annotations

from config import settings


def test_weekly_dominates_daily_with_3x_margin() -> None:
    """Weekly cap must be >= 3x daily cap.

    The 3x ratio is the standard belt-and-suspenders: 5 trading days,
    headroom for back-to-back bad days, prevents a single day from
    closing the week.
    """
    assert settings.WEEKLY_LOSS_LIMIT >= settings.DAILY_LOSS_LIMIT * 3, (
        f"WEEKLY_LOSS_LIMIT (${settings.WEEKLY_LOSS_LIMIT}) must be at least "
        f"3x DAILY_LOSS_LIMIT (${settings.DAILY_LOSS_LIMIT}) — currently "
        f"{settings.WEEKLY_LOSS_LIMIT / settings.DAILY_LOSS_LIMIT:.2f}x. "
        "This was the F-02 bug — see docs/audits/SYNTHESIS_2026-05-24.md."
    )


def test_weekly_is_positive() -> None:
    assert settings.WEEKLY_LOSS_LIMIT > 0, "WEEKLY_LOSS_LIMIT must be positive"


def test_daily_is_positive() -> None:
    assert settings.DAILY_LOSS_LIMIT > 0, "DAILY_LOSS_LIMIT must be positive"


def test_per_trade_dollar_cap_sane() -> None:
    """Per-trade $-cap must be a fraction of the daily cap.

    Sanity: a single trade cannot blow the entire daily budget.
    """
    assert settings.MAX_ACTUAL_STOP_DOLLARS_PER_TRADE <= settings.DAILY_LOSS_LIMIT / 2, (
        f"MAX_ACTUAL_STOP_DOLLARS_PER_TRADE (${settings.MAX_ACTUAL_STOP_DOLLARS_PER_TRADE}) "
        f"must be <= half DAILY_LOSS_LIMIT (${settings.DAILY_LOSS_LIMIT}/2). "
        "A single trade cannot consume the entire daily budget."
    )
