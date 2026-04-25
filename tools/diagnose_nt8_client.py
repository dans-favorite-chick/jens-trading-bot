"""
2026-04-25 diagnostic: identify which NT8-side component is sending data to
bridge :8765 by spying on what bridge fans out to bots on :8766.

Connects to bridge :8766 as a read-only "spy" bot named diag_spy. Listens for
any tick/dom message bridge forwards from the active NT8 client. The message
shape and field set unambiguously identifies the source:

    TickStreamer.cs (current architecture):
        tick:  has fields like bid, ask, bid_stack, ask_stack, cvd, ts
        dom:   has bid_stack, ask_stack arrays (DOM depth)

    JenTradingBotV1_DataFeed (legacy V1 indicator):
        bar:   has synthetic mom/prec/conf fields, secondary ES series

    OLDDONTUSEMarketDataBroadcasterv2 (legacy V2 strategy):
        bar:   bar-close JSON with computed bias, fake mom/prec

Bridge does NOT forward heartbeats to bots — only tick and dom messages.
On a closed Saturday market with no ticks, we'll see DOM updates only
(TickStreamer throttles them ~500ms). That's still enough to identify.

Usage: python tools/diagnose_nt8_client.py [--seconds 30]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter

try:
    import websockets
except ImportError:
    print("Need 'websockets' package. Install: pip install websockets", file=sys.stderr)
    sys.exit(2)

BRIDGE_BOT_URL = "ws://127.0.0.1:8766"
SPY_NAME = "diag_spy_2026_04_25"


def classify(sample: dict) -> str:
    """Best-effort component identification from a single tick/dom message."""
    keys = set(sample.keys())
    msg_type = sample.get("type", "")

    # TickStreamer v3 dom messages have bid_stack + ask_stack as scalars
    if msg_type == "dom" and {"bid_stack", "ask_stack"} <= keys:
        return "TickStreamer v3 (current architecture)"

    # TickStreamer v3 tick messages have price + bid/ask + bid_stack/ask_stack
    if msg_type == "tick" and {"price", "bid", "ask"} <= keys and "bid_stack" in keys:
        return "TickStreamer v3 (current architecture)"

    # Legacy V1 bar messages had momentum/precision synthetic fields
    if {"momentum", "precision"} <= keys or {"mom", "prec"} <= keys:
        return "JenTradingBotV1_DataFeed (LEGACY V1 indicator — should be deleted)"

    # Legacy V2 bar messages
    if msg_type == "bar" and {"close", "bias", "atr"} <= keys and "bid_stack" not in keys:
        return "MarketDataBroadcasterV2 (LEGACY V2 strategy — should be deleted)"

    # Generic tick missing TickStreamer's distinctive fields
    if msg_type == "tick" and "bid_stack" not in keys:
        return "Unknown tick source — no bid_stack field (probably legacy)"

    return f"Unrecognized: type={msg_type!r}, keys={sorted(keys)[:8]}"


async def spy(seconds: int) -> int:
    print(f"[DIAG] Connecting to bridge {BRIDGE_BOT_URL} as spy bot '{SPY_NAME}'")
    print(f"[DIAG] Listening for {seconds}s for any tick/dom messages...")
    print("[DIAG] (heartbeats are NOT forwarded to bots — we'll only see tick + dom)")
    print()

    try:
        async with websockets.connect(BRIDGE_BOT_URL) as ws:
            # Identify as read-only spy
            await ws.send(json.dumps({"type": "identify", "name": SPY_NAME}))

            seen_types: Counter = Counter()
            classifications: Counter = Counter()
            samples: dict[str, dict] = {}
            n = 0
            t_start = time.monotonic()
            t_end = t_start + seconds

            while time.monotonic() < t_end:
                remaining = max(0.5, t_end - time.monotonic())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                t = msg.get("type", "?")
                seen_types[t] += 1
                if t not in samples:
                    samples[t] = msg
                classifications[classify(msg)] += 1
                n += 1

                # Print first 5 messages verbatim so we see the actual format
                if n <= 5:
                    print(f"[MSG #{n}] {raw[:300]}")

            elapsed = time.monotonic() - t_start
            print()
            print("=" * 70)
            print(f"[DIAG] Window: {elapsed:.1f}s — captured {n} message(s)")
            print()
            print("Message type counts:")
            for t, c in seen_types.most_common():
                print(f"   {t:12s}  x{c}")
            print()
            print("Component identification (by message shape):")
            for kind, c in classifications.most_common():
                print(f"   x{c:4d}   {kind}")
            print()
            print("Sample message per type (full JSON):")
            for t, sample in samples.items():
                print(f"   --- type={t} ---")
                print(f"   {json.dumps(sample, indent=2, default=str)[:1500]}")
            print("=" * 70)

            # Verdict
            if not classifications:
                print("[VERDICT] No messages received in the window.")
                print("           If NT8 is connected on :8765, this means: either")
                print("           (a) the connected client is sending only heartbeats")
                print("           (which bridge does not forward), OR")
                print("           (b) the connected client speaks a different protocol")
                print("           and bridge is silently dropping its frames.")
                print("           Either way we still don't know who's connected — try")
                print("           a longer window or check bridge logs for the connect")
                print("           handshake (msg_type='connect' followed by instrument).")
                return 1

            top = classifications.most_common(1)[0][0]
            print(f"[VERDICT] Most-likely source: {top}")
            return 0

    except (ConnectionRefusedError, OSError) as e:
        print(f"[DIAG] Could not connect to bridge: {e}")
        print("       Is bridge_server.py running on :8766?")
        return 2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=int, default=30,
                   help="Capture window in seconds (default 30)")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return asyncio.run(spy(args.seconds))


if __name__ == "__main__":
    sys.exit(main())
