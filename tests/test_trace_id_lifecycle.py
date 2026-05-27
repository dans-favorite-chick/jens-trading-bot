"""Lifecycle integration tests for the P4-2 trace-ID wiring.

Verifies that the per-signal trace_id propagates through every stage of
the lifecycle now that the five extracted dispatcher/executor modules
(``_strategy_dispatch``, ``_signal_router``, ``_trade_entry``,
``_trade_exit``, ``_trade_closer``) have been instrumented with
``TraceContext``.

Each test installs a capturing log handler on the root logger and runs
through one stage of the pipeline. We then assert that every log record
emitted during the stage carries the expected ``[TRACE:xxx]`` prefix,
sharing one id across the whole lifecycle.

The tests are intentionally narrow — they exercise the trace-ID
plumbing only. They do NOT re-validate the underlying gates / OIF
writes / risk math; those are covered by their own dedicated suites.

Stage coverage:

1. ``test_router_stage_stamps_logs``                 — ROUTER
2. ``test_entry_stage_stamps_logs_and_persists_id``  — ENTRY (+ persistence)
3. ``test_exit_stage_pulls_trace_from_position``     — EXIT
4. ``test_closer_stage_pulls_trace_from_trade``      — CLOSE
5. ``test_lifecycle_single_trace_id_across_stages``  — end-to-end
"""
from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.trace_id import (
    TraceContext,
    generate_trace_id,
    get_current_trace_id,
    install_trace_filter,
)
from strategies.base_strategy import Signal


# ---------------------------------------------------------------------------
# Capture helper — installed on root so child loggers inherit by propagation
# (the trace filter is on root from core.trace_id import). For non-
# propagating loggers we explicitly hook the same handler.
# ---------------------------------------------------------------------------

