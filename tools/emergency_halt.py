#!/usr/bin/env python3
"""
Phoenix Bot — Emergency Halt

Creates a .HALT marker file that circuit_breakers.py watches for.
When detected, bots pause all new entries (existing position mgmt continues
per exit rules).

Usage:
  python tools/emergency_halt.py           # Create halt marker
  python tools/emergency_halt.py --clear   # Remove halt marker (resume)
  python tools/emergency_halt.py --status  # Check if halted

On halt:
  - Both prod and lab bots stop generating new entry signals
  - Existing positions continue to be managed (stops, targets, exits)
  - Telegram alert sent
  - Dashboard shows halted state

To fully stop (even existing position mgmt), kill the Python processes:
  tasklist | grep python
  taskkill //PID <pid> //F
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

PHOENIX_ROOT = Path(__file__).parent.parent
HALT_MARKER = PHOENIX_ROOT / "memory" / ".HALT"


def halt(reason: str = "") -> int:
    HALT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    content = f"halted at {datetime.now().isoformat()}\nreason: {reason or '(no reason given)'}\n"
    HALT_MARKER.write_text(content, encoding="utf-8")
    print(f"[HALT] Marker written: {HALT_MARKER}")
    print(f"[HALT] Bots will pause new entries on next circuit_breakers check (~5s).")
    print(f"[HALT] Existing positions continue to be managed per exit rules.")
    print(f"[HALT] To resume: python tools/emergency_halt.py --clear")
    return 0


def clear() -> int:
    if not HALT_MARKER.exists():
        print("[HALT] No halt marker present. Nothing to clear.")
        return 0
    HALT_MARKER.unlink()
    print(f"[HALT] Marker cleared. Bots will resume new entries on next check.")
    return 0


def status() -> int:
    if HALT_MARKER.exists():
        content = HALT_MARKER.read_text(encoding="utf-8")
        print(f"[HALT] ACTIVE")
        print(content)
        return 1
    else:
        print("[HALT] Not active. Bots free to trade.")
        return 0


def main():
    parser = argparse.ArgumentParser(description="Emergency halt for Phoenix bots")
    parser.add_argument("--clear", action="store_true", help="Remove halt marker")
    parser.add_argument("--status", action="store_true", help="Check halt status")
    parser.add_argument("--reason", type=str, default="", help="Reason for halt")
    args = parser.parse_args()

    if args.status:
        return status()
    if args.clear:
        return clear()
    return halt(args.reason)


if __name__ == "__main__":
    sys.exit(main())
