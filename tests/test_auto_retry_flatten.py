"""Auto-retry flatten for stuck exit_pending positions.

Forensic context: 2026-05-04 incident — two SHORT positions stuck in
NT8 for 1.5h and 3.5h respectively because:

  1. CLOSEPOSITION race: when the OCO stop fires AND a CLOSEPOSITION
     OIF arrives at the same instant, NT8 sometimes opens a fresh
     reverse position instead of no-op'ing (sees its stale "current
     position" cache).
  2. PhoenixOIFGuard quarantined CLOSEPOSITION OIFs without a trade_id
     (regex didn't match) — every retry was silently dropped.
  3. The bot's _resolve_exit_pending_positions() saw the divergence
     (Python flat / NT8 not flat), logged CRITICAL, and HALTED the
     strategy — but never retried the close. Operator intervention
     was required.

The fix replaces "log + halt + give up" with "auto-retry directional
MARKET cover order every cycle, escalate after 5 minutes". The
directional MARKET (BUY-to-cover SHORT, SELL-to-flatten LONG) bypasses
CLOSEPOSITION entirely, avoiding the OCO race.

These tests verify the new retry path:
  - BUY 1 MARKET emitted for stuck SHORT
  - SELL 1 MARKET emitted for stuck LONG
  - Retries fire on every reconciliation cycle (not one-shot)
  - Telegram + halt fire ONLY after the 5-minute escalation window
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
    """A minimal bot stub exposing only what _resolve_exit_pending_positions
    needs: positions, EXIT_PENDING_TIMEOUT_S, _conflict_reg, etc."""
    from core.position_manager import PositionManager
    bot = MagicMock()
    bot.positions = PositionManager()
    bot.EXIT_PENDING_TIMEOUT_S = 60.0
    bot._conflict_reg = None
    return bot


def _open_short_pending(pm, account="SimX", entry=27900.0, age_s=70.0):
    """Open a SHORT, mark exit_pending, age it past EXIT_PENDING_TIMEOUT_S."""
    pm.open_position(
        trade_id="TEST_SHORT_001", direction="SHORT", entry_price=entry,
        contracts=1, stop_price=entry + 25.0, target_price=entry - 37.5,
        strategy="test_strat", reason="t", account=account,
    )
    pm.mark_exit_pending("TEST_SHORT_001", exit_price=entry - 5.0,
                         exit_reason="stop_loss")
    pos = pm.get_position("TEST_SHORT_001")
    pos.exit_pending_since = time.time() - age_s
    return pos


def _open_long_pending(pm, account="SimX", entry=27900.0, age_s=70.0):
    pm.open_position(
        trade_id="TEST_LONG_001", direction="LONG", entry_price=entry,
        contracts=1, stop_price=entry - 25.0, target_price=entry + 37.5,
        strategy="test_strat", reason="t", account=account,
    )
    pm.mark_exit_pending("TEST_LONG_001", exit_price=entry + 5.0,
                         exit_reason="stop_loss")
    pos = pm.get_position("TEST_LONG_001")
    pos.exit_pending_since = time.time() - age_s
    return pos


# ─── core retry behaviour ─────────────────────────────────────────────

def test_short_stuck_triggers_buy_market_retry(fake_bot):
    """A SHORT pending past timeout, with NT8 still showing SHORT, must
    fire a BUY 1 MARKET cover order via write_oif."""
    from bots.base_bot import BaseBot
    _open_short_pending(fake_bot.positions, account="SimX")
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        BaseBot._resolve_exit_pending_positions(fake_bot)
    assert mock_write.called, "write_oif must be called on stuck retry"
    call_args = mock_write.call_args
    # First positional arg is action, kwargs has account, qty, etc.
    action = call_args.args[0] if call_args.args else call_args.kwargs.get("action")
    assert action == "BUY", f"SHORT cover must use BUY, got {action!r}"
    assert call_args.kwargs.get("qty") == 1
    assert call_args.kwargs.get("account") == "SimX"
    assert call_args.kwargs.get("order_type") == "MARKET"


def test_long_stuck_triggers_sell_market_retry(fake_bot):
    """A LONG pending past timeout must fire a SELL 1 MARKET."""
    from bots.base_bot import BaseBot
    _open_long_pending(fake_bot.positions, account="SimY")
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("LONG", 1, 27900.0)):
        BaseBot._resolve_exit_pending_positions(fake_bot)
    assert mock_write.called
    action = mock_write.call_args.args[0] if mock_write.call_args.args \
             else mock_write.call_args.kwargs.get("action")
    assert action == "SELL"
    assert mock_write.call_args.kwargs.get("account") == "SimY"


def test_retry_uses_market_not_closeposition(fake_bot):
    """Critical: must NOT use CLOSEPOSITION (races with OCO stop fill).
    Must use directional MARKET orders only."""
    from bots.base_bot import BaseBot
    _open_short_pending(fake_bot.positions)
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        BaseBot._resolve_exit_pending_positions(fake_bot)
    action = mock_write.call_args.args[0] if mock_write.call_args.args \
             else mock_write.call_args.kwargs.get("action")
    assert action not in ("CLOSEPOSITION", "EXIT", "EXIT_ALL", "CLOSE")
    assert action in ("BUY", "SELL")


# ─── retries fire EVERY cycle, not once ───────────────────────────────

def test_retry_fires_on_every_cycle(fake_bot):
    """Bug regression: previously, the timeout path halted-and-gave-up.
    Now it must retry on every reconciliation cycle while NT8 still shows
    a position."""
    from bots.base_bot import BaseBot
    _open_short_pending(fake_bot.positions)
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        # Three reconciliation cycles in a row, NT8 still stuck each time
        BaseBot._resolve_exit_pending_positions(fake_bot)
        BaseBot._resolve_exit_pending_positions(fake_bot)
        BaseBot._resolve_exit_pending_positions(fake_bot)
    assert mock_write.call_count == 3, (
        f"expected 3 retry attempts, got {mock_write.call_count}"
    )


def test_retry_includes_unique_trade_id_per_attempt(fake_bot):
    """Each retry must use a unique trade_id (with age_s suffix) so the
    OIF filename is fresh and not deduped by NT8."""
    from bots.base_bot import BaseBot
    _open_short_pending(fake_bot.positions, age_s=70.0)
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)):
        BaseBot._resolve_exit_pending_positions(fake_bot)
        # Bump age and retry
        pos = fake_bot.positions.get_position("TEST_SHORT_001")
        pos.exit_pending_since = time.time() - 100.0
        BaseBot._resolve_exit_pending_positions(fake_bot)
    tid_a = mock_write.call_args_list[0].kwargs.get("trade_id")
    tid_b = mock_write.call_args_list[1].kwargs.get("trade_id")
    assert tid_a != tid_b, "retry trade_ids must differ between attempts"
    assert "TEST_SHORT_001" in tid_a and "TEST_SHORT_001" in tid_b
    assert tid_a.startswith("TEST_SHORT_001_retry")


# ─── escalation window: telegram + halt ONLY after 5 minutes ─────────

def test_no_halt_within_escalation_window(fake_bot):
    """At age=70s (just past EXIT_PENDING_TIMEOUT_S=60s), retry happens
    but halt + telegram do NOT fire — operator hasn't been paged yet."""
    from bots.base_bot import BaseBot
    _open_short_pending(fake_bot.positions, age_s=70.0)
    with patch("bridge.oif_writer.write_oif"), \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)), \
         patch("core.telegram_notifier.send_sync") as mock_tg, \
         patch("core.strategy_risk_registry.StrategyRiskRegistry") as mock_reg_cls:
        BaseBot._resolve_exit_pending_positions(fake_bot)
    # Within the 5-minute window: no telegram, no halt
    assert not mock_tg.called, "telegram fired too early (within 5min window)"
    # halt_strategy should not have been invoked
    if mock_reg_cls.called:
        reg = mock_reg_cls.return_value
        assert not reg.halt_strategy.called