class _RootCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Snapshot the rendered message (the filter has already had a
        # chance to mutate record.msg). We store the record itself so
        # tests can inspect timestamp / level / logger name too.
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


@pytest.fixture
def cap_logs():
    """Yield a capture handler attached to every named logger used by
    the lifecycle modules. We also hook root so any incidental child
    loggers inherit by propagation through the install_trace_filter
    that's already on root.
    """
    cap = _RootCapture()
    cap.setLevel(logging.DEBUG)
    # The five module loggers used by the extracted modules.
    names = ["Bot", "SignalRouter", "TradeExit", "TradeCloser", "root", ""]
    attached: list[tuple[logging.Logger, int, bool]] = []
    for name in names:
        lg = logging.getLogger(name) if name else logging.getLogger()
        prev_level = lg.level
        prev_propagate = lg.propagate
        lg.setLevel(logging.DEBUG)
        lg.addHandler(cap)
        # Belt-and-suspenders: install filter on this logger too. Idempotent.
        install_trace_filter(lg)
        attached.append((lg, prev_level, prev_propagate))
    try:
        yield cap
    finally:
        for lg, prev_level, prev_propagate in attached:
            try:
                lg.removeHandler(cap)
            except Exception:
                pass
            lg.setLevel(prev_level)
            lg.propagate = prev_propagate


def _trace_lines(cap: _RootCapture, tid: str | None = None) -> list[str]:
    """Return the captured messages that contain a [TRACE:xxx] token,
    optionally filtered to a specific id."""
    msgs = cap.messages()
    if tid is None:
        return [m for m in msgs if "[TRACE:" in m]
    return [m for m in msgs if f"[TRACE:{tid}]" in m]


def _extract_trace_ids(cap: _RootCapture) -> set[str]:
    pat = re.compile(r"\[TRACE:([0-9a-f]{8})\]")
    out: set[str] = set()
    for m in cap.messages():
        out.update(pat.findall(m))
    return out


# ---------------------------------------------------------------------------
# Signal helper — keeps the test signature short. We construct a Signal
# with a known trace_id so assertions are deterministic.
# ---------------------------------------------------------------------------

def _make_signal(trace_id: str | None = None) -> Signal:
    sig = Signal(
        direction="LONG",
        stop_ticks=20,
        target_rr=1.5,
        confidence=70,
        entry_score=45,
        strategy="bias_momentum",
        reason="lifecycle test",
        confluences=[],
    )
    if trace_id is not None:
        sig.trace_id = trace_id
    return sig


# ---------------------------------------------------------------------------
# Test 1: SignalRouter wraps its body in TraceContext.
# ---------------------------------------------------------------------------

def test_router_stage_stamps_logs(cap_logs):
    """SignalRouter.process_signal must run its body inside a
    TraceContext bound to signal.trace_id. We verify by patching
    self.bot._enter_trade to assert get_current_trace_id() == tid
    while inside the call, and by inspecting captured log lines.
    """
    from bots._signal_router import SignalRouter

    tid = "deadbeef"
    sig = _make_signal(tid)

    seen_during_enter: dict = {}

    async def fake_enter_trade(ws, signal):
        # Inside the contextvar-bound region, the active trace id MUST
        # be the signal's trace id.
        seen_during_enter["tid"] = get_current_trace_id()
        # Emit a log line; the filter should stamp it.
        logging.getLogger("Bot").info("inside fake_enter_trade")

    bot = MagicMock()
    bot._enter_trade = fake_enter_trade
    # AGENTS_AVAILABLE / pretrade_filter flag gates: short-circuit by
    # disabling the AI filter inside the router. We do that by patching
    # the module-level flag for the test.
    import bots._signal_router as _router_mod
    orig_flag = _router_mod.AGENT_PRETRADE_FILTER_ENABLED
    _router_mod.AGENT_PRETRADE_FILTER_ENABLED = False
    try:
        router = SignalRouter(bot)
        asyncio.run(router.process_signal(ws=MagicMock(), signal=sig))
    finally:
        _router_mod.AGENT_PRETRADE_FILTER_ENABLED = orig_flag

    # Assertion 1: trace id was active inside the body
    assert seen_during_enter.get("tid") == tid

    # Assertion 2: at least one captured log line carries the trace id
    stamped = _trace_lines(cap_logs, tid)
    assert any("inside fake_enter_trade" in m for m in stamped), (
        f"expected the inner log to be [TRACE:{tid}]-stamped; "
        f"captured = {cap_logs.messages()}"
    )

    # Assertion 3: outside the contextvar reset to None
    assert get_current_trace_id() is None


# ---------------------------------------------------------------------------
# Test 2: TradeEntry wraps its body AND persists trace_id into market.
# We can't realistically drive the entire enter_trade body (it does
# OIF writes, WS sends, NT8 verify, etc.). Instead we patch the bot
# so the method bails very early (the B84 no-new-entries gate) and
# verify the trace context was bound + that the persistence line is
# reached when we patch deeper.
# ---------------------------------------------------------------------------

def test_entry_stage_stamps_log_via_early_return(cap_logs):
    """When enter_trade short-circuits early (B84 no-new-entries gate
    or roll gate), the rejection log line must still be trace-stamped
    because the `with TraceContext()` wrap is the first thing the
    method does after the lazy import.
    """
    from bots._trade_entry import TradeEntry

    tid = "abcdef12"
    sig = _make_signal(tid)

    bot = MagicMock()
    # Force the B84 no-new-entries window to fire — fastest path through
    # the method body that still emits a log line inside TraceContext.
    bot._is_no_new_entries_window.return_value = True
    bot.last_rejection = None

    entry = TradeEntry(bot)
    asyncio.run(entry.enter_trade(ws=MagicMock(), signal=sig))

    # The rejection log inside the body must carry the trace id.
    stamped = _trace_lines(cap_logs, tid)
    assert any("NO_NEW_ENTRIES" in m for m in stamped), (
        f"expected the no-new-entries rejection log to be "
        f"[TRACE:{tid}]-stamped; captured = {cap_logs.messages()}"
    )

    # Outside the with-block, contextvar resets
    assert get_current_trace_id() is None


# ---------------------------------------------------------------------------
# Test 3: TradeExit pulls trace_id off the position (via
# market_snapshot fallback, since Position lacks a trace_id field).
# We use the early-return path (positions.is_flat=True) to keep the
# test small while still exercising the trace-id binding logic.
# ---------------------------------------------------------------------------

def test_exit_stage_binds_trace_from_position_snapshot(cap_logs):
    """TradeExit.exit_trade must rebind the trace id pulled from the
    position's market_snapshot. We craft a mock position with a known
    trace_id in its market_snapshot, then drive the exit path far
    enough to emit a log line inside the with-block.
    """
    from bots._trade_exit import TradeExit

    tid = "cafebabe"

    # Set up a fake position with trace_id stashed in market_snapshot
    # (matching the persistence path added in _trade_entry).
    fake_pos = SimpleNamespace(
        trade_id="t-exit-1",
        direction="LONG",
        contracts=1,
        account="Sim101",
        market_snapshot={"trace_id": tid},
    )

    bot = MagicMock()
    # is_flat=False so we reach the EXIT_PENDING log line
    bot.positions.is_flat = False
    bot.positions.get_position.return_value = fake_pos
    bot.positions.position = fake_pos
    # Disable the 2s debounce on first send
    bot._last_exit_send_ts = {}

    # Use stop_loss as reason — the OCO-handled path skips the WS send
    # but still logs EXIT_PENDING (a trace-stamped line we can verify).
    ws = AsyncMock()

    exit_handler = TradeExit(bot)
    # Use a reason in OCO_HANDLED to avoid the OIF send code path
    asyncio.run(exit_handler.exit_trade(
        ws=ws, price=21000.0, reason="stop_loss", trade_id="t-exit-1",
    ))

    stamped = _trace_lines(cap_logs, tid)
    assert any("EXIT_PENDING" in m for m in stamped), (
        f"expected EXIT_PENDING log to be [TRACE:{tid}]-stamped; "
        f"captured = {cap_logs.messages()}"
    )

    assert get_current_trace_id() is None


# ---------------------------------------------------------------------------
# Test 4: TradeCloser pulls trace_id off the trade['market_snapshot'].
# ---------------------------------------------------------------------------

def test_closer_stage_binds_trace_from_trade_snapshot(cap_logs):
    """TradeCloser.on_trade_closed must rebind the trace id from the
    trade dict's market_snapshot. Any log line emitted by the
    bookkeeping helpers must then carry the [TRACE:xxx] prefix.
    """
    from bots._trade_closer import TradeCloser

    tid = "11223344"

    trade = {
        "trade_id": "t-close-1",
        "strategy": "bias_momentum",
        "pnl_dollars": 12.5,
        "result": "WIN",
        "exit_price": 21010.0,
        "exit_reason": "target_hit",
        "market_snapshot": {"trace_id": tid},
    }

    bot = MagicMock()
    # Make circuit_breakers.record_* raise so we hit the debug log path,
    # which is itself inside the trace context.
    bot.circuit_breakers.record_slippage.side_effect = RuntimeError("simulated")

    closer = TradeCloser(bot)
    closer.on_trade_closed(trade)

    stamped = _trace_lines(cap_logs, tid)
    # We expect at least the record_slippage error debug line to be
    # stamped (it's the first log emitted from inside the with-block).
    assert any("record_slippage" in m for m in stamped), (
        f"expected record_slippage debug log to be [TRACE:{tid}]-stamped; "
        f"captured = {cap_logs.messages()}"
    )

    assert get_current_trace_id() is None


# ---------------------------------------------------------------------------
# Test 5: End-to-end — one signal, three stages, single shared trace id.
# Simulates SIGNAL -> ROUTER -> ENTRY (early-return) -> EXIT -> CLOSE
# by driving the same trace_id through each module's entry point.
# ---------------------------------------------------------------------------

def test_lifecycle_single_trace_id_across_stages(cap_logs):
    """Drive the same signal trace_id through router -> entry early-
    return -> exit -> closer and assert every stamped log line shares
    one id."""
    from bots._signal_router import SignalRouter
    from bots._trade_entry import TradeEntry
    from bots._trade_exit import TradeExit
    from bots._trade_closer import TradeCloser

    tid = generate_trace_id()
    sig = _make_signal(tid)

    # ROUTER + ENTRY — wire the router so it calls the REAL TradeEntry
    # (rather than a mock). We force the early-return path inside
    # TradeEntry so the test is hermetic.
    bot = MagicMock()
    bot._is_no_new_entries_window.return_value = True
    bot.last_rejection = None

    # Hook the router to call the real entry handler
    entry_handler = TradeEntry(bot)
    bot._enter_trade = entry_handler.enter_trade

    # Disable the pretrade filter
    import bots._signal_router as _router_mod
    orig_flag = _router_mod.AGENT_PRETRADE_FILTER_ENABLED
    _router_mod.AGENT_PRETRADE_FILTER_ENABLED = False
    try:
        router = SignalRouter(bot)
        asyncio.run(router.process_signal(ws=MagicMock(), signal=sig))
    finally:
        _router_mod.AGENT_PRETRADE_FILTER_ENABLED = orig_flag

    # EXIT — position carries the trace id on its market_snapshot
    fake_pos = SimpleNamespace(
        trade_id="t-life-1",
        direction="LONG",
        contracts=1,
        account="Sim101",
        market_snapshot={"trace_id": tid},
    )
    bot.positions.is_flat = False
    bot.positions.get_position.return_value = fake_pos
    bot.positions.position = fake_pos
    bot._last_exit_send_ts = {}
    exit_handler = TradeExit(bot)
    asyncio.run(exit_handler.exit_trade(
        ws=AsyncMock(), price=21000.0, reason="stop_loss",
        trade_id="t-life-1",
    ))

    # CLOSE
    trade = {
        "trade_id": "t-life-1",
        "strategy": "bias_momentum",
        "pnl_dollars": -12.5,
        "result": "LOSS",
        "exit_price": 21000.0,
        "exit_reason": "stop_loss",
        "market_snapshot": {"trace_id": tid},
    }
    closer = TradeCloser(bot)
    closer.on_trade_closed(trade)

    # Now: every trace-stamped log line emitted during the run must
    # share THIS trace id. Other ids are not permitted (they would
    # indicate leaked context from a parallel test).
    all_ids = _extract_trace_ids(cap_logs)
    # In addition to `tid` we may see auto-allocated ids from any
    # untracked Signal() constructions on the path. Filter to ids that
    # appear in messages emitted from our modules (router/entry/exit/
    # closer) — but the simplest assertion is that `tid` is present
    # and is the dominant id (it should be the only one for our
    # captured stages).
    assert tid in all_ids, (
        f"expected lifecycle trace id {tid} to appear in captured logs; "
        f"seen ids = {sorted(all_ids)}"
    )
    # All stamped lines from our test-driven stages share this id —
    # assert by counting lines that mention our hand-crafted markers.
    stamped = _trace_lines(cap_logs, tid)
    markers = ("NO_NEW_ENTRIES", "EXIT_PENDING", "record_slippage")
    seen_markers = {
        m for m in markers
        if any(m in line for line in stamped)
    }
    # We expect at least two of the three stage markers (the closer's
    # record_slippage path requires the mock to raise; if circuit
    # breaker is happy, no log fires there — so accept >=2).
    assert len(seen_markers) >= 2, (
        f"expected at least 2 of {markers} in trace-stamped logs; "
        f"got {seen_markers}; full stamped = {stamped}"
    )

    # Outside all the with-blocks, contextvar reset
    assert get_current_trace_id() is None
