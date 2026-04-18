"""
Phoenix Bot — Bridge Server

Central hub connecting NinjaTrader 8 to Python bots.

Architecture:
  NT8 TickStreamer.cs (WS client) → :8765 (this server) → :8766 → bots (WS clients)
  Bots send trade commands back → bridge → OIF files → NT8 incoming/ folder

Also exposes HTTP :8767/health for dashboard connectivity checks.
"""

import asyncio
import collections
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    NT8_WS_PORT, BOT_WS_PORT, HEALTH_HTTP_PORT,
    DISCONNECT_THRESHOLD_S, TICK_BUFFER_SIZE, FILE_FALLBACK_PATH,
    FILE_POLL_INTERVAL_S, LOG_DIR,
)
from bridge.oif_writer import write_oif, check_latest_fill

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)

# ─── Logging ────────────────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("Bridge")

# File loggers
_setup_file_logger = lambda name, path: (
    logging.getLogger(name).setLevel(logging.DEBUG),
    logging.getLogger(name).addHandler(logging.FileHandler(path)),
    setattr(logging.getLogger(name), "propagate", False),
    logging.getLogger(name),
)[-1]

conn_log = logging.getLogger("Connection")
conn_log.setLevel(logging.DEBUG)
conn_log.propagate = False
_ch = logging.FileHandler(os.path.join(LOG_DIR, "connection.log"))
_ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
conn_log.addHandler(_ch)
# Also log to console
_cc = logging.StreamHandler()
_cc.setFormatter(logging.Formatter("%(asctime)s  [CONN] %(message)s"))
conn_log.addHandler(_cc)

trade_log = logging.getLogger("Trades")
trade_log.setLevel(logging.DEBUG)
trade_log.propagate = False
_th = logging.FileHandler(os.path.join(LOG_DIR, "trades.log"))
_th.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
trade_log.addHandler(_th)


