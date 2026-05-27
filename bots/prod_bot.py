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

    # 2026-05-24 (operator directive): only_validated is now live-conditional.
    # In live mode (LIVE_TRADING=True) the bot MUST only load validated
    # strategies — that's table stakes for any real-money entry. In sim/paper
    # mode (LIVE_TRADING=False) prod stays open so the paper-trading harness
    # can observe every enabled strategy's behavior on Sim101.
    #
    # This is layer-1 of the live canary; layer-2 is core/live_canary_gate.py
    # which additionally filters by LIVE_STRATEGY_ALLOWLIST. Both fire in
    # live mode; only this one matters when LIVE_TRADING=False.
    @property
    def only_validated(self) -> bool:
        from config.settings import LIVE_TRADING
        return bool(LIVE_TRADING)

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

    # 2026-05-24 P0-2 (synthesis F-08, F-09): sim-overrides opt-in channel.
    # Applies config/sim_overrides.py iff PHOENIX_SIM_OVERRIDES=1. Refuses
    # to start if PHOENIX_SIM_OVERRIDES=1 + LIVE_TRADING=True. Critical-level
    # log line names every applied override.
    from core.sim_overrides_loader import load_and_apply_sim_overrides
    load_and_apply_sim_overrides()

    # 2026-05-24 LIVE CANARY: validate live-mode constraints BEFORE bot
    # instantiation. No-op in sim mode. Raises LiveCanaryViolation and
    # refuses to start if LIVE_TRADING=True and any constraint fails.
    from core.live_canary_gate import validate_live_config
    validate_live_config()

    bot = ProdBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Production bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
