"""
Phoenix Bot - RiskGate watchdog

Monitors `heartbeat/risk_gate.hb`. If the file's mtime is older than
the threshold (default 2.0 s), the gate is presumed dead — write a
killswitch OIF that closes any open positions and cancels working
orders. Independent process from the gate so a crashed gate can't
prevent its own kill-switch.

Runs as a daemon. Starts automatically via Task Scheduler if the
operator wires it (see scripts/register_phoenix_grading_task.ps1
and the future register_risk_gate_task.ps1).

Killswitch is fail-CLOSED: when in doubt, refuse to trade and force
flat. Operator must manually clear by removing the resulting halt
file or restarting the gate.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PHOENIX_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_PATH = PHOENIX_ROOT / "heartbeat" / "risk_gate.hb"

logger = logging.getLogger("RiskWatchdog")


def heartbeat_age_s(path: Path) -> float:
    """Returns seconds since the heartbeat file was last touched.
    Returns +inf if the file doesn't exist."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return float("inf")


def fire_killswitch(outgoing_dir: str, working_orders: list[str]) -> str:
    """Write the killswitch OIF. Imports lazily so unit tests can
    monkeypatch even without core.risk.oif_writer importable."""
    from core.risk.oif_writer import write_killswitch
    return write_killswitch(working_orders, outgoing_dir)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--threshold", type=float, default=2.0,
                   help="Heartbeat staleness threshold (s)")
    p.add_argument("--poll", type=float, default=0.5,
                   help="Polling period (s)")
    p.add_argument("--outgoing", type=str,
                   default=r"C:\Users\Trading PC\Documents\NinjaTrader 8\incoming")
    p.add_argument("--once", action="store_true",
                   help="Single check then exit (for tests)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    logger.info(f"Watchdog: heartbeat={HEARTBEAT_PATH}, threshold={args.threshold}s")

    fired = False
    while True:
        age = heartbeat_age_s(HEARTBEAT_PATH)
        if age > args.threshold and not fired:
            logger.critical(f"FATAL: heartbeat age {age:.2f}s > threshold {args.threshold}s — firing killswitch")
            try:
                path = fire_killswitch(args.outgoing, working_orders=[])
                logger.critical(f"killswitch OIF written: {path}")
            except Exception as e:
                logger.critical(f"killswitch write FAILED: {e!r}")
            fired = True
        elif age <= args.threshold and fired:
            logger.warning("heartbeat resumed; staying armed but not re-firing")
            fired = False
        if args.once:
            return 0 if not fired else 2
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
