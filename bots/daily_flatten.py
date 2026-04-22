"""Daily flatten helper for Phoenix Bot.

B84 (2026-04-22): fires at 15:54 CT — two minutes before NT8's Auto Close
Position safety net at 15:55 CT, six minutes before the CME globex
maintenance break at 16:00 CT. The layered architecture:

  15:53 CT  NO_NEW_ENTRIES gate (base_bot._enter_trade refuses new positions)
  15:54 CT  Phoenix DailyFlattener (PRIMARY — this file)
  15:54:45  Phoenix logs WARN if any position is still open
  15:55 CT  NT8 Auto Close Position (SAFETY NET, configured in NT8 GUI)
  16:00 CT  CME globex 1-hour maintenance break (HARD FLOOR)

Supersedes: B83 (15:58 CT, 2-min runway before the maintenance break).
B83 was right in spirit but now NT8 itself flattens at 15:55 CT, so
Phoenix needs to be ~1 minute earlier than that to stay the primary layer.

Defaults read from config.settings.DAILY_FLATTEN_HOUR_CT /
DAILY_FLATTEN_MINUTE_CT so the schedule has a single source of truth.
"""
from __future__ import annotations

from datetime import datetime, time, date
from typing import Optional, Callable, Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

# Single-source defaults. Imports inside the helpers so this module
# stays importable in ultra-minimal test environments where
# config.settings may not be available.
def _default_flatten_hour() -> int:
    try:
        from config.settings import DAILY_FLATTEN_HOUR_CT
        return int(DAILY_FLATTEN_HOUR_CT)
    except Exception:
        return 15


def _default_flatten_minute() -> int:
    try:
        from config.settings import DAILY_FLATTEN_MINUTE_CT
        return int(DAILY_FLATTEN_MINUTE_CT)
    except Exception:
        return 54


def should_flatten_now(
    now_ct: datetime,
    last_flatten_date: Optional[date],
    flatten_hour: Optional[int] = None,
    flatten_minute: Optional[int] = None,
) -> bool:
    """Return True if a daily flatten should fire now.

    Fires when current CT time >= (flatten_hour, flatten_minute) AND we
    have not already flattened today. Defaults to 15:54 CT via
    config.settings (B84).
    """
    fh = _default_flatten_hour() if flatten_hour is None else flatten_hour
    fm = _default_flatten_minute() if flatten_minute is None else flatten_minute
    if now_ct.time() < time(fh, fm):
        return False
    if last_flatten_date is not None and now_ct.date() == last_flatten_date:
        return False
    return True


class DailyFlattener:
    """Driver that closes all open positions at the daily flatten time.

    Wired into BaseBot (B84); prod and sim both inherit it. sim_bot
    overrides `_daily_flatten_loop` to add post-flatten debrief + recap
    hooks; prod uses the vanilla loop.
    """

    def __init__(
        self,
        positions_manager: Any,
        websocket_send_fn: Optional[Callable] = None,
        logger: Any = None,
        flatten_hour: Optional[int] = None,
        flatten_minute: Optional[int] = None,
    ):
        self.pm = positions_manager
        self.ws_send = websocket_send_fn
        self.logger = logger
        self.flatten_hour = (
            _default_flatten_hour() if flatten_hour is None else flatten_hour
        )
        self.flatten_minute = (
            _default_flatten_minute() if flatten_minute is None else flatten_minute
        )
        self.last_flatten_date: Optional[date] = None
        # B84: snapshot of the trade_ids the flatten issued exits for,
        # plus the wall-clock the flatten fired. Consumed by the grace-
        # window watcher in base_bot to log AWAITING_FILL_CONFIRMATION
        # and the 15:54:45 WARN if any remain open.
        self.last_flatten_fired_at_ct: Optional[datetime] = None
        self.last_flatten_trade_ids: list[str] = []

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
        # Reason string encodes the flatten time so trade_memory / dashboard
        # consumers can distinguish pre-B84 closes from post-B84 closes.
        reason = f"daily_flatten_{self.flatten_hour:02d}{self.flatten_minute:02d}CT"
        closed = 0
        issued_ids: list[str] = []
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
                if trade_id:
                    issued_ids.append(trade_id)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"daily_flatten close failed trade_id={trade_id}: {e}")

        if self.logger:
            self.logger.info(
                f"daily_flatten fired at {now_ct.isoformat()} closed={closed}"
            )
        self.last_flatten_date = now_ct.date()
        self.last_flatten_fired_at_ct = now_ct
        self.last_flatten_trade_ids = issued_ids
        return closed
