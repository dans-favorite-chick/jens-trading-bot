"""
Phoenix Bot — Contract Rollover Helper

Detects when front-month contract is near expiration and returns the
appropriate active symbol. Prevents trading a dead/illiquid contract.

Called at bot startup (base_bot.py) and once per day at 07:00 CDT.

Rollover rule (CME quarterly cycle):
  H/M/U/Z = March/June/September/December, 3rd Friday expiration.
  Front-month typically rolls ~8 trading days before expiration.
"""

import logging
from datetime import date, datetime, timedelta

from config.settings import (
    INSTRUMENT,
    CONTRACT_EXPIRATION,
    NEXT_CONTRACT,
    NEXT_CONTRACT_EXPIRATION,
    ROLL_DAYS_BEFORE_EXPIRATION,
)

logger = logging.getLogger("Rollover")


def _trading_days_until(target: date, today: date = None) -> int:
    """Approx trading days between today and target (skips weekends)."""
    if today is None:
        today = date.today()
    if target <= today:
        return 0
    days = 0
    cur = today
    while cur < target:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon-Fri
            days += 1
    return days


def get_active_contract(today: date = None) -> dict:
    """
    Return dict with active contract info + rollover status.

    Returns:
      {
        "symbol": "MNQM6 06-26",
        "expiration": date(2026, 6, 19),
        "days_to_expiration": 63,
        "should_roll": False,
        "roll_target": "MNQU6 09-26",
        "warning": None  # or human-readable warning string
      }
    """
    if today is None:
        today = date.today()

    current_exp = datetime.strptime(CONTRACT_EXPIRATION, "%Y-%m-%d").date()
    next_exp = datetime.strptime(NEXT_CONTRACT_EXPIRATION, "%Y-%m-%d").date()

    days_current = _trading_days_until(current_exp, today)
    days_next = _trading_days_until(next_exp, today)

    # Decide which contract is active
    if days_current <= 0:
        # Front month expired → use next contract
        active = {
            "symbol": NEXT_CONTRACT,
            "expiration": next_exp,
            "days_to_expiration": days_next,
            "should_roll": False,
            "roll_target": None,
            "warning": f"Front-month {INSTRUMENT} expired {today - current_exp} days ago. Using {NEXT_CONTRACT}.",
        }
    elif days_current <= ROLL_DAYS_BEFORE_EXPIRATION:
        # Near expiration — roll now
        active = {
            "symbol": NEXT_CONTRACT,
            "expiration": next_exp,
            "days_to_expiration": days_next,
            "should_roll": True,
            "roll_target": NEXT_CONTRACT,
            "warning": f"ROLLOVER: {INSTRUMENT} expires in {days_current} trading days. Switching to {NEXT_CONTRACT}.",
        }
    else:
        # Normal front-month trading
        active = {
            "symbol": INSTRUMENT,
            "expiration": current_exp,
            "days_to_expiration": days_current,
            "should_roll": False,
            "roll_target": NEXT_CONTRACT,
            "warning": None,
        }

    return active


def log_rollover_status(today: date = None) -> None:
    """Log rollover status at bot startup."""
    info = get_active_contract(today)
    if info["warning"]:
        logger.warning(f"[ROLLOVER] {info['warning']}")
    else:
        logger.info(
            f"[ROLLOVER] Active: {info['symbol']} "
            f"(expires {info['expiration']}, {info['days_to_expiration']} trading days)"
        )


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    info = get_active_contract()
    print(f"Active contract: {info['symbol']}")
    print(f"Expiration: {info['expiration']} ({info['days_to_expiration']} trading days)")
    print(f"Should roll: {info['should_roll']}")
    if info["warning"]:
        print(f"WARNING: {info['warning']}")
