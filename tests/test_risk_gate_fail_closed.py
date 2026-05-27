"""P1-2 — RiskGateSink fail-CLOSED behavior (synthesis F-05).

When PHOENIX_RISK_GATE=1 is explicitly set, an unreachable risk-gate pipe
must cause the sink to REFUSE the OIF submission (so the bot doesn't
write the order) instead of falling back to DirectFileSink (which would
silently bypass the gate). The default-fail-soft behavior is preserved
for backward compat when fail_closed=False (the explicit default).
"""
from __future__ import annotations

import logging
from unittest import mock

import pytest

from phoenix_bot.orchestrator.oif_writer import (
    RiskGateSink,
    DirectFileSink,
    get_default_sink,
)


@pytest.fixture(autouse=True)
def _reset_warning_flags():
    """Reset the class-level _warned flags so each test sees a fresh
    one-shot log emission."""
    RiskGateSink._fallback_warned = False
    RiskGateSink._fail_closed_warned = False
    yield
    RiskGateSink._fallback_warned = False
    RiskGateSink._fail_closed_warned = False


# ─── Default (fail-soft) behavior — backward compatibility ──────────────

def test_default_is_fail_soft():
    sink = RiskGateSink()
    assert sink.fail_closed is False
    assert isinstance(sink._fallback, DirectFileSink)


def test_fail_soft_falls_back_on_pipe_failure(caplog):
    caplog.set_level(logging.WARNING)
    sink = RiskGateSink(fail_closed=False)
    with mock.patch.object(sink, "_call_pipe",
                           side_effect=IOError("pipe missing")):
        with mock.patch.object(sink._fallback, "submit",
                               return_value={"v": 1, "id": "x",
                                             "decision": "ACCEPT",
                                             "sink": "direct"}) as fallback:
            resp = sink.submit({"v": 1, "id": "abc", "op": "PLACE"})
            fallback.assert_called_once()
    assert resp["decision"] == "ACCEPT"
    assert resp["sink"] == "fallback"
    assert any("[RISK_GATE]" in r.message and "falling back" in r.message
               for r in caplog.records)


# ─── Fail-CLOSED behavior (P1-2 — synthesis F-05) ────────────────────────

def test_fail_closed_no_fallback_sink_constructed():
    """fail_closed=True must NOT instantiate a DirectFileSink fallback —
    that's the whole point. If the fallback existed, a bug elsewhere
    could route through it."""
    sink = RiskGateSink(fail_closed=True)
    assert sink.fail_closed is True
    assert sink._fallback is None


def test_fail_closed_refuses_when_pipe_unreachable(caplog):
    caplog.set_level(logging.CRITICAL)
    sink = RiskGateSink(fail_closed=True)
    with mock.patch.object(sink, "_call_pipe",
                           side_effect=IOError("pipe missing")):
        resp = sink.submit({"v": 1, "id": "abc", "op": "PLACE"})
    assert resp["decision"] == "REFUSE"
    assert resp["sink"] == "risk_gate_unavailable"
    assert "risk_gate_unavailable" in resp["reason"]
    assert any("[RISK_GATE_DOWN]" in r.message
               and r.levelno == logging.CRITICAL
               for r in caplog.records)


def test_fail_closed_critical_log_fires_once_per_process(caplog):
    caplog.set_level(logging.CRITICAL)
    sink = RiskGateSink(fail_closed=True)
    with mock.patch.object(sink, "_call_pipe",
                           side_effect=IOError("pipe missing")):
        for _ in range(5):
            sink.submit({"v": 1, "id": "abc", "op": "PLACE"})
    crit_records = [r for r in caplog.records if r.levelno == logging.CRITICAL
                    and "[RISK_GATE_DOWN]" in r.message]
    assert len(crit_records) == 1, (
        "CRITICAL [RISK_GATE_DOWN] should fire exactly once per process"
    )


def test_fail_closed_succeeds_when_pipe_reachable():
    """When the pipe IS reachable, fail-closed still passes traffic through."""
    sink = RiskGateSink(fail_closed=True)
    ok_resp = {"v": 1, "id": "abc", "decision": "ACCEPT",
               "oif_path": "/tmp/x.txt", "sink": "risk_gate"}
    with mock.patch.object(sink, "_call_pipe", return_value=ok_resp):
        resp = sink.submit({"v": 1, "id": "abc", "op": "PLACE"})
    assert resp == ok_resp


# ─── get_default_sink() honors PHOENIX_RISK_GATE=1 ───────────────────────

def test_get_default_sink_returns_directfilesink_when_env_unset(monkeypatch):
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)
    sink = get_default_sink()
    assert isinstance(sink, DirectFileSink)


def test_get_default_sink_returns_directfilesink_when_env_zero(monkeypatch):
    monkeypatch.setenv("PHOENIX_RISK_GATE", "0")
    sink = get_default_sink()
    assert isinstance(sink, DirectFileSink)


def test_get_default_sink_returns_fail_closed_when_env_one(monkeypatch):
    monkeypatch.setenv("PHOENIX_RISK_GATE", "1")
    sink = get_default_sink()
    assert isinstance(sink, RiskGateSink)
    assert sink.fail_closed is True, (
        "PHOENIX_RISK_GATE=1 must enable fail-CLOSED mode per P1-2"
    )
