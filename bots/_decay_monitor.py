"""Decay monitor loop — extracted from base_bot.py 2026-05-24 (P4-1 Stage 1).

Observational-only loop. Reads strategy performance state and emits the
15:10 daily summary telegram. No order flow / no risk gate impact.

Original location: bots/base_bot.py:5524-5616 as BaseBot._decay_monitor_loop.
"""
from __future__ import annotations

import asyncio
import logging

from core import telegram_notifier as tg

logger = logging.getLogger("DecayMonitor")


class DecayMonitor:
    """Encapsulates the decay-monitor loop. Owns no critical state — only
    reads from the bot. Safe to extract because: observational, no OIF
    writes, no risk-gate calls.
    """

    def __init__(self, bot):
        # Hold a reference to BaseBot for state reads. (Future: pass only
        # the specific fields needed; for now keep coupling tight = diff small.)
        self.bot = bot

    async def run(self) -> None:
        """Hourly decay check + 15:10 CT daily summary push.

        - CRITICAL strategies → immediate Telegram alert (every hour)
        - WARNING strategies → Telegram alert at most once per 4 hours
        - 15:10 CT → daily summary: P&L, trades, top exit reason, degraded strats
        """
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        import collections

        ct_tz = _ZI("America/Chicago")
        _last_warning_alert: float = 0.0       # epoch seconds
        _daily_summary_fired_for: object = None  # date object

        while True:
            try:
                now_ct = _dt.now(ct_tz)

                # ── hourly decay check ─────────────────────────────────
                try:
                    summary = self.bot.decay_monitor.summary()
                    reports = summary.get("reports", {})
                    criticals = [n for n, r in reports.items() if r.get("status") == "CRITICAL"]
                    warnings  = [n for n, r in reports.items() if r.get("status") == "WARNING"]

                    if criticals:
                        await tg.notify_alert(
                            "STRATEGY DECAY CRITICAL",
                            f"Strategies: {criticals}\nCheck dashboard /api/risk-mgmt",
                        )
                    elif warnings:
                        import time as _time
                        if _time.monotonic() - _last_warning_alert > 4 * 3600:
                            await tg.notify_alert(
                                "STRATEGY DECAY WARNING",
                                f"Strategies: {warnings}\nMonitoring — not yet critical.",
                            )
                            _last_warning_alert = _time.monotonic()
                except Exception as _e:
                    logger.debug(f"[DECAY_MONITOR] check error: {_e}")

                # ── 15:10 CT daily summary ─────────────────────────────
                if (now_ct.hour == 15 and now_ct.minute == 10
                        and _daily_summary_fired_for != now_ct.date()):
                    try:
                        today_str = str(now_ct.date())
                        all_trades = list(getattr(self.bot.trade_memory, "trades", []))
                        trades_today = [
                            t for t in all_trades
                            if str(t.get("exit_time") or t.get("ts") or "").startswith(today_str)
                        ]
                        wins   = sum(1 for t in trades_today if t.get("pnl_dollars", 0) > 0)
                        losses = sum(1 for t in trades_today if t.get("pnl_dollars", 0) < 0)
                        pnl    = sum(float(t.get("pnl_dollars", 0) or 0) for t in trades_today)
                        n      = len(trades_today)
                        wr     = (wins / n * 100) if n else 0.0

                        # top exit reason
                        reasons = [t.get("exit_reason", "unknown") for t in trades_today if t.get("exit_reason")]
                        top_reason = collections.Counter(reasons).most_common(1)
                        top_reason_str = top_reason[0][0] if top_reason else "n/a"

                        # degraded strategies from decay monitor
                        try:
                            _sum = self.bot.decay_monitor.summary()
                            degraded = [
                                n for n, r in _sum.get("reports", {}).items()
                                if r.get("status") in ("WARNING", "CRITICAL")
                            ]
                        except Exception:
                            degraded = []

                        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                        degraded_str = ", ".join(degraded) if degraded else "none"
                        msg = (
                            f"\U0001F4CB <b>15:10 DAILY SUMMARY</b>\n"
                            f"P&amp;L: <b>{pnl_str}</b>\n"
                            f"Trades: {n} ({wins}W / {losses}L) WR={wr:.0f}%\n"
                            f"Top exit: {top_reason_str}\n"
                            f"Degraded: {degraded_str}"
                        )
                        from core.telegram_notifier import send as _tg_send
                        await _tg_send(msg)
                        _daily_summary_fired_for = now_ct.date()
                        logger.info(f"[DECAY_MONITOR] 15:10 daily summary sent: pnl={pnl_str} {n}t")
                    except Exception as _e:
                        logger.warning(f"[DECAY_MONITOR] daily summary error: {_e}")

            except Exception as _outer:
                logger.debug(f"[DECAY_MONITOR] loop error: {_outer}")

            await asyncio.sleep(3600)  # check once per hour
