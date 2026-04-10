"""
Phoenix Bot — Lab (Experimental) Bot

Runs all strategies including unvalidated ones. 24/7 capable.
Separate P&L tracking, does NOT affect prod daily limits.
Lab runs with AGGRESSIVE defaults — lower thresholds to generate
trades and collect data for learning.
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

    def load_strategies(self):
        """Load all strategies with aggressive lab overrides."""
        super().load_strategies()

        # Override strategy configs for more aggressive trading
        lab_overrides = {
            "bias_momentum": {
                "min_confluence": 1.5,   # Way lower than prod (3.0)
                "min_tf_votes": 2,       # 2 of 4 TFs instead of 3
                "min_momentum": 30,      # Lower momentum bar (vs 55)
                "stop_ticks": 10,
                "target_rr": 1.5,
            },
            "spring_setup": {
                "min_wick_ticks": 4,     # Smaller wicks (vs 6)
                "require_vwap_reclaim": False,  # Don't require VWAP
                "require_delta_flip": False,    # Don't require delta
                "stop_multiplier": 1.5,
                "target_rr": 1.5,
            },
            "vwap_pullback": {
                "min_confluence": 2.0,
                "min_tf_votes": 2,
                "stop_ticks": 8,
                "target_rr": 1.5,
            },
            "high_precision_only": {
                "min_confluence": 2.5,
                "min_tf_votes": 2,
                "min_precision": 35,
                "stop_ticks": 8,
                "target_rr": 1.5,
            },
        }

        for strat in self.strategies:
            if strat.name in lab_overrides:
                for k, v in lab_overrides[strat.name].items():
                    strat.config[k] = v
                logger.info(f"Lab override applied to {strat.name}")

        # Set aggressive runtime params
        self._runtime_params.update({
            "min_confluence": 1.5,
            "min_momentum_confidence": 30,
            "risk_per_trade": 10.0,
            "max_daily_loss": 50.0,
        })

        logger.info(f"Lab bot: {len(self.strategies)} strategies loaded with aggressive settings")


def main():
    bot = LabBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Lab bot stopped (Ctrl+C)")


if __name__ == "__main__":
    main()
