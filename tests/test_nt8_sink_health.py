"""Unit tests for ``core.nt8_sink_health``.

The state owner is exercised in isolation — no bots, no signals, no
strategies. Telegram is patched out at the module level so tests
can assert "called once" / "not called" semantics.

Test plan from
``docs/superpowers/specs/2026-06-02-nt8-sink-auto-pause-design.md``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# Force the module's runtime dir to a tmp path before *any* test runs.
# We do this in a session-level autouse fixture in conftest to keep
# real ``runtime/`` clean. For this file we use a per-test fixture
# that points the module-level constant at tmp_path.
import core.nt8_sink_health as sink_mod
from core.nt8_sink_health import (
    NT8SinkHealth,
    NT8SinkState,
    get_sink_health,
    reset_sink_health_cache,
)


@pytest.fixture(autouse=True)
def _isolate_runtime(tmp_path, monkeypatch):
    """Redirect the module's RUNTIME_DIR + reset the singleton cache."""
    monkeypatch.setattr(sink_mod, "_RUNTIME_DIR", tmp_path)
    reset_sink_health_cache()
    yield
    reset_sink_health_cache()


@pytest.fixture
def no_telegram(monkeypatch):
    """Mock out telegram so we can assert call counts.

    The notifier is imported lazily inside the fire helpers, so we
    patch ``core.telegram_notifier.send_sync`` directly. The mock
    returns False (matching the real "not configured" path).
    """
    from unittest.mock import MagicMock
    import core.telegram_notifier as tg
    m = MagicMock(return_value=False)
    monkeypatch.setattr(tg, "send_sync", m)
    return m


# ─── tests ───────────────────────────────────────────────────────────────

def test_initial_state_unpaused(no_telegram):
    """Fresh boot (no on-disk state) → paused=False, telegram silent."""
    health = get_sink_health("prod")
    assert health.is_paused() is False
    assert health.state.paused_at is None
    assert health.state.paused_reason is None
    no_telegram.assert_not_called()


def test_record_protect_failed_trips(no_telegram):
    """A protect-failed call sets paused + populates the audit fields."""
    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="2a0c8180",
        strategy="bias_momentum",
        direction="LONG",
        account="Sim101",
    )
    assert health.is_paused() is True
    assert health.state.paused_at is not None
    assert "2a0c8180" in (health.state.paused_reason or "")
    assert health.state.paused_trade_id == "2a0c8180"
    assert health.state.paused_strategy == "bias_momentum"
    assert health.state.paused_account == "Sim101"
    no_telegram.assert_called_once()


def test_record_protect_failed_idempotent(no_telegram):
    """Second call while already paused must be a no-op (no field
    overwrite, no second Telegram fire).
    """
    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="first", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )
    snapshot = (
        health.state.paused_at,
        health.state.paused_trade_id,
        health.state.paused_reason,
    )
    no_telegram.reset_mock()

    health.record_protect_failed(
        trade_id="second", strategy="other_strategy",
        direction="SHORT", account="Sim101",
    )
    assert (
        health.state.paused_at,
        health.state.paused_trade_id,
        health.state.paused_reason,
    ) == snapshot, "idempotent call must not mutate state"
    no_telegram.assert_not_called()


def test_clear_resets_state(no_telegram):
    """After clear(by=...): paused=False, cleared_at populated, by recorded."""
    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="tid", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )
    no_telegram.reset_mock()

    health.clear(by="dashboard")
    assert health.is_paused() is False
    assert health.state.cleared_at is not None
    assert health.state.cleared_by == "dashboard"
    no_telegram.assert_called_once()


def test_clear_when_not_paused_is_noop(no_telegram):
    """clear() on an already-unpaused sink does not fire telegram."""
    health = get_sink_health("prod")
    assert health.is_paused() is False
    health.clear(by="dashboard")
    no_telegram.assert_not_called()
    assert health.state.cleared_at is None


def test_state_persists_to_disk(no_telegram, tmp_path):
    """Write paused state, instantiate a second NT8SinkHealth pointing
    at the same file, and verify the load sees paused=True with the
    fields intact.
    """
    health = get_sink_health("prod")
    health.record_protect_failed(
        trade_id="persist_tid", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )
    # Force a re-read by bypassing the cache.
    reset_sink_health_cache()
    fresh = get_sink_health("prod")
    assert fresh.is_paused() is True
    assert fresh.state.paused_trade_id == "persist_tid"
    assert fresh.state.paused_strategy == "bias_momentum"


def test_state_survives_round_trip_with_paused_state(
    no_telegram, tmp_path, caplog,
):
    """Pre-seed a paused state on disk → reset cache → new instance
    boots paused and emits the CRITICAL boot banner.
    """
    state_file = tmp_path / "nt8_sink_state_prod.json"
    state_file.write_text(json.dumps({
        "paused": True,
        "paused_at": "2026-06-02T09:03:07",
        "paused_reason": "PROTECT FAILED on 2a0c8180 (bias_momentum LONG)",
        "paused_trade_id": "2a0c8180",
        "paused_strategy": "bias_momentum",
        "paused_account": "Sim101",
        "cleared_at": None,
        "cleared_by": None,
    }))
    reset_sink_health_cache()
    with caplog.at_level("CRITICAL", logger="NT8SinkHealth"):
        health = get_sink_health("prod")
    assert health.is_paused() is True
    boot_lines = [r for r in caplog.records if "booting paused" in r.getMessage()]
    assert len(boot_lines) == 1, "boot-paused CRITICAL banner must fire exactly once"


def test_per_bot_isolation(no_telegram):
    """prod and sim hold independent state files; mutating one does not
    affect the other.
    """
    prod = get_sink_health("prod")
    sim = get_sink_health("sim")
    assert prod is not sim
    prod.record_protect_failed(
        trade_id="prod_tid", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )
    assert prod.is_paused() is True
    assert sim.is_paused() is False


def test_atomic_write_does_not_corrupt_on_crash(
    no_telegram, tmp_path, monkeypatch,
):
    """If os.replace fails mid-stream, the previous on-disk state must
    remain intact (the tmp file may leak but the canonical path is
    untouched).
    """
    health = get_sink_health("prod")
    # First write — establishes a valid baseline file on disk.
    health.record_protect_failed(
        trade_id="baseline", strategy="bias_momentum",
        direction="LONG", account="Sim101",
    )
    baseline = (tmp_path / "nt8_sink_state_prod.json").read_text(encoding="utf-8")

    # Now inject a failure into os.replace for the *next* write only.
    original_replace = sink_mod.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated crash mid-rename")
        return original_replace(src, dst)

    monkeypatch.setattr(sink_mod.os, "replace", flaky_replace)

    # clear() triggers a persist; it should raise but leave file untouched.
    with pytest.raises(OSError):
        health.clear(by="test")

    surviving = (tmp_path / "nt8_sink_state_prod.json").read_text(encoding="utf-8")
    assert surviving == baseline, (
        "atomic write must leave the previous file intact on rename failure"
    )
