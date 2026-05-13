"""
Phoenix Bot — Position Manager (multi-position capable)

Tracks open positions, unrealized P&L, and manages stop/target exits.

Phase C (2026-04-21): refactored to multi-position storage keyed by
trade_id. Legacy single-position API (self.position, is_flat,
close_position(price, reason), check_exits) preserved for back-compat
so existing callers continue to work. New multi-position methods
expose active_positions, is_flat_for(strategy), get_position(trade_id),
close_all(), and trade_id-scoped variants of scale_out / move_stop /
close / check_exits.
"""

import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    TICK_SIZE,
    COMMISSION_PER_SIDE,
    EXCHANGE_FEES_PER_SIDE,
    SLIPPAGE_TICKS_PER_SIDE,
)

logger = logging.getLogger("PositionManager")

# MNQ: each tick (0.25) = $0.50
DOLLAR_PER_TICK = TICK_SIZE * 2


def compute_trade_costs(contracts: int) -> dict:
    """B13: total cost for one round-turn (entry + exit) of N contracts.

    Returns commission, exchange fees, slippage breakdown plus
    aggregated `fees_dollars` and `cost_total_dollars`.

    All multipliers are *2 (round-turn = entry + exit).
    """
    commission_total = 2 * COMMISSION_PER_SIDE * contracts
    exchange_total   = 2 * EXCHANGE_FEES_PER_SIDE * contracts
    slippage_total   = 2 * SLIPPAGE_TICKS_PER_SIDE * DOLLAR_PER_TICK * contracts
    fees_total       = commission_total + exchange_total
    cost_total       = fees_total + slippage_total
    return {
        "commission_dollars":    round(commission_total, 2),
        "exchange_fees_dollars": round(exchange_total, 2),
        "slippage_dollars":      round(slippage_total, 2),
        "fees_dollars":          round(fees_total, 2),
        "cost_total_dollars":    round(cost_total, 2),
    }

# ── P0.1: Trade memory persistence path (D13 fix) ────────────────────
# Resolved against project root (same directory layout used by
# core/trade_memory.py and dashboard/server.py's /api/today-pnl).
# Exposed at module level so tests can monkeypatch the path without
# touching the real file.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRADE_MEMORY_PATH = os.path.join(_PROJECT_ROOT, "logs", "trade_memory.json")


