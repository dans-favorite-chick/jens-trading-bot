"""
Phoenix Bot — Production Bot

Runs validated strategies only during defined trading windows.
Tight risk limits, no experiments.
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

    # Sprint H (2026-05-04): only_validated set UNCONDITIONALLY to False
    # at operator request. Pre-Sprint-H, prod loaded only validated=True
    # strategies — currently 2 (bias_momentum, ib_breakout) — making prod
    # nearly silent. Operator wants full strategy roster on prod for
    # debug visibility before going live.
    #
    # ⚠️  LIVE-MODE SAFETY IMPLICATION (operator-acknowledged):
    # When LIVE_TRADING=True is flipped, prod will fire ALL 10 enabled
    # strategies on real money — including 7 strategies that have NOT
    # passed the project discipline (50+ trades / PF>1.3 in lab). Some
    # are KILL_CANDIDATEs per Sprint C/F validation_tracker findings.
    #
    # BEFORE going live, operator should EITHER:
    #   (a) audit each strategy's lab data and either disable
    #       (`enabled=False` in config/strategies.py) or accept the risk
    #   (b) restore the validated gate by changing this back to
    #       `only_validated = True` (or use the conditional pattern:
    #       `@property def only_validated(self): return LIVE_TRADING`)
    #
    # The safety gate is now off by operator decision (2026-05-04).
    only_validated = False

    # Sprint I (2026-05-03): FORCE_ACCOUNT removed at operator request.
    # Pre-Sprint-I (B57 2026-04-22), prod pinned EVERY signal to Sim101
    # for clean single-account P&L. With Sprint H expanding the strategy
    # roster from 2 → 10, that pin became a single-account bottleneck:
    # only ONE position could be open across the whole prod bot at a
    # time, while sim_bot ran all 10 concurrently on per-strategy
    # accounts.
    #
    # FORCE_ACCOUNT=None → routing falls through to
    # config/account_routing.py:get_account_for_signal(), which uses
    # STRATEGY_ACCOUNT_MAP (per-strategy NT8 account). Prod now mirrors
    # sim_bot's per-strategy account topology: each strategy fires on
    # its own dedicated Sim* account, so concurrent positions across
    # strategies are possible.
    #
    # ⚠️  LIVE-MODE SAFETY IMPLICATION (operator-acknowledged):
    # When LIVE_TRADING=True is flipped, EVERY strategy in
    # STRATEGY_ACCOUNT_MAP routes to its mapped account. Operator must
    # audit STRATEGY_ACCOUNT_MAP before go-live and either:
    #   (a) confirm each mapped account is the intended live-money
    #       destination
    #   (b) restore the single-account pin by setting FORCE_ACCOUNT to
    #       the desired live-money account name (e.g. the real funded
    #       account)
    FORCE_ACCOUNT = None


def main():
    bot = ProdBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Production bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
