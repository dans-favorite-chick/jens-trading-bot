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

──────────────────────────────────────────────────────────────────────
2026-06-04 PHANTOM-NT8 LIVE EMERGENCY FOLLOWUP
(FINDING-2026-06-04-RECON-REPLAY-AS-ENTRY)

Two pre-cycle guards added below to STOP the active OIF flood on Sim101
observed 06-04 17:50–18:13 CDT:

  Guard A — pipeline-health gate
    Calls bridge.oif_writer.diagnose_oif_pipeline_health() before each
    reconciliation tick. If unhealthy (e.g. oldest file in `incoming/`
    has aged past stale_threshold = 30s, signalling NT8 ATI silent
    rejection per FINDING-2026-06-04-PHANTOM-NT8), SKIP the orphan-
    adoption sub-step ONLY. Exit-pending resolution still runs so the
    EXIT_PENDING_TIMEOUT_S=60s deadline stays honest even when the
    pipeline is hung. (Red-team round 1 fix: pre-fix, Guard A
    `continue`d the entire cycle, which would have blocked exit
    finalization too — creating a paging spam loop on stuck positions
    during the very window Guard A was designed to handle gracefully.)
    The bridge already logs CRITICAL on stuck OIFs; we just refuse to
    add more OIFs to the pileup.

  Guard B — per-cycle account/direction dedup
    Maintains a module-level "recently emitted" map keyed by
    `(account, direction)`. If a RECONCILED-tagged OIF (i.e. an
    open_position(reconciled=True) / oco_writer call) was emitted for
    the same account+direction within the last RUNTIME_RECON_INTERVAL_S
    × 3 seconds, SKIP this cycle's adoption for that account. Prevents
    the 30s/1m/1.5m re-emit cascade that built Sim101 0→SHORT 7 in
    137ms at 18:01:13 today.

These guards are SUFFICIENT to stop the live flood. They do NOT fix the
underlying H2/H3 root causes (positions.open_position(reconciled=True)
not persisting adoption, _reconciled_<account> not honored by
is_flat_for slot interlock) — those require edits to
core/position_manager.py (PROTECTED per .claude/PROTECTED_FILES.md:71)
and are proposed in chat for operator OA as a separate follow-up.
──────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Tuple

logger = logging.getLogger("RuntimeReconciliation")


# ──────────────────────────────────────────────────────────────────
# Guard B — per-cycle dedup state (module-level so the loop sees it
# across iterations within a single bot process)
# ──────────────────────────────────────────────────────────────────
# Key: (account, direction) — value: monotonic ts of the last reconciled
# adoption emission for that pair. Module-level so it survives across
# RuntimeReconciliationLoop.run() iterations within a single bot
# process. NOT shared across PIDs (multi-PID emission is a separate
# concern flagged by FINDING-2026-06-04-MULTI-PID).
_RECENT_RECON_EMIT: Dict[Tuple[str, str], float] = {}

# How many runtime-recon intervals must pass before the same
# (account, direction) is allowed to re-adopt. 3x = ~90s at the default
# 30s interval — enough to let a slow NT8 outgoing/ position file
# refresh after the operator manually flattens, while still permitting
# legitimate fresh orphan adoption.
_DEDUP_INTERVAL_MULTIPLIER: int = 3


def _stale_recon_emits(now: float, dedup_ttl_s: float) -> None:
    """Drop entries older than dedup_ttl_s from _RECENT_RECON_EMIT.

    Called on every cycle entry to keep the map bounded.
    """
    stale = [k for k, ts in _RECENT_RECON_EMIT.items() if now - ts > dedup_ttl_s]
    for k in stale:
        _RECENT_RECON_EMIT.pop(k, None)


def _mark_recon_emit(account: str, direction: str, now: float) -> None:
    """Record an emission so Guard B can short-circuit the next cycle."""
    if account and direction:
        _RECENT_RECON_EMIT[(account, direction)] = now


def _recent_recon_skip(account: str, direction: str, now: float,
                       dedup_ttl_s: float) -> bool:
    """Return True if we should skip adopting this (account, direction)
    because a reconciliation OIF was already emitted < dedup_ttl_s ago.
    """
    last = _RECENT_RECON_EMIT.get((account, direction))
    if last is None:
        return False
    return (now - last) < dedup_ttl_s


