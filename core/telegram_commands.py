"""
Phoenix Bot -- Telegram Command Interface

Listens for commands via Telegram and routes them to the bot.
Runs as a background async loop polling getUpdates.
Non-blocking: failures never affect trading.
"""

import asyncio
import logging
import os
import time

import requests

logger = logging.getLogger("TelegramCmd")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

COMMANDS = {
    "/status": "Show bot status, P&L, position",
    "/aggressive": "Switch to aggressive profile",
    "/safe": "Switch to safe profile",
    "/balanced": "Switch to balanced profile",
    "/pause": "Pause trading (set kill switch)",
    "/resume": "Resume trading (clear kill switch)",
    "/cockpit": "Show 12-layer cockpit grading",
    "/pnl": "Show today's P&L breakdown",
    "/intel": "Show latest market intelligence summary",
    "/help": "Show available commands",
}

POLL_INTERVAL = 5  # seconds


class TelegramCommands:
    """Listen for commands via Telegram and route to bot."""

    def __init__(self):
        self._last_update_id = 0
        self._session = requests.Session()
        self._session.trust_env = False

    def _is_configured(self) -> bool:
        return bool(TOKEN) and bool(CHAT_ID)

    def _send_reply(self, text: str):
        """Send a reply back to the configured chat."""
        if not self._is_configured():
            return
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            self._session.post(url, data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            }, timeout=10)
        except Exception as e:
            logger.debug(f"[TG CMD] Reply failed: {e}")

    def _get_updates(self) -> list[dict]:
        """Poll Telegram for new messages."""
        if not self._is_configured():
            return []
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {"offset": self._last_update_id + 1, "timeout": 3}
            resp = self._session.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result", [])
        except Exception as e:
            logger.debug(f"[TG CMD] Poll failed: {e}")
        return []

    def _handle_command(self, command: str, bot_ref) -> str:
        """Route a command to the bot and return a response string."""
        cmd = command.strip().lower().split()[0]  # Get first word

        if cmd == "/status":
            state = bot_ref.to_dict()
            pos = state.get("position", {})
            risk = state.get("risk", {})
            return (
                f"*Status:* {state.get('status', '?')}\n"
                f"Position: {pos.get('status', 'FLAT')}\n"
                f"Daily P&L: ${risk.get('daily_pnl', 0):.2f}\n"
                f"Trades: {risk.get('trades_today', 0)} "
                f"({risk.get('wins_today', 0)}W/{risk.get('losses_today', 0)}L)\n"
                f"WR: {risk.get('win_rate', 0):.0f}%\n"
                f"Recovery: {'YES' if risk.get('recovery_mode') else 'No'}"
            )

        elif cmd == "/aggressive":
            bot_ref.set_profile("aggressive")
            return "Profile switched to *AGGRESSIVE*"

        elif cmd == "/safe":
            bot_ref.set_profile("safe")
            return "Profile switched to *SAFE*"

        elif cmd == "/balanced":
            bot_ref.set_profile("balanced")
            return "Profile switched to *BALANCED*"

        elif cmd == "/pause":
            bot_ref.risk.kill("Telegram /pause command")
            return "Trading *PAUSED* (kill switch set)"

        elif cmd == "/resume":
            bot_ref.risk.state.killed = False
            bot_ref.risk.state.kill_reason = ""
            return "Trading *RESUMED* (kill switch cleared)"

        elif cmd == "/cockpit":
            if hasattr(bot_ref, '_cockpit_result') and bot_ref._cockpit_result:
                cg = bot_ref._cockpit_result
                lines = [f"*Cockpit:* {cg.get('score', '?')}"]
                for layer in cg.get("layers", []):
                    icon = {"GREEN": "+", "YELLOW": "~", "RED": "-"}.get(layer["status"], "?")
                    lines.append(f"  [{icon}] {layer['name']}: {layer['detail']}")
                return "\n".join(lines)
            return "Cockpit not graded yet"

        elif cmd == "/pnl":
            risk = bot_ref.risk.to_dict()
            return (
                f"*Daily P&L:* ${risk.get('daily_pnl', 0):.2f}\n"
                f"Trades: {risk.get('trades_today', 0)}\n"
                f"Wins: {risk.get('wins_today', 0)} | Losses: {risk.get('losses_today', 0)}\n"
                f"WR: {risk.get('win_rate', 0):.0f}%\n"
                f"Daily limit used: {risk.get('daily_used_pct', 0):.0f}%"
            )

        elif cmd == "/intel":
            if hasattr(bot_ref, '_latest_intel') and bot_ref._latest_intel:
                intel = bot_ref._latest_intel
                vix = intel.get("vix", {})
                vix_val = vix.get("value", "N/A") if isinstance(vix, dict) else "N/A"
                return (
                    f"*Market Intel:*\n"
                    f"VIX: {vix_val}\n"
                    f"Trade OK: {intel.get('trade_ok', '?')}\n"
                    f"Trump Warning: {intel.get('trump_warning', 'None')}\n"
                    f"Fetch: {intel.get('fetch_time_s', '?')}s"
                )
            return "No intel data available yet"

        elif cmd == "/help":
            lines = ["*Available Commands:*"]
            for c, desc in COMMANDS.items():
                lines.append(f"`{c}` - {desc}")
            return "\n".join(lines)

        else:
            return f"Unknown command: `{cmd}`\nSend /help for available commands"

    async def poll_commands(self, bot_ref):
        """
        Poll Telegram for incoming commands every 5 seconds.
        Runs as background asyncio task. Non-blocking.
        """
        if not self._is_configured():
            logger.info("[TG CMD] Telegram not configured -- command listener disabled")
            return

        logger.info("[TG CMD] Command listener started")
        loop = asyncio.get_event_loop()

        while True:
            try:
                # Run blocking HTTP call in executor
                updates = await loop.run_in_executor(None, self._get_updates)

                for update in updates:
                    update_id = update.get("update_id", 0)
                    self._last_update_id = max(self._last_update_id, update_id)

                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = str(msg.get("chat", {}).get("id", ""))

                    # Only respond to messages from our configured chat
                    if chat_id != CHAT_ID:
                        continue

                    if text.startswith("/"):
                        logger.info(f"[TG CMD] Received: {text}")
                        try:
                            response = self._handle_command(text, bot_ref)
                            await loop.run_in_executor(None, self._send_reply, response)
                        except Exception as e:
                            logger.error(f"[TG CMD] Command handler error: {e}")
                            await loop.run_in_executor(
                                None, self._send_reply, f"Error: {e}")

            except Exception as e:
                logger.debug(f"[TG CMD] Poll loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)