@dataclass
class Position:
    trade_id: str   # Unique ID flowing through the whole pipeline
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    entry_time: float
    contracts: int
    stop_price: float           # Current (live) stop — mutated by TRAIL/BE/chandelier
    target_price: float
    strategy: str
    reason: str
    market_snapshot: dict  # Snapshot of market data at entry

    # ── 2026-05-13 (fast-abort bug fix): preserve the ORIGINAL stop ─────
    # `stop_price` above gets mutated by TRAIL / BE_STOP / chandelier
    # logic. When BE_STOP computed `stop_dist = abs(entry - stop_price)`
    # AFTER a TRAIL had already moved stop close to entry, stop_dist
    # would be tiny (e.g. 1 tick), so BE-trigger at 0.5R fired at +1
    # tick of profit — locking in a 0-tick "stop" that exits on entry
    # noise. Forensically observed 2026-05-13 17:21 and 17:39 trades
    # closing in 8s and 20s with reason=stop_loss despite price moving
    # 0-2 ticks. See base_bot.py:1785-1805 for the BE_STOP site that now
    # reads this field instead of `stop_price`.
    initial_stop_price: float = 0.0   # Set by PositionManager.open_position

    # ── 2026-05-13 v2 (trail too-tight fix): high-water-mark tracker ───
    # The first fast-abort fix (commit 7f1411f) added a min-profit floor
    # before TRAIL fires + extended the grace window. But the underlying
    # `(entry+price)/2` midpoint formula still gave back 50% of every
    # unrealized peak — observed trade 398523b9 closed at +$0.18 / 10
    # ticks after peaking at +23 ticks, because the midpoint trail moved
    # stop to entry+11t and a normal 12t retrace clipped it. Research
    # confirmed midpoint trail isn't a published pattern. Replaced with
    # high-water-mark trail (Chandelier-style without ATR dependency):
    # stop = peak_favorable_price - trail_distance_ticks. This field
    # tracks the running peak so the trail anchors to the BEST price
    # seen, not the LIVE price. Initialized to entry_price at open.
    high_water_price: float = 0.0   # Set by PositionManager.open_position

    # ── P0.6 (D7) exit_pending state ───────────────────────────────
    # Flipped by mark_exit_pending(). While exit_pending is True, the
    # Position remains in the PositionManager (NOT yet closed) but is
    # blocked from new entries on the same account. Runtime
    # reconciliation transitions the position to "closed" (full
    # close_position / trade_history append) only once NT8 confirms
    # FLAT for the instrument+account. If exit_pending persists beyond
    # EXIT_PENDING_TIMEOUT_S, base_bot fires a CRITICAL alert + halts
    # the strategy so a lingering "thinks flat but isn't" divergence
    # never bleeds silently.
    exit_pending: bool = False
    exit_pending_since: float = 0.0   # unix epoch seconds
    pending_exit_price: float = 0.0
    pending_exit_reason: str = ""

    # ── 2026-05-04 (Sprint D F1): EXIT_TIMEOUT alert dedup state ────
    # Per-position one-shot flag for the EXIT_TIMEOUT escalation. Once
    # the first telegram has fired for this Position's stuck-exit
    # window, this stays True for the rest of its lifecycle. Hourly
    # rollups use _exit_timeout_last_alert_ts to spread one telegram
    # per hour while still stuck.
    _exit_timeout_alerted: bool = False
    _exit_timeout_last_alert_ts: float = 0.0

    # ── Phase 4C multi-account routing ─────────────────────────────
    # account is the NT8 sim account the entry was routed to; exit /
    # scale-out / BE-stop OIFs must use the same account.
    # sub_strategy is the opening_session sub-evaluator name (or None
    # for flat strategies) — used by resolver at exit time.
    account: str = "Sim101"
    sub_strategy: Optional[str] = None

    # ── Scale-out / Trend Rider state ───────────────────────────────
    original_contracts: int = 0    # Set at open (0 = not in rider mode)
    scaled_out: bool = False       # True once partial scale-out has been executed
    be_stop_active: bool = False   # True once stop moved to break-even
    rider_mode: bool = False       # True when holding remaining contract for trend

    # ── Managed-exit state (Noise Area, strategies with dynamic exits) ──
    exit_trigger: Optional[str] = None   # e.g. "price_returns_inside_noise_area"
                                         #      "chandelier_trail_3atr"
    eod_flat_time_et: Optional[str] = None
    metadata: dict = field(default_factory=dict)  # Strategy-specific (UB/LB for Noise Area)

    # ── Per-signal scale-out override (ORB=1.0R per Zarattini 2024) ─────
    scale_out_rr: Optional[float] = None

    # ── Chandelier trail config + live state ────────────────────────────
    trail_config: Optional[dict] = None           # {"atr_mult": 3.0, ...}
    trail_state: object = None                    # ChandelierTrailState instance

    # ── B77 startup reconciliation (2026-04-21) ─────────────────────────
    # True for positions adopted from NT8 outgoing/*_position.txt at boot.
    # Strategy-side exit triggers MUST NOT fire for reconciled positions —
    # they have no strategy context (no entry signal, no market snapshot).
    # They are managed only by the passive safety-net OCO attached at
    # reconcile time + DailyFlattener. Operator closes them manually.
    reconciled: bool = False

    # ── Sprint F (2026-05-04): tier classifier persistence ────────────
    # The signal-time tier ("A++"/"A"/"B"/"C") flowing through to the
    # closed-trade record so the indicator audit can answer empirically:
    # "does the A++/A/B/C ordering predict outcome?" Pre-Sprint-F trades
    # carry tier=None and remain backward-compatible.
    tier: Optional[str] = None

    # ── B76 stop-modify via cancel+replace (2026-04-21) ─────────────────
    # NT8-assigned order_ids captured by
    # bridge.oif_writer.scan_outgoing_for_order_id after bracket/protect
    # commit. Populated on the Position so subsequent stop-moves
    # (trail / BE / chandelier) can pass the id into write_modify_stop.
    # Empty string = not captured; callers log [STOP_MOVE_NO_ID] and
    # fall back to Python-only stop mutation.
    stop_order_id: str = ""
    target_order_id: str = ""


