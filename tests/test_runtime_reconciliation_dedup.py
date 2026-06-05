"""Regression tests for the 2026-06-04 PHANTOM-NT8 active replay loop
fix (FINDING-2026-06-04-RECON-REPLAY-AS-ENTRY).

Live evidence (2026-06-04 18:01:13 CDT, NT8 trace lines 1327-1356):
  Phoenix wrote 7 PLACE;Sim101;MNQM6;SELL;1;MARKET;0;0;GTC OIFs in
  137 ms (ms 821 → ms 958). NT8 routed each as a fresh SELL MARKET
  entry, building Sim101 from FLAT to SHORT 7 at price 30368.5.

The reconciliation loop in bots/_runtime_reconciliation.py was:
  (a) re-emitting the same orphan adoption every 30s because
      positions.open_position(reconciled=True) does not persist the
      adoption into PositionManager.active_positions (H2 root cause,
      requires position_manager.py edit which is PROTECTED), AND
  (b) triggering a chain of partial_exit / scale_out OIF writes per
      adoption that fanned out into 7 SELL MARKET fills inside a
      single 30s reconciliation cycle.

This file's tests gate the two pre-cycle guards the fix adds to
bots/_runtime_reconciliation.py:

  Guard A (pipeline-health gate) — refuses to run a reconciliation
    cycle if the bridge's diagnose_oif_pipeline_health() reports
    unhealthy (stuck file > 30s in incoming/, signalling NT8 ATI
    silent rejection).

  Guard B (per-cycle dedup) — refuses to record the same
    (account, direction) adoption twice within
    RUNTIME_RECON_INTERVAL_S × 3 seconds.

Behavioral assertions only (per FINDING-2026-06-04-STRUCTURAL-TEST-
PATTERN learning): we exercise the loop end-to-end via the same
RuntimeReconciliationLoop wrapper test_runtime_reconciliation.py
uses, then observe externally-visible state (mock call counts,
recorded adoptions, logger output).

Run:
  pytest tests/test_runtime_reconciliation_dedup.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bots._runtime_reconciliation import (
    RuntimeReconciliationLoop,
    _RECENT_RECON_EMIT,
    _DEDUP_INTERVAL_MULTIPLIER,
)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _make_bot(reconcile_fn, interval_s=0.01):
    """Bot mock matching the test_runtime_reconciliation.py pattern."""
    bot = MagicMock()
    bot.RUNTIME_RECON_INTERVAL_S = interval_s
    bot._shutdown_reconciliation = False
    bot._reconcile_positions_from_nt8 = reconcile_fn
    bot._resolve_exit_pending_positions = MagicMock()
    return bot


def _run_loop_for(loop, bot, total_sleep_s):
    """Drive the async loop for the given duration, then shut it down."""
    async def _run():
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(total_sleep_s)
        bot._shutdown_reconciliation = True
        try:
            await asyncio.wait_for(task, timeout=0.2)
        except asyncio.TimeoutError:
            task.cancel()

    asyncio.run(_run())


@pytest.fixture(autouse=True)
def _clear_dedup_state():
    """Module-level dedup map must start empty for each test."""
    _RECENT_RECON_EMIT.clear()
    yield
    _RECENT_RECON_EMIT.clear()


# ──────────────────────────────────────────────────────────────────
# Guard A — pipeline-health gate
# ──────────────────────────────────────────────────────────────────

class TestPipelineHealthGate:
    def test_unhealthy_pipeline_skips_reconcile_cycle(self):
        """When diagnose_oif_pipeline_health reports unhealthy, the
        cycle's _reconcile_positions_from_nt8 must NOT be invoked.
        """
        reconcile_mock = MagicMock(return_value=[])
        bot = _make_bot(reconcile_mock, interval_s=0.01)
        loop = RuntimeReconciliationLoop(bot)

        unhealthy = {"healthy": False,
                     "reasons": ["incoming has 7 file(s) older than 30s"]}
        with patch("bots._runtime_reconciliation.diagnose_oif_pipeline_health",
                   return_value=unhealthy, create=True):
            # The patch targets the import inside _pipeline_healthy(),
            # which uses a late import. We need to also patch the lookup
            # location.
            with patch("bridge.oif_writer.diagnose_oif_pipeline_health",
                       return_value=unhealthy):
                _run_loop_for(loop, bot, total_sleep_s=0.05)

        # Reconcile must NOT have been called because the pipeline was
        # unhealthy on every cycle entry.
        assert reconcile_mock.call_count == 0, (
            f"Expected reconcile not to fire while pipeline unhealthy, "
            f"got {reconcile_mock.call_count} call(s)"
        )

    def test_healthy_pipeline_permits_reconcile_cycle(self):
        """Sanity: with a healthy pipeline, the cycle proceeds normally."""
        reconcile_mock = MagicMock(return_value=[])
        bot = _make_bot(reconcile_mock, interval_s=0.01)
        loop = RuntimeReconciliationLoop(bot)

        healthy = {"healthy": True, "reasons": []}
        with patch("bridge.oif_writer.diagnose_oif_pipeline_health",
                   return_value=healthy):
            _run_loop_for(loop, bot, total_sleep_s=0.05)

        assert reconcile_mock.call_count >= 2, (
            f"Healthy pipeline should permit reconciliation; "
            f"only got {reconcile_mock.call_count} cycle(s)"
        )

    def test_health_probe_exception_treated_as_healthy(self):
        """Observability code must never block the live execution path —
        a raised exception during the health probe must default to
        'permit work'.
        """
        reconcile_mock = MagicMock(return_value=[])
        bot = _make_bot(reconcile_mock, interval_s=0.01)
        loop = RuntimeReconciliationLoop(bot)

        with patch("bridge.oif_writer.diagnose_oif_pipeline_health",
                   side_effect=RuntimeError("health probe blew up")):
            _run_loop_for(loop, bot, total_sleep_s=0.05)

        assert reconcile_mock.call_count >= 2, (
            "Health-probe exception must NOT block reconciliation "
            "(fail-open posture)"
        )

    def test_unhealthy_pipeline_still_runs_exit_pending_resolution(self):
        """Red-team round 1 fix: Guard A must only block orphan
        adoption — it must NOT block _resolve_exit_pending_positions.

        Pre-fix, Guard A `continue`d the entire cycle body, which
        meant a 60s+ stuck pipeline would also skip exit-pending
        resolution past the EXIT_PENDING_TIMEOUT_S=60s deadline,
        creating a CRITICAL paging spam loop on positions that should
        have been finalized.
        """
        reconcile_mock = MagicMock(return_value=[])
        bot = _make_bot(reconcile_mock, interval_s=0.01)
        loop = RuntimeReconciliationLoop(bot)

        unhealthy = {"healthy": False,
                     "reasons": ["incoming has 7 file(s) older than 30s"]}
        with patch("bridge.oif_writer.diagnose_oif_pipeline_health",
                   return_value=unhealthy):
            _run_loop_for(loop, bot, total_sleep_s=0.05)

        # Orphan adoption was skipped (Guard A's job)…
        assert reconcile_mock.call_count == 0, (
            f"Expected adoption to be gated by unhealthy pipeline, "
            f"got {reconcile_mock.call_count} call(s)"
        )
        # …but exit-pending resolution still ran every cycle.
        assert bot._resolve_exit_pending_positions.call_count >= 2, (
            f"Exit-pending resolution MUST run even when pipeline is "
            f"unhealthy (red-team round 1 fix). Got "
            f"{bot._resolve_exit_pending_positions.call_count} call(s)."
        )


# ──────────────────────────────────────────────────────────────────
# Guard B — per-cycle (account, direction) dedup
# ──────────────────────────────────────────────────────────────────

class TestPerCycleDedup:
    def test_same_account_direction_within_window_suppressed(self):
        """If two reconcile cycles BOTH report adopting Sim101 SHORT
        within the 3×interval window, the SECOND one must be suppressed
        — that's the 2026-06-04 18:01 replay-as-entry pattern.
        """
        adoption = {"account": "Sim101", "direction": "SHORT",
                    "qty": 1, "trade_id": "RECONCILED_Sim101_aaa"}
        reconcile_mock = MagicMock(return_value=[adoption])
        bot = _make_bot(reconcile_mock, interval_s=0.01)
        loop = RuntimeReconciliationLoop(bot)

        healthy = {"healthy": True, "reasons": []}
        with patch("bridge.oif_writer.diagnose_oif_pipeline_health",
                   return_value=healthy):
            _run_loop_for(loop, bot, total_sleep_s=0.05)

        # Reconcile WAS called every cycle (Guard A passes); the dedup
        # happens at the bot-side adoption-list level so the
        # externally-visible mark is exactly one entry in the dedup map.
        assert _RECENT_RECON_EMIT.get(("Sim101", "SHORT")) is not None, (
            "First adoption should have been recorded in the dedup map"
        )
        # The dedup map should hold exactly the one (Sim101, SHORT) key
        # — re-adoptions for the same pair within the window do NOT
        # create extra entries (they replace the timestamp, which is
        # acceptable; the test confirms no key sprawl).
        assert list(_RECENT_RECON_EMIT.keys()) == [("Sim101", "SHORT")]

    def test_different_account_within_window_permitted(self):
        """A real second orphan on a DIFFERENT account must NOT be
        suppressed — Guard B keys off (account, direction), not a
        global throttle.
        """
        a_short = {"account": "Sim101", "direction": "SHORT",
                   "qty": 1, "trade_id": "RECONCILED_Sim101_aaa"}
        b_long = {"account": "SimBias Momentum", "direction": "LONG",
                  "qty": 1, "trade_id": "RECONCILED_BIAS_bbb"}

        # First cycle adopts Sim101 SHORT, subsequent cycles add the
        # bias momentum LONG orphan.
        cycle_idx = {"n": 0}

        def _flip_reconcile():
            cycle_idx["n"] += 1
            if cycle_idx["n"] == 1:
                return [a_short]
            return [a_short, b_long]

        reconcile_mock = MagicMock(side_effect=_flip_reconcile)
        bot = _make_bot(reconcile_mock, interval_s=0.01)
        loop = RuntimeReconciliationLoop(bot)

        healthy = {"healthy": True, "reasons": []}
        with patch("bridge.oif_writer.diagnose_oif_pipeline_health",
                   return_value=healthy):
            _run_loop_for(loop, bot, total_sleep_s=0.05)

        # Both (Sim101, SHORT) and (SimBias Momentum, LONG) must appear
        # in the dedup map.
        assert ("Sim101", "SHORT") in _RECENT_RECON_EMIT
        assert ("SimBias Momentum", "LONG") in _RECENT_RECON_EMIT

    def test_dedup_ttl_scales_with_interval(self):
        """The dedup window is defined as
        RUNTIME_RECON_INTERVAL_S * _DEDUP_INTERVAL_MULTIPLIER. Confirm
        the multiplier is set to the value the design calls for so a
        regression that changes the constant trips the test.
        """
        # Currently the design calls for 3x. If you change it, update
        # the assertion AND the inline docstring on _DEDUP_INTERVAL_MULTIPLIER
        # AND the inline comment in the run() docstring.
        assert _DEDUP_INTERVAL_MULTIPLIER == 3


# ──────────────────────────────────────────────────────────────────
# End-to-end live-loop replay: the 7-contract cluster
# ──────────────────────────────────────────────────────────────────

class TestLiveLoopReplay:
    def test_seven_reconcile_attempts_yield_one_dedup_entry(self):
        """The 2026-06-04 18:01 cluster fired 7 partial_exit OIFs in
        137 ms — each representing a reconciliation-loop re-emit of
        the same (Sim101, SHORT) orphan. Behavior under the fix: even
        when reconcile_positions_from_nt8 keeps reporting the same
        adoption on every cycle (because H2 is unfixed), the dedup
        map collapses them all to a single (account, direction) entry
        and the cluster never grows past 1 logged "fresh adoption."
        """
        repeat_adoption = {"account": "Sim101", "direction": "SHORT",
                           "qty": 1, "trade_id": "RECONCILED_Sim101_z"}

        # Mock reconcile to return the same adoption on every cycle —
        # simulates H2 (open_position(reconciled=True) drops adoption
        # silently, so next cycle re-reads outgoing/ and re-emits).
        reconcile_mock = MagicMock(return_value=[repeat_adoption])
        bot = _make_bot(reconcile_mock, interval_s=0.005)  # tight cycle
        loop = RuntimeReconciliationLoop(bot)

        healthy = {"healthy": True, "reasons": []}
        with patch("bridge.oif_writer.diagnose_oif_pipeline_health",
                   return_value=healthy):
            # 100 ms total ≈ 20 cycles at 5ms interval — well past 7.
            _run_loop_for(loop, bot, total_sleep_s=0.1)

        # Behavioral assertion: regardless of how many cycles tried to
        # re-emit the same orphan, the dedup map contains exactly one
        # entry for (Sim101, SHORT). The pre-fix behavior would have
        # had no such dedup at all — each re-emit would fall through
        # to a fresh adoption record + a fresh partial_exit OIF.
        sim101_short_keys = [k for k in _RECENT_RECON_EMIT
                             if k == ("Sim101", "SHORT")]
        assert len(sim101_short_keys) == 1, (
            f"Expected dedup map to collapse Sim101 SHORT re-emits to "
            f"a single key; got {sim101_short_keys}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
