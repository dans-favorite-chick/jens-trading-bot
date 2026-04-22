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

def _reject_live_account(account: str, caller: str) -> None:
    """
    B59 hard-guard: Phoenix must never route any order to the live account.
    Reads LIVE_ACCOUNT from env (populated from .env on bot startup via
    load_dotenv(override=True)). Raises RuntimeError on any match — caller
    is NOT expected to catch this; it's a fatal belt-and-suspenders check
    paired with disabling ATI on the live account in NT8 Control Center.

    To override (manual testing only), unset LIVE_ACCOUNT in the env before
    launching the bot.
    """
    live = os.environ.get("LIVE_ACCOUNT", "").strip()
    if live and str(account).strip() == live:
        raise RuntimeError(
            f"[LIVE_GUARD] {caller}: BLOCKED attempt to write order to "
            f"live account '{account}'. Phoenix must NEVER trade the live "
            f"account via OIF. If this was intentional manual testing, "
            f"unset LIVE_ACCOUNT in .env. Otherwise this is a routing bug."
        )


def _require_account(account: str | None, caller: str) -> str:
    """
    Phase 4C: PLACE/EXIT paths must carry an explicit NT8 account. A silent
    fallback to the module-level ACCOUNT masked routing bugs — every trade
    landed on Sim101 regardless of strategy. Raise loudly instead.

    B59: also blocks the live account (LIVE_ACCOUNT env var) at the
    narrowest choke-point so every PLACE/EXIT path is guarded in one place.
    """
    if account is None or not str(account).strip():
        raise ValueError(
            f"{caller}: account is required (Phase 4C multi-account routing). "
            f"Callers must resolve via config.account_routing."
            f"get_account_for_signal() and pass account=<SimFoo>."
        )
    _reject_live_account(account, caller)
    return account


def _build_entry_line(side: str, qty: int, order_type: str,
                      limit_price: float, stop_price: float,
                      oco_id: str = "",
                      account: str | None = None) -> str:
    """side: BUY or SELL. Returns OIF line (no newline).

    B3 compliance: stop-loss orders route via _build_stop_line() and emit
    STOPMARKET. The "STOP" branch here is for stop-limit entries (a
    distinct order type — requires both stop trigger + limit fill), kept
    for backwards-compat on callers that historically passed "STOP".

    4C: account must be explicit — routing per strategy/sub-strategy.
    """
    acct = _require_account(account, "_build_entry_line")
    ot = order_type.upper()
    if ot == "LIMIT":
        lp, sp = f"{limit_price:.2f}", "0"
    elif ot == "STOPMARKET":
        # Triggers at stop_price, fills at market — correct form per NT8 ATI.
        lp, sp = "0", f"{stop_price:.2f}"
    elif ot == "STOP":
        # Stop-limit (NOT stop-loss). Triggers at stop_price, fills at limit_price.
        # For stop-LOSS protection use _build_stop_line() which emits STOPMARKET.
        lp, sp = f"{limit_price:.2f}", f"{stop_price:.2f}"
    else:  # MARKET
        lp, sp = "0", "0"
    # B41: sim sub-accounts don't accept TIF=DAY; use GTC (same as stops/targets).
    return f"PLACE;{acct};{INSTRUMENT};{side};{qty};{ot};{lp};{sp};GTC;{oco_id};;;"


def _build_stop_line(side: str, qty: int, stop_price: float,
                     oco_id: str = "", tif: str = "GTC",
                     account: str | None = None) -> str:
    """Universal stop-loss = STOPMARKET. B3 fix: NT8 rejects bare "STOP" for
    stop-loss orders; "STOPMARKET" is the ATI-accepted form that triggers at
    stop_price and fills at market. side = SELL (protect LONG) or BUY (protect SHORT).

    4C: account must be explicit — routing per strategy/sub-strategy.
    """
    acct = _require_account(account, "_build_stop_line")
    return f"PLACE;{acct};{INSTRUMENT};{side};{qty};STOPMARKET;0;{stop_price:.2f};{tif};{oco_id};;;"


def _build_target_line(side: str, qty: int, target_price: float,
                       oco_id: str = "", tif: str = "GTC",
                       account: str | None = None) -> str:
    """Universal target = LIMIT.

    4C: account must be explicit — routing per strategy/sub-strategy.
    """
    acct = _require_account(account, "_build_target_line")
    return f"PLACE;{acct};{INSTRUMENT};{side};{qty};LIMIT;{target_price:.2f};0;{tif};{oco_id};;;"


