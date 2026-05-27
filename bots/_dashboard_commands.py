"""Dashboard command dispatcher — extracted from base_bot.py 2026-05-24
(P4-1 Stage 2).

Receives commands from the Flask dashboard (via DashboardPusher's poll
loop) and dispatches them back to BaseBot methods. Read-mostly: the
handler itself doesn't mutate critical state; it delegates to BaseBot's
toggle_strategy / update_runtime_params / set_profile etc. The one
exception is the `shutdown` branch, which sets shutdown flags on the
bot and schedules the WebSocket close to unblock _connect_and_listen.

Original location: bots/base_bot.py:1984 as BaseBot._handle_dashboard_command.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("DashboardCommands")


class DashboardCommandDispatcher:
    """Dispatch dashboard-originated commands back into BaseBot methods.

    Constructed once per BaseBot instance; `handle(cmd)` is invoked
    synchronously from DashboardPusher's poll loop when a command
    arrives from the Flask dashboard.
    """

    def __init__(self, bot):
        self.bot = bot

    def handle(self, cmd: dict) -> None:
        """Process a command from the dashboard."""
        cmd_type = cmd.get("type", "")
        if cmd_type == "set_profile":
            self.bot.set_profile(cmd.get("profile", "balanced"))
        elif cmd_type == "toggle_strategy":
            self.bot.toggle_strategy(cmd.get("name", ""), cmd.get("enabled", True))
        elif cmd_type == "update_params":
            self.bot.update_runtime_params(cmd.get("params", {}))
        elif cmd_type == "test_trade":
            logger.info(f"[TEST TRADE] {cmd.get('action', 'ENTER_LONG')}")
            # TODO: fire test trade
        elif cmd_type == "shutdown":
            # 2026-05-13: graceful exit requested by dashboard / watchdog.
            # Stop scanning, close WS, let run() return → process exits
            # cleanly. Positions are NOT flattened — they remain to be
            # managed by NT8 OCO brackets or the next bot start.
            # Flattening on shutdown would turn a routine restart into a
            # market-order event, which is too risky for the common
            # "watchdog restarting after disconnect" case. Replaces the
            # CTRL_BREAK_EVENT path lost in commit 8b471af.
            logger.warning(
                "[SHUTDOWN] command received via dashboard — exiting cleanly"
            )
            self.bot._shutdown_requested = True
            self.bot._shutdown_reconciliation = True  # quiesce the recon loop too
            # Close WS to unblock the async-for in _connect_and_listen.
            # Wrap in try/except: ws may already be closed, or there may
            # be no running event loop on this thread (shouldn't happen
            # since we're called from _dashboard_loop, but defensive).
            if self.bot._ws is not None:
                try:
                    asyncio.ensure_future(self.bot._ws.close())
                except Exception as _e:
                    logger.warning(
                        f"[SHUTDOWN] ws.close scheduling failed: {_e!r}"
                    )
