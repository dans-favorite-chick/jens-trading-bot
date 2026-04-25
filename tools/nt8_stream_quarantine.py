"""
Phoenix Bot - NT8 Stream Quarantine Live Monitor (Phase B+ Section 1)

Subscribes to the bridge's bot port (:8766), feeds every tick through
the shared :class:`StreamValidator` singleton, and either:

  * ``--watch``: prints a 1-Hz refreshing per-port table forever, or
  * default mode: captures ticks for 10 seconds, prints a one-shot
    summary, exits.

Spec'd output table::

    port  | instrument | last_px  | peer_med | drift% | quarantined | reason
    ----- + ---------- + -------- + -------- + ------ + ----------- + ------
    55117 | MNQM6      | 27432.50 | 27432.50 |  +0.0% | NO          | -
    55779 | MNQM6      |  7192.25 | 27432.50 | -73.8% | YES         | static band (price < min for MNQ)

Caveats
-------

* The bridge's bot-fanout WebSocket payload does NOT carry the source
  NT8 client port. We can only run one of the three signals (static
  band + tick grid) against fanned-out ticks because cross-client MAD
  needs per-port partitioning. For full cross-client validation, point
  this at the bridge's per-port view via the StreamValidator singleton
  exposed by ``bridge.bridge_server`` once Section 2 wires it up.

* This tool does NOT physically disconnect the offending NT8 client;
  the bridge owns the socket. It only reads the validator state and
  presents it; quarantine enforcement at the bridge is gated on the
  ``PHOENIX_STREAM_VALIDATOR=1`` env flag (currently dormant).

Usage
-----

::

    python tools/nt8_stream_quarantine.py            # 10s capture + summary
    python tools/nt8_stream_quarantine.py --watch    # live 1-Hz table
    python tools/nt8_stream_quarantine.py --port 55117  # filter to one port
    python tools/nt8_stream_quarantine.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Make ``core.bridge.stream_validator`` importable when run from the
# tools/ subdirectory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.bridge.stream_validator import (  # noqa: E402
    StreamValidator,
    get_validator,
)

BRIDGE_BOT_WS = "ws://127.0.0.1:8766"
DEFAULT_CAPTURE_SECONDS = 10.0


# ─── Table rendering ────────────────────────────────────────────────

def _format_table(snap: dict, port_filter: Optional[int]) -> str:
    """Render the spec'd 7-column table from a health snapshot."""
    header = (
        "port  | instrument | last_px  | peer_med | drift% | "
        "quarantined | reason"
    )
    sep = (
        "----- + ---------- + -------- + -------- + ------ + "
        "----------- + ------"
    )
    rows = [header, sep]

    ports = snap.get("ports", {})
    if port_filter is not None:
        ports = {k: v for k, v in ports.items() if int(k) == port_filter}

    # Compute a cross-client peer median per instrument for display.
    by_inst: dict[str, list[float]] = {}
    for _, s in ports.items():
        med = s.get("median_30") or 0.0
        if med > 0:
            by_inst.setdefault(s.get("instrument", ""), []).append(med)
    inst_peer: dict[str, float] = {}
    for inst, meds in by_inst.items():
        if len(meds) >= 2:
            meds_sorted = sorted(meds)
            inst_peer[inst] = meds_sorted[len(meds_sorted) // 2]

    if not ports:
        rows.append("(no ports observed yet)")
        return "\n".join(rows)

    for port, s in sorted(ports.items()):
        inst = s.get("instrument", "") or "?"
        last_px = s.get("last_price") or 0.0
        own_med = s.get("median_30") or 0.0
        peer_med = inst_peer.get(inst, own_med)
        if peer_med > 0:
            drift = (own_med - peer_med) / peer_med * 100.0
            drift_str = f"{drift:+.1f}%"
        else:
            drift_str = "  -  "
        peer_med_str = f"{peer_med:.2f}" if peer_med > 0 else "  -  "
        q = "YES" if s.get("quarantined") else "NO "
        reason = s.get("reason") or "-"
        if reason == "ok":
            reason = "-"
        rows.append(
            f"{port:>5} | {inst:<10} | "
            f"{last_px:>8.2f} | {peer_med_str:>8} | {drift_str:>6} | "
            f"{q:<11} | {reason}"
        )
    return "\n".join(rows)


# ─── Bridge consumer ────────────────────────────────────────────────

async def _consume_bridge(
    validator: StreamValidator,
    port_hint: int = 0,
) -> None:
    """Connect to the bridge bot port; route every tick through the validator.

    The bridge fanout does NOT include source-port info, so we use
    ``port_hint`` (default 0) as a stand-in. To get true per-port
    visibility, run alongside the bridge once it shares its singleton
    via the env-flag wiring (Section 2).
    """
    try:
        import websockets  # type: ignore
    except ImportError:
        print("ERROR: websockets library not installed. "
              "Run: pip install websockets", file=sys.stderr)
        return
    while True:
        try:
            async with websockets.connect(BRIDGE_BOT_WS) as ws:
                await ws.send(json.dumps({"type": "identify",
                                          "name": "stream_quarantine_monitor"}))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") != "tick":
                        continue
                    inst = msg.get("instrument", "MNQM6") or "MNQM6"
                    try:
                        price = float(msg.get("price", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    validator.on_tick(
                        port=port_hint, instrument=inst, price=price
                    )
        except Exception as exc:
            print(
                f"[monitor] bridge consumer error: {exc!r} "
                f"— reconnecting in 3s",
                file=sys.stderr,
            )
            await asyncio.sleep(3)


# ─── Live --watch loop ──────────────────────────────────────────────

async def _watch_loop(
    validator: StreamValidator,
    port_filter: Optional[int],
    json_mode: bool,
    hz: float = 1.0,
) -> None:
    period = 1.0 / max(hz, 0.1)
    while True:
        snap = validator.health_snapshot()
        if json_mode:
            print(json.dumps(snap, default=str))
            sys.stdout.flush()
        else:
            os.system("cls" if sys.platform == "win32" else "clear")
            print(
                f"=== Phoenix NT8 Stream Quarantine "
                f"— {datetime.now():%H:%M:%S} ==="
            )
            print(_format_table(snap, port_filter))
            print(
                f"\n(bands: {snap['bands_loaded']}; "
                f"window_n={snap['window_n']}; "
                f"mad_threshold={snap['mad_threshold_pct']*100:.1f}%)"
            )
        await asyncio.sleep(period)


# ─── Default mode: 10s capture + summary ────────────────────────────

async def _capture_then_summary(
    validator: StreamValidator,
    port_filter: Optional[int],
    json_mode: bool,
    seconds: float = DEFAULT_CAPTURE_SECONDS,
) -> int:
    consumer = asyncio.create_task(_consume_bridge(validator))
    try:
        await asyncio.sleep(seconds)
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass

    snap = validator.health_snapshot()
    if json_mode:
        print(json.dumps(snap, default=str, indent=2))
    else:
        print(
            f"=== Phoenix NT8 Stream Quarantine "
            f"— {seconds:.0f}s capture summary ==="
        )
        print(_format_table(snap, port_filter))
    return 0


async def _watch_mode(
    validator: StreamValidator,
    port_filter: Optional[int],
    json_mode: bool,
) -> int:
    consumer = asyncio.create_task(_consume_bridge(validator))
    table = asyncio.create_task(
        _watch_loop(validator, port_filter, json_mode)
    )
    try:
        await asyncio.gather(consumer, table)
    except asyncio.CancelledError:
        pass
    return 0


# ─── CLI entry ──────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Live monitor for the Phoenix NT8 multi-stream validator. "
            "Default: 10s capture + summary. With --watch: 1-Hz "
            "refreshing table forever."
        )
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="Live 1-Hz refreshing table forever (Ctrl+C to exit).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Filter the table to one source port only.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit health snapshot as JSON instead of an ASCII table.",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_CAPTURE_SECONDS,
        help=(
            f"Capture window (default {DEFAULT_CAPTURE_SECONDS:.0f}s) "
            f"for non-watch mode."
        ),
    )
    args = p.parse_args()

    validator = get_validator()

    try:
        if args.watch:
            return asyncio.run(_watch_mode(validator, args.port, args.json))
        return asyncio.run(
            _capture_then_summary(
                validator, args.port, args.json, seconds=args.seconds
            )
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
