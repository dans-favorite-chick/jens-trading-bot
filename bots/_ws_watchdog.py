"""WebSocket watchdog — extracted from base_bot.py 2026-05-24 (P4-1 Stage 1).

Defends against silent WebSocket half-close: bridge-side socket dies (TCP
keepalive failure, OS-level close without FIN) but `async for message in ws`
blocks forever on a frame that will never arrive.

2026-05-24 (P1-6 F-11 fix): switched the staleness sentinel from
`_last_ws_message_time` (any frame) to `last_wsping_received_time` (a
dedicated `wsping` control frame the bridge broadcasts every 30s). Why:
during 0-tick lulls (weekends, lunch, overnight) the prior any-frame
heuristic could not distinguish "market quiet" from "WS dead" and was
defensively reconnecting every ~106 seconds. With the new scheme, the
bridge's `wsping` is the proof-of-life signal — its absence (not the
absence of ticks) is what proves the socket is dead.

NOTE: `self.bot._last_ws_message_time` is still stamped by the receive
loop on every frame, but it is no longer this watchdog's staleness
sentinel. Use `self.bot._ws_watchdog.last_wsping_received_time` for that.

Skip-windows:
  - 16:00-17:00 CT NT8 daily maintenance break (no traffic expected,
    would false-positive even on pings since the bridge stays up but
    the bot may be intentionally idle/restarting)
  - sentinel `last_wsping_received_time == 0` (no ping ever received,
    e.g. fresh process before first bridge cycle)

Original location: bots/base_bot.py:5620-5698.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("WSWatchdog")

# Module-level constants (lifted from inside the original method body).
WS_STALE_THRESHOLD_S = 90.0
CHECK_INTERVAL_S = 15.0


class WSWatchdog:
    """Application-level WebSocket keepalive watchdog.

    Wraps a BaseBot instance and monitors `self.last_wsping_received_time`.

    Fields used:
      - self.last_wsping_received_time  (read + write-on-reset) — set by
        the BaseBot receive loop when a `{"type":"wsping"}` frame arrives.
      - self.bot._ws                    (read; force-closed on stale)
    """

    def __init__(self, bot) -> None:
        self.bot = bot
        # 2026-05-24 P1-6: dedicated "ping received" timestamp. Distinct
        # from _last_ws_message_time so quiet markets (no ticks but pings
        # still arriving) do NOT trip the watchdog.
        self.last_wsping_received_time: float = 0.0

    async def run(self) -> None:
        """Force-reconnect if no `wsping` received in WS_STALE_THRESHOLD_S.

        2026-05-24 P1-6: the staleness check is now ping-based, not
        any-frame-based. The bridge broadcasts a `{"type":"wsping","ts":...}`
        message every 30s to every connected bot. The bot's receive loop
        in `_connect_and_listen` updates `self.last_wsping_received_time`
        when it sees one. This watchdog reconnects only when those pings
        stop, which is the genuine "WS dead" signal regardless of market
        activity.

        Symptom this defends against (observed 2026-05-12 ~08:09 CT):
          - Bot process alive, async tasks all firing on schedule
          - Bridge `bots_connected` no longer lists this bot
          - `local=` price stuck at last received value
          - No `[BAR 1m]` log entries for >5 min
          - Dashboard reports prod as "reconnecting"

        The websocket connection is configured with `ping_interval=None`
        (no protocol-level keepalive — see `_connect_and_listen`), so
        without an application-level timeout the bot is deaf.

        Skip-windows:
          - 16:00-17:00 CT: NT8 daily maintenance break
          - sentinel `last_wsping_received_time == 0`: no ping received
            yet (fresh process, bridge restarting, etc.)
        """
        ct_tz = ZoneInfo("America/Chicago")

        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)

            try:
                # Skip during NT8 daily maintenance break.
                if datetime.now(ct_tz).hour == 16:
                    continue

                last_ping = self.last_wsping_received_time
                if not last_ping:
                    continue  # haven't received first wsping yet

                age = time.time() - last_ping
                if age <= WS_STALE_THRESHOLD_S:
                    continue

                ws = self.bot._ws
                if ws is None:
                    continue

                logger.warning(
                    f"[WS_WATCHDOG] no wsping in {age:.0f}s "
                    f"(threshold {WS_STALE_THRESHOLD_S:.0f}s) — "
                    f"force-closing WS to trigger reconnect"
                )
                try:
                    await asyncio.wait_for(
                        ws.close(code=1011, reason="bot-side ws-stale watchdog"),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[WS_WATCHDOG] ws.close timed out after 5s")
                except Exception as _close_err:
                    logger.warning(f"[WS_WATCHDOG] ws.close failed: {_close_err!r}")

                # Reset so we don't immediately re-close after reconnect.
                # The next real wsping from the bridge will overwrite this.
                self.last_wsping_received_time = time.time()

            except Exception as _outer:
                logger.debug(f"[WS_WATCHDOG] loop error: {_outer!r}")
