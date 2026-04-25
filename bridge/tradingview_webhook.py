"""
Phoenix Bot — TradingView Webhook Receiver (Phase B+ Section 3.1)

A hardened Flask app that accepts trade signals from TradingView (or any
upstream relay). Signals flow through the SAME OIFSink Protocol as the
in-process strategies, so risk-gate / fail-closed semantics apply
uniformly. The webhook is a SECOND consumer of the protocol, not a
parallel path.

Hardening layers (each rejects on its own; first failure short-circuits):
  1. Source-IP allowlist (defense in depth — Tailscale/NAT is the real
     boundary, but we still pin allowed origins).
  2. HMAC-SHA256 over the raw request body (constant-time compare).
  3. Body schema validation (strategy / action / qty / instrument / price /
     ts / nonce — all required).
  4. Strategy allowlist (operator-managed; default empty = reject all).
  5. Replay protection: 24-hour rolling nonce cache, capped at 10k.
  6. Rate limit: 10 req/min per source IP.

Default state on missing TRADINGVIEW_WEBHOOK_SECRET is FAIL-CLOSED: every
request returns HTTP 503 with reason "secret not configured". This is
deliberate — an unconfigured receiver must never accept signals.

Bind address default is 127.0.0.1 (Tailscale-bound only). Operators must
NOT change this to 0.0.0.0; let the upstream tunnel do the IP exposure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from typing import Optional

from flask import Flask, jsonify, request


logger = logging.getLogger("TradingViewWebhook")


# Default TradingView webhook source IPs (published by TradingView for
# webhook alerts as of 2024-2025). 127.0.0.1 is included so a local curl
# probe can hit the receiver during operator testing.
_DEFAULT_TV_IPS = (
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
    "127.0.0.1",
)


# Replay-cache + rate-limit defaults. Both are intentionally bounded so a
# burst of malformed / spoofed requests can't blow up memory.
_NONCE_CACHE_MAX = 10_000
_NONCE_TTL_S = 24 * 60 * 60  # 24 hours
_RATE_LIMIT_PER_MIN = 10
_RATE_LIMIT_WINDOW_S = 60


def _csv_env(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Parse a comma-separated env var into a tuple of stripped tokens.
    Empty / unset env var returns the default."""
    raw = os.environ.get(name, "")
    if not raw:
        return tuple(default)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _allowed_ips() -> tuple[str, ...]:
    """Resolve the IP allowlist from env, falling back to the published
    TradingView source IPs + localhost."""
    return _csv_env("TRADINGVIEW_ALLOWED_IPS", _DEFAULT_TV_IPS)


def _allowed_strategies() -> tuple[str, ...]:
    """Resolve the strategy allowlist. Empty (default) = reject everything
    (fail-closed). Operator must set TRADINGVIEW_ALLOWED_STRATEGIES to
    enable any signal."""
    return _csv_env("TRADINGVIEW_ALLOWED_STRATEGIES", ())


# ─── In-memory state (single Flask process) ─────────────────────────
# Kept module-level so tests can clear() between cases and so a single
# Gunicorn worker model maps cleanly to one cache.
_nonce_cache: "OrderedDict[str, float]" = OrderedDict()
_rate_buckets: dict[str, deque] = {}
_state_lock = threading.Lock()


def _reset_state() -> None:
    """Test hook: wipe replay cache + rate buckets."""
    with _state_lock:
        _nonce_cache.clear()
        _rate_buckets.clear()


def _evict_expired_nonces(now: float) -> None:
    """Drop nonces older than _NONCE_TTL_S. Called on every accept attempt
    so the cache self-trims even under low traffic."""
    cutoff = now - _NONCE_TTL_S
    while _nonce_cache:
        oldest_nonce, oldest_ts = next(iter(_nonce_cache.items()))
        if oldest_ts < cutoff:
            _nonce_cache.popitem(last=False)
        else:
            break


def _record_nonce(nonce: str, now: float) -> bool:
    """Add nonce to the replay cache. Returns False if the nonce was
    already present (replay attempt)."""
    with _state_lock:
        _evict_expired_nonces(now)
        if nonce in _nonce_cache:
            return False
        _nonce_cache[nonce] = now
        # Cap the cache so a burst of unique nonces can't OOM us.
        while len(_nonce_cache) > _NONCE_CACHE_MAX:
            _nonce_cache.popitem(last=False)
        return True


