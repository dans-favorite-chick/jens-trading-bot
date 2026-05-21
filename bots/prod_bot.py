"""
Phoenix Bot — Paper-Trading Tester (named "prod" historically)

Despite the "prod" name, this bot is the paper-trading tester:
single-account harness pinned to Sim101 for simple P&L tracking and
strategy-behavior verification. The actual multi-strategy validation
work runs in sim_bot, which routes per-strategy across the dedicated
Sim* accounts in config/account_routing.py:STRATEGY_ACCOUNT_MAP.

Operational role
----------------
- prod_bot  → Sim101 paper account; tester / smoke-test harness
- sim_bot   → multi-account lab; the real strategy validation work

The historical "production" docstring (validated only / tight limits)
no longer matches the operator's current usage — Sprint H opened it
up to all strategies + all hours for debug visibility. It remains a
single-account tester regardless.
"""

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bots.base_bot import BaseBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("ProdBot")


class ProdBot(BaseBot):
    bot_name = "prod"

    # Sprint H (2026-05-04): only_validated=False — prod loads every
    # enabled strategy regardless of `validated` flag. Pre-Sprint-H,
    # prod ran only validated=True strategies (2 of 10), making the
    # paper-trading harness nearly silent. Operator opened it up so
    # all 10 strategies emit signals through the Sim101 tester for
    # behavioral debug visibility.
    #
    # If/when prod is ever pointed at a real-money account (e.g. via a
    # different FORCE_ACCOUNT below), revisit this gate first — either
    # set `only_validated = True` or use a conditional pattern like
    # `@property def only_validated(self): return LIVE_TRADING`.
    only_validated = False

    # B57 (2026-04-22) — restored 2026-05-03 after operator clarified:
    # prod_bot IS the paper-trading tester. Every signal is pinned to
    # the Sim101 paper account so P&L attribution stays trivial and
    # there's no risk of a stray test signal landing on a real account.
    #
    # The multi-account per-strategy routing (one Sim* account per
    # strategy via config/account_routing.py:STRATEGY_ACCOUNT_MAP) is
    # the JOB of sim_bot, not prod_bot. Prod stays single-account on
    # purpose — concurrent positions across strategies happen on sim.
    #
    # Trade-off acknowledged: only ONE position at a time across all
    # 10 strategies on prod. That's expected; prod is for verifying
    # individual signals fire correctly, not for measuring full-roster
    # throughput.
    #
    # If prod is ever repurposed for a different account, change this
    # value (do NOT set to None unless the operator wants per-strategy
    # routing fall-through).
    FORCE_ACCOUNT = "Sim101"


def main():
    # 2026-05-20 PHASE 13 SHIP AUDIT pt2 (F-009): single-instance guard.
    # See bots/sim_bot.py for full context. Same protection applied here.
    from core.single_instance import acquire_or_exit
    acquire_or_exit("prod")

    bot = ProdBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Production bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