# ═══════════════════════════════════════════════════════════════════════
# CANCEL builders — B4 (account-scoped) + B5 (single-order)
# ═══════════════════════════════════════════════════════════════════════

def cancel_all_orders_line(account: str = None) -> str:
    """Build account-scoped CANCELALLORDERS OIF line (B2 + B4 fix).

    NT8 ATI spec: CANCELALLORDERS has 13 semicolons (was 15 in the legacy
    broken form that included INSTRUMENT — which both over-scoped and
    rejected at parse time). The account field MUST be populated; the
    no-args form `CANCELALLORDERS;;;;;;;;;;;;;` cancels across EVERY
    NT8-connected account (including live brokerage accounts — verified
    2026-04-19 in test_05).

    Args:
        account: Target account name. Defaults to config.settings.ACCOUNT
                 (the bot's currently-connected account). NEVER permitted
                 to be empty or None — will raise ValueError.

    Returns:
        OIF line like `CANCELALLORDERS;Sim101;;;;;;;;;;;;` (13 semicolons).

    Raises:
        ValueError: if `account` resolves to empty string.
    """
    # B75 DEPRECATION: CANCELALLORDERS is fundamentally unsafe in Phoenix.
    # NT8 ATI ignores the account field and cancels orders on EVERY
    # connected account regardless of scoping. We preserve this function
    # for NT8's own consumption patterns but flag every call at ERROR
    # so any residual use surfaces loudly. Normal Phoenix exit flow no
    # longer calls this; OCO auto-cancel handles bracket cleanup when
    # EXIT MARKET flattens the position.
    logger.error(
        "[CANCELALLORDERS_DEPRECATED] Caller '%s' is invoking "
        "cancel_all_orders_line — NT8 ATI WILL cross-cancel this to "
        "every connected account. Switch to per-order CANCEL or drop "
        "the pre-exit cancel and rely on OCO auto-cancel. (B75)",
        "cancel_all_orders_line",
    )

    if account is None:
        # B58: NO fallback. Pre-B58 this silently defaulted to module-level
        # Sim101, which caused a CANCEL_ALL from one position's exit flow
        # to cancel unrelated orders on Sim101 (including prod_bot's
        # pending orders). Every caller must pass account explicitly.
        raise ValueError(
            "cancel_all_orders_line requires an explicit account. "
            "Pre-B58 code silently defaulted to Sim101 and cancelled "
            "orders on the wrong account. Pass account=<SimXxx>."
        )
    if not account or not str(account).strip():
        raise ValueError(
            "cancel_all_orders_line requires a non-empty account. "
            "The no-args form cancels across ALL NT8-connected accounts "
            "(including live brokerage) and is never an acceptable default."
        )
    # B59: block any cancel targeting the live account.
    _reject_live_account(account, "cancel_all_orders_line")
    # B44 fix: NT8 ATI rejected `CANCELALLORDERS;Sim101;;;;;;;;;;;;` with
    # "invalid # of parameters, should be 13 but is 14". NT8 wants 13
    # fields = 12 semicolons total (1 after CANCELALLORDERS + 11 trailing).
    return f"CANCELALLORDERS;{account};;;;;;;;;;;"


def cancel_single_order_line(order_id: str) -> str:
    """Build single-order CANCEL OIF line (B5 fix).

    NT8 ATI spec: `CANCEL;;;;;;;;;;<ORDER ID>;;<[STRATEGY ID]>` — ORDER ID
    at field position 10 (see Phase 1 verification findings). Returns the
    12-field form with ORDER ID populated and STRATEGY ID empty.

    Example: `cancel_single_order_line("oif_abc123")` →
             `"CANCEL;;;;;;;;;;oif_abc123;;"`

    Args:
        order_id: The ORDER ID of the order to cancel. Typically the
                  `trade_id` used as field 10 in the original PLACE OIF.

    Returns:
        OIF line ready to be written to NT8's incoming folder.

    Raises:
        ValueError: if `order_id` is empty — blank order_id would match
                    all NT8-tracked orders, same failure mode as B4.
    """
    if not order_id or not str(order_id).strip():
        raise ValueError("cancel_single_order_line requires a non-empty order_id")
    return f"CANCEL;;;;;;;;;;{order_id};;"


