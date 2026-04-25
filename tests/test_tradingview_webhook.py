"""
Tests for bridge/tradingview_webhook.py (Phase B+ Section 3.1).

Covers:
  - Valid HMAC + valid IP + allowlisted strategy + fresh nonce -> ACCEPT
  - Invalid HMAC -> 401
  - Valid HMAC but IP not in allowlist -> 403
  - Strategy not allowlisted -> 403
  - Replay (duplicate nonce) -> 409
  - Rate limit: 11 reqs in 60s from same IP -> last is 429
  - Fail-closed when TRADINGVIEW_WEBHOOK_SECRET unset -> 503
  - Body validation -> 400

Run: pytest tests/test_tradingview_webhook.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bridge import tradingview_webhook as tvw  # noqa: E402


_SECRET = "test-secret-do-not-use-in-prod-0123456789abcdef"
_STRATEGY = "tv_breakout_v1"


def _sign(body_bytes: bytes, secret: str = _SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_payload(**overrides) -> dict:
    body = {
        "strategy": _STRATEGY,
        "action": "BUY",
        "qty": 1,
        "instrument": "MNQ 06-26",
        "price": 18500.25,
        "ts": "2026-04-25T12:34:56Z",
        "nonce": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


@pytest.fixture
def app(monkeypatch):
    """Build a fresh Flask app per test with env configured for happy-path."""
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("TRADINGVIEW_ALLOWED_IPS", "127.0.0.1")
    monkeypatch.setenv("TRADINGVIEW_ALLOWED_STRATEGIES", _STRATEGY)
    tvw._reset_state()
    return tvw.create_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def fake_sink(monkeypatch):
    """Replace _resolve_sink so we don't write a real OIF and we can
    assert the sink received the structured request."""
    received = []

    class _FakeSink:
        def submit(self, request):
            received.append(request)
            return {
                "v": 1,
                "id": request.get("nonce", "x"),
                "decision": "ACCEPT",
                "oif_path": r"C:\fake\oif_path.txt",
                "sink": "fake",
            }

    sink = _FakeSink()
    monkeypatch.setattr(tvw, "_resolve_sink", lambda: sink)
    return received


# ─── Happy path ─────────────────────────────────────────────────────

def test_valid_signal_returns_accept(client, fake_sink):
    body = _make_payload()
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Phoenix-Signature": _sign(raw),
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert rv.status_code == 200, rv.get_data(as_text=True)
    data = rv.get_json()
    assert data["ok"] is True
    assert data["decision"] == "ACCEPT"
    assert data["oif_path"] == r"C:\fake\oif_path.txt"
    # Sink received a normalised request including source marker + nonce.
    assert len(fake_sink) == 1
    assert fake_sink[0]["source"] == "tradingview_webhook"
    assert fake_sink[0]["strategy"] == _STRATEGY
    assert fake_sink[0]["action"] == "BUY"
    assert fake_sink[0]["qty"] == 1


# ─── HMAC failure ───────────────────────────────────────────────────

def test_invalid_hmac_returns_401(client, fake_sink):
    body = _make_payload()
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={
            "Content-Type": "application/json",
            # Wrong digest
            "X-Phoenix-Signature": "sha256=" + ("0" * 64),
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert rv.status_code == 401
    assert rv.get_json()["reason"] == "invalid signature"
    assert fake_sink == []


# ─── IP allowlist ───────────────────────────────────────────────────

def test_disallowed_ip_returns_403(client, fake_sink):
    body = _make_payload()
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Phoenix-Signature": _sign(raw),
        },
        environ_overrides={"REMOTE_ADDR": "10.0.0.99"},
    )
    assert rv.status_code == 403
    assert "ip" in rv.get_json()["reason"].lower()
    assert fake_sink == []


# ─── Strategy allowlist ─────────────────────────────────────────────

def test_strategy_not_in_allowlist_returns_403(client, fake_sink):
    body = _make_payload(strategy="not_allowed_strategy")
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Phoenix-Signature": _sign(raw),
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert rv.status_code == 403
    assert "strategy" in rv.get_json()["reason"].lower()
    assert fake_sink == []


# ─── Replay protection ──────────────────────────────────────────────

def test_replay_same_nonce_returns_409(client, fake_sink):
    body = _make_payload()
    raw = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Phoenix-Signature": _sign(raw),
    }
    rv1 = client.post("/webhook/tradingview", data=raw, headers=headers,
                      environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    assert rv1.status_code == 200
    rv2 = client.post("/webhook/tradingview", data=raw, headers=headers,
                      environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    assert rv2.status_code == 409
    assert "replay" in rv2.get_json()["reason"].lower() or \
           "duplicate" in rv2.get_json()["reason"].lower()
    assert len(fake_sink) == 1  # second request never reached the sink


# ─── Rate limit ─────────────────────────────────────────────────────

def test_rate_limit_eleventh_request_returns_429(client, fake_sink):
    """10 valid requests inside the 60s window pass; the 11th is rejected."""
    last_status = None
    for i in range(11):
        body = _make_payload()  # fresh nonce each iteration
        raw = json.dumps(body).encode("utf-8")
        rv = client.post(
            "/webhook/tradingview",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Phoenix-Signature": _sign(raw),
            },
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        last_status = rv.status_code
        if i < 10:
            assert rv.status_code == 200, (
                f"req {i}: expected 200, got {rv.status_code} "
                f"({rv.get_data(as_text=True)})"
            )
    assert last_status == 429
    # Only 10 ACCEPTs reached the sink.
    assert len(fake_sink) == 10


# ─── Fail-closed when secret unset ──────────────────────────────────

def test_unconfigured_secret_returns_503(monkeypatch, fake_sink):
    """With TRADINGVIEW_WEBHOOK_SECRET unset (or placeholder), every
    request -> 503 with reason 'secret not configured'."""
    monkeypatch.delenv("TRADINGVIEW_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("TRADINGVIEW_ALLOWED_IPS", "127.0.0.1")
    monkeypatch.setenv("TRADINGVIEW_ALLOWED_STRATEGIES", _STRATEGY)
    tvw._reset_state()
    app = tvw.create_app()
    client = app.test_client()
    body = _make_payload()
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Phoenix-Signature": _sign(raw),
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert rv.status_code == 503
    assert rv.get_json()["reason"] == "secret not configured"
    assert fake_sink == []


def test_placeholder_secret_returns_503(monkeypatch, fake_sink):
    """A literal '<placeholder...>' value must also fail-closed; that's
    the value committed to the .env so a fresh checkout never accepts."""
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "<placeholder -- operator must set>")
    monkeypatch.setenv("TRADINGVIEW_ALLOWED_IPS", "127.0.0.1")
    monkeypatch.setenv("TRADINGVIEW_ALLOWED_STRATEGIES", _STRATEGY)
    tvw._reset_state()
    app = tvw.create_app()
    client = app.test_client()
    body = _make_payload()
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={"Content-Type": "application/json", "X-Phoenix-Signature": _sign(raw)},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert rv.status_code == 503
    assert fake_sink == []


# ─── Body validation ────────────────────────────────────────────────

def test_missing_required_field_returns_400(client, fake_sink):
    body = _make_payload()
    body.pop("nonce")
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Phoenix-Signature": _sign(raw),
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert rv.status_code == 400
    assert "nonce" in rv.get_json()["reason"]
    assert fake_sink == []


def test_invalid_action_returns_400(client, fake_sink):
    body = _make_payload(action="HODL")
    raw = json.dumps(body).encode("utf-8")
    rv = client.post(
        "/webhook/tradingview",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Phoenix-Signature": _sign(raw),
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert rv.status_code == 400
    assert "action" in rv.get_json()["reason"].lower()
    assert fake_sink == []


# ─── Health endpoint ────────────────────────────────────────────────

def test_health_endpoint_does_not_leak_config(client):
    rv = client.get("/webhook/tradingview/health")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["ok"] is True
    # Health response must NOT reveal whether the secret is configured.
    assert "secret" not in json.dumps(data).lower()
