"""
B77 Startup Reconciliation (2026-04-21)

Before the bot starts routing new signals, scan NT8's outgoing/*_position.txt
for every routed account. For every non-FLAT position found, reconstruct a
Position in PositionManager and attach a wide passive-protection OCO
(safety-net stop + target) so the orphan position can't drift unprotected.

Why this exists
---------------
Hard audit A2: Phoenix had NO startup reconciliation. A bot crash / restart
while a position was open left that position sitting in NT8 with no Phoenix-
side tracking AND no OCO protection. On restart, the bot would happily take
new trades while that orphan position drifted to an arbitrary loss.

Design contract
---------------
- Reconciled positions are flagged Position.reconciled=True.
- Strategy-side exit triggers MUST check `pos.reconciled` and skip those
  positions — they have no entry signal context to exit on.
- The safety-net OCO attached at reconcile time is the ONLY automatic exit
  path (plus DailyFlattener at 4pm CT). Operator closes manually otherwise.
- Strategy inference is best-effort: if no account → strategy mapping can
  be derived, we still adopt the position with strategy="_reconciled".

Public surface
--------------
    reconcile_positions_from_nt8(
        positions,                    # PositionManager
        outgoing_dir,                 # path to NT8 outgoing/
        instrument,                   # e.g. "MNQM6"
        routed_accounts,              # iterable[str]
        oco_writer=write_protection_oco,  # injectable for tests
        telegram_notify=None,         # optional callable(str)
        safety_stop_ticks=100,
        safety_target_ticks=150,
        tick_size=0.25,
    ) -> list[dict]                   # per-position adoption records
"""
from __future__ import annotations

import os
import uuid
import logging
from typing import Callable, Iterable, Optional

logger = logging.getLogger("StartupReconciliation")


def _infer_strategy_from_account(account: str) -> Optional[str]:
    """Reverse-lookup the strategy key for a given NT8 account name.

    Walks config.account_routing.STRATEGY_ACCOUNT_MAP. Returns None if no
    match (caller falls back to "_reconciled" placeholder).
    """
    try:
        from config.account_routing import STRATEGY_ACCOUNT_MAP
    except Exception:
        return None
    for strat_name, mapping in STRATEGY_ACCOUNT_MAP.items():
        if strat_name == "_default":
            continue
        if isinstance(mapping, str):
            if mapping == account:
                return strat_name
        elif isinstance(mapping, dict):
            for sub, acct in mapping.items():
                if acct == account:
                    return f"{strat_name}:{sub}"
    return None


def _iter_routed_accounts() -> list[str]:
    """Flatten STRATEGY_ACCOUNT_MAP values into a unique list of accounts."""
    try:
        from config.account_routing import STRATEGY_ACCOUNT_MAP
    except Exception:
        return []
    accounts: set[str] = set()
    for key, value in STRATEGY_ACCOUNT_MAP.items():
        if key == "_default":
            if isinstance(value, str):
                accounts.add(value)
            continue
        if isinstance(value, str):
            accounts.add(value)
        elif isinstance(value, dict):
            accounts.update(value.values())
    return sorted(accounts)