# ═══════════════════════════════════════════════════════════════════════
# Atomic file staging: write to .tmp, rename to .txt
# ═══════════════════════════════════════════════════════════════════════

def _stage_oif(cmd: str, trade_id: str, suffix: str = "") -> tuple[str, str]:
    """
    Write cmd directly to incoming/*.txt.

    B45 (rev 3): Earlier attempts at cross-directory staging (.tmp and .stage)
    both broke NT8's FileSystemWatcher — either producing read-error warnings
    or failing to trigger ATI consumption at all (os.replace from a sibling
    directory doesn't fire NT8's CREATE event reliably). NT8's Log-tab noise
    from the "Could not find file ...tmp" messages was cosmetic; ATI was
    still processing the .txt files correctly. Direct write wins: the
    partial-write window on a ~100-byte file is sub-millisecond and NT8
    reads the file only after its watcher sees a complete write event.
    """
    global _oif_counter
    _oif_counter += 1
    os.makedirs(OIF_INCOMING, exist_ok=True)
    tag = f"_{trade_id}" if trade_id else ""
    sfx = f"_{suffix}" if suffix else ""
    fname = f"oif{_oif_counter}{tag}{sfx}.txt"
    final_path = os.path.join(OIF_INCOMING, fname)
    # Return same path for both — _commit_staged becomes a no-op for the
    # "rename" step; file is already at its final location.
    with open(final_path, "w") as f:
        f.write(cmd + "\n")
    return final_path, final_path