def _pipeline_healthy(stale_threshold_s: float = 30.0) -> bool:
    """Guard A helper — read-only health check on NT8 incoming/.

    Returns True if the bridge says incoming/ is healthy (no file
    older than stale_threshold_s). Returns True on any exception so
    a health-check failure can never silently block reconciliation
    (defaults to "permit work" — matches the rest of Phoenix's
    fail-open posture on observability).

    A False return value means: there is at least one OIF sitting in
    incoming/ NT8 has not consumed within the threshold. In that
    state, emitting another OIF is guaranteed to make things worse —
    so skip this reconciliation cycle.
    """
    try:
        # Late import — bridge.oif_writer is heavy (drags config, glob,
        # bridge state) and we only want to pay that cost on actual
        # cycles, not at module import time.
        from bridge.oif_writer import diagnose_oif_pipeline_health
        report = diagnose_oif_pipeline_health(
            stale_incoming_threshold_s=stale_threshold_s,
        )
        return bool(report.get("healthy", True))
    except Exception as e:
        # Fail open — never let observability code block reconciliation.
        logger.debug(
            f"[RUNTIME_RECON] pipeline-health probe raised "
            f"(treating as healthy): {e!r}"
        )
        return True


class RuntimeReconciliationLoop:
    def __init__(self, bot):
        self.bot = bot

    async def run(self) -> None:
        """P0.3 + P0.6: periodic NT8-ledger reconciliation during the
        session.

        Each cycle:
          0. NEW 2026-06-04 (FINDING-RECON-REPLAY-AS-ENTRY) — pre-check
             OIF pipeline health (Guard A). If incoming/ has a file
             aged past 30s, NT8's ATI is most likely silently rejecting,
             and emitting another OIF will pile onto the existing flood
             — skip the cycle.
          1. Run `_reconcile_positions_from_nt8` to adopt any orphan NT8
             position not tracked in PositionManager (P0.3). Each
             adoption is recorded in Guard B's dedup map; a re-emit
             for the same (account, direction) within the dedup window
             is dropped at the bot's adopted-list level.
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

                now = time.monotonic()
                dedup_ttl_s = (
                    float(self.bot.RUNTIME_RECON_INTERVAL_S)
                    * _DEDUP_INTERVAL_MULTIPLIER
                )

                # House-keeping: drop expired dedup entries so the map
                # doesn't grow unbounded over a multi-day session.
                _stale_recon_emits(now, dedup_ttl_s)

                # Guard A — pipeline-health gate (red-team round 1
                # fix: gates ONLY the orphan-adoption block, NOT
                # _resolve_exit_pending_positions). If NT8's ATI is
                # silently rejecting (stuck file > 30s in incoming/),
                # we must not add new orphan OIFs to the pile — but
                # exit-pending finalization is a PYTHON-state-only
                # operation (no new OIFs unless _resolve detects a
                # genuinely-stuck position and writes its own retry
                # CLOSEPOSITION). Letting it run keeps the
                # EXIT_PENDING_TIMEOUT_S=60s deadline honest even when
                # the pipeline is hung.
                _skip_adoption = not _pipeline_healthy(stale_threshold_s=30.0)
                if _skip_adoption:
                    logger.warning(
                        "[RUNTIME_RECON] SKIPPING orphan-adoption sub-step "
                        "— OIF pipeline unhealthy (stuck file in "
                        "incoming/ > 30s; NT8 ATI likely rejecting). "
                        "Operator: check NT8 Log tab + Tools → Options "
                        "→ ATI tab. Exit-pending resolution still runs."
                    )

                # P0.3: orphan adoption (skipped when Guard A trips).
                adopted = [] if _skip_adoption else self.bot._reconcile_positions_from_nt8()
                if adopted:
                    # Guard B — record each adoption so the next cycle
                    # can short-circuit a re-emit for the same
                    # (account, direction) within the dedup window.
                    # We also filter out any adoption that was a
                    # within-window duplicate from being logged as a
                    # fresh orphan to avoid muddying the audit log.
                    fresh_adoptions = []
                    for adoption in adopted:
                        acct = adoption.get("account") or ""
                        direc = (adoption.get("direction") or "").upper()
                        if _recent_recon_skip(acct, direc, now, dedup_ttl_s):
                            logger.warning(
                                f"[RUNTIME_RECON] DEDUP suppressed "
                                f"reconcile-adoption for {acct} "
                                f"{direc} (last emit "
                                f"{now - _RECENT_RECON_EMIT[(acct, direc)]:.1f}s "
                                f"ago, ttl={dedup_ttl_s:.0f}s). Likely "
                                f"a stale outgoing/ position file from "
                                f"a freshly-flattened account."
                            )
                            continue
                        _mark_recon_emit(acct, direc, now)
                        fresh_adoptions.append(adoption)
                    if fresh_adoptions:
                        logger.info(
                            f"[RUNTIME_RECON] adopted {len(fresh_adoptions)} "
                            f"orphan position(s) mid-session (accounts: "
                            f"{[a['account'] for a in fresh_adoptions]})"
                        )
                # P0.6: exit_pending resolution.
                self.bot._resolve_exit_pending_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[RUNTIME_RECON] cycle failed (will retry next interval): {e!r}"
                )
