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
    only_validated = True  # Only runs validated strategies
    # B57 2026-04-22: ProdBot routes EVERY signal to Sim101 (single-account
    # mode). This is the first go-live candidate bot — keeping it on one
    # account makes P&L tracking clean and prevents collisions with
    # sim_bot's 16 per-strategy accounts. To change, set FORCE_ACCOUNT=None
    # which falls back to config/account_routing.py map resolution.
    FORCE_ACCOUNT = "Sim101"


def main():
    bot = ProdBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Production bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
