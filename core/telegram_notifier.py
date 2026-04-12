"""
Phoenix Bot — Telegram Trade Notifications

Sends trade entry, exit, and P&L notifications to Telegram.
Non-blocking: failures are logged but never affect trading.

Messages sent:
  - Trade entry: direction, strategy, price, stop, target, risk
  - Trade exit: direction, P&L, result (WIN/LOSS), exit reason
  - Daily summary: total P&L, trades, win rate
  - Council bias: session bias vote result
  - Alerts: recovery mode, kill switch, news gate blocks
"""

import asyncio
import logging
import os
import time

import requests

logger = logging.getLogger("Telegram")

# Load from environment
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Rate limiting: max 1 message per second
_last_send_time = 0
_MIN_INTERVAL = 1.0


def _is_configured() -> bool:
    return bool(TOKEN) and bool(CHAT_ID)


def send_sync(text: str, parse_mode: str = "Markdown") -> bool:
    """
    Send a Telegram message synchronously. Non-blocking safe.
    Returns True if sent, False on failure.
    """
    global _last_send_time

    if not _is_configured():
        logger.debug("[TG] Not configured — skipping")
        return False

    # Rate limit
    now = time.time()
    if now - _last_send_time < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - (now - _last_send_time))

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": False,
        }
        session = requests.Session()
        session.trust_env = False
        response = session.post(url, data=payload, timeout=10)
        _last_send_time = time.time()

        if response.status_code == 200:
            logger.info(f"[TG] Message sent ({len(text)} chars)")
            return True
        else:
            logger.warning(f"[TG] Send failed: {response.status_code} {response.text[:100]}")
            return False

    except Exception as e:
        logger.warning(f"[TG] Send error: {e}")
        return False


async def send(text: str, parse_mode: str = "Markdown") -> bool:
    """Async wrapper — runs send_sync in executor to avoid blocking."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, send_sync, text, parse_mode)


# ─── Trade Notification Formatters ─────────────────────────────────

async def notify_entry(trade_id: str, direction: str, strategy: str,
                       price: float, stop: float, target: float,
                       contracts: int, risk_dollars: float, tier: str,
                       regime: str):
    """Send trade entry notification."""
    emoji = "\U0001F7E2" if direction == "LONG" else "\U0001F534"  # green/red circle
    msg = (
        f"{emoji} *ENTRY: {direction}*\n"
        f"Strategy: `{strategy}`\n"
        f"Price: `{price:.2f}`\n"
        f"Stop: `{stop:.2f}` | Target: `{target:.2f}`\n"
        f"Size: {contracts}x | Risk: ${risk_dollars:.2f} ({tier})\n"
        f"Regime: {regime}\n"
        f"ID: `{trade_id}`"
    )
    await send(msg)


async def notify_exit(trade_id: str, direction: str, strategy: str,
                      entry_price: float, exit_price: float,
                      pnl_dollars: float, pnl_ticks: float,
                      result: str, exit_reason: str, hold_time_s: float):
    """Send trade exit notification with P&L."""
    if result == "WIN":
        emoji = "\U0001F4B0"  # money bag
        pnl_str = f"+${pnl_dollars:.2f}"
    else:
        emoji = "\U0001F4A8"  # dash
        pnl_str = f"-${abs(pnl_dollars):.2f}"

    hold_min = hold_time_s / 60
    msg = (
        f"{emoji} *EXIT: {direction} {result}*\n"
        f"Strategy: `{strategy}`\n"
        f"Entry: `{entry_price:.2f}` | Exit: `{exit_price:.2f}`\n"
        f"*P&L: {pnl_str}* ({pnl_ticks:+.1f} ticks)\n"
        f"Reason: {exit_reason}\n"
        f"Hold: {hold_min:.1f} min\n"
        f"ID: `{trade_id}`"
    )
    await send(msg)


async def notify_daily_summary(daily_pnl: float, trades: int, wins: int,
                                losses: int, win_rate: float,
                                recovery_mode: bool):
    """Send end-of-day summary."""
    emoji = "\U0001F4CA"  # chart
    pnl_str = f"+${daily_pnl:.2f}" if daily_pnl >= 0 else f"-${abs(daily_pnl):.2f}"
    status = "\U000026A0 RECOVERY MODE" if recovery_mode else "Normal"

    msg = (
        f"{emoji} *DAILY SUMMARY*\n"
        f"P&L: *{pnl_str}*\n"
        f"Trades: {trades} ({wins}W / {losses}L)\n"
        f"Win Rate: {win_rate:.0f}%\n"
        f"Status: {status}"
    )
    await send(msg)


async def notify_council(bias: str, vote_count: str, summary: str):
    """Send council bias vote result."""
    emoji = {
        "BULLISH": "\U0001F7E2",   # green
        "BEARISH": "\U0001F534",   # red
        "NEUTRAL": "\U0001F7E1",   # yellow
    }.get(bias, "\U00002753")      # question mark

    msg = (
        f"{emoji} *COUNCIL: {bias}*\n"
        f"Vote: {vote_count}\n"
        f"{summary[:200]}"
    )
    await send(msg)


async def notify_alert(alert_type: str, message: str):
    """Send general alert (recovery mode, kill switch, news gate, etc.)."""
    msg = f"\U000026A0 *ALERT: {alert_type}*\n{message}"
    await send(msg)


# ─── Standalone Test ───────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    # Re-read after dotenv
    TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
    CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    print(f"Token: {'SET' if TOKEN else 'MISSING'} ({len(TOKEN)} chars)")
    print(f"Chat ID: {CHAT_ID or 'MISSING'}")

    if _is_configured():
        result = send_sync("\U0001F525 *Phoenix Bot Online*\nTelegram notifications active!")
        print(f"Test send: {'SUCCESS' if result else 'FAILED'}")
    else:
        print("Telegram not configured — check .env")