class PositionManager:
    """Multi-position manager keyed by trade_id.

    Legacy single-position callers use `.position` (returns the sole
    active position if there's exactly one, else the most-recently-
    opened one), `.is_flat`, and `.close_position(price, reason)` —
    these now operate on the sole active position when there's
    exactly one, matching pre-refactor semantics.

    Phase C multi-position callers use `.active_positions`,
    `.is_flat_for(strategy)`, `.get_position(trade_id)`, `.close_all()`,
    and trade_id-scoped variants.
    """

    def __init__(self, load_history: bool = False):
        # Canonical storage: trade_id -> Position
        self._positions: dict[str, Position] = {}
        # Trade history (closed trades) — list of trade dicts
        self.trade_history: list[dict] = []
        # Insertion-order tracker for "most-recently-opened" semantics
        # (used when legacy .position is called with multiple positions).
        self._open_order: list[str] = []
        # Fix A (2026-04-23): pending-entry tracker. When a LIMIT entry
        # OIF is submitted but the fill doesn't confirm within
        # wait_for_fill's 5s window, the base_bot took the ENTRY_PENDING
        # branch and returned without recording the pending order anywhere.
        # Next signal on the same account fired another entry → NT8 rejected
        # "Exceeds account's maximum position quantity" because the first
        # limit was still working. We now stash a small pending-entry
        # record per account so the signal-gate can see "entry in flight,
        # skip."  Key = account, value = {trade_id, strategy, direction,
        # limit_price, qty, submitted_at}. Entries older than
        # PENDING_ENTRY_TIMEOUT_S are considered expired (limit cancelled
        # by NT8 TIF or stale record) and ignored.
        self._pending_entries: dict[str, dict] = {}

        # ── P0.1 (D13): hydrate historical closed trades from disk ──────
        # Dashboard P&L and any consumer of .trade_history / .recent_trades()
        # used to reset to 0 on every restart. trade_memory.json is the
        # durable source of truth; load it into memory at init time.
        # Graceful: missing -> empty+INFO; corrupt -> empty+WARNING.
        if load_history:
            self._load_trade_history()

    def _load_trade_history(self) -> None:
        """Populate self.trade_history from the merged trade_memory view.

        Routes through core.trade_memory.load_all_trades() which reads the
        legacy logs/trade_memory.json (pre-2026-05-12 shared history) AND
        every per-bot logs/trade_memory_<bot>.json file, deduping by
        trade_id with per-bot files winning on collision.

        Before 2026-05-13 this method raw-opened TRADE_MEMORY_PATH directly
        — which silently lost every trade written to the per-bot files after
        the 2026-05-12 trade_memory file split (commit 02b0efd). Result:
        bot startup hydrated only pre-split history, so prod's first
        post-restart trade looked like the bot's first trade ever for
        anything reading PositionManager.trade_history.

        Graceful failure modes preserved:
        - Missing dir / no files at all: stays empty, INFO log.
        - Legacy file corrupt / wrong shape: stays empty, WARNING log.
        - Success: full schema preserved per-row; INFO log with count.

        Module-level TRADE_MEMORY_PATH is still read at call-time so tests
        can monkeypatch position_manager.TRADE_MEMORY_PATH. Its parent
        directory becomes the load_all_trades logs_dir — tests that
        monkeypatch to tmp_path/trade_memory.json get tmp_path as logs_dir,
        preserving the original test ergonomics.
        """
        # Resolve lazily so tests can monkeypatch the module attribute.
        path = sys.modules[__name__].TRADE_MEMORY_PATH
        logs_dir = os.path.dirname(path)
        legacy_exists = os.path.exists(path)

        try:
            from core.trade_memory import load_all_trades
            rows = load_all_trades(logs_dir=logs_dir)
        except Exception as e:
            logger.warning(
                "[TRADE_MEMORY] load_all_trades(%s) failed (%s: %s) — "
                "starting fresh", logs_dir, type(e).__name__, e,
            )
            return

        # Diagnostic on the legacy file: if it exists but produced no
        # trades via load_all_trades, it's likely corrupt or wrong-shape
        # (load_all_trades skips unparseable files silently). Surface that
        # at WARNING so it doesn't hide. Matches pre-refactor semantics.
        if legacy_exists and not rows:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    logger.warning(
                        "[TRADE_MEMORY] %s did not contain a JSON list "
                        "(got %s) — starting fresh",
                        path, type(data).__name__,
                    )
                    return
                # Empty list is benign — fall through to INFO log below.
            except Exception as e:
                logger.warning(
                    "[TRADE_MEMORY] failed to load %s (%s: %s) — "
                    "starting fresh", path, type(e).__name__, e,
                )
                return

        if not rows:
            logger.info(
                "[TRADE_MEMORY] no trade_memory.json found at %s — "
                "starting fresh", path,
            )
            return

        # Schema-preserving: keep rows as-is; downstream consumers key off
        # pnl_dollars / exit_time / bot_id / strategy which already exist
        # (verified against 968-row live file 2026-04-22).
        self.trade_history = list(rows)
        logger.info(
            "[TRADE_MEMORY] loaded %d historical trades from %s "
            "(legacy + per-bot files merged)",
            len(self.trade_history), logs_dir,
        )

    # ─── Legacy single-position API (back-compat) ──────────────────────

    @property
    def position(self) -> Position | None:
        """Legacy: returns the sole active position, or the most-recently-
        opened if multiple are open, or None if flat.

        In single-position mode (pre-Phase-C runtime) this returns exactly
        what callers expect. In multi-position mode, callers should migrate
        to `.get_position(trade_id)` or `.active_positions`.
        """
        if not self._positions:
            return None
        if len(self._positions) == 1:
            return next(iter(self._positions.values()))
        # Multiple open — return most recently opened (insertion order)
        last_id = self._open_order[-1]
        return self._positions.get(last_id)

    @property
    def is_flat(self) -> bool:
        """Legacy: True if NO positions are open anywhere."""
        return len(self._positions) == 0

    @property
    def is_long(self) -> bool:
        """Legacy: True if the sole active position is LONG."""
        pos = self.position
        return pos is not None and pos.direction == "LONG"

    @property
    def is_short(self) -> bool:
        """Legacy: True if the sole active position is SHORT."""
        pos = self.position
        return pos is not None and pos.direction == "SHORT"

    # ─── Multi-position API (Phase C) ──────────────────────────────────

    @property
    def active_positions(self) -> list[Position]:
        """All currently-open positions."""
        return list(self._positions.values())

    @property
    def active_count(self) -> int:
        return len(self._positions)

    def is_flat_for(self, strategy: str) -> bool:
        """True if the given strategy has NO active position.

        Used by _evaluate_strategies in multi-position runtime to allow
        each strategy to independently enter when its own slot is free.
        """
        for pos in self._positions.values():
            if pos.strategy == strategy:
                return False
        return True

    def get_position(self, trade_id: str) -> Position | None:
        """Exact lookup by trade_id."""
        return self._positions.get(trade_id)

    def get_position_by_strategy(self, strategy: str) -> Position | None:
        """First active position for a given strategy (None if none)."""
        for pos in self._positions.values():
            if pos.strategy == strategy:
                return pos
        return None

    # ─── Fix A (2026-04-23): pending-entry tracker ───────────────────

    # Entries older than this are considered stale (e.g. NT8 TIF expired
    # the limit, or the bot restarted losing context). 15 min is generous
    # for limit orders on sim accounts with GTC TIF — adjust if needed.
    PENDING_ENTRY_TIMEOUT_S: float = 900.0

    def record_pending_entry(self, account: str, trade_id: str, strategy: str,
                             direction: str, limit_price: float, qty: int) -> None:
        """Stash a pending LIMIT entry that NT8 accepted but hasn't filled.

        Called from base_bot._enter_trade's ENTRY_PENDING branch so the
        signal gate can see this limit is in flight on the account and
        not fire a duplicate entry.
        """
        self._pending_entries[account] = {
            "trade_id": trade_id,
            "strategy": strategy,
            "direction": direction,
            "limit_price": float(limit_price),
            "qty": int(qty),
            "submitted_at": time.time(),
        }
        logger.info(
            f"[PENDING_ENTRY:{trade_id}] {direction} {qty} @ {limit_price:.2f} "
            f"on {account} (strategy={strategy}) — registered"
        )

    def clear_pending_entry(self, account: str) -> None:
        """Remove the pending record for an account (called when entry
        fills, is rejected, or is cancelled)."""
        stale = self._pending_entries.pop(account, None)
        if stale:
            logger.debug(f"[PENDING_ENTRY] cleared {account} (trade={stale['trade_id']})")

    def has_pending_entry(self, account: str) -> bool:
        """True if an un-filled LIMIT entry is known to be working on
        this account. Stale entries (older than PENDING_ENTRY_TIMEOUT_S)
        are auto-expired and return False.

        2026-05-06 fix: when expiring a stale pending record, ALSO send
        a CANCEL OIF for the original trade_id. Prior behavior cleared
        only the Python-side record but left the NT8-side LIMIT order
        working — the next entry attempt would then collide with the
        still-active LIMIT and NT8 would reject "Exceeds account's
        maximum position quantity" (e.g. SimBias Momentum 03:13 today,
        SimDom Pull Back 2026-05-05 20:30).
        """
        rec = self._pending_entries.get(account)
        if rec is None:
            return False
        if (time.time() - rec["submitted_at"]) > self.PENDING_ENTRY_TIMEOUT_S:
            stale_trade_id = rec.get("trade_id", "")
            logger.info(
                f"[PENDING_ENTRY] {account} record expired "
                f"(trade={stale_trade_id}, age>{self.PENDING_ENTRY_TIMEOUT_S}s)"
            )
            # Send CANCEL OIF to NT8 so the still-working LIMIT order
            # gets cleaned up at the exchange side. Best-effort: never
            # let a cancel-write failure block the Python-side expiry —
            # operator can manually cancel from NT8 if this fails.
            self._cancel_stale_nt8_order(account, stale_trade_id)
            self._pending_entries.pop(account, None)
            return False
        return True

    def _cancel_stale_nt8_order(self, account: str, trade_id: str) -> None:
        """Write a CANCEL OIF for an aged-out pending entry's order_id.

        Phoenix uses trade_id as the NT8 ORDER ID (see
        bridge/oif_writer.py:_build_entry_line — the field-10 ORDER ID
        slot in PLACE OIFs is populated with trade_id, and
        cancel_single_order_line targets that same slot).

        Best-effort. Logs but never raises — callers must continue
        regardless of cancel success/failure.
        """
        if not trade_id:
            logger.warning(
                f"[PENDING_ENTRY] {account} expiry: empty trade_id, "
                f"can't issue CANCEL OIF — operator must check NT8 manually"
            )
            return
        try:
            from bridge.oif_writer import write_oif
            written = write_oif(action="CANCEL", trade_id=trade_id, account=account)
            if written:
                logger.info(
                    f"[PENDING_ENTRY] {account} CANCEL OIF written for "
                    f"stale trade_id={trade_id} ({len(written)} file(s))"
                )
            else:
                logger.warning(
                    f"[PENDING_ENTRY] {account} CANCEL OIF returned no "
                    f"files for trade_id={trade_id} — operator should "
                    f"verify NT8 has no working LIMIT on this account"
                )
        except Exception as e:
            logger.warning(
                f"[PENDING_ENTRY] {account} CANCEL OIF write failed for "
                f"trade_id={trade_id}: {e!r} — operator should verify "
                f"NT8 has no working LIMIT on this account"
            )

    def get_pending_entry(self, account: str) -> dict | None:
        """Return the pending-entry record for an account, honouring
        the same staleness check as has_pending_entry()."""
        if not self.has_pending_entry(account):
            return None
        return self._pending_entries.get(account)

    # ─── Open ─────────────────────────────────────────────────────────

    def open_position(self, trade_id: str, direction: str, entry_price: float,
                      contracts: int, stop_price: float, target_price: float,
                      strategy: str, reason: str, market_snapshot: dict = None,
                      exit_trigger: str = None, eod_flat_time_et: str = None,
                      metadata: dict = None,
                      scale_out_rr: float = None, trail_config: dict = None,
                      account: str = "Sim101", sub_strategy: str | None = None,
                      reconciled: bool = False,
                      tier: str | None = None):
        """Open a new position.

        Rejects if a position already exists with the same trade_id OR
        the same strategy (prevents double-opening for one strategy).
        Multiple DIFFERENT strategies may have concurrent positions.
        """
        if trade_id in self._positions:
            logger.warning(f"[{trade_id}] Cannot open position — trade_id already active")
            return False
        if not self.is_flat_for(strategy):
            existing = self.get_position_by_strategy(strategy)
            logger.warning(
                f"[{trade_id}] Cannot open position — strategy '{strategy}' "
                f"already has trade_id={existing.trade_id if existing else '?'} open"
            )
            return False

        # Lazy-instantiate the Chandelier trail if the Signal asked for one.
        trail_state = None
        if exit_trigger and exit_trigger.startswith("chandelier_trail") and trail_config:
            try:
                from core.chandelier_exit import ChandelierTrailState
                trail_state = ChandelierTrailState(
                    direction=direction.upper(),
                    entry_price=entry_price,
                    atr_mult=float(trail_config.get("atr_mult", 3.0)),
                )
            except Exception as e:
                logger.warning(f"[{trade_id}] Chandelier trail init failed (non-blocking): {e}")

        pos = Position(
            trade_id=trade_id,
            direction=direction.upper(),
            entry_price=entry_price,
            entry_time=time.time(),
            contracts=contracts,
            stop_price=stop_price,
            initial_stop_price=stop_price,   # 2026-05-13: preserve original R
            high_water_price=entry_price,    # 2026-05-13 v2: HWM trail anchor
            target_price=target_price,
            strategy=strategy,
            reason=reason,
            market_snapshot=market_snapshot or {},
            original_contracts=contracts,  # Capture for scale-out math
            exit_trigger=exit_trigger,
            eod_flat_time_et=eod_flat_time_et,
            metadata=metadata or {},
            scale_out_rr=scale_out_rr,
            trail_config=trail_config,
            trail_state=trail_state,
            account=account,
            sub_strategy=sub_strategy,
            reconciled=reconciled,
            tier=tier,
        )
        self._positions[trade_id] = pos
        self._open_order.append(trade_id)
        logger.info(f"[OPEN:{trade_id}] {direction} {contracts}x @ {entry_price} "
                     f"SL={stop_price} TP={target_price} strat={strategy} "
                     f"account={account}"
                     + (f"/{sub_strategy}" if sub_strategy else ""))
        return True

    # ─── Close ────────────────────────────────────────────────────────

    def _resolve_trade_id(self, trade_id: str | None) -> str | None:
        """Legacy-callers passed no trade_id and assumed exactly one
        position is open. Resolve to the sole active trade_id, or None."""
        if trade_id is not None:
            return trade_id
        if len(self._positions) == 0:
            return None
        if len(self._positions) == 1:
            return next(iter(self._positions.keys()))
        # Ambiguous — multiple open. Caller must supply trade_id.
        logger.error(
            f"Ambiguous close_position() — {len(self._positions)} positions "
            f"open, no trade_id provided. Refusing to close blindly."
        )
        return None

    def close_position(self, exit_price: float, exit_reason: str,
                       trade_id: str | None = None) -> dict | None:
        """Close a position.

        Legacy call signature close_position(price, reason) works when
        exactly one position is open. Multi-position callers must pass
        trade_id explicitly.
        """
        tid = self._resolve_trade_id(trade_id)
        if tid is None or tid not in self._positions:
            return None

        pos = self._positions[tid]
        if pos.direction == "LONG":
            ticks_pnl = (exit_price - pos.entry_price) / TICK_SIZE
        else:
            ticks_pnl = (pos.entry_price - exit_price) / TICK_SIZE

        gross_pnl = ticks_pnl * DOLLAR_PER_TICK * pos.contracts
        # B13 (2026-05-03): full cost accounting via central calculator.
        # Replaces old commission-only calc. Slippage + exchange fees added.
        costs = compute_trade_costs(pos.contracts)
        net_pnl = gross_pnl - costs["cost_total_dollars"]
        hold_time = time.time() - pos.entry_time

        trade = {
            "trade_id": pos.trade_id,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "contracts": pos.contracts,
            "stop_price": pos.stop_price,
            "target_price": pos.target_price,
            "pnl_ticks": round(ticks_pnl, 1),
            # B13: pnl_dollars defaults to NET (after all costs). Halt
            # thresholds, weekly caps, and floor checks all read this field.
            "pnl_dollars":           round(net_pnl, 2),
            "pnl_dollars_gross":     round(gross_pnl, 2),
            "pnl_dollars_net":       round(net_pnl, 2),
            "commission_dollars":    costs["commission_dollars"],
            "exchange_fees_dollars": costs["exchange_fees_dollars"],
            "slippage_dollars":      costs["slippage_dollars"],
            "fees_dollars":          costs["fees_dollars"],
            "cost_total_dollars":    costs["cost_total_dollars"],
            # Legacy field aliases retained so existing consumers don't
            # break. Both reference the SAME values as the new B13 fields.
            "gross_pnl":             round(gross_pnl, 2),
            "commission":            costs["commission_dollars"],
            "result": "WIN" if net_pnl > 0 else "LOSS",
            "hold_time_s": round(hold_time, 1),
            "strategy": pos.strategy,
            "sub_strategy": pos.sub_strategy,
            "account": pos.account,
            "entry_reason": pos.reason,
            "exit_reason": exit_reason,
            "entry_time": pos.entry_time,
            "exit_time": time.time(),
            "market_snapshot": pos.market_snapshot,
            # Sprint F (2026-05-04): tier persisted from entry signal so
            # the indicator audit can rank A++/A/B/C predictive value.
            # Pre-Sprint-F trades have tier=None.
            "tier": pos.tier,
        }

        self.trade_history.append(trade)
        del self._positions[tid]
        try:
            self._open_order.remove(tid)
        except ValueError:
            pass

        logger.info(f"[CLOSE:{pos.trade_id}] {trade['direction']} @ {exit_price} "
                     f"P&L=${trade['pnl_dollars']:.2f} ({trade['pnl_ticks']}t) "
                     f"reason={exit_reason} hold={trade['hold_time_s']:.0f}s")

        return trade

    # ─── P0.6 (D7) exit_pending state management ───────────────────────
    def mark_exit_pending(
        self, trade_id: str, exit_price: float, exit_reason: str,
        now: float | None = None,
    ) -> bool:
        """Flag a position as awaiting NT8 flatten confirmation.

        The Position remains in `_positions` (NOT deleted) so downstream
        consumers see it as a blocker for new entries on its account.
        Runtime reconciliation will finalize (call close_position) once
        NT8 outgoing/ confirms FLAT. Returns True on success, False if
        trade_id not found.
        """
        tid = self._resolve_trade_id(trade_id)
        if tid is None or tid not in self._positions:
            return False
        pos = self._positions[tid]
        pos.exit_pending = True
        pos.exit_pending_since = time.time() if now is None else now
        pos.pending_exit_price = exit_price
        pos.pending_exit_reason = exit_reason
        logger.info(
            f"[EXIT_PENDING:{tid}] {pos.direction} @ {exit_price} "
            f"reason={exit_reason} — awaiting NT8 flatten confirmation"
        )
        return True

    def finalize_exit_pending(self, trade_id: str) -> dict | None:
        """Complete an exit_pending position using the stashed exit
        price/reason. Called by runtime reconciliation once NT8 is FLAT
        for the position's account. No-op if the position isn't pending.
        """
        tid = self._resolve_trade_id(trade_id)
        if tid is None or tid not in self._positions:
            return None
        pos = self._positions[tid]
        if not pos.exit_pending:
            # Not in pending state — caller shouldn't call this. Return
            # None instead of silently closing.
            logger.warning(
                f"[FINALIZE:{tid}] called but position is not exit_pending — "
                f"ignoring"
            )
            return None
        return self.close_position(
            pos.pending_exit_price, pos.pending_exit_reason, trade_id=tid,
        )

    def exit_pending_positions(self) -> list[Position]:
        """All positions currently awaiting NT8 flatten confirmation."""
        return [p for p in self._positions.values() if p.exit_pending]

    def has_exit_pending_for_account(self, account: str) -> bool:
        """P0.6: prevents new entries on an account while a close is
        pending — callers should check this before emitting a new signal
        so we don't double-fill during the reconciliation window."""
        return any(
            p.exit_pending and p.account == account
            for p in self._positions.values()
        )

    def close_all(self, exit_price: float, exit_reason: str) -> list[dict]:
        """Close ALL active positions (e.g. 4pm CT daily flatten).

        Returns list of trade records in the order closed.
        """
        closed = []
        # Snapshot the keys because we're mutating during iteration.
        for tid in list(self._positions.keys()):
            trade = self.close_position(exit_price, exit_reason, trade_id=tid)
            if trade is not None:
                closed.append(trade)
        if closed:
            logger.info(f"[CLOSE_ALL] flattened {len(closed)} position(s) reason={exit_reason}")
        return closed

    # ─── Scale-out / move-stop (trade_id-aware, legacy-compatible) ─────

    def scale_out_partial(self, exit_price: float, n_contracts: int,
                          exit_reason: str = "scale_out",
                          trade_id: str | None = None) -> dict | None:
        """Exit N contracts, keep remaining open. Records a partial trade.

        Legacy callers: trade_id=None works when exactly one position is open.
        """
        tid = self._resolve_trade_id(trade_id)
        if tid is None or tid not in self._positions:
            return None

        pos = self._positions[tid]

        # Delegate to full close if exiting everything
        if n_contracts >= pos.contracts:
            return self.close_position(exit_price, exit_reason, trade_id=tid)

        # Compute P&L for exited portion only
        if pos.direction == "LONG":
            ticks_pnl = (exit_price - pos.entry_price) / TICK_SIZE
        else:
            ticks_pnl = (pos.entry_price - exit_price) / TICK_SIZE

        gross_pnl = ticks_pnl * DOLLAR_PER_TICK * n_contracts
        # B13 (2026-05-03): full cost accounting for partial exits too.
        costs = compute_trade_costs(n_contracts)
        net_pnl = gross_pnl - costs["cost_total_dollars"]

        partial_trade = {
            "trade_id":      pos.trade_id + "_scale1",
            "direction":     pos.direction,
            "entry_price":   pos.entry_price,
            "exit_price":    exit_price,
            "contracts":     n_contracts,
            "pnl_ticks":     round(ticks_pnl, 1),
            "pnl_dollars":           round(net_pnl, 2),
            "pnl_dollars_gross":     round(gross_pnl, 2),
            "pnl_dollars_net":       round(net_pnl, 2),
            "commission_dollars":    costs["commission_dollars"],
            "exchange_fees_dollars": costs["exchange_fees_dollars"],
            "slippage_dollars":      costs["slippage_dollars"],
            "fees_dollars":          costs["fees_dollars"],
            "cost_total_dollars":    costs["cost_total_dollars"],
            "gross_pnl":             round(gross_pnl, 2),  # legacy alias
            "commission":            costs["commission_dollars"],  # legacy alias
            "result":        "WIN" if net_pnl > 0 else "LOSS",
            "hold_time_s":   round(time.time() - pos.entry_time, 1),
            "strategy":      pos.strategy,
            "sub_strategy":  pos.sub_strategy,
            "account":       pos.account,
            "entry_reason":  pos.reason,
            "exit_reason":   exit_reason,
            "entry_time":    pos.entry_time,
            "exit_time":     time.time(),
            "partial":       True,
            "market_snapshot": pos.market_snapshot,
            # Sprint F: tier persists on partials too (see open/close hooks)
            "tier":          pos.tier,
        }

        # Reduce live position by exited contracts
        pos.contracts -= n_contracts
        pos.scaled_out = True

        self.trade_history.append(partial_trade)

        logger.info(f"[SCALE_OUT:{pos.trade_id}] Exited {n_contracts}x @ {exit_price:.2f} "
                    f"P&L=${net_pnl:.2f} ({ticks_pnl:.1f}t) | "
                    f"{pos.contracts}x still open")
        return partial_trade

    def move_stop_to_be(self, be_price: Optional[float] = None,
                        trade_id: str | None = None):
        """Move stop price to break-even (entry by default). Safety-clamped.

        Legacy callers: trade_id=None works when exactly one position is open.
        """
        tid = self._resolve_trade_id(trade_id)
        if tid is None or tid not in self._positions:
            return
        pos = self._positions[tid]
        if be_price is None:
            be_price = pos.entry_price

        old_stop = pos.stop_price
        # Safety: only move stop if it improves our position
        if pos.direction == "LONG" and be_price <= old_stop:
            return   # Would move stop further from entry — wrong direction
        if pos.direction == "SHORT" and be_price >= old_stop:
            return

        pos.stop_price = be_price
        pos.be_stop_active = True
        logger.info(f"[BE_STOP:{pos.trade_id}] Stop {old_stop:.2f} -> {be_price:.2f} (BE locked)")

    # ─── Exit triggers ────────────────────────────────────────────────

    def check_exits(self, current_price: float, max_hold_min: float = None,
                    trade_id: str | None = None) -> str | None:
        """Legacy single-position exit check.

        Returns exit reason string for the sole active position, or None.
        When multiple positions are open, caller should use check_exits_all()
        to iterate. If trade_id is supplied, checks that specific position.

        Fix (2026-05-03): skip positions already marked exit_pending.
        Otherwise the tick loop re-triggers stop_loss against the same
        position every tick until runtime reconciliation finalizes it,
        producing duplicate EXIT commands (5-9 per actual close in the
        forensic trade log).
        """
        tid = self._resolve_trade_id(trade_id)
        if tid is None or tid not in self._positions:
            return None
        pos = self._positions[tid]
        if pos.exit_pending:
            return None
        return self._check_exit_one(pos, current_price, max_hold_min)

    def check_exits_all(self, current_price: float,
                        max_hold_min: float = None) -> list[tuple[str, str]]:
        """Multi-position exit check. Returns list of (trade_id, reason)
        tuples for every position whose stop/target/time-stop triggered.

        Fix (2026-05-03): positions in exit_pending state are skipped.
        Their EXIT command has already been sent to NT8; re-triggering
        is the source of the duplicate-EXIT-command storm seen in
        logs/trades.log between 2026-05-03 17:37 and 23:06 (4 of 4
        dom_pullback trades emitted 5-9 redundant EXIT commands each).
        """
        triggers: list[tuple[str, str]] = []
        for tid, pos in self._positions.items():
            if pos.exit_pending:
                continue
            reason = self._check_exit_one(pos, current_price, max_hold_min)
            if reason is not None:
                triggers.append((tid, reason))
        return triggers

    @staticmethod
    def _check_exit_one(pos: Position, current_price: float,
                        max_hold_min: float = None) -> str | None:
        # Stop loss
        if pos.direction == "LONG" and current_price <= pos.stop_price:
            return "stop_loss"
        if pos.direction == "SHORT" and current_price >= pos.stop_price:
            return "stop_loss"

        # Take profit
        if pos.direction == "LONG" and current_price >= pos.target_price:
            return "target_hit"
        if pos.direction == "SHORT" and current_price <= pos.target_price:
            return "target_hit"

        # Time stop
        if max_hold_min:
            hold_seconds = time.time() - pos.entry_time
            if hold_seconds >= max_hold_min * 60:
                return "time_stop"

        return None

    # ─── P&L + serialization ──────────────────────────────────────────

    def unrealized_pnl(self, current_price: float,
                       trade_id: str | None = None) -> float:
        """Unrealized P&L in dollars.

        Legacy: with trade_id=None, returns the sole active position's P&L,
        or the SUM across all active positions when multiple are open.
        Multi-position callers should pass trade_id for per-position P&L.
        """
        if not self._positions:
            return 0.0
        if trade_id is not None:
            pos = self._positions.get(trade_id)
            if pos is None:
                return 0.0
            return self._unrealized_one(pos, current_price)
        # Aggregate
        return sum(self._unrealized_one(p, current_price)
                   for p in self._positions.values())

    @staticmethod
    def _unrealized_one(pos: Position, current_price: float) -> float:
        if pos.direction == "LONG":
            ticks = (current_price - pos.entry_price) / TICK_SIZE
        else:
            ticks = (pos.entry_price - current_price) / TICK_SIZE
        return ticks * DOLLAR_PER_TICK * pos.contracts

    def to_dict(self, current_price: float = 0.0) -> dict:
        """Serialize for dashboard.

        Legacy: when 0 or 1 positions open, returns the pre-refactor
        single-position shape. When multiple are open, returns the
        most-recently-opened for primary fields and includes a new
        `all_positions` list for multi-position dashboards.
        """
        if not self._positions:
            return {
                "status": "FLAT",
                "direction": None,
                "entry_price": None,
                "stop_price": None,
                "target_price": None,
                "contracts": 0,
                "strategy": None,
                "unrealized_pnl": 0.0,
                "hold_time_s": 0,
                "active_count": 0,
                "all_positions": [],
            }

        # Primary = most recently opened (legacy .position semantics)
        pos = self.position
        primary = {
            "status": "IN_TRADE",
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "stop_price": pos.stop_price,
            "target_price": pos.target_price,
            "contracts": pos.contracts,
            "original_contracts": pos.original_contracts,
            "strategy": pos.strategy,
            "reason": pos.reason,
            "unrealized_pnl": round(self._unrealized_one(pos, current_price), 2),
            "hold_time_s": round(time.time() - pos.entry_time, 0),
            "scaled_out": pos.scaled_out,
            "be_stop_active": pos.be_stop_active,
            "rider_mode": pos.rider_mode,
        }
        primary["active_count"] = len(self._positions)
        primary["all_positions"] = [
            {
                "trade_id": p.trade_id,
                "strategy": p.strategy,
                "direction": p.direction,
                "entry_price": p.entry_price,
                "stop_price": p.stop_price,
                "target_price": p.target_price,
                "contracts": p.contracts,
                "account": p.account,
                "unrealized_pnl": round(self._unrealized_one(p, current_price), 2),
                "hold_time_s": round(time.time() - p.entry_time, 0),
            }
            for p in self._positions.values()
        ]
        return primary

    def recent_trades(self, n: int = 20) -> list[dict]:
        """Return last N trades for dashboard trade log."""
        return self.trade_history[-n:]
