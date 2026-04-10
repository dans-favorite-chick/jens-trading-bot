"""
Phoenix Bot — Lab (Experimental) Bot

Runs all strategies including unvalidated ones. 24/7 capable.
Separate P&L tracking, does NOT affect prod daily limits.
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
logger = logging.getLogger("LabBot")


class LabBot(BaseBot):
    bot_name = "lab"
    only_validated = False  # Runs ALL enabled strategies, including unvalidated


def main():
    bot = LabBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Lab bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
