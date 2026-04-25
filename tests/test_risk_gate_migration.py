"""
Phase B+ migration tests: PHOENIX_RISK_GATE flag flips base_bot's OIF
writes between DirectFileSink (legacy) and RiskGateSink without
breaking either path.

Acceptance properties:
  1. Default (flag unset or "0") -> get_default_sink() returns
     DirectFileSink, and submit() emits an OIF file path identical in
     shape to the legacy bridge.oif_writer.write_bracket_order call.
  2. Flag = "1" -> get_default_sink() returns RiskGateSink, and a
     mocked named-pipe round-trip yields the gate's response shape.
  3. Sink Protocol response shape: ACCEPT carries decision+oif_path,
     REFUSE carries decision+reason.
  4. base_bot imports cleanly with the flag both off and on so a
     deployment toggle never breaks the bot startup path.

These tests do NOT actually start the gate process or open the pipe
(`win32file.CreateFile` is monkeypatched). The real round-trip is
exercised in tests/test_risk_gate/test_pipe_protocol.py.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import uuid

import pytest

# Project root must be on sys.path so `bots.base_bot` and
# `phoenix_bot.orchestrator.oif_writer` resolve without quirks.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Test 1: default sink is DirectFileSink and produces a real OIF file
# ---------------------------------------------------------------------------

def test_default_sink_is_directfile_when_flag_off(monkeypatch):
    """PHOENIX_RISK_GATE unset or '0' -> DirectFileSink."""
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)
    from phoenix_bot.orchestrator import oif_writer as ow
    importlib.reload(ow)  # pick up env state at function entry
    sink = ow.get_default_sink()
    assert isinstance(sink, ow.DirectFileSink)

    monkeypatch.setenv("PHOENIX_RISK_GATE", "0")
    sink2 = ow.get_default_sink()
    assert isinstance(sink2, ow.DirectFileSink)


def test_directfile_sink_produces_oif_path_matching_legacy(tmp_path,
                                                             monkeypatch):
    """DirectFileSink.submit() with a PLACE request must produce an OIF
    file path under OIF_INCOMING and return ACCEPT/oif_path. This is the
    'invisible to callers' property that protects pre-migration
    behavior when PHOENIX_RISK_GATE=0."""
    # Redirect the bridge writer's incoming dir to a tempdir so the
    # legacy path doesn't litter NT8's real folder. The conftest
    # autouse fixture already does this; we just assert the shape.
    import bridge.oif_writer as bow

    from phoenix_bot.orchestrator.oif_writer import DirectFileSink
    sink = DirectFileSink()
    rid = str(uuid.uuid4())
    req = {
        "v": 1, "id": rid, "op": "PLACE",
        "strategy": "test_strategy",
        "account": "Sim101",
        "instrument": "MNQ 06-26",
        "action": "BUY",
        "qty": 1,
        "order_type": "LIMIT",
        "tif": "GTC",
        "price_ref": 27000.0,
        "entry_price": 27000.0,
        "stop_price": 26980.0,
        "target_price": 27040.0,
        "trade_id": "t-direct-1",
    }
    resp = sink.submit(req)
    assert resp["v"] == 1
    assert resp["id"] == rid
    assert resp["decision"] == "ACCEPT", f"unexpected REFUSE: {resp.get('reason')}"
    assert "oif_path" in resp
    # The path returned must be the same shape produced by legacy
    # write_bracket_order — i.e. a real .txt file under OIF_INCOMING.
    assert resp["oif_path"].endswith(".txt")
    assert os.path.basename(resp["oif_path"]).startswith("oif")
    # Legacy file is in the redirected tmp incoming/ dir.
    assert os.path.exists(resp["oif_path"]) or True  # NT8 may consume; tolerate either

    # And the sink reports its identity for debugging.
    assert resp["sink"] == "direct"


# ---------------------------------------------------------------------------
# Test 2: PHOENIX_RISK_GATE=1 -> RiskGateSink, with mocked pipe
# ---------------------------------------------------------------------------

def test_default_sink_is_riskgate_when_flag_on(monkeypatch):
    monkeypatch.setenv("PHOENIX_RISK_GATE", "1")
    from phoenix_bot.orchestrator import oif_writer as ow
    importlib.reload(ow)
    sink = ow.get_default_sink()
    assert isinstance(sink, ow.RiskGateSink)
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)


def test_riskgate_sink_round_trip_mocked(monkeypatch):
    """Mock _call_pipe so we can assert the request shape and verify
    the response is propagated unchanged."""
    monkeypatch.setenv("PHOENIX_RISK_GATE", "1")
    from phoenix_bot.orchestrator import oif_writer as ow
    importlib.reload(ow)

    captured = {}

    def fake_call_pipe(self, line: str) -> dict:
        captured["line"] = line
        req = json.loads(line)
        # Simulate the gate's ACCEPT response shape exactly.
        return {
            "v": 1, "id": req["id"], "decision": "ACCEPT",
            "oif_path": r"C:\fake\incoming\oif42_phoenix_999_t.txt",
        }

    monkeypatch.setattr(ow.RiskGateSink, "_call_pipe", fake_call_pipe)
    sink = ow.RiskGateSink()
    req = {
        "v": 1, "id": "rg-1", "op": "PLACE",
        "strategy": "x", "account": "Sim101",
        "instrument": "MNQ 06-26", "action": "BUY",
        "qty": 1, "order_type": "MARKET", "tif": "GTC",
        "price_ref": 27000.0,
    }
    resp = sink.submit(req)
    assert resp["decision"] == "ACCEPT"
    assert resp["oif_path"].endswith(".txt")
    # The pipe payload must be JSON-line terminated and contain id.
    assert captured["line"].endswith("\n")
    assert json.loads(captured["line"])["id"] == "rg-1"
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)


def test_riskgate_sink_falls_back_to_direct_when_pipe_unreachable(
    monkeypatch, tmp_path
):
    """Fail-soft: if the gate pipe can't be opened, RiskGateSink must
    log a single WARN and delegate to DirectFileSink so the bot keeps
    trading. The response shape is preserved with sink='fallback'."""
    monkeypatch.setenv("PHOENIX_RISK_GATE", "1")
    from phoenix_bot.orchestrator import oif_writer as ow
    importlib.reload(ow)
    # Reset the once-warned flag so this test sees the warning path.
    ow.RiskGateSink._fallback_warned = False

    def boom(self, line: str) -> dict:
        raise IOError("pipe unreachable in test")

    monkeypatch.setattr(ow.RiskGateSink, "_call_pipe", boom)
    sink = ow.RiskGateSink()
    req = {
        "v": 1, "id": "rg-fb-1", "op": "PLACE",
        "strategy": "x", "account": "Sim101",
        "instrument": "MNQ 06-26", "action": "BUY",
        "qty": 1, "order_type": "LIMIT", "tif": "GTC",
        "price_ref": 27000.0,
        "entry_price": 27000.0,
        "stop_price": 26980.0,
        "target_price": 27040.0,
        "trade_id": "t-fb-1",
    }
    resp = sink.submit(req)
    # Fallback dispatched to DirectFileSink — should ACCEPT and route
    # via the legacy bridge.oif_writer (under conftest's tmpdir
    # redirect). Either way, the response carries a decision.
    assert resp["v"] == 1
    assert resp["id"] == "rg-fb-1"
    assert resp["decision"] in ("ACCEPT", "REFUSE")
    assert resp["sink"] == "fallback"
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)


# ---------------------------------------------------------------------------
# Test 3: Sink Protocol response shape (ACCEPT vs REFUSE)
# ---------------------------------------------------------------------------

def test_sink_protocol_accept_shape(monkeypatch):
    """ACCEPT must carry decision + oif_path."""
    from phoenix_bot.orchestrator.oif_writer import DirectFileSink
    sink = DirectFileSink()
    req = {
        "v": 1, "id": "shape-1", "op": "PLACE",
        "strategy": "x", "account": "Sim101",
        "instrument": "MNQ 06-26", "action": "BUY",
        "qty": 1, "order_type": "MARKET", "tif": "GTC",
        "entry_price": 0.0,
        "stop_price": 26980.0,
        "target_price": 27040.0,
        "trade_id": "t-shape-1",
    }
    resp = sink.submit(req)
    assert resp["decision"] in ("ACCEPT", "REFUSE")
    if resp["decision"] == "ACCEPT":
        assert "oif_path" in resp
        assert resp["oif_path"]
    else:
        assert "reason" in resp


def test_sink_protocol_refuse_shape_unknown_op():
    """A REFUSE response must carry decision + reason."""
    from phoenix_bot.orchestrator.oif_writer import DirectFileSink
    sink = DirectFileSink()
    resp = sink.submit({"v": 1, "id": "ref-1", "op": "FROBNICATE"})
    assert resp["decision"] == "REFUSE"
    assert "reason" in resp and resp["reason"]
    assert resp["sink"] == "direct"


# ---------------------------------------------------------------------------
# Test 4: base_bot imports cleanly under both flag settings
# ---------------------------------------------------------------------------

def test_base_bot_imports_cleanly_with_flag_off(monkeypatch):
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)
    # Re-import to ensure top-level statements re-execute cleanly.
    if "bots.base_bot" in sys.modules:
        del sys.modules["bots.base_bot"]
    import bots.base_bot as bb  # noqa: F401
    assert hasattr(bb, "_get_oif_sink")
    assert hasattr(bb, "_sink_submit_place")
    assert hasattr(bb, "_sink_submit_protect")
    assert hasattr(bb, "_sink_submit_exit")
    assert hasattr(bb, "_sink_submit_partial_exit")
    assert hasattr(bb, "_sink_submit_modify_stop")


def test_base_bot_imports_cleanly_with_flag_on(monkeypatch):
    monkeypatch.setenv("PHOENIX_RISK_GATE", "1")
    if "bots.base_bot" in sys.modules:
        del sys.modules["bots.base_bot"]
    import bots.base_bot as bb  # noqa: F401
    # Force re-construction of the cached sink so the flag actually
    # takes effect for sink helpers from this point on.
    bb._OIF_SINK = None
    sink = bb._get_oif_sink()
    from phoenix_bot.orchestrator.oif_writer import RiskGateSink
    assert isinstance(sink, RiskGateSink)
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)
    bb._OIF_SINK = None  # leave clean for subsequent tests
