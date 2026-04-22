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
import html
import logging
import os
import time

import requests

logger = logging.getLogger("Telegram")

# Phase C routing: pull optional overrides + tag flag from settings.
# Import is defensive — if settings import fails (e.g. stripped test env),
# fall back to no-overrides + tagging enabled.
try:
    from config import settings as _settings
    TELEGRAM_STRATEGY_CHAT_OVERRIDES: dict = dict(
        getattr(_settings, "TELEGRAM_STRATEGY_CHAT_OVERRIDES", {}) or {}
    )
    TELEGRAM_TAG_STRATEGY: bool = bool(
        getattr(_settings, "TELEGRAM_TAG_STRATEGY", True)
    )
except Exception:  # pragma: no cover — defensive
    TELEGRAM_STRATEGY_CHAT_OVERRIDES = {}
    TELEGRAM_TAG_STRATEGY = True


# P14: canonical HTML-escape helper for all user-supplied string fields.
# Telegram's HTML parse_mode treats <, >, & as markup. Any strategy name,
# exit reason, alert body, or other caller-provided string that isn't
# escaped will either corrupt the formatting or return HTTP 400. Use
# html.escape() (stdlib) — handles &, <, > and (with quote=True) quote
# characters, more complete than the manual .replace() chain that was
# applied only in 2 of 5 notifier paths before this fix.
def _esc(value) -> str:
    """Defensive HTML-escape. Tolerates non-str values (ints/floats)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)

# Load from environment
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Rate limiting: max 1 message per second
_last_send_time = 0
_MIN_INTERVAL = 1.0


def _is_configured() -> bool:
    return bool(TOKEN) and bool(CHAT_ID)


def _resolve_chat_id(strategy: str | None) -> str:
    """Phase C routing: strategy → override chat_id, else default CHAT_ID."""
    if strategy and strategy in TELEGRAM_STRATEGY_CHAT_OVERRIDES:
        return TELEGRAM_STRATEGY_CHAT_OVERRIDES[strategy]
    return CHAT_ID


def _apply_tag(msg: str, strategy: str | None) -> str:
    """Phase C tagging: prepend [strategy] once, if enabled and provided."""
    if not TELEGRAM_TAG_STRATEGY or not strategy:
        return msg
    # Escape strategy for HTML parse_mode (strategy keys may contain & etc.)
    tag = f"[{_esc(strategy)}]"
    raw_tag = f"[{strategy}]"
    # Don't duplicate if the message already starts with the tag (escaped or raw)
    if msg.startswith(tag) or msg.startswith(raw_tag):
        return msg
    return f"{tag} {msg}"


#: B54 — per-dedup-key 15-minute cooldown cache.
#: Any Telegram send with a non-empty `dedup_key` suppresses duplicates
#: within COOLDOWN_S of the last send for that key.
_COOLDOWN_S: float = 15 * 60  # 15 minutes
_cooldown_cache: dict[str, float] = {}


def send_sync(text: str, parse_mode: str = "HTML",
              chat_id: str | None = None,
              dedup_key: str | None = None) -> bool:
    """
    Send a Telegram message synchronously. Non-blocking safe.
    Returns True if sent, False on failure or cooldown-suppressed.

    B54: when `dedup_key` is non-empty, re-sends within 15 minutes for
    the same key are silently dropped. Use a stable key like
    f"{alert_type}:{strategy}" so different alerts don't collide but
    repeats of the same alert do.
    """
    global _last_send_time

    # B54 cooldown check
    if dedup_key:
        now_c = time.time()
        last = _cooldown_cache.get(dedup_key, 0.0)
        if now_c - last < _COOLDOWN_S:
            remaining = int(_COOLDOWN_S - (now_c - last))
            logger.debug(f"[TG] dedup-suppressed '{dedup_key}' ({remaining}s left)")
            return False

    target_chat = chat_id if chat_id else CHAT_ID
    if not TOKEN or not target_chat:
        logger.debug("[TG] Not configured — skipping")
        return False

    # Rate limit
    now = time.time()
    if now - _last_send_time < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - (now - _last_send_time))

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": target_chat,
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
            if dedup_key:
                _cooldown_cache[dedup_key] = time.time()
            return True
        else:
            logger.warning(f"[TG] Send failed: {response.status_code} {response.text[:100]}")
            return False

    except Exception as e:
        logger.warning(f"[TG] Send error: {e}")
        return False


async def send(text: str, parse_mode: str = "HTML",
               chat_id: str | None = None) -> bool:
    """Async wrapper — runs send_sync in executor to avoid blocking."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, send_sync, text, parse_mode, chat_id)


