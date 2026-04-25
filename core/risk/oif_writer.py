"""
risk_gate-side OIF writer. ATOMIC: write to <name>.tmp, fsync, rename
to <name>.txt. Distinct from bridge/oif_writer.py because the gate
owns its own incrementing counter and adds a `risk_gate=phoenix_<pid>`
tag (operator can grep for gate-mediated orders).

Public API:
    write_place_oif(req, n) -> str      # absolute path of committed OIF
    write_killswitch(working_order_ids) -> str
    _next_oif_index() -> int
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

# Counter is process-global to avoid collisions across pipe handlers.
_counter_lock = threading.Lock()
_counter_value = int(time.time() * 1000) % 1000000   # avoid post-restart collision


def _next_oif_index() -> int:
    global _counter_value
    with _counter_lock:
        _counter_value += 1
        return _counter_value


def _build_place_line(req: dict) -> str:
    """Compose the NT8 OIF PLACE line.

    Spec (NT8 ATI):
        PLACE;<account>;<instrument>;<action>;<qty>;<order_type>;
              <limit_price>;<stop_price>;<tif>;<oco_id>;<atm>;;;
    13 fields total = 12 semicolons.
    """
    return ";".join([
        "PLACE",
        str(req["account"]),
        str(req["instrument"]),
        str(req["action"]),
        str(int(req["qty"])),
        str(req.get("order_type", "MARKET")),
        str(req.get("limit_price", "0")),
        str(req.get("stop_price", "0")),
        str(req.get("tif", "GTC")),
        str(req.get("oco_id", "")),
        str(req.get("atm_template", "")),
        "",
        "",
    ])


def write_place_oif(req: dict, outgoing_dir: str) -> str:
    """Atomically write a PLACE OIF. Returns absolute path."""
    n = _next_oif_index()
    pid = os.getpid()
    fname = f"oif{n}_phoenix_{pid}_riskgate.txt"
    final = Path(outgoing_dir) / fname
    tmp = final.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    line = _build_place_line(req) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except (OSError, AttributeError):
            pass
    os.replace(tmp, final)
    return str(final)


def write_killswitch(working_order_ids: list[str], outgoing_dir: str) -> str:
    """Emit a CLOSEPOSITION + CANCELALLORDERS pair. Used when the gate's
    own watchdog detects a stale heartbeat. Returns the path written."""
    n = _next_oif_index()
    pid = os.getpid()
    fname = f"oif{n}_phoenix_{pid}_killswitch.txt"
    final = Path(outgoing_dir) / fname
    tmp = final.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    lines = ["CANCELALLORDERS;;;;;;;;;;;;\n"]
    for oid in working_order_ids:
        lines.append(f"CANCEL;{oid};;;;;;;;;;;;\n")
    lines.append("CLOSEPOSITION;;;;;;;;;;;;\n")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        try:
            os.fsync(f.fileno())
        except (OSError, AttributeError):
            pass
    os.replace(tmp, final)
    return str(final)
