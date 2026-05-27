"""Tests for the external Phoenix heartbeat probe.

No real network calls — fire_alert is monkey-patched to capture invocations.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402


@pytest.fixture
def alerts(monkeypatch):
    """Capture alerts as (kind, text, include_sms) tuples; suppress real I/O."""
    captured: list[tuple[str, str, bool]] = []

    def fake_fire(kind, text, *, include_sms):
        captured.append((kind, text, include_sms))

    monkeypatch.setattr(probe, "fire_alert", fake_fire)
    return captured


@pytest.fixture
def fresh_state():
    return {
        "consecutive_fail": 0,
        "consecutive_ok": 0,
        "down": False,
        "down_since": None,
        "down_since_iso": None,
    }


def _step_fail(state, n, fail_thr=3, recov_thr=2):
    for _ in range(n):
        probe.step(state, ok=False, detail="conn refused",
                   fail_threshold=fail_thr, recovery_threshold=recov_thr)


def _step_ok(state, n, fail_thr=3, recov_thr=2):
    for _ in range(n):
        probe.step(state, ok=True, detail="HTTP 200",
                   fail_threshold=fail_thr, recovery_threshold=recov_thr)


def test_no_alert_when_healthy(alerts, fresh_state):
    _step_ok(fresh_state, 10)
    assert alerts == []
    assert fresh_state["down"] is False


def test_threshold_fail_no_alert_below(alerts, fresh_state):
    """Two failures (below threshold of 3) must NOT fire."""
    _step_fail(fresh_state, 2, fail_thr=3)
    assert alerts == []
    assert fresh_state["down"] is False
    assert fresh_state["consecutive_fail"] == 2


def test_threshold_fires_down_at_threshold(alerts, fresh_state):
    """Third consecutive failure crosses threshold and fires DOWN."""
    _step_fail(fresh_state, 3, fail_thr=3)
    assert any(kind == "DOWN" for kind, _, _ in alerts)
    assert fresh_state["down"] is True
    down_alerts = [a for a in alerts if a[0] == "DOWN"]
    # First DOWN alert includes SMS
    assert down_alerts[0][2] is True


def test_recovery_requires_m_consecutive_successes(alerts, fresh_state):
    """After going down, one success must NOT clear; M successes must."""
    _step_fail(fresh_state, 3, fail_thr=3, recov_thr=2)
    alerts.clear()
    # One success — still considered down
    _step_ok(fresh_state, 1, recov_thr=2)
    assert not any(kind == "RESOLVED" for kind, _, _ in alerts)
    assert fresh_state["down"] is True
    # Second success crosses recovery threshold
    _step_ok(fresh_state, 1, recov_thr=2)
    resolved = [a for a in alerts if a[0] == "RESOLVED"]
    assert len(resolved) == 1
    # RESOLVED must NOT trigger SMS (alert-fatigue rule)
    assert resolved[0][2] is False
    assert fresh_state["down"] is False


def test_no_resolved_alert_if_never_down(alerts, fresh_state):
    """Successes when never down should not emit RESOLVED."""
    _step_fail(fresh_state, 1)  # below threshold
    _step_ok(fresh_state, 5)
    assert not any(kind == "RESOLVED" for kind, _, _ in alerts)


def test_flap_below_threshold_no_alert(alerts, fresh_state):
    """Failures that get interrupted by a success reset the fail counter."""
    _step_fail(fresh_state, 2)
    _step_ok(fresh_state, 1)
    _step_fail(fresh_state, 2)
    assert alerts == []
    assert fresh_state["down"] is False


def test_repeated_down_keeps_firing_each_probe(alerts, fresh_state):
    """While down, every additional failed probe should fire DOWN with updated duration."""
    _step_fail(fresh_state, 3, fail_thr=3)
    initial_count = len([a for a in alerts if a[0] == "DOWN"])
    _step_fail(fresh_state, 2)
    later_count = len([a for a in alerts if a[0] == "DOWN"])
    assert later_count > initial_count


def test_probe_health_mocked(monkeypatch):
    """probe_health uses urllib — mock it to verify branch logic, zero real HTTP."""
    class FakeResp:
        def __init__(self, status, body=b"{}"):
            self.status = status
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen_ok(url, timeout=10):
        return FakeResp(200, b'{"ok":true}')

    monkeypatch.setattr(probe.urllib.request, "urlopen", fake_urlopen_ok)
    ok, detail = probe.probe_health("http://example/health")
    assert ok is True
    assert "200" in detail

    def fake_urlopen_500(url, timeout=10):
        return FakeResp(500)

    monkeypatch.setattr(probe.urllib.request, "urlopen", fake_urlopen_500)
    ok, detail = probe.probe_health("http://example/health")
    assert ok is False
    assert "500" in detail

    def fake_urlopen_neterr(url, timeout=10):
        raise probe.urllib.error.URLError("connection refused")

    monkeypatch.setattr(probe.urllib.request, "urlopen", fake_urlopen_neterr)
    ok, detail = probe.probe_health("http://example/health")
    assert ok is False
    assert "NetError" in detail


def test_telegram_and_twilio_use_urllib_only(monkeypatch):
    """Make sure Telegram + Twilio senders DO NOT bypass urllib (no requests dep)."""
    calls = []

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, data=None, timeout=10):
        # Telegram path: positional URL string; Twilio path: Request object
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(probe.urllib.request, "urlopen", fake_urlopen)
    assert probe.send_telegram("TOKEN", "CHAT", "hi") is True
    assert probe.send_twilio_sms("SID", "TOK", "+1", "+2", "hi") is True
    assert any("api.telegram.org" in c for c in calls)
    assert any("api.twilio.com" in c for c in calls)
