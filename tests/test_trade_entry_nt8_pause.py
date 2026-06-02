"""Integration tests for the NT8-sink-health gate + trip in TradeEntry.

The gate sits at the top of ``TradeEntry.enter_trade``; the trip fires
right after the ``[PROTECT:{tid}] ALL 3 RETRIES FAILED`` CRITICAL log.

The gate is exercised behaviorally: with sink_health.is_paused()=True
the call must return before any OIF or aggregator activity. Verified
by checking that ``self.bot.aggregator.snapshot`` (the very next call
after the gate) is NOT touched and that the rejection is recorded
both in ``last_rejection`` and as a near_miss with
reason="nt8_sink_paused".

The trip is exercised structurally: the production code path that
fires it (3 OCO retries + emergency flatten) is too deeply nested to
drive cleanly with mocks. A regression test verifies the trip call is
wired in the right spot relative to the CRITICAL log and that the
state owner's ``record_protect_failed`` is invoked via that
wiring — verified by a source-level grep + a direct semantic check
that the call-site exists in the right block.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import core.nt8_sink_health as sink_mod
from core.nt8_sink_health import (
    NT8SinkState,
    get_sink_health,
    reset_sink_health_cache,
)
from strategies.base_strategy import Signal


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_ENTRY = REPO_ROOT / "bots" / "_trade_entry.py"


# ─── fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(sink_mod, "_RUNTIME_DIR", tmp_path)
    reset_sink_health_cache()
    yield
    reset_sink_health_cache()


@pytest.fixture
def no_telegram(monkeypatch):
    """Silence telegram in case the trip fires inside a test."""
    import core.telegram_notifier as tg
    monkeypatch.setattr(tg, "send_sync", MagicMock(return_value=False))


def _make_signal(trade_id: str = "deadbeef") -> Signal:
    sig = Signal(
        direction="LONG",
        stop_ticks=20,
        target_rr=1.5,
        confidence=70,
        entry_score=45,
        strategy="bias_momentum",
        reason="nt8 pause gate test",
        confluences=[],
    )
    sig.trade_id = trade_id
    return sig


def _mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.bot_name = "prod"
    bot._is_no_new_entries_window.return_value = False
    bot.last_rejection = None
    return bot


# ─── gate tests ──────────────────────────────────────────────────────────

def test_enter_trade_skips_when_paused(no_telegram):
    """is_paused()=True must short-circuit before aggregator.snapshot().

    The behavioral proof: ``bot.aggregator.snapshot`` (the very next
    call after the gate) must not have been touched, and
    ``last_rejection`` must reflect the pause.
    """
    from bots._trade_entry import TradeEntry

    # Pre-trip the singleton.
    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="prior_failure",
        strategy="bias_momentum",
        direction="LONG",
        account="Sim101",
    )
    assert health.is_paused() is True

    bot = _mock_bot()
    entry = TradeEntry(bot)
    asyncio.run(entry.enter_trade(ws=MagicMock(), signal=_make_signal()))

    bot.aggregator.snapshot.assert_not_called()
    assert bot.last_rejection is not None
    assert "NT8 sink paused" in bot.last_rejection


def test_enter_trade_logs_near_miss_when_paused(no_telegram):
    """The gate must log a near_miss with reason="nt8_sink_paused" so
    operators can see which signals would have fired during the pause.
    """
    from bots._trade_entry import TradeEntry

    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="t1", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )

    bot = _mock_bot()
    entry = TradeEntry(bot)
    asyncio.run(entry.enter_trade(ws=MagicMock(), signal=_make_signal()))

    bot.history.log_near_miss.assert_called_once()
    args, _kwargs = bot.history.log_near_miss.call_args
    # log_near_miss(sig_dict, market_snapshot, reason)
    assert args[2] == "nt8_sink_paused"


def test_enter_trade_proceeds_when_not_paused(no_telegram):
    """Regression guard: with sink unpaused the gate must NOT short-
    circuit. We assert by checking that ``aggregator.snapshot()`` is
    invoked (the very next call after the gate).
    """
    from bots._trade_entry import TradeEntry

    assert get_sink_health("prod").is_paused() is False

    bot = _mock_bot()
    # Force a deeper-path early return so we don't have to mock the
    # whole OIF pipeline: have aggregator.snapshot raise after we observe it.
    bot.aggregator.snapshot.side_effect = RuntimeError("stop_here_for_test")

    entry = TradeEntry(bot)
    with pytest.raises(RuntimeError, match="stop_here_for_test"):
        asyncio.run(entry.enter_trade(ws=MagicMock(), signal=_make_signal()))

    bot.aggregator.snapshot.assert_called_once()


# ─── trip wiring (structural) ────────────────────────────────────────────

def test_protect_failed_call_is_wired_after_critical_log():
    """The ``record_protect_failed(...)`` call must sit between the
    ``[PROTECT:{tid}] ALL 3 RETRIES FAILED`` CRITICAL log and the
    emergency-flatten block. Source-level check keeps this fast and
    robust against the rest of the file moving around.
    """
    src = TRADE_ENTRY.read_text(encoding="utf-8")

    # Anchor on the CRITICAL log fragment used by both the production
    # code and the existing log greps (prod_bot_stdout.log).
    anchor = "ALL 3 RETRIES FAILED"
    assert anchor in src, "PROTECT-failed CRITICAL log moved — update this regression test"
    anchor_pos = src.index(anchor)

    # The trip call must appear AFTER the anchor and BEFORE the next
    # "emergency flatten" block opener so it fires at the right time.
    flatten_marker = "P1-7: emergency flatten"
    assert flatten_marker in src, (
        "emergency-flatten marker comment moved — update this regression test"
    )
    flatten_pos = src.index(flatten_marker, anchor_pos)

    block = src[anchor_pos:flatten_pos]
    assert "get_sink_health(" in block, (
        "expected get_sink_health(...) call between the PROTECT-failed "
        "CRITICAL log and the emergency-flatten block (Phase 1 Task 2 of "
        "the NT8 auto-pause spec)"
    )
    assert "record_protect_failed(" in block, (
        "expected record_protect_failed(...) call between the PROTECT-failed "
        "CRITICAL log and the emergency-flatten block"
    )


def test_protect_failed_call_passes_correct_fields(no_telegram):
    """The wiring must pass trade_id / strategy / direction / account
    onward. We assert this by reading the source and pattern-matching
    the keyword arguments.
    """
    src = TRADE_ENTRY.read_text(encoding="utf-8")
    # Find the record_protect_failed call site near the CRITICAL log.
    anchor = src.index("ALL 3 RETRIES FAILED")
    region = src[anchor:anchor + 2000]
    m = re.search(
        r"record_protect_failed\(\s*([^)]+)\)",
        region,
        re.DOTALL,
    )
    assert m, "record_protect_failed call not found near PROTECT CRITICAL log"
    args_blob = m.group(1)
    for kw in ("trade_id=tid", "strategy=signal.strategy",
               "direction=signal.direction", "account=_account"):
        assert kw in args_blob, (
            f"expected `{kw}` in record_protect_failed call args, got `{args_blob}`"
        )
