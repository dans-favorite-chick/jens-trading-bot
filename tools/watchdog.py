"""
Phoenix Bot — Watchdog Process

Standalone health monitor that ensures the trading system stays alive.
Runs independently of bridge, bots, and dashboard.

Monitors:
  - Bridge health endpoint (:8767)
  - Bot connectivity (via bridge health)
  - Dashboard responsiveness (:5000)
  - Bot process liveness (via dashboard API)

Actions:
  - Auto-restarts bots when they disconnect
  - Logs disconnect forensics (frequency, duration, patterns)
  - Sends Telegram alerts on failures
  - Tracks uptime statistics
  - Writes forensics log for debugging persistent issues

Usage:
    python tools/watchdog.py                    # Run watchdog
    python tools/watchdog.py --no-restart       # Monitor only, no auto-restart
    python tools/watchdog.py --bots prod,sim        # Default — both bots
    python tools/watchdog.py --bots sim             # Only sim (prod dropped)
    python tools/watchdog.py --bots prod            # Only prod
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timedelta
from threading import Thread

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import (
    HEALTH_HTTP_PORT, DASHBOARD_PORT, BOT_WS_PORT,
)

# ─── Configuration ─────────────────────────────────────────────────
POLL_INTERVAL_S = 5        # Health check every 5s
RESTART_COOLDOWN_S = 30    # Min seconds between restart attempts for same bot
RESTART_MAX_ATTEMPTS = 5   # Max restart attempts before alerting and backing off
BACKOFF_BASE_S = 30        # Exponential backoff base after max attempts
BRIDGE_TIMEOUT_S = 3       # HTTP timeout for bridge health
DASHBOARD_TIMEOUT_S = 3    # HTTP timeout for dashboard API
FORENSICS_MAX = 500        # Max disconnect events to keep in memory
STALE_BOT_THRESHOLD_S = 20 # Bot considered dead if no state push in this window

# ─── Logging ───────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(LOG_DIR, "watchdog.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("Watchdog")

# ─── Forensics Log ────────────────────────────────────────────────
FORENSICS_PATH = os.path.join(LOG_DIR, "disconnect_forensics.jsonl")


def _log_forensic(event: dict):
    """Append a forensic event to the JSONL log."""
    event["ts"] = datetime.now().isoformat()
    try:
        with open(FORENSICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        logger.error(f"Forensics write failed: {e}")


# ─── Telegram Alerts ──────────────────────────────────────────────
_tg_last_send = 0
_TG_MIN_INTERVAL = 10  # Don't spam — max 1 alert per 10s


def _send_telegram(message: str):
    """Send a Telegram alert (non-blocking, best-effort)."""
    global _tg_last_send
    now = time.time()
    if now - _tg_last_send < _TG_MIN_INTERVAL:
        return

    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    _tg_last_send = now
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": f"🔧 WATCHDOG: {message}"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Best effort — never crash the watchdog for Telegram


# ─── HTTP Helpers ────────────────────────────────��─────────────────
def _fetch_json(url: str, timeout: float = 3) -> dict | None:
    """Fetch JSON from URL. Returns None on any failure."""
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(req.read().decode())
    except Exception:
        return None


def _post_json(url: str, data: dict, timeout: float = 3) -> dict | None:
    """POST JSON to URL. Returns response dict or None."""
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except Exception:
        return None


# ─── Bot State Tracker ────────────────────────────────────────────
class BotTracker:
    """Tracks health state for a single bot."""

    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.last_seen_ts = 0.0           # Last time we confirmed bot alive
        self.last_disconnect_ts = 0.0     # When last disconnect was detected
        self.last_restart_ts = 0.0        # When last restart was attempted
        self.restart_count = 0            # Consecutive restart attempts
        self.total_disconnects = 0        # Lifetime disconnect count
        self.total_restarts = 0           # Lifetime restart count
        self.uptime_start = 0.0           # When current uptime period started
        self.disconnect_history: deque = deque(maxlen=FORENSICS_MAX)
        self._was_connected = False       # For edge detection

    def mark_connected(self):
        now = time.time()
        if not self._was_connected:
            # Transition: disconnected → connected
            if self.last_disconnect_ts > 0:
                downtime = now - self.last_disconnect_ts
                logger.info(f"[{self.name}] RECONNECTED after {downtime:.1f}s downtime")
                _log_forensic({
                    "event": "reconnected",
                    "bot": self.name,
                    "downtime_s": round(downtime, 1),
                    "restart_attempts": self.restart_count,
                })
            self.uptime_start = now
            self.restart_count = 0  # Reset consecutive restart counter
        self._was_connected = True
        self.connected = True
        self.last_seen_ts = now

    def mark_disconnected(self, reason: str = "unknown"):
        now = time.time()
        if self._was_connected:
            # Transition: connected → disconnected
            uptime = now - self.uptime_start if self.uptime_start else 0
            self.total_disconnects += 1
            logger.warning(f"[{self.name}] DISCONNECTED — reason={reason}, "
                           f"uptime_was={uptime:.0f}s, total_disconnects={self.total_disconnects}")
            _log_forensic({
                "event": "disconnected",
                "bot": self.name,
                "reason": reason,
                "uptime_s": round(uptime, 1),
                "total_disconnects": self.total_disconnects,
                "time_of_day": datetime.now().strftime("%H:%M:%S"),
                "hour": datetime.now().hour,
            })
            self.disconnect_history.append({
                "ts": now,
                "reason": reason,
                "uptime_s": uptime,
            })
            _send_telegram(f"{self.name} bot DISCONNECTED (#{self.total_disconnects}) — {reason}")
        self._was_connected = False
        self.connected = False
        self.last_disconnect_ts = now

    def should_restart(self) -> bool:
        """Should we attempt a restart now?"""
        now = time.time()
        # Still in cooldown?
        if now - self.last_restart_ts < RESTART_COOLDOWN_S:
            return False
        # Hit max attempts? Use exponential backoff
        if self.restart_count >= RESTART_MAX_ATTEMPTS:
            backoff = BACKOFF_BASE_S * (2 ** (self.restart_count - RESTART_MAX_ATTEMPTS))
            backoff = min(backoff, 300)  # Cap at 5 minutes
            if now - self.last_restart_ts < backoff:
                return False
            # Log escalation
            logger.error(f"[{self.name}] {self.restart_count} restart attempts failed — "
                         f"backing off {backoff:.0f}s. Manual intervention may be needed.")
        return True

    def record_restart(self):
        now = time.time()
        self.restart_count += 1
        self.total_restarts += 1
        self.last_restart_ts = now
        logger.info(f"[{self.name}] Restart attempt #{self.restart_count} "
                     f"(total lifetime: {self.total_restarts})")
        _log_forensic({
            "event": "restart_attempt",
            "bot": self.name,
            "attempt": self.restart_count,
            "total_restarts": self.total_restarts,
        })

    def get_stats(self) -> dict:
        now = time.time()
        current_uptime = now - self.uptime_start if self.connected and self.uptime_start else 0
        return {
            "name": self.name,
            "connected": self.connected,
            "current_uptime_s": round(current_uptime, 1),
            "total_disconnects": self.total_disconnects,
            "total_restarts": self.total_restarts,
            "restart_count_current": self.restart_count,
            "last_seen_ago_s": round(now - self.last_seen_ts, 1) if self.last_seen_ts else None,
            "last_disconnect_ago_s": round(now - self.last_disconnect_ts, 1) if self.last_disconnect_ts else None,
            "recent_disconnects_1h": sum(
                1 for d in self.disconnect_history
                if now - d["ts"] < 3600
            ),
        }


# ─── Watchdog Core ────────────────────────────────────────────────
class Watchdog:
    """Main watchdog process — monitors and auto-restarts trading bots."""

    def __init__(self, bot_names: list[str], auto_restart: bool = True):
        self.trackers = {name: BotTracker(name) for name in bot_names}
        self.auto_restart = auto_restart
        self.bridge_alive = False
        self.dashboard_alive = False
        self._bridge_url = f"http://127.0.0.1:{HEALTH_HTTP_PORT}/health"
        self._dashboard_status_url = f"http://127.0.0.1:{DASHBOARD_PORT}/api/bot/status"
        self._dashboard_start_url = f"http://127.0.0.1:{DASHBOARD_PORT}/api/bot/start"
        self._start_time = time.time()
        self._checks = 0
        self._last_status_line = ""

    def check_bridge(self) -> dict | None:
        """Check bridge health. Returns health dict or None if unreachable."""
        health = _fetch_json(self._bridge_url, timeout=BRIDGE_TIMEOUT_S)
        was_alive = self.bridge_alive
        self.bridge_alive = health is not None

        if not self.bridge_alive and was_alive:
            logger.error("BRIDGE DOWN — health endpoint unreachable")
            _log_forensic({"event": "bridge_down"})
            _send_telegram("BRIDGE is DOWN — health endpoint unreachable!")
        elif self.bridge_alive and not was_alive:
            logger.info("BRIDGE UP — health endpoint responding")
            _log_forensic({"event": "bridge_up"})

        return health

    def check_dashboard(self) -> bool:
        """Check if dashboard is responding."""
        result = _fetch_json(
            f"http://127.0.0.1:{DASHBOARD_PORT}/api/bot/status",
            timeout=DASHBOARD_TIMEOUT_S,
        )
        was_alive = self.dashboard_alive
        self.dashboard_alive = result is not None

        if not self.dashboard_alive and was_alive:
            logger.warning("DASHBOARD DOWN — API unreachable")
            _log_forensic({"event": "dashboard_down"})
        elif self.dashboard_alive and not was_alive:
            logger.info("DASHBOARD UP — API responding")

        return self.dashboard_alive

    def check_bots(self, bridge_health: dict | None):
        """Check bot connectivity from bridge health data."""
        if bridge_health is None:
            # Bridge is down — mark all bots disconnected
            for tracker in self.trackers.values():
                if tracker.connected:
                    tracker.mark_disconnected("bridge_unreachable")
            return

        bots_connected = bridge_health.get("bots_connected", [])
        nt8_status = bridge_health.get("nt8_status", "unknown")
        tick_age = bridge_health.get("nt8_last_tick_age_s", 999)

        for name, tracker in self.trackers.items():
            if name in bots_connected:
                tracker.mark_connected()
            else:
                reason = "not_in_bots_connected"
                if nt8_status == "disconnected":
                    reason = "nt8_disconnected"
                elif tick_age > 30:
                    reason = f"nt8_stale_{tick_age:.0f}s"
                tracker.mark_disconnected(reason)

    def restart_bot(self, name: str):
        """Attempt to restart a bot via dashboard API."""
        tracker = self.trackers[name]

        if not tracker.should_restart():
            return

        tracker.record_restart()

        if not self.dashboard_alive:
            logger.warning(f"[{name}] Can't restart — dashboard is down")
            return

        # First try to stop any zombie process
        _post_json(self._dashboard_start_url.replace("/start", "/stop"),
                    {"name": name}, timeout=5)
        time.sleep(2)  # Brief pause between stop and start

        # Now start
        result = _post_json(self._dashboard_start_url, {"name": name}, timeout=5)
        if result and result.get("ok"):
            pid = result.get("pid", "?")
            logger.info(f"[{name}] Restart command sent — PID={pid}")
            _send_telegram(f"{name} bot restarted (attempt #{tracker.restart_count}, PID={pid})")
        elif result:
            error = result.get("error", "unknown")
            logger.warning(f"[{name}] Restart failed: {error}")
            # "already running" is fine — means the bot reconnected on its own
            if "already running" in str(error).lower():
                logger.info(f"[{name}] Bot is already running — may reconnect on its own")
        else:
            logger.error(f"[{name}] Restart request failed — no response from dashboard")

    def print_status(self, bridge_health: dict | None):
        """Print compact status line to console."""
        now = datetime.now().strftime("%H:%M:%S")
        parts = [f"[{now}]"]

        # Bridge
        if self.bridge_alive and bridge_health:
            nt8 = bridge_health.get("nt8_status", "?")
            tick_rate = bridge_health.get("tick_rate_10s", 0)
            parts.append(f"Bridge:OK NT8:{nt8} ticks:{tick_rate:.0f}/s")
        else:
            parts.append("Bridge:DOWN")

        # Bots
        for name, tracker in self.trackers.items():
            stats = tracker.get_stats()
            if tracker.connected:
                uptime = stats["current_uptime_s"]
                if uptime < 60:
                    up_str = f"{uptime:.0f}s"
                elif uptime < 3600:
                    up_str = f"{uptime/60:.0f}m"
                else:
                    up_str = f"{uptime/3600:.1f}h"
                parts.append(f"{name}:UP({up_str})")
            else:
                dc = stats["total_disconnects"]
                parts.append(f"{name}:DOWN(dc={dc},try={tracker.restart_count})")

        # Dashboard
        parts.append(f"Dash:{'OK' if self.dashboard_alive else 'DOWN'}")

        line = " | ".join(parts)
        if line != self._last_status_line:
            logger.info(line)
            self._last_status_line = line

    def run_once(self):
        """Single health check cycle."""
        self._checks += 1

        # 1. Check bridge
        bridge_health = self.check_bridge()

        # 2. Check dashboard
        self.check_dashboard()

        # 3. Check bots via bridge data
        self.check_bots(bridge_health)

        # 4. Auto-restart disconnected bots
        if self.auto_restart:
            for name, tracker in self.trackers.items():
                if not tracker.connected and self.bridge_alive:
                    self.restart_bot(name)

        # 5. Print status
        self.print_status(bridge_health)

    def run(self):
        """Main watchdog loop."""
        logger.info("=" * 60)
        logger.info("  PHOENIX WATCHDOG STARTED")
        logger.info(f"  Monitoring: {list(self.trackers.keys())}")
        logger.info(f"  Auto-restart: {self.auto_restart}")
        logger.info(f"  Poll interval: {POLL_INTERVAL_S}s")
        logger.info(f"  Restart cooldown: {RESTART_COOLDOWN_S}s")
        logger.info(f"  Forensics log: {FORENSICS_PATH}")
        logger.info("=" * 60)

        _send_telegram("Watchdog started — monitoring " + ", ".join(self.trackers.keys()))

        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Watchdog stopped by user")
                break
            except Exception as e:
                logger.error(f"Watchdog cycle error: {e}")
            time.sleep(POLL_INTERVAL_S)

    def get_report(self) -> dict:
        """Generate a full status report (for dashboard API)."""
        now = time.time()
        return {
            "watchdog_uptime_s": round(now - self._start_time, 1),
            "checks_completed": self._checks,
            "bridge_alive": self.bridge_alive,
            "dashboard_alive": self.dashboard_alive,
            "auto_restart": self.auto_restart,
            "bots": {name: tracker.get_stats() for name, tracker in self.trackers.items()},
        }


# ─── Disconnect Pattern Analyzer ─────────────────────────────────
def analyze_forensics(filepath: str = FORENSICS_PATH):
    """Read forensics log and print pattern analysis."""
    if not os.path.exists(filepath):
        print("No forensics log found.")
        return

    events = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue

    disconnects = [e for e in events if e.get("event") == "disconnected"]
    restarts = [e for e in events if e.get("event") == "restart_attempt"]
    reconnects = [e for e in events if e.get("event") == "reconnected"]

    print(f"\n{'=' * 60}")
    print(f"  DISCONNECT FORENSICS REPORT")
    print(f"  Log: {filepath}")
    print(f"  Events: {len(events)} total")
    print(f"{'=' * 60}\n")

    print(f"  Disconnects:  {len(disconnects)}")
    print(f"  Restarts:     {len(restarts)}")
    print(f"  Reconnects:   {len(reconnects)}")

    if disconnects:
        # Reason breakdown
        reasons: dict[str, int] = {}
        for d in disconnects:
            r = d.get("reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1

        print(f"\n  Disconnect reasons:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count}")

        # Hour-of-day breakdown
        hours: dict[int, int] = {}
        for d in disconnects:
            h = d.get("hour", -1)
            if h >= 0:
                hours[h] = hours.get(h, 0) + 1

        if hours:
            print(f"\n  Disconnects by hour:")
            for h in sorted(hours):
                bar = "#" * hours[h]
                print(f"    {h:02d}:00  {bar} ({hours[h]})")

        # Uptime before disconnect
        uptimes = [d.get("uptime_s", 0) for d in disconnects if d.get("uptime_s")]
        if uptimes:
            avg_up = sum(uptimes) / len(uptimes)
            min_up = min(uptimes)
            max_up = max(uptimes)
            print(f"\n  Uptime before disconnect:")
            print(f"    Avg: {avg_up:.0f}s  Min: {min_up:.0f}s  Max: {max_up:.0f}s")

        # Bot breakdown
        bots: dict[str, int] = {}
        for d in disconnects:
            b = d.get("bot", "?")
            bots[b] = bots.get(b, 0) + 1
        print(f"\n  Per-bot disconnects:")
        for bot, count in sorted(bots.items(), key=lambda x: -x[1]):
            print(f"    {bot}: {count}")

    if reconnects:
        downtimes = [r.get("downtime_s", 0) for r in reconnects if r.get("downtime_s")]
        if downtimes:
            avg_down = sum(downtimes) / len(downtimes)
            print(f"\n  Avg downtime before reconnect: {avg_down:.1f}s")

    print(f"\n{'=' * 60}\n")


# ─── Dashboard API Server (optional) ─────────────────────────────
def _start_api_server(watchdog: Watchdog, port: int = 5001):
    """Tiny HTTP server exposing watchdog status on :5001."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/status":
                body = json.dumps(watchdog.get_report(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/forensics":
                report = {"events": []}
                if os.path.exists(FORENSICS_PATH):
                    with open(FORENSICS_PATH, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                report["events"].append(json.loads(line.strip()))
                            except json.JSONDecodeError:
                                continue
                body = json.dumps(report, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass  # Suppress request logging

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Watchdog API on http://127.0.0.1:{port}/status")


# ─── Main ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Phoenix Bot Watchdog")
    parser.add_argument("--no-restart", action="store_true",
                        help="Monitor only — don't auto-restart bots")
    parser.add_argument("--bots", type=str, default="prod,sim",
                        help="Comma-separated bot names to watch (default: prod,sim). "
                             "B56 2026-04-22: prod_bot was dropped from default after "
                             "double-submit rejects when prod+sim hit the same 16 accounts. "
                             "2026-04-24 re-enable (Jennifer): prod_bot now routes ALL "
                             "strategies to Sim101; sim_bot routes to the 16 dedicated "
                             "SimXxx sub-accounts. Disjoint account sets → no double-submit. "
                             "Pass --bots sim to drop prod from the watch list.")
    parser.add_argument("--analyze", action="store_true",
                        help="Analyze disconnect forensics log and exit")
    parser.add_argument("--api-port", type=int, default=5001,
                        help="Watchdog API port (default: 5001)")
    args = parser.parse_args()

    if args.analyze:
        analyze_forensics()
        return

    bot_names = [b.strip() for b in args.bots.split(",") if b.strip()]
    watchdog = Watchdog(bot_names, auto_restart=not args.no_restart)

    # Start optional API server
    _start_api_server(watchdog, port=args.api_port)

    watchdog.run()


if __name__ == "__main__":
    main()
