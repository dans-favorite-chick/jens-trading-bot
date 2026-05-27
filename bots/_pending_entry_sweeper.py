"""Pending-entry sweeper — P1-7 (2026-05-25).

Background coroutine that walks every in-flight LIMIT entry registered
in ``core/pending_entry_tracker.py`` and cancels any that has exceeded
its configured ``timeout_s``.

Pattern follows ``bots/_ws_watchdog.py``:
  - constructed once per BaseBot in __init__
  - scheduled from BaseBot.run() via asyncio.ensure_future(self._pending_entry_sweeper.run())
  - skip windows + ready-guard prevent race against startup reconciliation

Why this lives outside _trade_entry.py: the sweeper has to fire
independently of any tick/signal — a quiet market is exactly the scenario
where a stale LIMIT can outlive its thesis without any signal-evaluation
path noticing. The original per-account ``has_pending_entry`` lazy
expiry depended on a fresh signal landing on the same account.

Safety:
  - READY_GUARD_S: the sweeper skips its first N seconds of process
    uptime so startup reconciliation (which can register / adopt
    pre-existing NT8 orders) doesn't get cancelled out from under it.
  - On each expired entry, the sweeper emits a CANCEL OIF via the
    canonical ``bridge.oif_writer.cancel_single_order_line`` -> staged
    write path. No raw CANCEL strings are written here.
  - Cancel-OIF write failures are logged but NEVER raised — the entry
    is still marked timeout_cancelled in the tracker so subsequent
    signals know it's terminal. Operator must clear the residual NT8
    order manually if the cancel write itself failed.
  - trade_memory is updated via the canonical writer
    (``self.bot.trade_memory.record``) with a minimal row stamped
    terminal_state="timeout_cancelled".
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger("PendingEntrySweeper")

# How often the sweeper checks the tracker. 10s aligns with the spec:
# fine-grained enough that a 90s default timeout misses by < 11% in the
# worst case, coarse enough not to thrash the file-system lock.
CHECK_INTERVAL_S = 10.0

# Initial guard window. Sweeper skips its first N seconds of uptime so
# startup reconciliation (_reconcile_positions_from_nt8) can adopt any
# pre-existing NT8 orders before the sweeper would start cancelling them.
# 30s is double the RUNTIME_RECON_INTERVAL_S default, giving startup
# plenty of room.
READY_GUARD_S = 30.0


class PendingEntrySweeper:
    """Async sweeper loop. One per BaseBot."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._started_at: float = time.time()
        # Track whether we've already emitted the "guard cleared" log so
        # the operator sees exactly one "[PENDING_SWEEPER] active" line.
        self._guard_cleared_logged: bool = False

    # ─── Cancel helpers ──────────────────────────────────────────────────
    def _emit_cancel_oif(self, pe) -> bool:
        """Write a CANCEL OIF for ``pe`` via the canonical oif_writer path.

        Returns True on at least one file written, False on failure.
        Never raises — operator notification happens via logger.
        """
        try:
            from bridge.oif_writer import write_oif as _write_oif
        except Exception as e:
            logger.error(
                f"[PENDING_TIMEOUT:{pe.trade_id}] oif_writer import failed: {e!r} "
                f"— operator must cancel {pe.account} order manually"
            )
            return False

        try:
            # B5 single-order CANCEL: trade_id doubles as ORDER ID in the
            # PLACE OIF and is therefore the cancel key.
            written = _write_oif(
                action="CANCEL",
                trade_id=pe.trade_id,
                account=pe.account,
            )
            if not written:
                logger.error(
                    f"[PENDING_TIMEOUT:{pe.trade_id}] CANCEL OIF returned no "
                    f"files for {pe.account} — operator should verify NT8"
                )
                return False
            logger.warning(
                f"[PENDING_TIMEOUT:{pe.trade_id}] CANCEL OIF written for "
                f"{pe.strategy} {pe.side} {pe.qty} @ {pe.limit_price:.2f} "
                f"on {pe.account} ({len(written)} file(s))"
            )
            return True
        except Exception as e:
            logger.error(
                f"[PENDING_TIMEOUT:{pe.trade_id}] CANCEL OIF write failed: {e!r} "
                f"— operator must verify NT8 has no working LIMIT on {pe.account}"
            )
            return False

    def _record_terminal_to_trade_memory(self, pe) -> None:
        """Append a minimal trade_memory row tagging the timeout-cancelled
        entry. Uses the canonical TradeMemory.record() — no raw file IO.

        The row carries terminal_state="timeout_cancelled" so downstream
        readers (validation_tracker, dashboard) can filter these out of
        WR / PF calculations without confusing them for legitimate losses.
        Schema is additive — existing readers that don't know about
        terminal_state will ignore the field per their .get() defaults.
        """
        tm = getattr(self.bot, "trade_memory", None)
        if tm is None:
            return
        try:
            row = {
                "trade_id": pe.trade_id,
                "strategy": pe.strategy,
                "account": pe.account,
                "direction": "LONG" if pe.side == "BUY" else "SHORT",
                "entry_price": pe.limit_price,
                "contracts": pe.qty,
                "result": "NO_FILL",
                "pnl_dollars": 0.0,
                "exit_time": datetime.now().isoformat(),
                "terminal_state": pe.terminal_state,
                "terminal_reason": pe.terminal_reason or "",
                "reason": f"pending_entry_{pe.terminal_state}",
                "instrument": pe.instrument,
            }
            tm.record(row, bot_id=getattr(self.bot, "bot_name", "unknown"))
        except Exception as e:
            logger.warning(
                f"[PENDING_TIMEOUT:{pe.trade_id}] trade_memory.record failed: {e!r}"
            )

    # ─── Sweep step ──────────────────────────────────────────────────────
    def _sweep(self) -> int:
        """Run one sweep cycle. Returns count of entries timed out."""
        try:
            from core.pending_entry_tracker import get_pending_entry_tracker
        except Exception as e:
            logger.error(f"[PENDING_SWEEPER] tracker import failed: {e!r}")
            return 0

        tracker = get_pending_entry_tracker()
        # The tracker's sweep_once does the atomic mark; we just handle
        # the OIF write + trade_memory side effects.
        expired = tracker.sweep_once()
        for pe in expired:
            # Also clear the per-account pending entry dict in the
            # position-manager so the entry-gate doesn't keep blocking
            # the account on a now-cancelled limit.
            try:
                pm = getattr(self.bot, "positions", None)
                if pm is not None:
                    pm.clear_pending_entry(pe.account)
            except Exception:
                pass
            self._emit_cancel_oif(pe)
            self._record_terminal_to_trade_memory(pe)
        return len(expired)

    # ─── Loop ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Main sweeper coroutine. Cancels any pending LIMIT whose age
        exceeds its configured timeout.

        Skip-windows:
          - first READY_GUARD_S of process uptime (startup reconciliation
            grace period; pre-existing NT8 orders may still be adopting)
          - 16:00-17:00 CT NT8 daily maintenance break (NT8 ATI offline,
            cancels would stack up in incoming/)
        """
        from zoneinfo import ZoneInfo
        ct_tz = ZoneInfo("America/Chicago")

        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            try:
                # Skip during NT8 daily maintenance break.
                if datetime.now(ct_tz).hour == 16:
                    continue

                age_s = time.time() - self._started_at
                if age_s < READY_GUARD_S:
                    continue
                if not self._guard_cleared_logged:
                    logger.info(
                        f"[PENDING_SWEEPER] ready guard cleared "
                        f"({READY_GUARD_S:.0f}s) — active"
                    )
                    self._guard_cleared_logged = True

                count = self._sweep()
                if count:
                    logger.warning(
                        f"[PENDING_SWEEPER] cancelled {count} stale "
                        f"pending entry/entries this cycle"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[PENDING_SWEEPER] cycle error: {e!r}")
