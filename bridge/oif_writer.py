"""
Phoenix Bot — OIF (Order Instruction File) Writer

Writes trade files to NT8's incoming folder. NT8 monitors this folder
and executes orders immediately. This is the proven reliable trade path.

OIF format: PLACE;Account;Instrument;Action;Qty;OrderType;LimitPrice;StopPrice;TIF;OcoId;;;

Universal rules (roadmap v4):
- All stops = STOPMARKET (execution certainty over price precision)
- All targets = LIMIT (price precision over fill certainty)
- Bracket orders = staged atomic write: all .tmp files created first,
  then renamed to .txt in order (stop → target → entry). If any tmp
  write fails, nothing becomes visible to NT8.
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


# ═══════════════════════════════════════════════════════════════════════
# Low-level OIF line builders
# ═══════════════════════════════════════════════════════════════════════

def _build_entry_line(side: str, qty: int, order_type: str,
                      limit_price: float, stop_price: float,
                      oco_id: str = "") -> str:
    """side: BUY or SELL. Returns OIF line (no newline)."""
    ot = order_type.upper()
    if ot == "LIMIT":
        lp, sp = f"{limit_price:.2f}", "0"
    elif ot == "STOPMARKET":
        lp, sp = "0", f"{stop_price:.2f}"
    elif ot == "STOP":  # stop-limit; backwards compat
        lp, sp = f"{limit_price:.2f}", f"{stop_price:.2f}"
    else:  # MARKET
        lp, sp = "0", "0"
    return f"PLACE;{ACCOUNT};{INSTRUMENT};{side};{qty};{ot};{lp};{sp};DAY;{oco_id};;;"


def _build_stop_line(side: str, qty: int, stop_price: float,
                     oco_id: str = "", tif: str = "GTC") -> str:
    """Universal stop = STOPMARKET. side = SELL (protect LONG) or BUY (protect SHORT)."""
    return f"PLACE;{ACCOUNT};{INSTRUMENT};{side};{qty};STOPMARKET;0;{stop_price:.2f};{tif};{oco_id};;;"


def _build_target_line(side: str, qty: int, target_price: float,
                       oco_id: str = "", tif: str = "GTC") -> str:
    """Universal target = LIMIT."""
    return f"PLACE;{ACCOUNT};{INSTRUMENT};{side};{qty};LIMIT;{target_price:.2f};0;{tif};{oco_id};;;"


# ═══════════════════════════════════════════════════════════════════════
# Atomic file staging: write to .tmp, rename to .txt
# ═══════════════════════════════════════════════════════════════════════

def _stage_oif(cmd: str, trade_id: str, suffix: str = "") -> tuple[str, str]:
    """
    Write cmd to a .tmp file. Returns (tmp_path, final_path). NT8 only
    watches .txt — the .tmp file is invisible until os.rename flips it.
    """
    global _oif_counter
    _oif_counter += 1
    os.makedirs(OIF_INCOMING, exist_ok=True)
    tag = f"_{trade_id}" if trade_id else ""
    sfx = f"_{suffix}" if suffix else ""
    final_path = os.path.join(OIF_INCOMING, f"oif{_oif_counter}{tag}{sfx}.txt")
    tmp_path = final_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(cmd + "\n")
    return tmp_path, final_path


def _commit_staged(staged: list[tuple[str, str]], trade_id: str) -> list[str]:
    """Rename all staged .tmp → .txt in order. Returns list of committed paths."""
    written = []
    for tmp, final in staged:
        try:
            os.rename(tmp, final)
            written.append(final)
            logger.info(f"[OIF:{trade_id or 'N/A'}] committed {os.path.basename(final)}")
        except OSError as e:
            logger.error(f"[OIF:{trade_id}] commit failed {tmp}: {e}")
            # Best-effort cleanup of uncommitted .tmp files
            for remaining_tmp, _ in staged:
                if os.path.exists(remaining_tmp):
                    try:
                        os.remove(remaining_tmp)
                    except OSError:
                        pass
            return written
    return written


def _rollback_staged(staged: list[tuple[str, str]], trade_id: str):
    """Delete all uncommitted .tmp files."""
    for tmp, _ in staged:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
                logger.info(f"[OIF:{trade_id}] rolled back {os.path.basename(tmp)}")
            except OSError as e:
                logger.error(f"[OIF:{trade_id}] rollback failed {tmp}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# Public API: atomic bracket order
# ═══════════════════════════════════════════════════════════════════════

def write_bracket_order(
    direction: str,           # "LONG" or "SHORT"
    qty: int,
    entry_type: str,          # "LIMIT" | "STOPMARKET" | "MARKET"
    entry_price: float,       # Used when entry_type is LIMIT or STOPMARKET
    stop_price: float,
    target_price: float = None,   # None = managed exit (no bracket target)
    trade_id: str = "",
    oco_id: str = None,
) -> list[str]:
    """
    Atomic bracket order: stages entry + stop + target (or just entry + stop)
    to .tmp files, then commits all with atomic rename. If any stage fails,
    nothing reaches NT8.

    Order of commits: stop → target → entry. Protection is visible to NT8
    before entry so the fill-without-stop window is minimized.

    Note: NT8 may reject stop/target orders for non-existent positions, but
    bracketed orders linked by OCO group ID are accepted server-side and
    attach to the entry fill. Without OCO support, NT8 will queue them.
    """
    direction = direction.upper()
    if direction not in ("LONG", "SHORT"):
        logger.error(f"[OIF:{trade_id}] invalid direction: {direction}")
        return []
    if qty < 1:
        logger.error(f"[OIF:{trade_id}] refusing bracket with qty={qty}")
        return []

    # OCO group ensures stop/target cancel each other
    if oco_id is None:
        oco_id = f"OCO_{trade_id or _uuid.uuid4().hex[:8]}"

    entry_side = "BUY" if direction == "LONG" else "SELL"
    exit_side = "SELL" if direction == "LONG" else "BUY"

    # Build lines
    entry_line = _build_entry_line(
        entry_side, qty, entry_type, entry_price, entry_price  # stop-market entry uses entry_price as stop
    )
    stop_line = _build_stop_line(exit_side, qty, stop_price, oco_id=oco_id)
    target_line = (
        _build_target_line(exit_side, qty, target_price, oco_id=oco_id)
        if target_price is not None else None
    )

    # Stage
    staged = []
    try:
        staged.append(_stage_oif(entry_line, trade_id, suffix="entry"))
        staged.append(_stage_oif(stop_line, trade_id, suffix="stop"))
        if target_line is not None:
            staged.append(_stage_oif(target_line, trade_id, suffix="target"))
    except OSError as e:
        logger.error(f"[OIF:{trade_id}] stage failed: {e}")
        _rollback_staged(staged, trade_id)
        return []

    # Commit in order: stop first (so protection OIFs are on disk before entry),
    # then target, then entry. If entry commit fails, stop/target are orphaned
    # but NT8 will reject them for lack of position (harmless).
    # Commit ordering: index 0 = entry (hold), 1 = stop, 2 = target → we reorder:
    commit_order = []
    if len(staged) == 3:
        commit_order = [staged[1], staged[2], staged[0]]  # stop, target, entry
    else:
        commit_order = [staged[1], staged[0]]  # stop, entry

    written = _commit_staged(commit_order, trade_id)
    if len(written) != len(commit_order):
        logger.error(f"[OIF:{trade_id}] PARTIAL BRACKET COMMIT — check NT8 state")
    else:
        logger.info(f"[OIF:{trade_id}] bracket committed: {direction} qty={qty} "
                    f"entry={entry_type}@{entry_price:.2f} stop={stop_price:.2f} "
                    f"target={target_price if target_price else 'managed'} oco={oco_id}")
    return written


# ═══════════════════════════════════════════════════════════════════════
# Legacy API (retained for existing callers — now uses STOPMARKET)
# ═══════════════════════════════════════════════════════════════════════

def write_oif(action: str, qty: int = 1, stop_price: float = None,
              target_price: float = None, trade_id: str = "",
              order_type: str = "MARKET", limit_price: float = 0.0) -> list[str]:
    """
    Legacy entrypoint. For new code prefer write_bracket_order().

    Args:
        action: ENTER_LONG, ENTER_SHORT, EXIT, CANCEL_ALL
        qty: Number of contracts (default 1)
        stop_price: Optional stop loss price (bracket OCO)
        target_price: Optional profit target price (bracket OCO)
        trade_id: Unique trade ID for correlation
        order_type: "MARKET" (default), "LIMIT", or "STOPMARKET"
        limit_price: Required when order_type="LIMIT". Price to limit entry at.

    Returns:
        List of file paths written.
    """
    global _oif_counter
    action = action.upper().strip()
    qty = int(qty)
    if qty < 1 and action not in ("CANCEL_ALL", "CANCELALLORDERS", "EXIT",
                                   "EXIT_ALL", "CLOSE", "CLOSEPOSITION"):
        logger.error(f"[OIF:{trade_id or 'N/A'}] Refusing to write entry with qty={qty}")
        return []

    # Bracket path
    if action in ("ENTER_LONG", "BUY") and stop_price and target_price:
        return write_bracket_order(
            "LONG", qty, order_type, limit_price or 0.0,
            stop_price, target_price, trade_id,
        )
    if action in ("ENTER_SHORT", "SELL") and stop_price and target_price:
        return write_bracket_order(
            "SHORT", qty, order_type, limit_price or 0.0,
            stop_price, target_price, trade_id,
        )

    # Non-bracket path (legacy single-order)
    cmds = []
    if action in ("ENTER_LONG", "BUY"):
        cmds.append(_build_entry_line("BUY", qty, order_type, limit_price, 0.0) + "\n")
    elif action in ("ENTER_SHORT", "SELL"):
        cmds.append(_build_entry_line("SELL", qty, order_type, limit_price, 0.0) + "\n")
    elif action in ("EXIT", "EXIT_ALL", "CLOSE", "CLOSEPOSITION"):
        cmds.append(f"CLOSEPOSITION;{ACCOUNT};{INSTRUMENT};DAY;;;;;;;;;\n")
    elif action == "PARTIAL_EXIT_LONG":
        cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};SELL;{qty};MARKET;0;0;DAY;;;;\n")
    elif action == "PARTIAL_EXIT_SHORT":
        cmds.append(f"PLACE;{ACCOUNT};{INSTRUMENT};BUY;{qty};MARKET;0;0;DAY;;;;\n")
    elif action == "PLACE_STOP_SELL":
        if stop_price:
            cmds.append(_build_stop_line("SELL", qty, stop_price) + "\n")
    elif action == "PLACE_STOP_BUY":
        if stop_price:
            cmds.append(_build_stop_line("BUY", qty, stop_price) + "\n")
    elif action == "CANCEL_ALL":
        cmds.append(f"CANCELALLORDERS;{ACCOUNT};{INSTRUMENT};;;;;;;;;;;;\n")
    else:
        logger.warning(f"Unknown OIF action: {action}")
        return []

    # P1 fix: legacy write_oif() single-order path now uses the same atomic
    # staging (.tmp → os.rename to .txt) that write_bracket_order() uses.
    # Before this fix, the legacy path did a plain `open().write()` which
    # leaves NT8 seeing a half-written .txt if Python crashes between
    # open() and close() — the filesystem watcher can pick up a zero-byte
    # or truncated command. NT8 would either reject it silently or (worse)
    # parse a partial command. Staging to .tmp first and renaming on
    # success guarantees NT8 only ever sees fully-formed .txt files.
    #
    # Same helpers used by write_bracket_order: _stage_oif + _commit_staged.
    # If any .tmp write fails mid-batch, _commit_staged rolls back.
    staged = []
    try:
        for cmd in cmds:
            # _stage_oif expects the line without a trailing newline; strip
            # any newline the legacy callers may have appended (it re-adds
            # one in the .tmp write path).
            line = cmd[:-1] if cmd.endswith("\n") else cmd
            staged.append(_stage_oif(line, trade_id))
    except OSError as e:
        logger.error(f"[OIF:{trade_id or 'N/A'}] stage failed: {e}")
        _rollback_staged(staged, trade_id)
        return []

    written = _commit_staged(staged, trade_id)
    if len(written) != len(staged):
        logger.error(
            f"[OIF:{trade_id or 'N/A'}] PARTIAL LEGACY COMMIT — "
            f"{len(written)}/{len(staged)} files visible to NT8"
        )
    else:
        for path, cmd in zip(written, cmds):
            logger.info(f"[OIF:{trade_id or 'N/A'}] {path} -> {cmd.strip()}")
    return written


def write_partial_exit(direction: str, n_contracts: int = 1,
                       trade_id: str = "") -> list[str]:
    """Exit N contracts at market (partial close)."""
    action = "PARTIAL_EXIT_LONG" if direction.upper() == "LONG" else "PARTIAL_EXIT_SHORT"
    paths = write_oif(action, qty=n_contracts, trade_id=trade_id)
    logger.info(f"[OIF:PARTIAL_EXIT:{trade_id}] {direction} close {n_contracts}x -> {paths}")
    return paths


def write_be_stop(direction: str, stop_price: float, n_contracts: int = 1,
                  trade_id: str = "") -> list[str]:
    """Place a standalone STOPMARKET at break-even for remaining contracts."""
    action = "PLACE_STOP_SELL" if direction.upper() == "LONG" else "PLACE_STOP_BUY"
    paths = write_oif(action, qty=n_contracts, stop_price=stop_price, trade_id=trade_id)
    logger.info(f"[OIF:BE_STOP:{trade_id}] {direction} stop @ {stop_price:.2f} -> {paths}")
    return paths


def check_fills(since_time: float = 0) -> list[dict]:
    """Read fill confirmations from NT8's outgoing folder."""
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
    """Wait for fill confirmation with retry. Non-blocking async."""
    import asyncio
    start = time.time()
    check_start = start

    while (time.time() - start) < timeout_s:
        fills = check_fills(since_time=check_start - 1)
        for fill in fills:
            content = fill["content"].upper()
            filename = fill.get("file", "")
            if trade_id and trade_id in filename:
                pass
            elif trade_id and trade_id not in filename:
                if fill.get("mtime", 0) < check_start:
                    continue
            if "REJECT" in content or "ERROR" in content:
                logger.error(f"[OIF:{trade_id}] ORDER REJECTED: {fill['content']}")
                return {"status": "REJECTED", "content": fill["content"],
                        "latency_ms": (time.time() - start) * 1000}
            if "FILLED" in content:
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
