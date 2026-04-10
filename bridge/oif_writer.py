"""
Phoenix Bot — OIF (Order Instruction File) Writer

Writes trade files to NT8's incoming folder. NT8 monitors this folder
and executes orders immediately. This is the proven reliable trade path.

OIF format: PLACE;Account;Instrument;Action;Qty;OrderType;LimitPrice;StopPrice;TIF;;;;
"""

import os
import glob
import logging
import time

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import OIF_INCOMING, OIF_OUTGOING, ACCOUNT, INSTRUMENT

logger = logging.getLogger("OIF")

_oif_counter = 0


def write_oif(action: str, qty: int = 1, stop_price: float = None, target_price: float = None) -> list[str]:
    """
    Write OIF file(s) to NT8 incoming folder.

    Args:
        action: ENTER_LONG, ENTER_SHORT, EXIT, CANCEL_ALL
        qty: Number of contracts (default 1)
        stop_price: Optional stop loss price (Phase 2: OCO brackets)
        target_price: Optional profit target price (Phase 2: OCO brackets)

    Returns:
        List of file paths written
    """
    global _oif_counter
    action = action.upper().strip()
    qty = max(1, int(qty))
    written = []

    cmds = []

    if action in ("ENTER_LONG", "BUY"):
        cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};MARKET;0;0;DAY;;;;\n")
        # Phase 2: OCO bracket orders
        if stop_price and target_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};STOP;{stop_price:.2f};0;DAY;;;;\n")
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};LIMIT;{target_price:.2f};0;DAY;;;;\n")

    elif action in ("ENTER_SHORT", "SELL"):
        cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};MARKET;0;0;DAY;;;;\n")
        if stop_price and target_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};STOP;{stop_price:.2f};0;DAY;;;;\n")
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};LIMIT;{target_price:.2f};0;DAY;;;;\n")

    elif action in ("EXIT", "EXIT_ALL", "CLOSE", "CLOSEPOSITION"):
        cmds.append(f"CLOSEPOSITION;{ACCOUNT};{INSTRUMENT};DAY;;;;;;;;;\n")

    elif action == "CANCEL_ALL":
        cmds.append(f"CANCELALLORDERS;{ACCOUNT};{INSTRUMENT};;;;;;;;;;;;\n")

    else:
        logger.warning(f"Unknown OIF action: {action}")
        return written

    os.makedirs(OIF_INCOMING, exist_ok=True)

    for cmd in cmds:
        _oif_counter += 1
        filepath = os.path.join(OIF_INCOMING, f"oif{_oif_counter}.txt")
        try:
            with open(filepath, "w") as f:
                f.write(cmd)
            written.append(filepath)
            logger.info(f"[OIF] {filepath} -> {cmd.strip()}")
        except Exception as e:
            logger.error(f"[OIF FAILED] {filepath}: {e}")

    return written


def check_fills() -> list[dict]:
    """
    Read fill confirmations from NT8's outgoing folder.

    Returns list of dicts: [{"file": "oif1.txt", "content": "FILLED;1;24219", "mtime": 1234567890.0}]
    """
    fills = []
    try:
        files = glob.glob(os.path.join(OIF_OUTGOING, "*.txt"))
        for f in sorted(files, key=os.path.getmtime, reverse=True)[:5]:
            try:
                content = open(f).read().strip()
                fills.append({
                    "file": os.path.basename(f),
                    "content": content,
                    "mtime": os.path.getmtime(f),
                })
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Could not read outgoing folder: {e}")
    return fills


def check_latest_fill() -> str | None:
    """Return the content of the most recent fill file, or None."""
    fills = check_fills()
    return fills[0]["content"] if fills else None


# ── CLI test mode ───────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="OIF Writer — test mode")
    parser.add_argument("--test", action="store_true", help="Write a test CLOSEPOSITION file")
    parser.add_argument("--action", default="EXIT", help="Action: ENTER_LONG, ENTER_SHORT, EXIT, CANCEL_ALL")
    parser.add_argument("--qty", type=int, default=1)
    args = parser.parse_args()

    if args.test:
        print(f"Writing test OIF to: {OIF_INCOMING}")
        paths = write_oif("CLOSEPOSITION", 1)
        print(f"Wrote: {paths}")
        time.sleep(1.5)
        fill = check_latest_fill()
        print(f"Latest fill: {fill}")
    else:
        paths = write_oif(args.action, args.qty)
        print(f"Wrote: {paths}")