def _commit_staged(staged: list[tuple[str, str]], trade_id: str) -> list[str]:
    """Rename all staged .tmp → .txt in order. Returns list of committed paths."""
    written = []
    for tmp, final in staged:
        try:
            if tmp != final:
                os.replace(tmp, final)  # legacy path
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
    account: str | None = None,   # 4C: required per-strategy NT8 account
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
    # B59 hard-guard at the top-level entrypoint — fails before any fs IO.
    if account:
        _reject_live_account(account, f"write_bracket_order[{trade_id}]")
    # 4C: surface missing account before any file IO.
    _require_account(account, "write_bracket_order")

    # OCO group ensures stop/target cancel each other
    if oco_id is None:
        oco_id = f"OCO_{trade_id or _uuid.uuid4().hex[:8]}"

    entry_side = "BUY" if direction == "LONG" else "SELL"
    exit_side = "SELL" if direction == "LONG" else "BUY"

    # Build lines
    entry_line = _build_entry_line(
        entry_side, qty, entry_type, entry_price, entry_price,  # stop-market entry uses entry_price as stop
        account=account,
    )
    stop_line = _build_stop_line(exit_side, qty, stop_price, oco_id=oco_id, account=account)
    target_line = (
        _build_target_line(exit_side, qty, target_price, oco_id=oco_id, account=account)
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

    # B46: Post-submit incoming-folder clearance check. If NT8 ATI consumed
    # the file, it disappears from incoming/ within milliseconds. If it's
    # still there 1s later, NT8 rejected it (bad TIF, bad account, etc.)
    # or the ATI server isn't running. Either way — red flag.
    _verify_consumed(written, trade_id, timeout_s=1.0)

    # B76: capture NT8 order_ids (best-effort) for later cancel+replace.
    try:
        if len(written) == len(commit_order):
            _ids = {}
            _soid = scan_outgoing_for_order_id(account, stop_price, timeout_s=0.5)
            if _soid:
                _ids["stop"] = _soid
            if target_price is not None:
                _toid = scan_outgoing_for_order_id(account, target_price, timeout_s=0.5)
                if _toid:
                    _ids["target"] = _toid
            if _ids:
                _recent_order_ids[trade_id] = _ids
    except Exception as _e:
        logger.debug(f"[OIF:{trade_id}] B76 order_id capture skipped: {_e}")

    return written


def _verify_consumed(paths: list[str], trade_id: str, timeout_s: float = 1.0) -> list[str]:
    """Check that NT8 consumed (deleted) the submitted OIF files within timeout.

    Returns a list of paths STILL PRESENT (i.e. NOT consumed — a red flag).
    Logs an error + emits a Telegram warning if anything is stuck.
    """
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        remaining = [p for p in paths if os.path.exists(p)]
        if not remaining:
            return []
        _time.sleep(0.1)
    stuck = [p for p in paths if os.path.exists(p)]
    if stuck:
        names = ", ".join(os.path.basename(p) for p in stuck)
        logger.error(
            f"[OIF_STUCK:{trade_id}] NT8 did NOT consume {len(stuck)} "
            f"OIF file(s) within {timeout_s}s: {names}. "
            f"ATI likely rejected — check NT8 Log tab."
        )
        try:
            from core.telegram_notifier import send_sync
            send_sync(
                f"⚠️ [OIF_STUCK] {trade_id}: {len(stuck)} file(s) not "
                f"consumed by NT8 — probable ATI rejection. "
                f"Check NT8 Log tab. Files: {names}",
                dedup_key="oif_stuck",
            )
        except Exception:
            pass
    return stuck


def verify_nt8_position(account: str, expected_direction: str, expected_qty: int,
                        instrument: str = None, timeout_s: float = 3.0) -> dict:
    """Read NT8's outgoing/ position file to verify a fill actually happened.

    Format NT8 writes: `outgoing/MNQM6 Globex_{account}_position.txt`
    containing a single line like `LONG;1;26741.25` or `FLAT;0;0`.

    Returns {status: "confirmed"|"wrong_direction"|"wrong_qty"|"flat"|"missing",
             observed_direction, observed_qty, observed_price}.
    """
    import time as _time
    inst = instrument or INSTRUMENT
    outgoing = os.path.join(os.path.dirname(OIF_INCOMING), "outgoing")
    # NT8 uses space between instrument and exchange suffix in filename
    candidates = [
        os.path.join(outgoing, f"{inst} Globex_{account}_position.txt"),
        os.path.join(outgoing, f"{inst}_{account}_position.txt"),
    ]
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        for path in candidates:
            if os.path.exists(path):
                try:
                    content = open(path).read().strip()
                    parts = content.split(";")
                    if len(parts) >= 3:
                        obs_dir, obs_qty, obs_price = parts[0], int(parts[1]), float(parts[2])
                        result = {
                            "observed_direction": obs_dir,
                            "observed_qty": obs_qty,
                            "observed_price": obs_price,
                        }
                        if obs_dir == "FLAT":
                            result["status"] = "flat"
                        elif obs_dir != expected_direction:
                            result["status"] = "wrong_direction"
                        elif obs_qty != expected_qty:
                            result["status"] = "wrong_qty"
                        else:
                            result["status"] = "confirmed"
                        return result
                except Exception as e:
                    logger.debug(f"[NT8_POS] read error {path}: {e}")
        _time.sleep(0.15)
    return {"status": "missing", "observed_direction": None,
            "observed_qty": 0, "observed_price": 0.0}


# ═══════════════════════════════════════════════════════════════════════
# B76: stop-modify via cancel + replace
# ═══════════════════════════════════════════════════════════════════════

# Module-level recent-order-id dict. Keys: trade_id. Values: dict with
# "stop" and/or "target" NT8 order_id strings. Populated by
# write_bracket_order / write_protection_oco on successful commit;
# consumed by base_bot to stash on Position.
_recent_order_ids: dict = {}


def scan_outgoing_for_order_id(account, expected_price, tolerance=0.01, timeout_s=2.5):
    """Poll outgoing/ for new order-status files matching a price.
    Returns NT8 order_id string if found, else None.

    NT8 writes outgoing/{account}_{order_id}.txt with content like
    'WORKING;0;26826.25'. Match on account prefix + price within tolerance.
    """
    outgoing_dir = os.path.join(os.path.dirname(OIF_INCOMING), "outgoing")
    import glob as _g
    pattern = os.path.join(outgoing_dir, f"{account}_*.txt")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        for fp in _g.glob(pattern):
            try:
                with open(fp) as _fh:
                    content = _fh.read().strip()
                parts = content.split(";")
                if len(parts) >= 3:
                    file_price = float(parts[2])
                    if abs(file_price - expected_price) <= tolerance:
                        fname = os.path.basename(fp)
                        oid = fname.replace(f"{account}_", "").replace(".txt", "")
                        return oid
            except Exception:
                pass
        time.sleep(0.15)
    return None


def write_modify_stop(direction, new_stop_price, n_contracts, trade_id, account, old_stop_order_id):
    """B76: cancel the existing STOPMARKET stop (by NT8 order_id) and
    immediately submit a replacement at new_stop_price. Brief unprotected
    window between cancel confirm and new-stop accepted is accepted.

    Returns list of OIF file paths written (2 on success) or [] on
    early-guard rejection.
    """
    _reject_live_account(account, f"write_modify_stop[{trade_id}]")
    if direction is None or direction.upper() not in ("LONG", "SHORT"):
        logger.error(f"[STOP_MODIFY:{trade_id}] invalid direction: {direction}")
        return []
    if n_contracts < 1:
        logger.error(f"[STOP_MODIFY:{trade_id}] invalid qty: {n_contracts}")
        return []
    if not old_stop_order_id or not str(old_stop_order_id).strip():
        logger.error(f"[STOP_MODIFY:{trade_id}] missing old_stop_order_id — can't cancel")
        return []

    exit_side = "SELL" if direction.upper() == "LONG" else "BUY"
    cancel_line = cancel_single_order_line(old_stop_order_id)
    new_stop_line = _build_stop_line(
        side=exit_side, qty=n_contracts, stop_price=new_stop_price, account=account,
    )

    staged = []
    try:
        staged.append(_stage_oif(cancel_line, trade_id, suffix="stop_cancel"))
        staged.append(_stage_oif(new_stop_line, trade_id, suffix="stop_replace"))
    except OSError as e:
        logger.error(f"[STOP_MODIFY:{trade_id}] stage failed: {e}")
        return []

    written = _commit_staged(staged, trade_id)
    if len(written) == len(staged):
        logger.info(
            f"[STOP_MODIFY:{trade_id}] cancelled {str(old_stop_order_id)[:12]} "
            f"+ submitted new STOPMARKET @ {new_stop_price:.2f}"
        )
    else:
        logger.error(
            f"[STOP_MODIFY:{trade_id}] partial commit: {len(written)}/{len(staged)}"
        )
    return written


# ═══════════════════════════════════════════════════════════════════════
# Legacy API (retained for existing callers — now uses STOPMARKET)
# ═══════════════════════════════════════════════════════════════════════

def write_protection_oco(direction: str, qty: int, stop_price: float,
                         target_price: float, trade_id: str,
                         account: str) -> list[str]:
    """
    B55: post-fill OCO protection. direction is the filled position side
    (LONG or SHORT); stop + target always go the OPPOSITE direction.
    Uses the same OCO_id on both legs so one fills cancels the other.

    B63: After the initial commit we verify NT8 consumed BOTH legs (the
    stop and the target file disappeared from incoming/). Three outcomes:

      - Both consumed → success, return [stop_path, target_path].
      - Exactly one consumed (half-success) → log [PROTECT_HALF], remove
        the stuck file, and re-stage the SAME leg exactly once. If the
        retry is also stuck, treat as failure and return [] so caller's
        3-retry loop engages (and ultimately flattens on full failure).
      - Neither consumed → treat as failure, return [] (caller retries).

    Jennifer's observation: winning trades going +20 points then
    reversing to LOSS without the LIMIT target triggering implies the
    target leg was NEVER working in NT8 — either it was rejected at
    submit (stuck in incoming/) or it was cancelled by an unrelated
    CANCEL_ALL. Half-success recovery addresses the former; the
    account-scoped CANCEL_ALL (B58) addresses the latter.
    """
    direction = direction.upper()
    if direction not in ("LONG", "SHORT"):
        logger.error(f"[PROTECT:{trade_id}] invalid direction: {direction}")
        return []
    # B59 hard-guard — fail before staging any protection files.
    _reject_live_account(account, f"write_protection_oco[{trade_id}]")
    exit_side = "SELL" if direction == "LONG" else "BUY"
    oco_id = f"OCO_{trade_id.split('_')[0]}"  # strip "_protect" suffix

    stop_line = _build_stop_line(exit_side, qty, stop_price,
                                 oco_id=oco_id, account=account)
    target_line = _build_target_line(exit_side, qty, target_price,
                                     oco_id=oco_id, account=account)

    staged = []
    try:
        staged.append(_stage_oif(stop_line, trade_id, suffix="stop"))
        staged.append(_stage_oif(target_line, trade_id, suffix="target"))
    except OSError as e:
        logger.error(f"[PROTECT:{trade_id}] stage failed: {e}")
        return []

    written = _commit_staged(staged, trade_id)
    if len(written) != 2:
        logger.error(f"[PROTECT:{trade_id}] commit produced only "
                     f"{len(written)}/2 files — treating as failure")
        for p in written:
            try: os.remove(p)
            except OSError: pass
        return []
    stop_path, target_path = written[0], written[1]
    logger.info(f"[PROTECT:{trade_id}] OCO committed: stop={stop_price:.2f} "
                f"target={target_price:.2f} oco={oco_id}")

    # B55/B63 bulletproof: verify NT8 consumed both files within 1.5s.
    stuck = _verify_consumed(written, trade_id, timeout_s=1.5)
    if not stuck:
        # B76: capture NT8 order_ids for later cancel+replace stop-modify.
        try:
            _ids = {}
            _soid = scan_outgoing_for_order_id(account, stop_price, timeout_s=0.5)
            if _soid:
                _ids["stop"] = _soid
            _toid = scan_outgoing_for_order_id(account, target_price, timeout_s=0.5)
            if _toid:
                _ids["target"] = _toid
            if _ids:
                _recent_order_ids[trade_id] = _ids
        except Exception as _e:
            logger.debug(f"[PROTECT:{trade_id}] B76 order_id capture skipped: {_e}")
        return written  # Both legs consumed — happy path.

    # If BOTH legs stuck → full failure; nothing is Working in NT8. Clean
    # up and let caller's retry loop engage.
    if len(stuck) == 2:
        logger.warning(f"[PROTECT:{trade_id}] BOTH legs stuck — NT8 "
                       f"rejected OCO pair; cleaning and returning failure")
        for p in stuck:
            try: os.remove(p)
            except OSError: pass
        return []

    # Half-success: exactly one leg consumed, the other stuck. This is the
    # Jennifer scenario: an unprotected leg is in NT8 and the position is
    # half-covered (e.g. stop Working but NO target → winner reverses to
    # loss without the LIMIT triggering, or vice versa).
    stuck_path = stuck[0]
    stuck_leg = "target" if stuck_path == target_path else "stop"
    working_leg = "stop" if stuck_leg == "target" else "target"
    logger.warning(
        f"[PROTECT_HALF:{trade_id}] {working_leg} leg consumed by NT8 but "
        f"{stuck_leg} leg stuck ({os.path.basename(stuck_path)}). "
        f"Position is half-protected — attempting single re-place of "
        f"{stuck_leg} leg."
    )
    try: os.remove(stuck_path)
    except OSError: pass

    # Re-stage the missing leg ONCE with a fresh oif counter. Same OCO id
    # so the already-Working leg still cancels when this one fills.
    try:
        retry_line = (target_line if stuck_leg == "target" else stop_line)
        retry_tmp, retry_final = _stage_oif(retry_line, trade_id,
                                            suffix=f"{stuck_leg}_retry")
    except OSError as e:
        logger.error(f"[PROTECT_HALF:{trade_id}] retry stage failed: {e} — "
                     f"position still half-protected, returning failure")
        try: os.remove(working_leg == "stop" and stop_path or target_path)
        except OSError: pass
        return []

    retry_written = _commit_staged([(retry_tmp, retry_final)], trade_id)
    if not retry_written:
        logger.error(f"[PROTECT_HALF:{trade_id}] retry commit failed — "
                     f"position still half-protected, returning failure")
        return []

    retry_stuck = _verify_consumed(retry_written, trade_id, timeout_s=1.5)
    if retry_stuck:
        logger.error(
            f"[PROTECT_HALF:{trade_id}] {stuck_leg} retry ALSO stuck — "
            f"giving up; caller's outer retry will re-place the full pair "
            f"or flatten. Cleaning up remaining files."
        )
        for p in retry_stuck:
            try: os.remove(p)
            except OSError: pass
        # Also remove the working leg so caller's outer retry starts clean
        # (otherwise we'd have a Working stop/target in NT8 that could
        # double-hit when the outer retry submits another OCO pair).
        working_path = stop_path if stuck_leg == "target" else target_path
        try:
            from bridge.oif_writer import cancel_all_orders_line
            cancel_line = cancel_all_orders_line(account=account)
            cancel_tmp, cancel_final = _stage_oif(cancel_line, trade_id,
                                                  suffix="half_cleanup")
            _commit_staged([(cancel_tmp, cancel_final)], trade_id)
            logger.warning(
                f"[PROTECT_HALF:{trade_id}] submitted CANCEL_ALL on "
                f"{account} to clear the lingering {working_leg} leg "
                f"before outer retry."
            )
        except Exception as e:
            logger.error(f"[PROTECT_HALF:{trade_id}] cleanup cancel failed: {e}")
        return []

    logger.info(
        f"[PROTECT_HALF:{trade_id}] {stuck_leg} retry consumed — OCO "
        f"now fully protected (stop + target Working in NT8)."
    )
    # Return both legs: the originally-working path + the retry path.
    working_path = stop_path if stuck_leg == "target" else target_path
    return [working_path, retry_written[0]]


def write_oif(action: str, qty: int = 1, stop_price: float = None,
              target_price: float = None, trade_id: str = "",
              order_type: str = "MARKET", limit_price: float = 0.0,
              account: str | None = None,
              direction: str | None = None) -> list[str]:
    """
    Legacy entrypoint. For new code prefer write_bracket_order().

    Args:
        action: ENTER_LONG, ENTER_SHORT, EXIT, CANCEL_ALL, CANCEL, PARTIAL_EXIT_*,
                PLACE_STOP_SELL, PLACE_STOP_BUY
        qty: Number of contracts (default 1)
        stop_price: Optional stop loss price (bracket OCO)
        target_price: Optional profit target price (bracket OCO)
        trade_id: Unique trade ID for correlation
        order_type: "MARKET" (default), "LIMIT", or "STOPMARKET"
        limit_price: Required when order_type="LIMIT". Price to limit entry at.
        account: NT8 account name (4C). REQUIRED for every action except
                 CANCEL (single-order by trade_id). For CANCEL_ALL, falls
                 back to module-level ACCOUNT with a WARNING log if omitted.

    Returns:
        List of file paths written.
    """
    global _oif_counter
    action = action.upper().strip()
    qty = int(qty)
    # B59 hard-guard at the legacy write_oif() entrypoint.
    if account:
        _reject_live_account(account, f"write_oif[{action}:{trade_id}]")
    if qty < 1 and action not in ("CANCEL_ALL", "CANCELALLORDERS", "EXIT",
                                   "EXIT_ALL", "CLOSE", "CLOSEPOSITION"):
        logger.error(f"[OIF:{trade_id or 'N/A'}] Refusing to write entry with qty={qty}")
        return []

    # 4C: enforce explicit account on every PLACE/EXIT path. Single-order
    # CANCEL doesn't need one; CANCEL_ALL has its own fallback+warn path.
    _PLACE_EXIT_ACTIONS = {
        "ENTER_LONG", "BUY", "ENTER_SHORT", "SELL",
        "EXIT", "EXIT_ALL", "CLOSE", "CLOSEPOSITION",
        "PARTIAL_EXIT_LONG", "PARTIAL_EXIT_SHORT",
        "PLACE_STOP_SELL", "PLACE_STOP_BUY",
    }
    if action in _PLACE_EXIT_ACTIONS:
        _require_account(account, f"write_oif action={action}")

    # B55: PLACE_PROTECTION — post-fill OCO stop+target attachment.
    if action == "PLACE_PROTECTION":
        if not direction or not stop_price or not target_price:
            logger.error(f"[PROTECT:{trade_id}] missing direction/stop/target")
            return []
        return write_protection_oco(
            direction=direction, qty=qty,
            stop_price=stop_price, target_price=target_price,
            trade_id=trade_id, account=account or ACCOUNT,
        )

    # Bracket path (legacy callers that still pass stop+target with entry —
    # B55 migrated the primary caller, base_bot, to split submit, but the
    # legacy bracket path remains for scripts/tests.)
    if action in ("ENTER_LONG", "BUY") and stop_price and target_price:
        return write_bracket_order(
            "LONG", qty, order_type, limit_price or 0.0,
            stop_price, target_price, trade_id, account=account,
        )
    if action in ("ENTER_SHORT", "SELL") and stop_price and target_price:
        return write_bracket_order(
            "SHORT", qty, order_type, limit_price or 0.0,
            stop_price, target_price, trade_id, account=account,
        )

    # Non-bracket path (legacy single-order)
    cmds = []
    if action in ("ENTER_LONG", "BUY"):
        cmds.append(_build_entry_line("BUY", qty, order_type, limit_price, 0.0, account=account) + "\n")
    elif action in ("ENTER_SHORT", "SELL"):
        cmds.append(_build_entry_line("SELL", qty, order_type, limit_price, 0.0, account=account) + "\n")
    elif action in ("EXIT", "EXIT_ALL", "CLOSE", "CLOSEPOSITION"):
        # B41: GTC universal — DAY rejected by 24/7 connections (Coinbase etc.)
        cmds.append(f"CLOSEPOSITION;{account};{INSTRUMENT};GTC;;;;;;;;;\n")
    elif action == "PARTIAL_EXIT_LONG":
        cmds.append(f"PLACE;{account};{INSTRUMENT};SELL;{qty};MARKET;0;0;GTC;;;;\n")
    elif action == "PARTIAL_EXIT_SHORT":
        cmds.append(f"PLACE;{account};{INSTRUMENT};BUY;{qty};MARKET;0;0;GTC;;;;\n")
    elif action == "PLACE_STOP_SELL":
        if stop_price:
            cmds.append(_build_stop_line("SELL", qty, stop_price, account=account) + "\n")
    elif action == "PLACE_STOP_BUY":
        if stop_price:
            cmds.append(_build_stop_line("BUY", qty, stop_price, account=account) + "\n")
    elif action == "CANCEL_ALL":
        # B75 HARD BLOCK: refuse to emit CANCELALLORDERS from any bot path.
        # NT8 ATI cross-cancels across every connected account regardless
        # of the account field. That's how the 2026-04-22 orphan-long
        # incidents happened (prod_bot Sim101 exit nuked sim_bot OCOs).
        # Callers that legitimately need per-order cancel should use the
        # CANCEL action with a captured NT8 order_id (B75 Option-A).
        logger.error(
            "[CANCEL_ALL_BLOCKED:%s] write_oif CANCEL_ALL refused (B75). "
            "NT8 ATI cross-cancels CANCELALLORDERS across every connected "
            "account. Use OCO auto-cancel (send EXIT MARKET) or per-order "
            "CANCEL by captured NT8 order_id.",
            trade_id or "N/A",
        )
        return []  # No OIF written — caller proceeds without explicit cancel
    elif action == "CANCEL":
        # B5: single-order cancel. trade_id doubles as ORDER ID here.
        if not trade_id:
            logger.error("[OIF] CANCEL action requires trade_id (maps to ORDER ID)")
            return []
        cmds.append(cancel_single_order_line(trade_id) + "\n")
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
                       trade_id: str = "",
                       account: str | None = None) -> list[str]:
    """Exit N contracts at market (partial close). 4C: account required."""
    action = "PARTIAL_EXIT_LONG" if direction.upper() == "LONG" else "PARTIAL_EXIT_SHORT"
    paths = write_oif(action, qty=n_contracts, trade_id=trade_id, account=account)
    logger.info(f"[OIF:PARTIAL_EXIT:{trade_id}] {direction} close {n_contracts}x -> {paths}")
    return paths


def write_be_stop(direction: str, stop_price: float, n_contracts: int = 1,
                  trade_id: str = "",
                  account: str | None = None) -> list[str]:
    """Place a standalone STOPMARKET at break-even for remaining contracts.
    4C: account required."""
    action = "PLACE_STOP_SELL" if direction.upper() == "LONG" else "PLACE_STOP_BUY"
    paths = write_oif(action, qty=n_contracts, stop_price=stop_price,
                      trade_id=trade_id, account=account)
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

    # CLI test mode defaults to module-level ACCOUNT since there's no signal
    # context here. Production callers must always pass account explicitly
    # via config.account_routing.get_account_for_signal().
    if args.test:
        print(f"Writing test OIF to: {OIF_INCOMING}")
        paths = write_oif("CLOSEPOSITION", 1, account=ACCOUNT)
        print(f"Wrote: {paths}")
        time.sleep(1.5)
        fill = check_latest_fill()
        print(f"Latest fill: {fill}")
    else:
        paths = write_oif(args.action, args.qty, account=ACCOUNT)
        print(f"Wrote: {paths}")
