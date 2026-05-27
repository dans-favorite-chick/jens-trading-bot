"""Runtime position reconciliation loop — extracted from base_bot.py
2026-05-24 (P4-1 Stage 2).

Periodically reconciles Phoenix's position state against NT8's outgoing
folder so mid-session orphans (NT8 has a fill Phoenix didn't capture,
or Phoenix thinks it's in a position NT8 closed) surface within
RUNTIME_RECON_INTERVAL_S.

The reconciliation WORK lives in BaseBot's _reconcile_positions_from_nt8
and _resolve_exit_pending_positions helpers — this module just owns the
scheduling loop. Position-state mutations stay on BaseBot per Stage 3
risk policy (see docs/audits/BASE_BOT_DECOMPOSITION_PLAN.md).

Original location: bots/base_bot.py:1252 as BaseBot._runtime_reconciliation_loop.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("RuntimeReconciliation")


class RuntimeReconciliationLoop:
    def __init__(self, bot):
        self.bot = bot

    async def run(self) -> None:
        """P0.3 + P0.6: periodic NT8-ledger reconciliation during the
        session.

        Each cycle:
          1. Run `_reconcile_positions_from_nt8` to adopt any orphan NT8
             position not tracked in PositionManager (P0.3).
          2. Walk every `exit_pending` position: if NT8 shows FLAT for
             its account+instrument, call `finalize_exit_pending` to
             promote it to a closed trade. If a position has been
             exit_pending longer than EXIT_PENDING_TIMEOUT_S, fire a
             CRITICAL alert so the operator can investigate.

        A clean-shutdown flag (`self.bot._shutdown_reconciliation`) lets
        run() stop the loop gracefully without hanging on sleep.

        Exceptions are caught + logged so one bad cycle doesn't kill the
        loop — the next tick keeps trying.
        """
        while not getattr(self.bot, "_shutdown_reconciliation", False):
            try:
                await asyncio.sleep(self.bot.RUNTIME_RECON_INTERVAL_S)
                if getattr(self.bot, "_shutdown_reconciliation", False):
                    break
                # P0.3: orphan adoption.
                adopted = self.bot._reconcile_positions_from_nt8()
                if adopted:
                    logger.info(
                        f"[RUNTIME_RECON] adopted {len(adopted)} orphan "
                        f"position(s) mid-session (accounts: "
                        f"{[a['account'] for a in adopted]})"
                    )
                # P0.6: exit_pending resolution.
                self.bot._resolve_exit_pending_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[RUNTIME_RECON] cycle failed (will retry next interval): {e!r}"
                )
