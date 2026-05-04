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

    # B57 2026-04-22: ProdBot routes EVERY signal to Sim101 (single-account
    # mode). This is the first go-live candidate bot — keeping it on one
    # account makes P&L tracking clean and prevents collisions with
    # sim_bot's 16 per-strategy accounts. To change, set FORCE_ACCOUNT=None
    # which falls back to config/account_routing.py map resolution.
    #
    # NOTE: With Sprint H's expanded strategy roster, FORCE_ACCOUNT="Sim101"
    # creates a single-account bottleneck — only ONE position can be open
    # at a time on prod. This is intentional (mirrors live-money model).
    # Sim_bot has per-strategy accounts so all 10 can hold concurrent
    # positions simultaneously.
    FORCE_ACCOUNT = "Sim101"


def main():
    bot = ProdBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Production bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
