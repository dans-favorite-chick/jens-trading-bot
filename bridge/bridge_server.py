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
import time
from datetime import datetime, timezone

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

                    # Fan out to all connected bots
                    await self._broadcast_to_bots(json.dumps(data))

                    # Log every 1000th tick to avoid spam
                    if self.ticks_received % 1000 == 0:
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
                    # Forward to bots so aggregator can track it
                    await self._broadcast_to_bots(json.dumps(data))

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

                elif msg_type == "status":
                    # Bot status update (for dashboard)
                    pass

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"Bot handler error ({bot_name}): {e}")
        finally:
            self.bot_connections.pop(bot_name, None)
            self.bot_names.pop(bot_id, None)
            self._log_event("warn", f"Bot '{bot_name}' disconnected")

    # ─── Broadcast to Bots ──────────────────────────────────────────
    async def _broadcast_to_bots(self, message: str):
        if not self.bot_connections:
            return

        dead = []
        for name, ws in self.bot_connections.items():
            try:
                await ws.send(message)
                self.ticks_forwarded += 1
            except Exception:
                dead.append(name)

        for name in dead:
            self.bot_connections.pop(name, None)
            self._log_event("warn", f"Bot '{name}' removed (send failed)")

    # ─── Trade Command Handler ──────────────────────────────────────
    async def _handle_trade_command(self, bot_name: str, data: dict):
        action = data.get("action", "").upper()
        qty = data.get("qty", 1)
        stop_price = data.get("stop_price")
        target_price = data.get("target_price")
        trade_id = data.get("trade_id", "")
        reason = data.get("reason", "")

        trade_log.info(f"[TRADE CMD:{trade_id}] bot={bot_name} action={action} qty={qty} "
                       f"stop={stop_price} target={target_price} reason={reason}")

        paths = write_oif(action, qty, stop_price, target_price, trade_id=trade_id)
        self.trades_executed += 1
        self.last_trade_time = time.time()
        self.last_trade_action = action

        # Wait for fill confirmation
        await asyncio.sleep(1.5)
        fill = check_latest_fill()
        if fill:
            trade_log.info(f"[NT8 FILL] {fill}")

        # Acknowledge back to bot
        try:
            ws = self.bot_connections.get(bot_name)
            if ws:
                await ws.send(json.dumps({
                    "type": "trade_ack",
                    "action": action,
                    "files": [os.path.basename(p) for p in paths],
                    "fill": fill,
                }))
        except Exception:
            pass

    # ─── Health Check ───────────────────────────────────────────────
    def _tick_rate_10s(self) -> float:
        now = time.time()
        return sum(1 for t in self.recent_tick_times if now - t <= 10) / 10.0

    def get_health(self) -> dict:
        now = time.time()
        nt8_age = now - self.nt8_last_tick_time if self.nt8_last_tick_time > 0 else -1

        if self.nt8_connected and nt8_age < 5:
            nt8_status = "live"
        elif self.nt8_connected and nt8_age < DISCONNECT_THRESHOLD_S:
            nt8_status = "stale"
        else:
            nt8_status = "disconnected"

        return {
            "nt8_status": nt8_status,
            "nt8_connected": self.nt8_connected,
            "nt8_instrument": self.nt8_instrument,
            "nt8_last_tick_age_s": round(nt8_age, 1),
            "bots_connected": list(self.bot_connections.keys()),
            "bots_count": len(self.bot_connections),
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
            "connection_events": self.connection_events[-20:],
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

    # ─── Health HTTP Server ─────────────────────────────────────────
    async def health_handler(self, reader, writer):
        """Simple HTTP handler for /health endpoint."""
        try:
            request = await asyncio.wait_for(reader.read(1024), timeout=5)
            request_line = request.decode("utf-8", errors="ignore").split("\r\n")[0]

            body = json.dumps(self.get_health(), indent=2)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
                + body
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

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
        bot_server = await websockets.serve(
            self.handle_bot,
            "127.0.0.1",
            BOT_WS_PORT,
            ping_interval=20,
            ping_timeout=10,
        )
        logger.info(f"[OK] Bot server  : ws://127.0.0.1:{BOT_WS_PORT}")
        self._log_event("info", f"Bot server on :{BOT_WS_PORT}")

        # Start Health HTTP server
        health_server = await asyncio.start_server(
            self.health_handler,
            "127.0.0.1",
            HEALTH_HTTP_PORT,
        )
        logger.info(f"[OK] Health HTTP : http://127.0.0.1:{HEALTH_HTTP_PORT}/health")

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
