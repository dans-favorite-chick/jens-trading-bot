"""Pipe protocol round-trip tests.

PipeServer's pure handler `_handle_message` lets us exercise the
full request → JSON → check chain → JSON response loop without ever
touching pywin32. The fake-pipe approach (via ``pipe_factory``) is
demonstrated in test_pipe_factory_injectable.

Heartbeat staleness is also covered here — the watchdog reads a file
mtime, which is straightforward to fake with tmp_path.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.risk.risk_config import RiskConfig
from core.risk.risk_gate import RiskGate
from core.risk.pipe_server import PipeServer

CT = ZoneInfo("America/Chicago")


def _request(**overrides) -> dict:
    base = {
        "v": 1, "id": "pipe-1", "op": "PLACE",
        "strategy": "bias_momentum", "account": "Sim101",
        "instrument": "MNQ 06-26", "action": "BUY", "qty": 1,
        "order_type": "MARKET", "tif": "GTC",
        "atm_template": "Phoenix_Standard",
        "price_ref": 27400.0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def gate(tmp_path):
    cfg = RiskConfig(
        oif_outgoing_dir=str(tmp_path / "incoming"),
        killswitch_marker_path=str(tmp_path / "memory" / ".HALT"),
        heartbeat_path=str(tmp_path / "heartbeat" / "risk_gate.hb"),
    )
    (tmp_path / "incoming").mkdir(parents=True)
    fixed_now = datetime(2026, 4, 22, 10, 0, tzinfo=CT)
    return RiskGate(cfg,
                    bridge_probe=lambda: {"nt8_status": "live"},
                    clock=lambda: fixed_now)


# ─── Round-trip: PLACE → ACCEPT ──────────────────────────────────

def test_place_round_trip_accept(gate):
    srv = PipeServer(gate)
    line = json.dumps(_request())
    raw = srv._handle_message(line)
    resp = json.loads(raw)
    assert resp["decision"] == "ACCEPT"
    assert "oif_path" in resp
    assert Path(resp["oif_path"]).exists()


# ─── Round-trip: malformed → REFUSE ──────────────────────────────

def test_invalid_json_round_trip_refuse(gate):
    srv = PipeServer(gate)
    raw = srv._handle_message("not json {")
    resp = json.loads(raw)
    assert resp["decision"] == "REFUSE"
    assert "bad_json" in resp["reason"]


# ─── pipe_factory injection: end-to-end via fake pipe class ──────

class _FakePipe:
    """Stand-in for a pywin32 pipe handle. Lets test code drive
    arbitrary lines through the server's pure handler."""

    def __init__(self):
        self.in_buf = b""
        self.out_buf = b""

    def write(self, data: bytes):
        self.in_buf += data

    def read(self) -> bytes:
        out = self.out_buf
        self.out_buf = b""
        return out


def test_pipe_factory_injectable(gate):
    """The PipeServer accepts a pipe_factory ctor arg so a fake pipe
    class can drive the loop in tests. Here we just verify the ctor
    accepts it and stores it without exploding."""
    fake = _FakePipe()
    srv = PipeServer(gate, pipe_factory=lambda: fake)
    assert srv.pipe_factory is not None
    # Drive a request through the pure handler — the same path the
    # pywin32 loop would invoke per connection.
    raw_resp = srv._handle_message(json.dumps(_request()))
    assert json.loads(raw_resp)["decision"] == "ACCEPT"


# ─── Heartbeat staleness probe ───────────────────────────────────

def test_heartbeat_age_fresh(tmp_path):
    from tools import watchdog_runner as wr
    hb = tmp_path / "risk_gate.hb"
    hb.write_text(str(time.time()))
    age = wr.heartbeat_age_s(hb)
    assert age < 1.0


def test_heartbeat_age_missing_is_inf(tmp_path):
    from tools import watchdog_runner as wr
    missing = tmp_path / "no_such.hb"
    age = wr.heartbeat_age_s(missing)
    assert age == float("inf")
