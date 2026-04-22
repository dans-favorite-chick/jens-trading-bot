"""
P0.3 — Runtime reconciliation timer (D12).

`startup_reconciliation` originally only ran at boot. If a trade broke
mid-session (NT8 has position, Phoenix ledger doesn't — or vice versa),
the divergence went unnoticed until the next restart. P0.3 puts the same
`reconcile_positions_from_nt8` on a 30-second timer during the session
so mid-session orphans get adopted within seconds of appearing.

Correctness requirements tested here:
  - Timer invokes reconcile on the configured interval
  - Idempotency: re-calling reconcile after an adoption must NOT
    re-adopt (phantom multiplication bug)
  - Telegram alert fires on adoption (forensic trail)
  - Loop respects `_shutdown_reconciliation` so bot shutdown doesn't
    hang waiting for the next sleep

Run: pytest tests/test_runtime_reconciliation.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.startup_reconciliation as recon
from core.position_manager import PositionManager


# ═══════════════════════════════════════════════════════════════════
# Idempotency — reconcile must skip accounts already tracked
# ═══════════════════════════════════════════════════════════════════
class TestIdempotency:
    """
    The runtime timer calls reconcile_positions_from_nt8 every 30s. Each
    call scans NT8's outgoing/ — an adopted orphan keeps showing up
    because NT8 still has the position. Without idempotency, every cycle
    would spawn a new phantom Position record in the manager.
    """

    def _mock_outgoing(self, tmp_path, account, direction, qty, avg_price,
                       instrument="MNQM6"):
        """Write the NT8 position file format that reconcile expects."""
        fname = f"{instrument} Globex_{account}_position.txt"
        (tmp_path / fname).write_text(f"{direction};{qty};{avg_price}\n")

    def test_first_call_adopts_orphan(self, tmp_path):
        self._mock_outgoing(tmp_path, "SimOpenDrive", "LONG", 1, 26741.25)
        pm = PositionManager()
        mock_oco = MagicMock(return_value=[str(tmp_path / "oco.txt")])

        adopted = recon.reconcile_positions_from_nt8(
            positions=pm,
            outgoing_dir=str(tmp_path),
            instrument="MNQM6",
            routed_accounts=["SimOpenDrive"],
            oco_writer=mock_oco,
        )
        assert len(adopted) == 1
        assert adopted[0]["account"] == "SimOpenDrive"
        assert pm.active_count == 1

    def test_second_call_does_not_re_adopt_same_account(self, tmp_path):
        """
        Same NT8 state across two calls. First adopts; second must see the
        account already tracked in PositionManager and skip.
        """
        self._mock_outgoing(tmp_path, "SimOpenDrive", "LONG", 1, 26741.25)
        pm = PositionManager()
        mock_oco = MagicMock(return_value=[str(tmp_path / "oco.txt")])

        first = recon.reconcile_positions_from_nt8(
            positions=pm, outgoing_dir=str(tmp_path), instrument="MNQM6",
            routed_accounts=["SimOpenDrive"], oco_writer=mock_oco,
        )
        second = recon.reconcile_positions_from_nt8(
            positions=pm, outgoing_dir=str(tmp_path), instrument="MNQM6",
            routed_accounts=["SimOpenDrive"], oco_writer=mock_oco,
        )
        assert len(first) == 1
        assert len(second) == 0  # idempotent: nothing new to adopt
        assert pm.active_count == 1  # no phantom multiplication

    def test_different_account_still_adopted_on_second_call(self, tmp_path):
        """
        Idempotency only gates by account. A *new* orphan on a different
        account must still be adopted on a later cycle.
        """
        self._mock_outgoing(tmp_path, "SimOpenDrive", "LONG", 1, 26741.25)
        pm = PositionManager()
        mock_oco = MagicMock(return_value=[str(tmp_path / "oco.txt")])

        first = recon.reconcile_positions_from_nt8(
            positions=pm, outgoing_dir=str(tmp_path), instrument="MNQM6",
            routed_accounts=["SimOpenDrive"], oco_writer=mock_oco,
        )
        # Mid-session: a new orphan appears on SimORB
        self._mock_outgoing(tmp_path, "SimORB", "SHORT", 2, 26800.0)

        second = recon.reconcile_positions_from_nt8(
            positions=pm, outgoing_dir=str(tmp_path), instrument="MNQM6",
            routed_accounts=["SimOpenDrive", "SimORB"], oco_writer=mock_oco,
        )
        assert len(second) == 1
        assert second[0]["account"] == "SimORB"
        assert pm.active_count == 2


# ═══════════════════════════════════════════════════════════════════
# Runtime timer loop — invocation cadence + shutdown
#
# These tests drive the async loop via asyncio.run() rather than the
# pytest-asyncio plugin (not installed in this env). Each test is a
# sync function that builds an async coroutine + runs it.
# ═══════════════════════════════════════════════════════════════════
def _make_bot_for_loop_test(reconcile_fn, interval_s=0.01):
    """Construct a MagicMock with just enough surface for the loop to run."""
    from bots.base_bot import BaseBot

    bot = MagicMock(spec=BaseBot)
    bot.RUNTIME_RECON_INTERVAL_S = interval_s
    bot._shutdown_reconciliation = False
    bot._reconcile_positions_from_nt8 = reconcile_fn
    # Bind the unbound async method to our mock.
    bot._runtime_reconciliation_loop = (
        BaseBot._runtime_reconciliation_loop.__get__(bot)
    )
    return bot


class TestRuntimeLoop:
    def test_loop_invokes_reconcile_on_interval(self):
        reconcile_mock = MagicMock(return_value=[])
        bot = _make_bot_for_loop_test(reconcile_mock)

        async def _run():
            task = asyncio.create_task(bot._runtime_reconciliation_loop())
            await asyncio.sleep(0.05)  # let a handful of cycles run
            bot._shutdown_reconciliation = True
            try:
                await asyncio.wait_for(task, timeout=0.1)
            except asyncio.TimeoutError:
                task.cancel()

        asyncio.run(_run())
        assert reconcile_mock.call_count >= 2

    def test_loop_respects_shutdown_flag(self):
        """Flipping _shutdown_reconciliation ends the loop cleanly."""
        reconcile_mock = MagicMock(return_value=[])
        bot = _make_bot_for_loop_test(reconcile_mock)

        async def _run():
            bot._shutdown_reconciliation = True  # flip BEFORE starting
            task = asyncio.create_task(bot._runtime_reconciliation_loop())
            # Loop should exit after the first sleep even if we never
            # change the flag again. Timeout well above interval (0.01s)
            # would indicate the loop is ignoring the flag.
            await asyncio.wait_for(task, timeout=0.2)

        asyncio.run(_run())  # completes without raising TimeoutError

    def test_loop_survives_exception_in_reconcile(self):
        """One bad cycle must NOT kill the loop — next tick keeps trying."""
        call_count = {"n": 0}

        def _flaky_reconcile():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first-call failure")
            return []

        bot = _make_bot_for_loop_test(_flaky_reconcile)

        async def _run():
            task = asyncio.create_task(bot._runtime_reconciliation_loop())
            await asyncio.sleep(0.05)
            bot._shutdown_reconciliation = True
            try:
                await asyncio.wait_for(task, timeout=0.1)
            except asyncio.TimeoutError:
                task.cancel()

        asyncio.run(_run())
        assert call_count["n"] >= 2


# ═══════════════════════════════════════════════════════════════════
# Telegram alert on adoption (forensic trail)
# ═══════════════════════════════════════════════════════════════════
class TestTelegramAlert:
    def test_telegram_fired_on_adoption(self, tmp_path):
        """The telegram_notify kwarg is the hook for P0.3's 'loud alert'
        requirement. Verify it fires with the account and direction."""
        fname = "MNQM6 Globex_SimOpenDrive_position.txt"
        (tmp_path / fname).write_text("LONG;1;26741.25\n")

        pm = PositionManager()
        telegram = MagicMock()
        mock_oco = MagicMock(return_value=[str(tmp_path / "oco.txt")])

        recon.reconcile_positions_from_nt8(
            positions=pm,
            outgoing_dir=str(tmp_path),
            instrument="MNQM6",
            routed_accounts=["SimOpenDrive"],
            oco_writer=mock_oco,
            telegram_notify=telegram,
        )
        telegram.assert_called_once()
        msg = telegram.call_args.args[0]
        assert "SimOpenDrive" in msg
        assert "LONG" in msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
