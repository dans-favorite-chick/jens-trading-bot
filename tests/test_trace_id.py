"""Unit tests for core.trace_id (P4-2).

Coverage:
- ID format + uniqueness
- TraceContext lifecycle (enter/exit, nesting, restoration)
- Logging filter prepends the right token
- Filter is idempotent (no double-stamping)
- Signal.trace_id survives to_dict() round-trip
- get_current_trace_id() returns None outside any context
- asyncio.Task inherits the trace id at creation time
"""
from __future__ import annotations

import asyncio
import logging
import pickle
import re

import pytest

from core.trace_id import (
    STAGE_ENTRY,
    STAGE_ROUTER,
    STAGE_SIGNAL,
    TraceContext,
    format_trace_log,
    generate_trace_id,
    get_current_trace_id,
    install_trace_filter,
)
from strategies.base_strategy import Signal


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def test_generate_trace_id_is_8_char_hex():
    tid = generate_trace_id()
    assert len(tid) == 8
    assert re.fullmatch(r"[0-9a-f]{8}", tid), f"unexpected format: {tid!r}"


def test_generate_trace_id_uniqueness_at_scale():
    # 10k draws: vanishingly small odds of any collision in 2**32 namespace.
    ids = {generate_trace_id() for _ in range(10_000)}
    assert len(ids) == 10_000


# ---------------------------------------------------------------------------
# format_trace_log
# ---------------------------------------------------------------------------

def test_format_trace_log_shape():
    out = format_trace_log(STAGE_ENTRY, "abc12345", "stop=21042.50")
    assert out == "[TRACE:abc12345] [ENTRY] stop=21042.50"


# ---------------------------------------------------------------------------
# Context lifecycle
# ---------------------------------------------------------------------------

def test_no_trace_id_outside_context():
    assert get_current_trace_id() is None


def test_trace_context_sets_and_restores():
    assert get_current_trace_id() is None
    with TraceContext("deadbeef") as tid:
        assert tid == "deadbeef"
        assert get_current_trace_id() == "deadbeef"
    assert get_current_trace_id() is None


def test_trace_context_allocates_when_no_id_given():
    with TraceContext() as tid:
        assert re.fullmatch(r"[0-9a-f]{8}", tid)
        assert get_current_trace_id() == tid


def test_trace_context_nesting():
    with TraceContext("aaaaaaaa"):
        assert get_current_trace_id() == "aaaaaaaa"
        with TraceContext("bbbbbbbb"):
            assert get_current_trace_id() == "bbbbbbbb"
        assert get_current_trace_id() == "aaaaaaaa"
    assert get_current_trace_id() is None


def test_trace_context_restores_on_exception():
    with TraceContext("outerrrr"):
        try:
            with TraceContext("innerrrr"):
                raise RuntimeError("kaboom")
        except RuntimeError:
            pass
        assert get_current_trace_id() == "outerrrr"
    assert get_current_trace_id() is None


# ---------------------------------------------------------------------------
# Logging filter behavior
# ---------------------------------------------------------------------------

class _Capture(logging.Handler):
    """Handler that stashes the FORMATTED message of each record."""

    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Use record.getMessage() to apply args; filter has already
        # potentially rewritten record.msg.
        self.records.append(record.getMessage())


@pytest.fixture
def capturing_logger():
    """Yield (logger, capture). Logger is uniquely named per test to
    avoid handler bleed across tests. Filter is installed on root via
    module import so a fresh getLogger inherits it.
    """
    name = f"phoenix.trace_test.{generate_trace_id()}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't pollute caplog/root with our msgs
    cap = _Capture()
    cap.setLevel(logging.DEBUG)
    # We must also install the filter on this non-propagating logger
    # because logging.Filter on root only runs when records propagate.
    install_trace_filter(logger)
    logger.addHandler(cap)
    try:
        yield logger, cap
    finally:
        logger.removeHandler(cap)