def test_halt_fires_after_escalation_window(fake_bot):
    """At age=400s (>5min), halt + telegram DO fire. Retry still happens too."""
    from bots.base_bot import BaseBot
    _open_short_pending(fake_bot.positions, age_s=400.0)
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27900.0)), \
         patch("core.telegram_notifier.send_sync") as mock_tg:
        BaseBot._resolve_exit_pending_positions(fake_bot)
    assert mock_write.called, "retry must STILL fire after escalation"
    assert mock_tg.called, "telegram must fire past 5-minute escalation window"


# ─── direction is taken from NT8, NOT Python state (desync safety) ────

def test_cover_action_uses_nt8_direction_when_python_says_long(fake_bot):
    """REGRESSION: 2026-05-04 08:01 — Python pos.direction was LONG but
    NT8 actually held SHORT 1 (phantom from CLOSEPOSITION-vs-OCO race).
    Old code computed `cover = "SELL"` from pos.direction, which NT8
    rejected (`Exceeds account's maximum position quantity` — SELL on
    top of SHORT 1 would mean SHORT 2). New code uses NT8's reported
    direction, so phantom SHORT → BUY 1 MARKET (correct cover)."""
    from bots.base_bot import BaseBot
    pm = fake_bot.positions
    # Python state says LONG (the ORIGINAL position before the race).
    pm.open_position(
        trade_id="desync1", direction="LONG", entry_price=27800.0,
        contracts=1, stop_price=27775.0, target_price=27825.0,
        strategy="bias_momentum", reason="t", account="SimBias Momentum",
    )
    pm.mark_exit_pending("desync1", exit_price=27775.0, exit_reason="stop_loss")
    pm.get_position("desync1").exit_pending_since = time.time() - 70.0

    # NT8 says SHORT (the phantom flip).
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 1, 27802.25)):
        BaseBot._resolve_exit_pending_positions(fake_bot)

    assert mock_write.called
    action = mock_write.call_args.args[0] if mock_write.call_args.args \
             else mock_write.call_args.kwargs.get("action")
    # Critical: must be BUY (covers SHORT), NOT SELL (which NT8 rejected)
    assert action == "BUY", (
        f"phantom SHORT must be covered with BUY, not {action!r} — "
        f"sending SELL on top of SHORT 1 triggers NT8's max-position "
        f"rejection (the exact bug seen in production at 08:01:06)"
    )
    assert mock_write.call_args.kwargs.get("account") == "SimBias Momentum"


