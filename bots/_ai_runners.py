"""AI agent runners — extracted from base_bot.py 2026-05-24 (P4-1 Stage 3).

Wraps the Phase-4 AI agent invocations. Each method is a coroutine that
the bot fires-and-forgets via asyncio.ensure_future.

Currently DISABLED via config/settings.py P0-4 flips (AGENT_COUNCIL_ENABLED,
AGENT_PRETRADE_FILTER_ENABLED, AGENT_DEBRIEF_ENABLED all False). Methods
still run but early-return. See docs/audits/SYNTHESIS_2026-05-24.md F-03
+ P0-4 for context.

Original location: bots/base_bot.py:5528 (_run_council) and 5558 (_run_debrief).
"""
from __future__ import annotations

import asyncio
import logging

from core import telegram_notifier as tg

# Phase 4: AI Agents (optional — failures never block trading). Mirror
# base_bot.py's defensive import: if the agents package fails to import,
# the runners will raise at call time, but the bot still boots. In normal
# operation base_bot.py's AGENTS_AVAILABLE gate prevents the launch sites
# from ever firing these coroutines when imports failed.
try:
    from agents import council_gate, session_debriefer
    from agents.council_gate import council_to_dict
    _AGENTS_AVAILABLE = True
except ImportError:
    council_gate = None  # type: ignore
    session_debriefer = None  # type: ignore
    council_to_dict = None  # type: ignore
    _AGENTS_AVAILABLE = False

logger = logging.getLogger("AIRunners")


class AIRunners:
    """Holds the two AI-agent coroutines that BaseBot fires-and-forgets.

    Operates on bot state but does NOT write OIF and does NOT block trades.
    The blocking-mode capability (synthesis F-03 / Agent C audit) is in
    agents/pretrade_filter.py, NOT here.
    """

    def __init__(self, bot):
        self.bot = bot

    async def run_council(self, market: dict) -> None:
        """Run council gate in background. Non-blocking — errors logged only."""
        try:
            logger.info("[COUNCIL] Running 7-voter session bias vote...")
            recent = self.bot.trade_memory.recent(10)

            # Enrich market with strategy performance for smarter voting
            market["strategy_performance"] = self.bot.tracker.get_all_summaries()

            # Fetch live market intelligence (VIX, news, economic calendar)
            try:
                from core.market_intel import get_full_intel
                intel = await get_full_intel()
                market["intel"] = intel
                self.bot._latest_intel = intel  # Phase 5: store for cockpit + TG commands
                logger.info(f"[COUNCIL] Intel loaded: VIX={intel.get('vix', 'N/A')}, "
                             f"news_tier={intel.get('highest_tier', 'N/A')}")
            except Exception as e:
                logger.warning(f"[COUNCIL] Market intel unavailable: {e}")
                market["intel"] = {}

            result = await council_gate.run_council(market, recent)
            self.bot._council_result = council_to_dict(result)
            logger.info(f"[COUNCIL] Result: {result.bias} ({result.vote_count}) "
                        f"in {result.total_latency_ms:.0f}ms")
            asyncio.ensure_future(tg.notify_council(
                result.bias, result.vote_count, result.summary))
        except Exception as e:
            logger.error(f"[COUNCIL] Failed (non-blocking): {e}")

    async def run_debrief(self) -> None:
        """Run session debrief in background. Non-blocking."""
        try:
            logger.info("[DEBRIEF] Running end-of-session coaching debrief...")
            path = await session_debriefer.run_debrief(bot_name=self.bot.bot_name)
            if path:
                logger.info(f"[DEBRIEF] Saved to {path}")
            else:
                logger.warning("[DEBRIEF] No debrief generated (no data or AI failure)")
        except Exception as e:
            logger.error(f"[DEBRIEF] Failed (non-blocking): {e}")