# ─── Trade Notification Formatters ─────────────────────────────────

async def notify_entry(trade_id: str, direction: str, strategy: str,
                       price: float, stop: float, target: float,
                       contracts: int, risk_dollars: float, tier: str,
                       regime: str):
    """B54: Jennifer wants Telegrams only on successful trade CLOSES with
    P&L (not on entries). This function is preserved for API compatibility
    but is a no-op. Re-enable by removing the early return if needed."""
    logger.debug(f"[TG] notify_entry suppressed (B54) tid={trade_id} "
                 f"{direction} {strategy} @ {price:.2f}")
    return


async def notify_exit(trade_id: str, direction: str, strategy: str,
                      entry_price: float, exit_price: float,
                      pnl_dollars: float, pnl_ticks: float,
                      result: str, exit_reason: str, hold_time_s: float):
    """B54: ONLY fire on successful trade closes with real P&L. Skip
    cancels, rejections, and zero-P&L phantom closes (those indicate
    the trade never actually executed on NT8)."""
    # Guard: must have a real entry and exit price and meaningful P&L-ticks
    if not entry_price or not exit_price or entry_price <= 0 or exit_price <= 0:
        logger.debug(f"[TG] notify_exit skipped: no real fill tid={trade_id}")
        return
    if result not in ("WIN", "LOSS"):
        logger.debug(f"[TG] notify_exit skipped: result={result!r} tid={trade_id}")
        return

    if result == "WIN":
        emoji = "\U0001F4B0"
        pnl_str = f"+${pnl_dollars:.2f}"
    else:
        emoji = "\U0001F4A8"
        pnl_str = f"-${abs(pnl_dollars):.2f}"

    # B54: tighter format — P&L first, no trade_id noise, no entry/exit price
    msg = (
        f"{emoji} <b>{_esc(strategy)} {_esc(result)} {pnl_str}</b>\n"
        f"{_esc(direction)} {pnl_ticks:+.1f}t · {_esc(exit_reason)}"
    )
    await send(_apply_tag(msg, strategy), chat_id=_resolve_chat_id(strategy))


async def notify_daily_summary(daily_pnl: float, trades: int, wins: int,
                                losses: int, win_rate: float,
                                recovery_mode: bool,
                                strategy: str | None = None):
    """Send end-of-day summary."""
    emoji = "\U0001F4CA"  # chart
    pnl_str = f"+${daily_pnl:.2f}" if daily_pnl >= 0 else f"-${abs(daily_pnl):.2f}"
    status = "\U000026A0 RECOVERY MODE" if recovery_mode else "Normal"

    msg = (
        f"{emoji} <b>DAILY SUMMARY</b>\n"
        f"P&amp;L: <b>{pnl_str}</b>\n"
        f"Trades: {trades} ({wins}W / {losses}L)\n"
        f"Win Rate: {win_rate:.0f}%\n"
        f"Status: {_esc(status)}"
    )
    await send(_apply_tag(msg, strategy), chat_id=_resolve_chat_id(strategy))


async def notify_council(bias: str, vote_count: str, summary: str,
                         strategy: str | None = None):
    """Send council bias vote result."""
    emoji = {
        "BULLISH": "\U0001F7E2",   # green
        "BEARISH": "\U0001F534",   # red
        "NEUTRAL": "\U0001F7E1",   # yellow
    }.get(bias, "\U00002753")      # question mark

    # P14: use html.escape() via _esc helper (consistent across notifiers).
    safe_summary = _esc(summary[:200] if summary else "")
    msg = (
        f"{emoji} <b>COUNCIL: {_esc(bias)}</b>\n"
        f"Vote: {_esc(vote_count)}\n"
        f"{safe_summary}"
    )
    await send(_apply_tag(msg, strategy), chat_id=_resolve_chat_id(strategy))


async def notify_alert(alert_type: str, message: str,
                       strategy: str | None = None):
    """Send general alert (recovery mode, kill switch, news gate, etc.).

    B54: alerts are deduped for 15 minutes per (alert_type, strategy) key.
    Repeated same-type alerts within the window are suppressed server-side.
    """
    msg = f"\U000026A0 <b>ALERT: {_esc(alert_type)}</b>\n{_esc(message)}"
    dedup_key = f"alert:{alert_type}:{strategy or ''}"
    # Async send wrapper does its own executor dispatch of send_sync;
    # send_sync honors dedup_key. Do the same here via the sync path.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: send_sync(
            _apply_tag(msg, strategy),
            chat_id=_resolve_chat_id(strategy),
            dedup_key=dedup_key,
        ),
    )


# ─── Standalone Test ───────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

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