def test_cover_action_uses_nt8_direction_when_python_says_short(fake_bot):
    """Mirror case: Python SHORT, NT8 actually LONG → SELL 1 MARKET."""
    from bots.base_bot import BaseBot
    pm = fake_bot.positions
    pm.open_position(
        trade_id="desync2", direction="SHORT", entry_price=27800.0,
        contracts=1, stop_price=27825.0, target_price=27775.0,
        strategy="bias_momentum", reason="t", account="SimX",
    )
    pm.mark_exit_pending("desync2", exit_price=27825.0, exit_reason="stop_loss")
    pm.get_position("desync2").exit_pending_since = time.time() - 70.0

    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("LONG", 1, 27802.25)):
        BaseBot._resolve_exit_pending_positions(fake_bot)

    action = mock_write.call_args.args[0] if mock_write.call_args.args \
             else mock_write.call_args.kwargs.get("action")
    assert action == "SELL"


def test_cover_qty_uses_nt8_qty_not_python_qty(fake_bot):
    """If NT8 reports SHORT 2 (somehow ended up oversized), cover should
    BUY 2, not BUY 1 — flatten the WHOLE actual NT8 position."""
    from bots.base_bot import BaseBot
    pm = fake_bot.positions
    pm.open_position(
        trade_id="oversize1", direction="LONG", entry_price=27800.0,
        contracts=1, stop_price=27775.0, target_price=27825.0,
        strategy="bias_momentum", reason="t", account="SimX",
    )
    pm.mark_exit_pending("oversize1", exit_price=27775.0, exit_reason="stop_loss")
    pm.get_position("oversize1").exit_pending_since = time.time() - 70.0

    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=("SHORT", 2, 27802.25)):  # 2 contracts in NT8
        BaseBot._resolve_exit_pending_positions(fake_bot)

    assert mock_write.call_args.kwargs.get("qty") == 2


# ─── happy path: NT8 confirmed FLAT cleans up normally ────────────────

def test_finalize_when_nt8_confirms_flat(fake_bot):
    """When NT8 reports FLAT (position file says FLAT or missing),
    the position is finalized normally. No retry fires."""
    from bots.base_bot import BaseBot
    _open_short_pending(fake_bot.positions)
    with patch("bridge.oif_writer.write_oif") as mock_write, \
         patch("core.startup_reconciliation._read_position_file",
               return_value=None):  # None = FLAT
        BaseBot._resolve_exit_pending_positions(fake_bot)
    assert not mock_write.called, "retry must NOT fire when NT8 is FLAT"
    # Position should now be finalized (closed and removed)
    assert fake_bot.positions.get_position("TEST_SHORT_001") is None
