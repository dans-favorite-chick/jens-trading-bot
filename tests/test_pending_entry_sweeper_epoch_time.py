"""
Finding E - PendingEntrySweeper writes float-epoch exit_time.

Pins the 2026-06-02 fix: _record_terminal_to_trade_memory(pe) now stamps
exit_time as time.time() (matching every other trade_memory writer) and
preserves the human-readable timestamp under exit_time_iso for forensics.

Background: the prior implementation wrote exit_time as an ISO string,
which broke:
  - dashboard equity-curve _exit_key float() coercion
  - RiskManager hydrate_from_trades since-cutoff comparison
    (44 NO_FILL records silently dropped vs the numeric cutoff)

Every other trade_memory writer (position_manager.close_position,
scale_out_partial) uses time.time() for exit_time. This test pins
PendingEntrySweeper to the same contract.

Run: pytest tests/test_pending_entry_sweeper_epoch_time.py -v
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from bots._pending_entry_sweeper import PendingEntrySweeper


# ─── Fakes ────────────────────────────────────────────────────────────────────

class _FakeTradeMemory:
    """Captures TradeMemory.record(row, bot_id=...) calls into a list."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, str]] = []

    def record(self, row, bot_id="unknown"):
        # Defensive copy so later code mutating the dict can't taint
        # what we asserted on.
        self.calls.append((dict(row), bot_id))


def _make_bot(bot_name: str = "test_bot") -> SimpleNamespace:
    """Minimal bot stub with the two attributes the sweeper reads:
    .trade_memory (a TradeMemory-like) and .bot_name (str)."""
    return SimpleNamespace(
        trade_memory=_FakeTradeMemory(),
        bot_name=bot_name,
    )


def _make_pe(
    *,
    trade_id: str = "abc123",
    strategy: str = "bias_momentum",
    account: str = "SimBias Momentum",
    side: str = "BUY",
    limit_price: float = 28300.75,
    qty: int = 1,
    terminal_state: str = "timeout_cancelled",
    terminal_reason: str = "age>timeout",
    instrument: str = "MNQM6",
) -> SimpleNamespace:
    """Fake PendingEntry mirroring the attributes referenced inside
    _record_terminal_to_trade_memory."""
    return SimpleNamespace(
        trade_id=trade_id,
        strategy=strategy,
        account=account,
        side=side,
        limit_price=limit_price,
        qty=qty,
        terminal_state=terminal_state,
        terminal_reason=terminal_reason,
        instrument=instrument,
    )


# ─── Finding E test ───────────────────────────────────────────────────────────

class TestPendingEntrySweeperEpochTime:
    """Pins that exit_time is a float epoch and exit_time_iso is preserved."""

    def test_terminal_row_exit_time_is_epoch_float(self):
        """Primary assertion (Finding E): _record_terminal_to_trade_memory
        must write exit_time as a float epoch, not an ISO string.

        Verifies:
          1) exit_time is numeric (int or float, NOT bool or str)
          2) exit_time is within 5 seconds of time.time()
          3) exit_time_iso key exists and is a valid ISO-8601 string
             (parseable via datetime.fromisoformat())
          4) bot_id was forwarded from self.bot.bot_name
        """
        from datetime import datetime as _dt

        bot = _make_bot(bot_name="sim_bot")
        sweeper = PendingEntrySweeper(bot)
        pe = _make_pe()

        t_before = time.time()
        sweeper._record_terminal_to_trade_memory(pe)
        t_after = time.time()

        assert len(bot.trade_memory.calls) == 1, (
            "TradeMemory.record() should have been called exactly once; "
            f"got {len(bot.trade_memory.calls)} call(s)"
        )
        captured_row, captured_bot_id = bot.trade_memory.calls[0]

        # 1) numeric type — and NOT a bool (bool is an int subclass in Python,
        #    so isinstance(True, int) is True; guard explicitly).
        assert "exit_time" in captured_row, "row missing 'exit_time'"
        exit_time = captured_row["exit_time"]
        assert not isinstance(exit_time, bool), (
            f"exit_time must not be a bool; got {exit_time!r}"
        )
        assert isinstance(exit_time, (int, float)), (
            f"exit_time must be a numeric epoch (int/float); "
            f"got type {type(exit_time).__name__} value={exit_time!r}"
        )

        # 2) within 5s of now — bracket by the timestamps we sampled
        #    around the call, so the test is robust to slow CI.
        assert (t_before - 1.0) <= exit_time <= (t_after + 1.0), (
            f"exit_time={exit_time} is not within the call window "
            f"[{t_before}, {t_after}]"
        )
        assert abs(exit_time - time.time()) < 5.0, (
            f"exit_time={exit_time} not within 5s of time.time()={time.time()}"
        )

        # 3) exit_time_iso forensic key present and parseable as ISO-8601
        assert "exit_time_iso" in captured_row, (
            "row missing 'exit_time_iso' — Finding E fix must preserve "
            "the human-readable timestamp under this key"
        )
        iso = captured_row["exit_time_iso"]
        assert isinstance(iso, str), (
            f"exit_time_iso must be a string; got type {type(iso).__name__}"
        )
        # Must parse cleanly via datetime.fromisoformat() — not just regex
        try:
            _dt.fromisoformat(iso)
        except (ValueError, TypeError) as exc:
            pytest.fail(
                f"exit_time_iso {iso!r} does not parse as ISO-8601: {exc}"
            )

        # 4) bot_id forwarded from self.bot.bot_name
        assert captured_bot_id == "sim_bot", (
            f"bot_id should be forwarded from bot.bot_name; got {captured_bot_id!r}"
        )

    def test_row_carries_no_fill_result_and_terminal_state(self):
        """Guard: result/pnl_dollars/terminal_state fields unchanged by the fix."""
        bot = _make_bot()
        sweeper = PendingEntrySweeper(bot)
        sweeper._record_terminal_to_trade_memory(_make_pe(terminal_state="timeout_cancelled"))
        row, _ = bot.trade_memory.calls[0]
        assert row["result"] == "NO_FILL"
        assert row["pnl_dollars"] == 0.0
        assert row["terminal_state"] == "timeout_cancelled"
        assert row["reason"] == "pending_entry_timeout_cancelled"
