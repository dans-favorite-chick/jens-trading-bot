"""Daily flatten helper for Phoenix Bot.

Closes all open positions at 4:00 PM Central Time (CME globex pause start).
Overnight holds are not allowed across the 4-5 PM CT globex pause.
"""
from __future__ import annotations

from datetime import datetime, time, date
from typing import Optional, Callable, Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def should_flatten_now(
    now_ct: datetime,
    last_flatten_date: Optional[date],
    flatten_hour: int = 16,
    flatten_minute: int = 0,
) -> bool:
    """Return True if a daily flatten should fire now.

    Fires when current CT time >= (flatten_hour, flatten_minute) AND we
    have not already flattened today.
    """
    if now_ct.time() < time(flatten_hour, flatten_minute):
        return False
    if last_flatten_date is not None and now_ct.date() == last_flatten_date:
        return False
    return True


class DailyFlattener:
    """Driver that closes all open positions at the daily flatten time."""

    def __init__(
        self,
        positions_manager: Any,
        websocket_send_fn: Optional[Callable] = None,
        logger: Any = None,
        flatten_hour: int = 16,
        flatten_minute: int = 0,
    ):
        self.pm = positions_manager
        self.ws_send = websocket_send_fn
        self.logger = logger
        self.flatten_hour = flatten_hour
        self.flatten_minute = flatten_minute
        self.last_flatten_date: Optional[date] = None

    def _iter_positions(self):
        active = getattr(self.pm, "active_positions", None)
        if active is not None:
            if isinstance(active, dict):
                return list(active.values())
            return list(active)
        single = getattr(self.pm, "position", None)
        if single:
            return [single]
        return []

    async def check_and_flatten(self, now_ct: Optional[datetime] = None) -> int:
        """Check and flatten if due. Returns count of positions closed."""
        if now_ct is None:
            now_ct = datetime.now(CT)
        if not should_flatten_now(
            now_ct, self.last_flatten_date, self.flatten_hour, self.flatten_minute
        ):
            return 0

        positions = self._iter_positions()
        reason = "daily_flatten_16CT"
        closed = 0
        for pos in positions:
            trade_id = getattr(pos, "trade_id", None) or (
                pos.get("trade_id") if isinstance(pos, dict) else None
            )
            last_price = getattr(pos, "last_known_price", None) or (
                pos.get("last_known_price") if isinstance(pos, dict) else None
            ) or 0.0
            try:
                if self.ws_send is not None:
                    await self.ws_send(trade_id, reason=reason)
                else:
                    self.pm.close_position(last_price, reason, trade_id=trade_id)
                closed += 1
            except Exception as e:
                if self.logger:
                    self.logger.error(f"daily_flatten close failed trade_id={trade_id}: {e}")

        if self.logger:
            self.logger.info(
                f"daily_flatten fired at {now_ct.isoformat()} closed={closed}"
            )
        self.last_flatten_date = now_ct.date()
        return closed
