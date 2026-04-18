"""
tick_replayer.py — Weekend Bridge Tester

Connects to the bridge on 127.0.0.1:8765 exactly like TickStreamer does.
Sends synthetic MNQ ticks so the full pipeline (bridge → bots → strategies)
can be tested without NT8 or live market data.

The bridge sees this as a normal NT8 connection — no changes needed anywhere.

Usage:
    python tools/tick_replayer.py                    # default settings
    python tools/tick_replayer.py --speed 10         # 10 ticks/sec
    python tools/tick_replayer.py --speed 60         # 1 tick/sec (default)
    python tools/tick_replayer.py --start 19500      # starting price
    python tools/tick_replayer.py --session morning  # 08:30-10:00 CST sim
"""

import socket
import json
import time
import random
import argparse
import sys
from datetime import datetime, timezone, timedelta

HOST = "127.0.0.1"
PORT = 8765
INSTRUMENT = "MNQM6"
TICK_SIZE = 0.25


def build_tick(price: float, spread_ticks: int = 1) -> str:
    bid = round(price - (spread_ticks * TICK_SIZE), 2)
    ask = round(price + (spread_ticks * TICK_SIZE), 2)
    vol = random.randint(1, 8)
    ts  = datetime.now(timezone.utc).isoformat()
    return json.dumps({
        "type":  "tick",
        "price": price,
        "bid":   bid,
        "ask":   ask,
        "vol":   vol,
        "ts":    ts,
    })


def build_connect() -> str:
    return json.dumps({
        "type":       "connect",
        "instrument": INSTRUMENT,
        "ts":         datetime.now(timezone.utc).isoformat(),
    })


def build_heartbeat() -> str:
    return json.dumps({
        "type": "heartbeat",
        "ts":   datetime.now(timezone.utc).isoformat(),
    })


def send(sock: socket.socket, msg: str):
    sock.sendall((msg + "\n").encode("utf-8"))


def run(start_price: float, ticks_per_sec: float, duration_sec: int):
    interval = 1.0 / ticks_per_sec
    price    = start_price

    print(f"Tick Replayer — connecting to bridge at {HOST}:{PORT}...")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    try:
        sock.connect((HOST, PORT))
    except ConnectionRefusedError:
        print(f"ERROR: Could not connect to {HOST}:{PORT}")
        print("Is the bridge running?  →  python bridge/bridge_server.py")
        sys.exit(1)

    print(f"Connected. Sending {ticks_per_sec:.1f} ticks/sec for {duration_sec}s "
          f"(Ctrl+C to stop)")
    print(f"Starting price: {price:.2f}")
    print()

    # Identify as NT8 instrument
    send(sock, build_connect())
    send(sock, build_heartbeat())

    total_ticks  = 0
    last_hb      = time.time()
    last_report  = time.time()
    end_time     = time.time() + duration_sec if duration_sec > 0 else float("inf")

    try:
        while time.time() < end_time:
            tick_start = time.time()

            # ── Random walk — realistic MNQ micro-structure ───────────
            # Bias: slight mean reversion + momentum
            move_ticks = random.choices(
                [-2, -1, -1, 0, 0, 1, 1, 2],
                weights=[1, 3, 3, 2, 2, 3, 3, 1],
            )[0]
            price = round(price + (move_ticks * TICK_SIZE), 2)
            price = max(price, 15000.0)   # floor — won't go below 15k

            send(sock, build_tick(price))
            total_ticks += 1

            # Heartbeat every 3 seconds
            now = time.time()
            if now - last_hb >= 3.0:
                send(sock, build_heartbeat())
                last_hb = now

            # Progress report every 10 seconds
            if now - last_report >= 10.0:
                elapsed = now - (end_time - duration_sec) if duration_sec > 0 else now
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                      f"ticks={total_ticks:,}  price={price:.2f}")
                last_report = now

            # Pace to target tick rate
            elapsed = time.time() - tick_start
            sleep_for = interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    except KeyboardInterrupt:
        print(f"\nStopped by user after {total_ticks:,} ticks.")
    except BrokenPipeError:
        print(f"\nBridge disconnected after {total_ticks:,} ticks.")
    finally:
        sock.close()

    print(f"Done. Final price: {price:.2f}  Total ticks: {total_ticks:,}")


def main():
    parser = argparse.ArgumentParser(description="Phoenix Bridge Tick Replayer")
    parser.add_argument("--start",    type=float, default=19500.0,
                        help="Starting MNQ price (default: 19500)")
    parser.add_argument("--speed",    type=float, default=1.0,
                        help="Ticks per second (default: 1.0  |  use 10+ for fast bar building)")
    parser.add_argument("--duration", type=int,   default=0,
                        help="Run for N seconds then stop (default: 0 = run until Ctrl+C)")
    args = parser.parse_args()

    run(
        start_price    = args.start,
        ticks_per_sec  = args.speed,
        duration_sec   = args.duration,
    )


if __name__ == "__main__":
    main()