def _rate_limit_ok(ip: str, now: float) -> bool:
    """Token-bucket-ish: keep the last N timestamps per IP, reject when
    the count inside the rolling window exceeds the limit."""
    cutoff = now - _RATE_LIMIT_WINDOW_S
    with _state_lock:
        bucket = _rate_buckets.setdefault(ip, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_PER_MIN:
            return False
        bucket.append(now)
        return True


# ─── HMAC verification ──────────────────────────────────────────────

def _verify_hmac(raw_body: bytes, header_value: Optional[str], secret: str) -> bool:
    """Constant-time verify the HMAC-SHA256 of raw_body against the value
    in header_value (which must be of the form `sha256=<hex>`)."""
    if not header_value or not secret:
        return False
    if not header_value.startswith("sha256="):
        return False
    received_hex = header_value[len("sha256="):].strip()
    if not received_hex:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # hmac.compare_digest handles equal-length string comparison safely.
    try:
        return hmac.compare_digest(expected, received_hex)
    except Exception:
        return False


# ─── Body validation ────────────────────────────────────────────────

_REQUIRED_FIELDS = ("strategy", "action", "qty", "instrument", "price", "ts", "nonce")
_VALID_ACTIONS = ("BUY", "SELL", "CLOSE")


def _validate_body(payload: dict) -> Optional[str]:
    """Returns None on success, an error reason string on failure."""
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    for f in _REQUIRED_FIELDS:
        if f not in payload:
            return f"missing required field: {f}"
    action = str(payload["action"]).upper()
    if action not in _VALID_ACTIONS:
        return f"invalid action: {payload['action']!r} (allowed: {_VALID_ACTIONS})"
    try:
        qty = int(payload["qty"])
    except (TypeError, ValueError):
        return "qty must be an integer"
    if qty < 1:
        return "qty must be >= 1"
    try:
        float(payload["price"])
    except (TypeError, ValueError):
        return "price must be numeric"
    nonce = str(payload["nonce"]).strip()
    if not nonce:
        return "nonce must be a non-empty string"
    return None


# ─── Sink resolution (override hook for tests) ──────────────────────

def _resolve_sink():
    """Resolve the OIFSink. Imported lazily so unit tests can monkeypatch
    `phoenix_bot.orchestrator.oif_writer.get_default_sink` before the
    first request arrives."""
    from phoenix_bot.orchestrator.oif_writer import get_default_sink
    return get_default_sink()


# ─── Flask app factory ──────────────────────────────────────────────

def create_app() -> Flask:
    """Build the Flask app. Factory pattern keeps tests isolated — each
    test_client() can spin a fresh app without import-time side effects."""
    app = Flask("phoenix_tradingview_webhook")

    @app.route("/webhook/tradingview", methods=["POST"])
    def tradingview_endpoint():
        now = time.time()
        # request.remote_addr is set by werkzeug from the socket peer.
        # Behind a reverse proxy the operator should configure
        # ProxyFix — by default we trust remote_addr only.
        source_ip = request.remote_addr or ""

        # Layer 0: fail-closed if secret is unset. NEVER accept signals
        # when the operator hasn't configured a real secret.
        secret = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
        if not secret or secret.startswith("<placeholder"):
            logger.warning("[TV_WEBHOOK] rejected: secret not configured (ip=%s)", source_ip)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": "secret not configured",
            }), 503

        # Layer 1: IP allowlist.
        allowed = _allowed_ips()
        if source_ip not in allowed:
            logger.warning("[TV_WEBHOOK] rejected: ip %s not in allowlist", source_ip)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": "source ip not allowed",
            }), 403

        # Layer 2: rate limit (applies BEFORE HMAC verification to keep
        # an attacker with a stolen signing key from spamming the gate).
        if not _rate_limit_ok(source_ip, now):
            logger.warning("[TV_WEBHOOK] rate limit exceeded for ip=%s", source_ip)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": "rate limit exceeded",
            }), 429

        # Layer 3: HMAC. Must use the EXACT raw body bytes — re-serializing
        # the parsed JSON would change whitespace and break the digest.
        raw_body = request.get_data(cache=True)
        sig_header = request.headers.get("X-Phoenix-Signature", "")
        if not _verify_hmac(raw_body, sig_header, secret):
            logger.warning("[TV_WEBHOOK] rejected: bad HMAC (ip=%s)", source_ip)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": "invalid signature",
            }), 401

        # Layer 4: parse + validate body.
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning("[TV_WEBHOOK] rejected: malformed JSON (%s)", e)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": f"invalid json: {e}",
            }), 400
        err = _validate_body(payload)
        if err:
            logger.warning("[TV_WEBHOOK] rejected: %s", err)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": err,
            }), 400

        # Layer 5: strategy allowlist. Default empty = reject everything.
        strategies = _allowed_strategies()
        if payload["strategy"] not in strategies:
            logger.warning("[TV_WEBHOOK] rejected: strategy %r not allowed",
                           payload["strategy"])
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": "strategy not allowed",
            }), 403

        # Layer 6: replay protection.
        nonce = str(payload["nonce"]).strip()
        if not _record_nonce(nonce, now):
            logger.warning("[TV_WEBHOOK] rejected: replay (nonce=%s)", nonce)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": "duplicate nonce (replay)",
            }), 409

        # All gates passed — route through OIFSink. The sink decides
        # whether the request is risk-gated, file-written, or whatever
        # PHOENIX_RISK_GATE configures.
        sink = _resolve_sink()
        sink_request = {
            "source": "tradingview_webhook",
            "strategy": payload["strategy"],
            "action": str(payload["action"]).upper(),
            "qty": int(payload["qty"]),
            "instrument": payload["instrument"],
            "price": float(payload["price"]),
            "ts": payload["ts"],
            "nonce": nonce,
        }
        try:
            response = sink.submit(sink_request)
        except Exception as e:
            logger.exception("[TV_WEBHOOK] sink submit failed: %s", e)
            return jsonify({
                "ok": False, "decision": "REFUSE",
                "oif_path": None, "reason": f"sink error: {e!r}",
            }), 500

        decision = response.get("decision", "REFUSE")
        oif_path = response.get("oif_path")
        reason = response.get("reason")
        http_status = 200 if decision == "ACCEPT" else 200  # always 200 on logical decision
        logger.info("[TV_WEBHOOK] strategy=%s action=%s qty=%s decision=%s",
                    payload["strategy"], sink_request["action"],
                    sink_request["qty"], decision)
        return jsonify({
            "ok": True,
            "decision": decision,
            "oif_path": oif_path,
            "reason": reason,
        }), http_status

    @app.route("/webhook/tradingview/health", methods=["GET"])
    def health():
        """Cheap liveness probe — does NOT leak whether the secret is set."""
        return jsonify({"ok": True, "service": "tradingview_webhook"}), 200

    return app
