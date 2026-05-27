"""Dashboard state pusher — extracted from base_bot.py 2026-05-24 (P4-1 Stage 2).

Periodically pushes bot state to the Flask dashboard. Read-only with
respect to bot's critical state — but may RECEIVE dashboard commands and
dispatch them via the bot's _handle_dashboard_command method.

Original location: bots/base_bot.py:1768-1831 as BaseBot._dashboard_loop.

The outer try/except guard around json.dumps + HTTP push is preserved
verbatim — `tests/test_dashboard_serialize.py::TestPushBoundaryGuarded`
greps base_bot.py for "Dashboard push failed:" and for 3+ refs to
`_json_default_safe`. Since this extraction does NOT modify base_bot.py
(only adds a thin delegator at the original method location is optional;
current Stage 2 plan keeps base_bot.py untouched), those source-grep
contracts remain satisfied.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from config.settings import DASHBOARD_PORT

# `_json_default_safe` lives at `bots.base_bot:836`. We CANNOT do a
# module-top import here because `bots.base_bot` imports this class at
# its own top, creating a circular import. Resolved by lazy-importing
# inside run() — by the time run() executes, base_bot is fully loaded.

logger = logging.getLogger("DashboardPusher")


class DashboardPusher:
    """Pushes bot snapshot JSON to the dashboard every 2s; polls for commands.

    Coupling to BaseBot (intentional, kept tight for Stage 2):
      - reads:  bot.bot_name, bot.to_dict()
      - calls:  bot._handle_dashboard_command(cmd)  (dispatch surface unchanged)

    Uses aiohttp when available (non-blocking) and falls back to
    urllib-in-executor so the WebSocket keepalive ping loop never
    starves on a blocking socket call (root cause of the cascading-
    disconnect incident this loop was originally rewritten to fix).
    """

    def __init__(self, bot):
        self.bot = bot

    async def run(self) -> None:
        """Push bot state to dashboard every 2s and poll for commands.
        Uses async HTTP to avoid blocking the event loop (which starves
        WebSocket keepalive pings and causes cascading disconnects).
        """
        # Lazy import — see circular-import note at top of this file.
        from bots.base_bot import _json_default_safe

        url_state = f"http://127.0.0.1:{DASHBOARD_PORT}/api/bot-state"
        url_cmds = f"http://127.0.0.1:{DASHBOARD_PORT}/api/commands?bot={self.bot.bot_name}"

        # Try aiohttp first (non-blocking), fall back to thread-pool urllib
        try:
            import aiohttp
            _use_aiohttp = True
        except ImportError:
            _use_aiohttp = False

        while True:
            try:
                if _use_aiohttp:
                    async with aiohttp.ClientSession() as sess:
                        # Push state. default=_json_default_safe coerces
                        # any leaked datetime/date in sub-component to_dict()
                        # outputs to ISO strings (BUG-TL1 fix).
                        state_json = json.dumps(self.bot.to_dict(), default=_json_default_safe)
                        async with sess.post(url_state, data=state_json,
                                             headers={"Content-Type": "application/json"},
                                             timeout=aiohttp.ClientTimeout(total=2)):
                            pass

                        # Poll commands
                        try:
                            async with sess.get(url_cmds, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                                cmds = await resp.json()
                                for cmd in cmds:
                                    self.bot._handle_dashboard_command(cmd)
                        except Exception:
                            pass
                else:
                    # Fallback: run blocking urllib in thread pool so it doesn't
                    # starve the event loop
                    loop = asyncio.get_event_loop()
                    state_json = json.dumps(
                        self.bot.to_dict(), default=_json_default_safe
                    ).encode("utf-8")
                    req = urllib.request.Request(
                        url_state, data=state_json,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=2))

                    # Poll commands
                    try:
                        resp = await loop.run_in_executor(
                            None, lambda: urllib.request.urlopen(url_cmds, timeout=2))
                        cmds = json.loads(resp.read().decode())
                        for cmd in cmds:
                            self.bot._handle_dashboard_command(cmd)
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(f"Dashboard push failed: {e}")

            await asyncio.sleep(2)