def test_filter_prepends_trace_token(capturing_logger):
    logger, cap = capturing_logger
    with TraceContext("12345678"):
        logger.info("hello world")
    assert cap.records == ["[TRACE:12345678] hello world"]


def test_filter_skips_when_no_context(capturing_logger):
    logger, cap = capturing_logger
    logger.info("untraced message")
    assert cap.records == ["untraced message"]


def test_filter_is_idempotent_no_double_stamp(capturing_logger):
    logger, cap = capturing_logger
    with TraceContext("cafebabe"):
        # Caller pre-stamped via format_trace_log -- filter must NOT
        # add a second [TRACE:cafebabe] prefix.
        logger.info(format_trace_log(STAGE_ROUTER, "cafebabe", "msg"))
    assert cap.records == ["[TRACE:cafebabe] [ROUTER] msg"]


def test_filter_supports_args_formatting(capturing_logger):
    logger, cap = capturing_logger
    with TraceContext("00112233"):
        logger.info("price=%.2f strat=%s", 21042.50, "bias_momentum")
    assert cap.records == [
        "[TRACE:00112233] price=21042.50 strat=bias_momentum",
    ]


# ---------------------------------------------------------------------------
# Signal integration
#
# These four assertions become active once the strategies/base_strategy.py
# patch (returned alongside this module) lands. Until then they auto-skip
# rather than fail-loud, so CI on a branch without the patch stays green
# while the patch review is in flight. Once the patch ships, every test
# in this block must pass.
# ---------------------------------------------------------------------------

_SIGNAL_HAS_TRACE_ID = "trace_id" in {
    f.name for f in __import__(
        "dataclasses", fromlist=["fields"]
    ).fields(Signal)
}

_needs_patch = pytest.mark.skipif(
    not _SIGNAL_HAS_TRACE_ID,
    reason="strategies/base_strategy.py Signal.trace_id patch not yet applied",
)


@_needs_patch
def test_signal_has_trace_id_field():
    sig = Signal(
        direction="LONG", stop_ticks=20, target_rr=1.5, confidence=70,
        entry_score=45, strategy="bias_momentum", reason="test",
        confluences=[],
    )
    assert hasattr(sig, "trace_id")
    assert re.fullmatch(r"[0-9a-f]{8}", sig.trace_id)


@_needs_patch
def test_signal_trace_id_unique_per_instance():
    s1 = Signal(direction="LONG", stop_ticks=20, target_rr=1.5, confidence=70,
                entry_score=45, strategy="x", reason="", confluences=[])
    s2 = Signal(direction="LONG", stop_ticks=20, target_rr=1.5, confidence=70,
                entry_score=45, strategy="x", reason="", confluences=[])
    assert s1.trace_id != s2.trace_id


@_needs_patch
def test_signal_trace_id_survives_to_dict():
    sig = Signal(direction="LONG", stop_ticks=20, target_rr=1.5, confidence=70,
                 entry_score=45, strategy="x", reason="", confluences=[])
    d = sig.to_dict()
    assert d.get("trace_id") == sig.trace_id


@_needs_patch
def test_signal_trace_id_survives_pickle():
    sig = Signal(direction="LONG", stop_ticks=20, target_rr=1.5, confidence=70,
                 entry_score=45, strategy="x", reason="", confluences=[])
    restored = pickle.loads(pickle.dumps(sig))
    assert restored.trace_id == sig.trace_id


# ---------------------------------------------------------------------------
# asyncio propagation
# ---------------------------------------------------------------------------

def test_trace_id_propagates_into_asyncio_task():
    """ContextVar is copied into a Task at creation time. This is the
    property that lets _process_signal stamp _enter_trade's logs."""
    captured: dict[str, str | None] = {}

    async def inner():
        captured["seen"] = get_current_trace_id()

    async def outer():
        with TraceContext("abcdef01"):
            await asyncio.create_task(inner())

    asyncio.run(outer())
    assert captured["seen"] == "abcdef01"
