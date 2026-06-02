"""Tests for the ``nt8_clear`` dashboard command branch.

Mirrors the structural style of ``tests/test_graceful_shutdown.py``
for the dashboard-command source check, plus two behavioral tests
that drive the dispatcher with a real ``NT8SinkHealth`` instance.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import core.nt8_sink_health as sink_mod
from core.nt8_sink_health import (
    get_sink_health,
    reset_sink_health_cache,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER_SRC = REPO_ROOT / "bots" / "_dashboard_commands.py"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sink_mod, "_RUNTIME_DIR", tmp_path)
    reset_sink_health_cache()
    import core.telegram_notifier as tg
    monkeypatch.setattr(tg, "send_sync", MagicMock(return_value=False))
    yield
    reset_sink_health_cache()


# ─── structural ──────────────────────────────────────────────────────────

def test_dispatcher_has_nt8_clear_branch():
    src = DISPATCHER_SRC.read_text(encoding="utf-8")
    assert 'elif cmd_type == "nt8_clear":' in src, (
        "DashboardCommandDispatcher missing nt8_clear branch (Phase 1 "
        "Task 4 of the NT8 auto-pause spec)"
    )
    assert "get_sink_health" in src, (
        "nt8_clear branch must call get_sink_health to look up the per-bot sink"
    )
    assert "_sink.clear(by=" in src, (
        "nt8_clear branch must call clear(by=...) so cleared_by carries "
        "the audit trail"
    )


# ─── behavioral ──────────────────────────────────────────────────────────

def test_nt8_clear_clears_paused_sink():
    from bots._dashboard_commands import DashboardCommandDispatcher

    bot = MagicMock()
    bot.bot_name = "prod"

    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="tid", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )
    assert health.is_paused() is True

    dispatcher = DashboardCommandDispatcher(bot)
    dispatcher.handle({"type": "nt8_clear", "source": "dashboard"})

    assert health.is_paused() is False
    assert health.state.cleared_by == "dashboard"


def test_nt8_clear_when_not_paused_is_noop(caplog):
    from bots._dashboard_commands import DashboardCommandDispatcher

    bot = MagicMock()
    bot.bot_name = "prod"
    health = get_sink_health("prod")
    assert health.is_paused() is False

    dispatcher = DashboardCommandDispatcher(bot)
    with caplog.at_level("INFO", logger="DashboardCommands"):
        dispatcher.handle({"type": "nt8_clear", "source": "dashboard"})

    assert health.is_paused() is False
    assert health.state.cleared_at is None
    assert any("already unpaused" in r.getMessage() for r in caplog.records), (
        "no-op branch must log the 'already unpaused' line"
    )


def test_nt8_clear_source_recorded_in_audit_trail():
    """``cmd['source']`` must propagate into ``state.cleared_by``."""
    from bots._dashboard_commands import DashboardCommandDispatcher

    bot = MagicMock()
    bot.bot_name = "prod"
    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="tid", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )

    dispatcher = DashboardCommandDispatcher(bot)
    dispatcher.handle({"type": "nt8_clear", "source": "slash:/nt8_clear"})
    assert health.state.cleared_by == "slash:/nt8_clear"
