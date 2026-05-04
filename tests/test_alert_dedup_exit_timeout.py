"""EXIT_TIMEOUT alert dedup — Sprint D F1 regression tests.

Forensic 2026-05-04: 26 duplicate EXIT_TIMEOUT pages in 13h for the
same 2 stuck positions. Dedup key was per-trade_id but TTL was 15min,
so the same condition kept re-firing every window.

New behavior:
  - One-shot at first crossing of RETRY_ESCALATE_S (5 minutes).
  - Hourly rollup if still stuck after that.
  - Single RESOLVED notification when NT8 confirms FLAT.

Tests verify each transition by patching send_sync and counting calls.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


@pytest.fixture
def fake_bot():
    from core.position_manager import PositionManager
    bot = MagicMock()
    bot.positions = PositionManager()
    bot.EXIT_PENDING_TIMEOUT_S = 60.0
    bot._conflict_reg = None
    return bot


def _open_short_pending_at_age(pm, account="SimX", entry=27900.0,
                               age_s=320.0):
    """Open a SHORT, mark exit_pending, age it past RETRY_ESCALATE_S=300s."""
    pm.open_position(
        trade_id="STUCK1", direction="SHORT", entry_price=entry,
        contracts=1, stop_price=entry + 25.0, target_price=entry - 37.5,
        strategy="test_strat", reason="t", account=account,
    )
    pm.mark_exit_pending("STUCK1", exit_price=entry - 5.0,
                         exit_reason="stop_loss")
    pos = pm.get_position("STUCK1")
    pos.exit_pending_since = time.time() - age_s
    return pos


# ─── one-shot at first threshold crossing ────────────────────────────

def test_one_telegram_at_first_threshold_crossing(fake_bot):
    """Cross 5min threshold once → exactly 1 telegram fires."""
    from bots.base_bot import BaseBot
    _open_short_pending_at_age(fake_bot.positions, age_s=320.0)
    with patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        BaseBot._resolve_exit_pending_positions(fake_bot)
    # Exactly 1 telegram for the initial escalation
    assert mock_tg.call_count == 1
    msg = mock_tg.call_args.args[0] if mock_tg.call_args.args \
          else mock_tg.call_args.kwargs.get("message")
    assert "EXIT_TIMEOUT" in msg
    assert "STILL_STUCK" not in msg  # not the rollup variant


# ─── no spam on subsequent polls ─────────────────────────────────────

def test_no_spam_on_subsequent_polls(fake_bot):
    """50 reconciliation cycles with NT8 still stuck → still 1 telegram."""
    from bots.base_bot import BaseBot
    _open_short_pending_at_age(fake_bot.positions, age_s=320.0)
    with patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        for _ in range(50):
            BaseBot._resolve_exit_pending_positions(fake_bot)
    assert mock_tg.call_count == 1, (
        f"expected exactly 1 telegram across 50 cycles, "
        f"got {mock_tg.call_count} (was 26 in production before fix)"
    )


# ─── pre-threshold: nothing fires ────────────────────────────────────

def test_no_alert_before_escalation_window(fake_bot):
    """At age=120s (past EXIT_PENDING_TIMEOUT_S=60s but before
    RETRY_ESCALATE_S=300s), no telegram fires — bot is still in the
    'silently retrying' window."""
    from bots.base_bot import BaseBot
    _open_short_pending_at_age(fake_bot.positions, age_s=120.0)
    with patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        BaseBot._resolve_exit_pending_positions(fake_bot)
    assert mock_tg.call_count == 0


# ─── hourly rollup ───────────────────────────────────────────────────

def test_hourly_rollup_after_initial(fake_bot):
    """After the initial alert, the next telegram fires only after
    HOURLY_ROLLUP_S (3600s) has passed."""
    from bots.base_bot import BaseBot
    pos = _open_short_pending_at_age(fake_bot.positions, age_s=320.0)
    with patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        # First cycle: initial alert fires
        BaseBot._resolve_exit_pending_positions(fake_bot)
        assert mock_tg.call_count == 1
        # Manually simulate "30 minutes pass and still stuck" — bump
        # exit_pending_since BACKWARDS so age_s grows but not past 1h yet.
        pos.exit_pending_since = time.time() - (320.0 + 1800.0)  # 30+ min later
        BaseBot._resolve_exit_pending_positions(fake_bot)
        assert mock_tg.call_count == 1, (
            "rollup must NOT fire before HOURLY_ROLLUP_S elapses"
        )
        # Now fast-forward the alert ts so the next cycle counts as 1h+ later
        pos._exit_timeout_last_alert_ts = time.time() - 3700.0
        pos.exit_pending_since = time.time() - (320.0 + 3700.0)
        BaseBot._resolve_exit_pending_positions(fake_bot)
        assert mock_tg.call_count == 2
        # The rollup message uses the STILL_STUCK wording
        rollup_msg = mock_tg.call_args.args[0] if mock_tg.call_args.args \
                     else mock_tg.call_args.kwargs.get("message")
        assert "STILL_STUCK" in rollup_msg


# ─── RESOLVED notification when NT8 confirms FLAT ────────────────────

def test_resolved_telegram_when_nt8_confirms_flat(fake_bot):
    """If we paged the operator and NT8 then goes FLAT → RESOLVED page."""
    from bots.base_bot import BaseBot
    _open_short_pending_at_age(fake_bot.positions, age_s=320.0)
    # First cycle: NT8 still stuck → initial alert fires
    with patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        BaseBot._resolve_exit_pending_positions(fake_bot)
        assert mock_tg.call_count == 1
        # Next cycle: NT8 reports FLAT → RESOLVED telegram fires
        with patch("core.startup_reconciliation._read_position_file",
                   return_value=None):
            BaseBot._resolve_exit_pending_positions(fake_bot)
        assert mock_tg.call_count == 2
        last_msg = mock_tg.call_args.args[0] if mock_tg.call_args.args \
                   else mock_tg.call_args.kwargs.get("message")
        assert "RESOLVED" in last_msg


def test_no_resolved_telegram_if_not_alerted(fake_bot):
    """If NT8 went FLAT before we ever paged (clean fast close), do NOT
    fire a RESOLVED — there was nothing to resolve."""
    from bots.base_bot import BaseBot
    # Mark exit_pending but keep age YOUNG (below RETRY_ESCALATE_S)
    _open_short_pending_at_age(fake_bot.positions, age_s=10.0)
    with patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=None):  # FLAT — clean close
        BaseBot._resolve_exit_pending_positions(fake_bot)
    assert mock_tg.call_count == 0


# ─── combined: forensic incident replay (26 → ~3) ────────────────────

def test_thirteen_hour_stuck_only_alerts_a_handful_of_times(fake_bot):
    """REGRESSION: simulate the 13h forensic incident — at each of 26
    polling cycles (one per ~30min), only ~14 telegrams should fire
    (1 initial + 12 hourly rollups + 1 RESOLVED when finally cleared),
    NOT 26+."""
    from bots.base_bot import BaseBot
    pos = _open_short_pending_at_age(fake_bot.positions, age_s=320.0)
    with patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        # 26 polls spaced 30min apart, simulating the original 13h window
        base_age = 320.0
        for i in range(26):
            pos.exit_pending_since = time.time() - (base_age + i * 1800)
            # Force the rollup state machine: pretend we crossed the
            # hourly boundary every other poll (every 60min)
            if i > 0 and i % 2 == 0:
                pos._exit_timeout_last_alert_ts = (
                    time.time() - (3700.0 + (i * 1))
                )
            BaseBot._resolve_exit_pending_positions(fake_bot)
    # Worst case is 1 initial + ~12 hourly rollups = 13. Pre-fix saw 26+.
    # Strict assertion: must be MUCH less than 26.
    assert mock_tg.call_count <= 14, (
        f"expected <=14 telegrams in 13h-window simulation, got "
        f"{mock_tg.call_count} (pre-fix saw 26+)"
    )
    assert mock_tg.call_count >= 1, "must have fired at least the initial alert"