class BridgeServer:
    def __init__(self):
        # NT8 connection state
        self.nt8_ws = None
        self.nt8_connected = False
        self.nt8_instrument = None
        self.nt8_last_tick_time = 0.0
        self.nt8_connect_time = None

        # Bot connections
        self.bot_connections: dict[str, websockets.WebSocketServerProtocol] = {}
        self.bot_names: dict[int, str] = {}  # ws id -> name
        self.bot_heartbeats: dict[str, dict] = {}  # name -> {ts, status}

        # Tick buffer (ring buffer for late-connecting bots)
        self.tick_buffer = collections.deque(maxlen=TICK_BUFFER_SIZE)

        # Connection event log (for dashboard)
        self.connection_events: list[dict] = []
        self.max_events = 200

        # Stats
        self.start_time = time.time()
        self.ticks_received = 0
        self.ticks_forwarded = 0
        self.trades_executed = 0

        # Live activity metrics
        self.recent_tick_times: collections.deque = collections.deque(maxlen=100)
        self.last_trade_time: float = 0.0
        self.last_trade_action: str = ""

        # DOM depth state (updated by TickStreamer dom messages)
        self.dom_bid_stack: float = 0.0
        self.dom_ask_stack: float = 0.0
        self.dom_imbalance: float = 0.5
        self.dom_last_update: float = 0.0

    # ─── Connection Events ──────────────────────────────────────────
    def _log_event(self, level: str, message: str):
        event = {
            "ts": datetime.now().isoformat(),
            "level": level,
            "message": message,
        }
        self.connection_events.append(event)
        if len(self.connection_events) > self.max_events:
            self.connection_events.pop(0)
        conn_log.info(f"[{level.upper()}] {message}")

    def _log_disconnect_forensic(self, bot_name: str, reason: str):
        """Write a detailed forensic record when a bot disconnects."""
        try:
            now = time.time()
            forensic = {
                "ts": datetime.now().isoformat(),
                "event": "bot_disconnect",
                "bot": bot_name,
                "reason": reason,
                "bridge_uptime_s": round(now - self.start_time, 0),
                "ticks_received": self.ticks_received,
                "ticks_forwarded": self.ticks_forwarded,
                "tick_rate_10s": round(self._tick_rate_10s(), 1),
                "nt8_connected": self.nt8_connected,
                "nt8_tick_age_s": round(now - self.nt8_last_tick_time, 1) if self.nt8_last_tick_time else None,
                "bots_remaining": list(self.bot_connections.keys()),
                "hour": datetime.now().hour,
            }
            log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "disconnect_forensics.jsonl")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(forensic, default=str) + "\n")
        except Exception as e:
            logger.debug(f"Forensic log write failed: {e}")

    # ─── NT8 TCP Handler (port 8765) ──────────────────────────────────
    # NT8 connects via raw TCP (not WebSocket) because .NET Framework 4.8's
    # ClientWebSocket has a known bug where SendAsync silently drops data.
    # Protocol: newline-delimited JSON over TCP.
    async def handle_nt8_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        remote = writer.get_extra_info("peername")
        self._log_event("info", f"NT8 client connected from {remote}")
        self.nt8_connected = True
        self.nt8_connect_time = time.time()
        self.nt8_last_tick_time = time.time()

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # Connection closed

                message = line.decode("utf-8", errors="ignore").strip()
                if not message:
                    continue

                # Detect stale WebSocket client sending HTTP upgrade headers
                # (old MarketDataBroadcaster indicators still loaded on a chart)
                if message.startswith(("GET ", "POST ", "HTTP/", "Host:", "Upgrade:",
                                       "Connection:", "Sec-WebSocket")):
                    logger.debug(f"WebSocket handshake on TCP port — old indicator still loaded? Ignoring.")
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"Bad JSON from NT8: {message[:100]}")
                    continue

                msg_type = data.get("type", "")

                if msg_type == "connect":
                    self.nt8_instrument = data.get("instrument", "unknown")
                    self._log_event("info", f"NT8 instrument: {self.nt8_instrument}")

                elif msg_type == "heartbeat":
                    self.nt8_last_tick_time = time.time()

                elif msg_type == "tick":
                    self.nt8_last_tick_time = time.time()
                    self.ticks_received += 1
                    self.recent_tick_times.append(time.time())

                    # Buffer for late-connecting bots
                    self.tick_buffer.append(data)

                    # Send to bots (parallel with timeout)
                    msg = json.dumps(data)
                    await self._broadcast_to_bots(msg)

                    # Yield to event loop every tick — lets pong frames,
                    # health HTTP requests, and bot handlers run
                    await asyncio.sleep(0)

                    # Log every 200th tick for visibility
                    if self.ticks_received % 200 == 0:
                        price  = data.get("price", "?")
                        n_bots = len(self.bot_connections)
                        logger.info(f"[TICK #{self.ticks_received:,}] price={price} bots={n_bots}")

                elif msg_type == "dom":
                    # DOM depth snapshot from TickStreamer (throttled ~500ms)
                    self.dom_bid_stack = float(data.get("bid_stack", 0))
                    self.dom_ask_stack = float(data.get("ask_stack", 0))
                    total = self.dom_bid_stack + self.dom_ask_stack
                    self.dom_imbalance = (self.dom_bid_stack / total) if total > 0 else 0.5
                    self.dom_last_update = time.time()
                    await self._broadcast_to_bots(json.dumps(data))
                    await asyncio.sleep(0)

        except asyncio.IncompleteReadError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error(f"NT8 handler error: {e}")
        finally:
            self.nt8_connected = False
            writer.close()
            self._log_event("warn", f"NT8 disconnected from {remote}")

    # ─── Bot Handler (port 8766) ────────────────────────────────────
    async def handle_bot(self, websocket):
        remote = websocket.remote_address
        bot_id = id(websocket)
        bot_name = f"bot_{bot_id}"

        # Wait for bot identification message
        try:
            first_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            try:
                intro = json.loads(first_msg)
                if intro.get("type") == "identify":
                    bot_name = intro.get("name", bot_name)
            except json.JSONDecodeError:
                pass
        except asyncio.TimeoutError:
            pass

        self.bot_connections[bot_name] = websocket
        self.bot_names[bot_id] = bot_name
        self._log_event("info", f"Bot '{bot_name}' connected from {remote}")

        # Send buffered ticks to new bot
        if self.tick_buffer:
            logger.info(f"Sending {len(self.tick_buffer)} buffered ticks to {bot_name}")
            for tick in self.tick_buffer:
                try:
                    await websocket.send(json.dumps(tick))
                except Exception:
                    break

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"Bad JSON from {bot_name}: {message[:100]}")
                    continue

                msg_type = data.get("type", "")

                if msg_type == "trade":
                    await self._handle_trade_command(bot_name, data)

                elif msg_type == "heartbeat":
                    # Track bot liveness — watchdog reads this from health
                    self.bot_heartbeats[bot_name] = {
                        "ts": time.time(),
                        "status": data.get("status", "unknown"),
                    }

                elif msg_type == "status":
                    # Bot status update (for dashboard)
                    pass

        except websockets.exceptions.ConnectionClosed as e:
            disconnect_reason = f"WS closed: code={e.code} reason={e.reason or 'none'}"
            self._log_event("warn", f"Bot '{bot_name}' {disconnect_reason}")
        except Exception as e:
            err_type = type(e).__name__
            disconnect_reason = f"{err_type}: {e}"
            logger.error(f"Bot handler error ({bot_name}): {disconnect_reason}")
        else:
            disconnect_reason = "async_loop_ended"
        finally:
            self.bot_connections.pop(bot_name, None)
            self.bot_names.pop(bot_id, None)
            self.bot_heartbeats.pop(bot_name, None)
            self._log_event("warn", f"Bot '{bot_name}' disconnected ({disconnect_reason})")
            self._log_disconnect_forensic(bot_name, disconnect_reason)

    # ─── Broadcast to Bots ──────────────────────────────────────────
    async def _broadcast_to_bots(self, message: str):
        if not self.bot_connections:
            return

        # Parallel send with timeout — prevents one slow bot from blocking
        # the event loop (which starves ping/pong and health endpoint).
        items = list(self.bot_connections.items())
        if len(items) == 1:
            # Fast path: single bot, no gather overhead
            name, ws = items[0]
            try:
                await asyncio.wait_for(ws.send(message), timeout=2)
                self.ticks_forwarded += 1
            except Exception as e:
                err_type = type(e).__name__
                err_msg = str(e) or "no message"
                self.bot_connections.pop(name, None)
                self._log_event("warn", f"Bot '{name}' removed ({err_type}: {err_msg})")
            return

        tasks = []
        names = []
        for name, ws in items:
            tasks.append(asyncio.wait_for(ws.send(message), timeout=2))
            names.append(name)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                err_type = type(result).__name__
                err_msg = str(result) or "no message"
                self.bot_connections.pop(name, None)
                self._log_event("warn", f"Bot '{name}' removed ({err_type}: {err_msg})")
            else:
                self.ticks_forwarded += 1

    # ─── Trade Command Handler ──────────────────────────────────────
    async def _handle_trade_command(self, bot_name: str, data: dict):
        action = data.get("action", "").upper()
        qty = data.get("qty", 1)
        stop_price = data.get("stop_price")
        target_price = data.get("target_price")
        trade_id = data.get("trade_id", "")
        reason = data.get("reason", "")
        order_type = data.get("order_type", "MARKET")
        limit_price = data.get("limit_price", 0.0)

        trade_log.info(f"[TRADE CMD:{trade_id}] bot={bot_name} action={action} qty={qty} "
                       f"type={order_type} limit={limit_price if order_type == 'LIMIT' else 'N/A'} "
                       f"stop={stop_price} target={target_price} reason={reason}")

        paths = write_oif(action, qty, stop_price, target_price, trade_id=trade_id,
                          order_type=order_type, limit_price=limit_price)

        if not paths and action not in ("CANCEL_ALL", "CANCELALLORDERS"):
            trade_log.error(f"[OIF FAIL:{trade_id}] write_oif returned 0 files for {action}!")
        else:
            self.trades_executed += 1
            self.last_trade_time = time.time()
            self.last_trade_action = action

        # Brief wait for fill (non-blocking for cancel/exit)
        fill = None
        if action not in ("CANCEL_ALL", "CANCELALLORDERS"):
            await asyncio.sleep(1.0)
            fill = check_latest_fill(since_time=time.time() - 3)
            if fill:
                trade_log.info(f"[NT8 FILL:{trade_id}] {fill}")

        # Acknowledge back to bot — bot now checks this
        try:
            ws = self.bot_connections.get(bot_name)
            if ws:
                await ws.send(json.dumps({
                    "type": "trade_ack",
                    "trade_id": trade_id,
                    "action": action,
                    "files": [os.path.basename(p) for p in paths],
                    "oif_ok": len(paths) > 0,
                    "fill": fill,
                }))
        except Exception:
            pass

    # ─── Health Check ───────────────────────────────────────────────
    def _tick_rate_10s(self) -> float:
        now = time.time()
        # Snapshot the deque to avoid RuntimeError if event loop appends during iteration
        try:
            ticks = list(self.recent_tick_times)
        except RuntimeError:
            return 0.0
        return sum(1 for t in ticks if now - t <= 10) / 10.0

    def get_health(self) -> dict:
        """Build health snapshot. Thread-safe — called from the health HTTP
        thread while the asyncio event loop mutates bot_connections, etc.
        All collection reads use list() snapshots to avoid mutation errors.
        """
        now = time.time()
        nt8_age = now - self.nt8_last_tick_time if self.nt8_last_tick_time > 0 else -1

        if self.nt8_connected and nt8_age < 5:
            nt8_status = "live"
        elif self.nt8_connected and nt8_age < DISCONNECT_THRESHOLD_S:
            nt8_status = "stale"
        else:
            nt8_status = "disconnected"

        # Snapshot mutable collections (event loop may modify concurrently)
        try:
            bots = list(self.bot_connections.keys())
        except RuntimeError:
            bots = []
        try:
            events = list(self.connection_events[-20:])
        except (RuntimeError, IndexError):
            events = []

        return {
            "nt8_status": nt8_status,
            "nt8_connected": self.nt8_connected,
            "nt8_instrument": self.nt8_instrument,
            "nt8_last_tick_age_s": round(nt8_age, 1),
            "bots_connected": bots,
            "bots_count": len(bots),
            "ticks_received": self.ticks_received,
            "ticks_forwarded": self.ticks_forwarded,
            "trades_executed": self.trades_executed,
            "uptime_s": round(now - self.start_time, 0),
            "tick_rate_10s": round(self._tick_rate_10s(), 1),
            "last_trade_ago_s": round(now - self.last_trade_time, 0) if self.last_trade_time else None,
            "last_trade_action": self.last_trade_action,
            "dom_bid_stack": self.dom_bid_stack,
            "dom_ask_stack": self.dom_ask_stack,
            "dom_imbalance": round(self.dom_imbalance, 3),
            "dom_age_s": round(now - self.dom_last_update, 1) if self.dom_last_update else None,
            "connection_events": events,
            # Bot heartbeats — watchdog uses this to detect hung bots
            "bot_heartbeats": dict(self.bot_heartbeats),
        }

    # ─── NT8 Stale Watcher ──────────────────────────────────────────
    async def stale_watcher(self):
        """Monitor NT8 connection health. Log warnings on stale data."""
        was_stale = False
        while True:
            await asyncio.sleep(2)
            if self.nt8_last_tick_time == 0:
                continue

            age = time.time() - self.nt8_last_tick_time

            if age > DISCONNECT_THRESHOLD_S and not was_stale:
                self._log_event("error", f"NT8 data stale ({age:.0f}s) — switching to file fallback")
                was_stale = True
            elif age < 5 and was_stale:
                self._log_event("info", "NT8 data resumed (TCP live)")
                was_stale = False

    # ─── File Fallback Poller ───────────────────────────────────────
    async def file_fallback_poller(self):
        """Poll file-based fallback when WebSocket is stale."""
        last_mtime = 0
        while True:
            await asyncio.sleep(FILE_POLL_INTERVAL_S)

            # Only poll when WebSocket is stale
            age = time.time() - self.nt8_last_tick_time if self.nt8_last_tick_time > 0 else 999
            if age < DISCONNECT_THRESHOLD_S:
                continue

            try:
                if not os.path.exists(FILE_FALLBACK_PATH):
                    continue
                mtime = os.path.getmtime(FILE_FALLBACK_PATH)
                if mtime <= last_mtime:
                    continue
                last_mtime = mtime

                with open(FILE_FALLBACK_PATH, "r") as f:
                    data = json.load(f)

                # Convert file data to tick format and broadcast
                tick = {
                    "type": "tick",
                    "price": data.get("close", data.get("price", 0)),
                    "bid": data.get("bid", 0),
                    "ask": data.get("ask", 0),
                    "vol": data.get("volume", 0),
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "source": "file_fallback",
                }
                self.tick_buffer.append(tick)
                await self._broadcast_to_bots(json.dumps(tick))
                self.ticks_received += 1

            except Exception as e:
                if "No such file" not in str(e):
                    logger.debug(f"File fallback poll error: {e}")

    # ─── Health HTTP Server (threaded — never blocked by event loop) ──
    def start_health_server(self):
        """Start health HTTP server on a background thread.
        This MUST run on its own thread so the dashboard poller always
        gets a response, even when the asyncio event loop is saturated
        broadcasting ticks to bots.
        """
        bridge = self  # closure reference

        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(bridge.get_health()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass  # Suppress per-request logging

        server = HTTPServer(("127.0.0.1", HEALTH_HTTP_PORT), HealthHandler)
        server.timeout = 5
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"[OK] Health HTTP : http://127.0.0.1:{HEALTH_HTTP_PORT}/health (threaded)")

    # ─── Main ───────────────────────────────────────────────────────
    async def run(self):
        logger.info("=" * 60)
        logger.info("  PHOENIX BRIDGE SERVER")
        logger.info("=" * 60)

        # Start NT8 TCP server (raw TCP, not WebSocket — see TickStreamer v2.0)
        nt8_server = await asyncio.start_server(
            self.handle_nt8_tcp,
            "127.0.0.1",
            NT8_WS_PORT,
        )
        logger.info(f"[OK] NT8 server  : tcp://127.0.0.1:{NT8_WS_PORT}")
        self._log_event("info", f"Bridge started — NT8 TCP server on :{NT8_WS_PORT}")

        # Start Bot WebSocket server
        # Pings DISABLED on localhost — they cause false disconnects when the
        # event loop is busy broadcasting ticks (can't process pong in time).
        # On 127.0.0.1, dead connections are detected instantly by send() failure.
        bot_server = await websockets.serve(
            self.handle_bot,
            "127.0.0.1",
            BOT_WS_PORT,
            ping_interval=None,
            ping_timeout=None,
            max_queue=1024,
        )
        logger.info(f"[OK] Bot server  : ws://127.0.0.1:{BOT_WS_PORT}")
        self._log_event("info", f"Bot server on :{BOT_WS_PORT}")

        # Start Health HTTP server (on separate thread — immune to event loop saturation)
        self.start_health_server()

        logger.info("")
        logger.info("Waiting for NT8 to connect...")
        logger.info("(Start NinjaTrader, load TickStreamer indicator on MNQM6 chart)")
        logger.info("")

        # Run background tasks
        await asyncio.gather(
            nt8_server.wait_closed(),
            bot_server.wait_closed(),
            self.stale_watcher(),
            self.file_fallback_poller(),
        )


def main():
    bridge = BridgeServer()
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        logger.info("Bridge stopped by user (Ctrl+C)")


if __name__ == "__main__":
    main()
