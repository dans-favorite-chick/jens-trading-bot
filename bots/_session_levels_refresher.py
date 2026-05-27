"""Session levels refresher — extracted from base_bot.py 2026-05-24 (P4-1 Stage 2).

Daily refresh of session-level pivot data (prior-day H/L, R1/S1, POC, PP) at
00:01 CT so a long-running bot picks up the newly written JSONL history
without a restart. Read-only with respect to position/risk state — only
calls `aggregator.session_levels.load_prior_day()` and logs the result.

Errors are logged and the loop retries in 1 hour rather than tight-looping
so one bad day doesn't wedge the task.

Original location: bots/base_bot.py:1845-1878 as
BaseBot._session_levels_refresh_task.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("SessionLevels")


class SessionLevelsRefresher:
    """Encapsulates the Phase 4B session-levels refresh loop. Owns no state —
    only reads `bot.aggregator.session_levels`. Safe to extract because:
    observational, no OIF writes, no risk-gate calls, no position mutation.
    """

    def __init__(self, bot):
        # Hold a reference to BaseBot for state reads. (Future: pass only
        # the aggregator; for now keep coupling tight = diff small.)
        self.bot = bot

    async def run(self) -> None:
        """Phase 4B: recompute prior-day OHLC + volume profile + pivots at
        00:01 CT each day so a long-running bot picks up the newly
        written JSONL history without a restart. Errors are logged and
        the loop retries in 1 hour so one bad day doesn't wedge the task.
        """
        from datetime import datetime as _dt, timedelta as _td
        while True:
            try:
                now = _dt.now()
                next_refresh = now.replace(hour=0, minute=1, second=0, microsecond=0)
                if next_refresh <= now:
                    next_refresh += _td(days=1)
                sleep_secs = (next_refresh - now).total_seconds()
                logger.info(
                    f"[SESSION_LEVELS] next refresh in {sleep_secs / 3600:.1f}h"
                )
                await asyncio.sleep(sleep_secs)
                sl = getattr(self.bot.aggregator, "session_levels", None)
                if sl is not None:
                    sl.load_prior_day()
                    logger.info(
                        f"[SESSION_LEVELS] refreshed prior-day "
                        f"H={sl.prior_day_high} L={sl.prior_day_low} "
                        f"POC={sl.prior_day_poc} PP={sl.pivot_pp}"
                    )
                else:
                    logger.warning(
                        "[SESSION_LEVELS] refresh skipped - aggregator has no session_levels"
                    )
            except Exception as e:
                logger.error(f"[SESSION_LEVELS] refresh task error: {e!r}")
                await asyncio.sleep(3600)  # retry in 1h rather than tight-loop
