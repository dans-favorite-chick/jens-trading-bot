"""Daily flatten helper for Phoenix Bot.

Closes all open positions at 3:58 PM Central Time — two minutes before the
CME globex 4:00 PM maintenance break. The 2-minute runway guarantees the
EXIT MARKET order reaches NT8 and fills BEFORE the break closes the book
at 16:00, so positions don't queue through the maintenance hour.

Overnight holds are not allowed across the 4-5 PM CT globex pause.

B83 (2026-04-22): moved from 16:00 → 15:58 after a SimSpring Setup LONG
sat open at 15:59 because the 30-second poll loop hadn't yet crossed the
old 16:00 fire gate. Firing at 15:58 gives up to 2 min of order-transit
runway before the break.
"""
from __future__ import annotations

from datetime import datetime, time, date
from typing import Optional, Callable, Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def should_flatten_now(
    now_ct: datetime,
    last_flatten_date: Optional[date],
    flatten_hour: int = 15,
    flatten_minute: int = 58,
) -> bool:
    """Return True if a daily flatten should fire now.

    Fires when current CT time >= (flatten_hour, flatten_minute) AND we
    have not already flattened today.

    Default 15:58 CT (B83) — two minutes before the CME globex 16:00
    maintenance break, so EXIT MARKET orders have runway to transit and
    fill before the book closes.
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
        flatten_hour: int = 15,
        flatten_minute: int = 58,
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
        reason = "daily_flatten_1558CT"
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
