"""Phase D (2026-06-02 overnight) tests for /api/market_state.

Covers:
  - live path (`_state["prod"]["market_state"]` present)
  - warehouse_fallback path (most-recent backfilled row)
  - stub path (warehouse query raises -> NEUTRAL stub)
  - response schema is what the dashboard tile expects
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client():
    from dashboard import server as srv
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c, srv


EXPECTED_KEYS = {
    "label", "realized_vol", "trend_strength", "choppiness_index",
    "computed_at", "source",
}


# ─── (a) live path ────────────────────────────────────────────────

def test_market_state_live_state_wins(client):
    c, srv = client
    with srv._state_lock:
        srv._state["prod"] = {
            "market_state": {
                "label": "TRENDING_NORMAL",
                "realized_vol": 1.05,
                "trend_strength": 0.42,
                "choppiness_index": 40.0,
                "computed_at": "2026-06-02T05:00:00+00:00",
            }
        }
    try:
        r = c.get("/api/market_state")
        assert r.status_code == 200
        d = r.get_json()
        assert d["label"] == "TRENDING_NORMAL"
        assert d["source"] == "live"
        assert d["realized_vol"] == 1.05
        assert EXPECTED_KEYS.issubset(d.keys())
    finally:
        with srv._state_lock:
            srv._state["prod"].pop("market_state", None)


# ─── (b) warehouse fallback path ──────────────────────────────────

def test_market_state_warehouse_fallback(client):
    c, srv = client
    # Make sure live path doesn't fire.
    with srv._state_lock:
        srv._state.pop("prod", None)
    # Real warehouse exists in this checkout (Phase 8 backfill).
    r = c.get("/api/market_state")
    assert r.status_code == 200
    d = r.get_json()
    assert EXPECTED_KEYS.issubset(d.keys())
    # source should be warehouse_fallback OR stub if DB is missing.
    assert d["source"] in ("warehouse_fallback", "stub")
    # If warehouse present, label must be one of the 6.
    if d["source"] == "warehouse_fallback":
        assert d["label"] in {
            "WHIPSAW_HIGH_VOL", "CHOPPY", "COMPRESSED",
            "TRENDING_HIGH_VOL", "TRENDING_NORMAL", "NEUTRAL",
        }


# ─── (c) stub path when warehouse query errors ───────────────────

def test_market_state_stub_on_warehouse_failure(client, monkeypatch):
    c, srv = client
    with srv._state_lock:
        srv._state.pop("prod", None)

    import duckdb as _dd

    def boom(*a, **k):
        raise RuntimeError("simulated warehouse outage")

    monkeypatch.setattr(_dd, "connect", boom)
    r = c.get("/api/market_state")
    assert r.status_code == 200
    d = r.get_json()
    assert d["source"] == "stub"
    assert d["label"] == "NEUTRAL"
    assert d["realized_vol"] == 0.0


# ─── (d) live path with stale computed_at gets a fresh one ──────

def test_market_state_live_missing_computed_at_filled(client):
    c, srv = client
    with srv._state_lock:
        srv._state["prod"] = {
            "market_state": {
                "label": "COMPRESSED",
                "realized_vol": 0.5,
                "trend_strength": 0.1,
                "choppiness_index": 70.0,
                # NOTE: no computed_at
            }
        }
    try:
        r = c.get("/api/market_state")
        d = r.get_json()
        assert d["computed_at"] is not None
        assert d["source"] == "live"
    finally:
        with srv._state_lock:
            srv._state["prod"].pop("market_state", None)