def _read_position_file(outgoing_dir: str, instrument: str,
                        account: str) -> Optional[tuple[str, int, float]]:
    """Read NT8's `{instrument} Globex_{account}_position.txt`.

    Returns (direction, qty, avg_price) for non-FLAT positions, else None.
    Format: `LONG;1;26741.25` or `FLAT;0;0`.
    """
    candidates = [
        os.path.join(outgoing_dir, f"{instrument} Globex_{account}_position.txt"),
        os.path.join(outgoing_dir, f"{instrument}_{account}_position.txt"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r") as f:
                content = f.read().strip()
        except OSError as e:
            logger.debug(f"[RECONCILE] read error {path}: {e}")
            continue
        parts = content.split(";")
        if len(parts) < 3:
            continue
        direction, qty_s, price_s = parts[0], parts[1], parts[2]
        if direction == "FLAT":
            return None
        if direction not in ("LONG", "SHORT"):
            logger.warning(f"[RECONCILE] unknown direction '{direction}' "
                           f"in {path} — skipping")
            return None
        try:
            qty = int(qty_s)
            avg_price = float(price_s)
        except ValueError:
            logger.warning(f"[RECONCILE] bad qty/price in {path}: {content!r}")
            return None
        if qty <= 0 or avg_price <= 0:
            return None
        return (direction, qty, avg_price)
    return None


def reconcile_positions_from_nt8(
    positions,
    outgoing_dir: str,
    instrument: str,
    routed_accounts: Optional[Iterable[str]] = None,
    oco_writer: Optional[Callable] = None,
    telegram_notify: Optional[Callable[[str], None]] = None,
    safety_stop_ticks: int = 100,
    safety_target_ticks: int = 150,
    tick_size: float = 0.25,
) -> list[dict]:
    """Scan NT8 outgoing/ and adopt every non-FLAT position.

    Returns a list of adoption records (one dict per position adopted).
    Each record contains {trade_id, account, direction, qty, avg_price,
    strategy, stop_price, target_price, oco_ok}.
    """
    if routed_accounts is None:
        routed_accounts = _iter_routed_accounts()

    # Lazy-import default OCO writer so tests can inject a mock without
    # triggering live NT8 path validation at import time.
    if oco_writer is None:
        from bridge.oif_writer import write_protection_oco as oco_writer  # noqa: E501

    adopted: list[dict] = []

    for account in routed_accounts:
        parsed = _read_position_file(outgoing_dir, instrument, account)
        if parsed is None:
            continue
        direction, qty, avg_price = parsed

        # Strategy inference (best-effort).
        inferred = _infer_strategy_from_account(account)
        strategy = inferred or "_reconciled"

        trade_id = f"RECONCILED_{account}_{uuid.uuid4().hex[:8]}"

        # Compute wide safety-net bracket.
        stop_offset = safety_stop_ticks * tick_size
        target_offset = safety_target_ticks * tick_size
        if direction == "LONG":
            stop_price = avg_price - stop_offset
            target_price = avg_price + target_offset
        else:
            stop_price = avg_price + stop_offset
            target_price = avg_price - target_offset

        ok = positions.open_position(
            trade_id=trade_id,
            direction=direction,
            entry_price=avg_price,
            contracts=qty,
            stop_price=stop_price,
            target_price=target_price,
            strategy=strategy,
            reason="reconciled_from_nt8",
            market_snapshot={"reconciled": True, "account": account},
            account=account,
            reconciled=True,
        )
        if not ok:
            logger.warning(
                f"[RECONCILE:{account}] positions.open_position refused "
                f"trade_id={trade_id} strategy={strategy} — probably a "
                f"duplicate strategy slot. Skipping safety-net OCO."
            )
            continue

        # Attach safety-net OCO. If it fails we leave the position open and
        # log LOUDLY — operator must flatten manually. Per task spec we do
        # NOT auto-close because we can't be sure this bot owns the position.
        oco_ok = False
        try:
            paths = oco_writer(
                direction=direction,
                qty=qty,
                stop_price=stop_price,
                target_price=target_price,
                trade_id=trade_id + "_protect",
                account=account,
            )
            oco_ok = bool(paths)
        except Exception as e:
            logger.error(f"[RECONCILE:{account}] safety-net OCO raised: {e!r}")

        msg = (
            f"[RECONCILED:{account}] adopted {direction} {qty} @ {avg_price} "
            f"from NT8 — safety-net OCO "
            f"{'attached' if oco_ok else 'FAILED (manual intervention)'} "
            f"stop={stop_price} target={target_price} strategy={strategy}"
        )
        if oco_ok:
            logger.info(msg)
        else:
            logger.error(msg)

        if telegram_notify is not None:
            try:
                emoji = "⚠️" if oco_ok else "🚨"
                status = "safety-net OCO attached" if oco_ok else "OCO FAILED — flatten manually"
                telegram_notify(
                    f"{emoji} Reconciled orphan {account} "
                    f"{direction} {qty}@{avg_price} — {status}"
                )
            except Exception as e:
                logger.debug(f"[RECONCILE] telegram notify failed: {e!r}")

        adopted.append({
            "trade_id": trade_id,
            "account": account,
            "direction": direction,
            "qty": qty,
            "avg_price": avg_price,
            "strategy": strategy,
            "stop_price": stop_price,
            "target_price": target_price,
            "oco_ok": oco_ok,
        })

    logger.info(
        f"[RECONCILE] scan complete — adopted {len(adopted)} orphan "
        f"position(s) across {len(list(routed_accounts))} routed account(s)"
    )
    return adopted
