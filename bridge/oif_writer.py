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

import uuid as _uuid
_oif_counter = int(time.time() * 1000) % 1000000  # Start from timestamp to avoid restart collisions


def write_oif(action: str, qty: int = 1, stop_price: float = None,
              target_price: float = None, trade_id: str = "") -> list[str]:
    """
    Write OIF file(s) to NT8 incoming folder.

    Args:
        action: ENTER_LONG, ENTER_SHORT, EXIT, CANCEL_ALL
        qty: Number of contracts (default 1)
        stop_price: Optional stop loss price (Phase 2: OCO brackets)
        target_price: Optional profit target price (Phase 2: OCO brackets)
        trade_id: Unique trade ID for correlation

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
        # OCO bracket orders: stop + target protect position in NT8
        # OIF format: PLACE;Acct;Inst;Action;Qty;Type;LimitPrice;StopPrice;TIF
        if stop_price and target_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};STOP;0;{stop_price:.2f};DAY;;;;\n")
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};LIMIT;{target_price:.2f};0;DAY;;;;\n")

    elif action in ("ENTER_SHORT", "SELL"):
        cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};MARKET;0;0;DAY;;;;\n")
        if stop_price and target_price:
            cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};STOP;0;{stop_price:.2f};DAY;;;;\n")
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
            logger.info(f"[OIF:{trade_id or 'N/A'}] {filepath} -> {cmd.strip()}")
        except Exception as e:
            logger.error(f"[OIF FAILED] {filepath}: {e}")

    return written


def check_fills(since_time: float = 0) -> list[dict]:
    """
    Read fill confirmations from NT8's outgoing folder.

    Args:
        since_time: Only return fills newer than this timestamp

    Returns list of dicts with file, content, mtime
    """
    fills = []
    try:
        files = glob.glob(os.path.join(OIF_OUTGOING, "*.txt"))
        for f in sorted(files, key=os.path.getmtime, reverse=True)[:10]:
            try:
                mtime = os.path.getmtime(f)
                if since_time and mtime < since_time:
                    continue
                content = open(f).read().strip()
                fills.append({
                    "file": os.path.basename(f),
                    "content": content,
                    "mtime": mtime,
                })
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Could not read outgoing folder: {e}")
    return fills


def check_latest_fill(since_time: float = 0) -> str | None:
    """Return the content of the most recent fill file, or None."""
    fills = check_fills(since_time)
    return fills[0]["content"] if fills else None


async def wait_for_fill(trade_id: str, timeout_s: float = 5.0,
                        poll_interval: float = 0.3) -> dict:
    """
    Wait for fill confirmation with retry. Non-blocking async.

    Returns:
        {"status": "FILLED", "content": "...", "latency_ms": N}
        {"status": "TIMEOUT", "content": None}
        {"status": "REJECTED", "content": "..."}
    """
    import asyncio
    start = time.time()
    check_start = start  # Only look at fills after we sent the order

    while (time.time() - start) < timeout_s:
        fills = check_fills(since_time=check_start - 1)  # 1s buffer
        for fill in fills:
            content = fill["content"].upper()
            if "REJECT" in content or "ERROR" in content:
                logger.error(f"[OIF:{trade_id}] ORDER REJECTED: {fill['content']}")
                return {"status": "REJECTED", "content": fill["content"],
                        "latency_ms": (time.time() - start) * 1000}
            if "FILLED" in content or "PLACE" in content:
                latency = (time.time() - start) * 1000
                logger.info(f"[OIF:{trade_id}] Fill confirmed in {latency:.0f}ms: {fill['content']}")
                return {"status": "FILLED", "content": fill["content"],
                        "latency_ms": latency}
        await asyncio.sleep(poll_interval)

    logger.warning(f"[OIF:{trade_id}] No fill confirmation after {timeout_s}s")
    return {"status": "TIMEOUT", "content": None,
            "latency_ms": (time.time() - start) * 1000}


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
