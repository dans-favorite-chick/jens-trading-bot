"""News scanner — extracted from base_bot.py 2026-05-24 (P4-1 Stage 1).

Periodic poll of news/macro feeds. Read-only with respect to bot state; may
update calendar_risk, cot_feed, and intermarket fields used for pre-trade gates.

Original location: bots/base_bot.py:5701-5758 as BaseBot._news_scanner_loop.

Behavior is identical to the original method. Every `self.X` access against
BaseBot fields has been rewritten as `self.bot.X`. Instance-local caches
(`_news_scanner`, `_latest_news_alerts`) live on the BaseBot instance so that
other methods (e.g. dashboard publishers) that read them keep working.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("NewsScanner")


class NewsScanner:
    """Background news/macro feed scanner.

    Polls every 2 minutes:
      1. core.news_scanner.NewsScanner for headline alerts.
      2. CalendarRiskManager.refresh_calendar() for macro events.
      3. COTFeed.refresh() for institutional positioning (daily).
      4. core.market_intel.get_full_intel() to feed VIX/DXY into intermarket.
    """

    def __init__(self, bot):
        self.bot = bot

    async def run(self) -> None:
        """Poll for news alerts + external data every 2 minutes. Non-blocking."""
        while True:
            try:
                from core.news_scanner import NewsScanner
                if not hasattr(self.bot, '_news_scanner'):
                    self.bot._news_scanner = NewsScanner()
                alerts = await self.bot._news_scanner.scan()
                if alerts:
                    for alert in alerts[:3]:  # Top 3 alerts
                        logger.info(f"[NEWS] {alert.get('type', '?')}: {alert.get('summary', '')[:80]}")
                    self.bot._latest_news_alerts = alerts
            except ImportError:
                pass  # Module not yet available
            except Exception as e:
                logger.debug(f"[NEWS] Scanner error: {e}")

            # Phase 8: Refresh calendar risk events
            try:
                await self.bot.calendar_risk.refresh_calendar()
            except Exception as e:
                logger.debug(f"[CALENDAR] Refresh error: {e}")

            # Phase 8: Refresh COT institutional positioning (daily)
            try:
                await self.bot.cot_feed.refresh()
            except Exception as e:
                logger.debug(f"[COT] Refresh error: {e}")

            # Phase 8: Feed intermarket engine with external data
            # B51: get_vix() and get_intermarket() return DICTS, not floats.
            # Unwrap .get("vix"/"vix_proxy"/"dxy") before float() conversion.
            try:
                from core.market_intel import get_full_intel
                intel = await get_full_intel()
                if intel:
                    im_data = {}
                    vix_raw = intel.get("vix")
                    if isinstance(vix_raw, dict):
                        v = vix_raw.get("vix") or vix_raw.get("vix_proxy")
                        if v:
                            im_data["VIX"] = float(v)
                    elif vix_raw:
                        im_data["VIX"] = float(vix_raw)
                    dxy_raw = intel.get("dxy")
                    if isinstance(dxy_raw, dict):
                        d = dxy_raw.get("dxy") or dxy_raw.get("value")
                        if d:
                            im_data["DXY"] = float(d)
                    elif dxy_raw:
                        im_data["DXY"] = float(dxy_raw)
                    if im_data:
                        self.bot.intermarket.update_from_external(im_data)
                        logger.debug(f"[INTERMARKET] Updated: {im_data}")
            except Exception as e:
                logger.debug(f"[INTERMARKET] Feed error: {e}")

            await asyncio.sleep(120)  # Every 2 minutes
