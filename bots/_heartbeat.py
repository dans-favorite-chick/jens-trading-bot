"""Heartbeat sender — extracted from base_bot.py 2026-05-24 (P4-1 Stage 1).

Periodic outbound keep-alive. Sends a 'heartbeat' frame to the bridge
every 10s so the bridge can detect hung bots. Reads bot state only —
does not mutate any BaseBot fields.

Original location: bots/base_bot.py:2039-2052 as BaseBot._heartbeat_loop.

BaseBot fields read:
    - self.bot._ws          (websockets connection or None)
    - self.bot.bot_name     (str)
    - self.bot.status       (str)

BaseBot fields written: none.

Behaviorally identical to the original loop. The bare `except Exception:
pass` is preserved verbatim — real WS failures are handled by the
reconnect loop, this loop is best-effort.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

logger = logging.getLogger("Heartbeat")


class HeartbeatSender:
    """Sends a JSON heartbeat frame to the bridge every 10 seconds.

    Usage (in BaseBot.run or equivalent):
        from bots._heartbeat import HeartbeatSender
        asyncio.ensure_future(HeartbeatSender(self).run())
    """

    def __init__(self, bot):
        self.bot = bot

    async def run(self) -> None:
        """Send periodic heartbeat to bridge so it can detect hung bots."""
        while True:
            try:
                if self.bot._ws and self.bot._ws.open:
                    await self.bot._ws.send(json.dumps({
                        "type": "heartbeat",
                        "name": self.bot.bot_name,
                        "status": self.bot.status,
                        "ts": time.time(),
                    }))
            except Exception:
                pass  # Best effort — reconnect loop handles real failures
            await asyncio.sleep(10)
