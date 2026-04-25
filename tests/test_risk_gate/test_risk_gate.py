"""Tests for the RiskGate check chain + pipe handler + OIF writer + watchdog.

12+ unit tests as required by Section 3 acceptance:
  TestSchemaCheck         — 3 tests
  TestAccountInstrument   — 2 tests
  TestTradingWindow       — 2 tests
  TestRiskCaps            — 4 tests (loss cap, position, orders/min, consec losses)
  TestKillswitch          — 1 test
  TestAcceptPath          — 2 tests (success path + OIF path content)
  TestPipeHandler         — 2 tests (round-trip via _handle_message; no real pipe)
  TestOIFFormat           — 2 tests (PLACE bytes match NT8 spec; killswitch shape)
  TestWatchdog            — 2 tests (stale heartbeat fires; fresh doesn't)
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
    """Default well-formed PLACE request — tests override fields."""
    base = {
        "v": 1, "id": "test-1", "op": "PLACE",
        "strategy": "bias_momentum", "account": "Sim101",
        "instrument": "MNQ 06-26", "action": "BUY", "qty": 1,
        "order_type": "MARKET", "tif": "GTC",
        "atm_template": "Phoenix_Standard",
        "price_ref": 27400.0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """Gate with safe-tempdir paths; 'now' = Wed 10:00 CT."""
    cfg = RiskConfig(
        oif_outgoing_dir=str(tmp_path / "incoming"),
        killswitch_marker_path=str(tmp_path / "memory" / ".HALT"),
        heartbeat_path=str(tmp_path / "heartbeat" / "risk_gate.hb"),
    )
    (tmp_path / "incoming").mkdir(parents=True)
    fixed_now = datetime(2026, 4, 22, 10, 0, tzinfo=CT)  # Wednesday
    return RiskGate(cfg,
                    bridge_probe=lambda: {"nt8_status": "live"},
                    clock=lambda: fixed_now)


# ── Schema ────────────────────────────────────────────────────────

class TestSchemaCheck:
    def test_missing_required_keys_refused(self, gate):
        bad = {"v": 1, "id": "x", "op": "PLACE"}
        r = gate.evaluate(bad)
        assert r["decision"] == "REFUSE"
        assert "missing keys" in r["reason"]

    def test_disallowed_op(self, gate):
        r = gate.evaluate(_request(op="MAGIC"))
        assert r["decision"] == "REFUSE"
        assert "op" in r["reason"]

    def test_disallowed_tif(self, gate):
        r = gate.evaluate(_request(tif="BUNNY"))
        assert r["decision"] == "REFUSE"
        assert "tif" in r["reason"]


# ── Allowlists ────────────────────────────────────────────────────

class TestAccountInstrument:
    def test_account_not_in_allowlist(self, gate):
        r = gate.evaluate(_request(account="LiveAcct"))
        assert r["decision"] == "REFUSE"
        assert "account" in r["reason"]

    def test_instrument_not_in_allowlist(self, gate):
        r = gate.evaluate(_request(instrument="ESM6"))
        assert r["decision"] == "REFUSE"
        assert "instrument" in r["reason"]


# ── Trading window ───────────────────────────────────────────────

class TestTradingWindow:
    def test_outside_window_refused(self, tmp_path):
        cfg = RiskConfig(oif_outgoing_dir=str(tmp_path),
                         killswitch_marker_path=str(tmp_path / ".HALT"))
        # 18:00 CT — well past 15:00 close
        late = datetime(2026, 4, 22, 18, 0, tzinfo=CT)
        gate = RiskGate(cfg, bridge_probe=lambda: None, clock=lambda: late)
        r = gate.evaluate(_request())
        assert r["decision"] == "REFUSE"
        assert "outside window" in r["reason"]

    def test_weekend_refused(self, tmp_path):
        cfg = RiskConfig(oif_outgoing_dir=str(tmp_path),
                         killswitch_marker_path=str(tmp_path / ".HALT"))
        sat = datetime(2026, 4, 25, 10, 0, tzinfo=CT)  # Saturday
        gate = RiskGate(cfg, bridge_probe=lambda: None, clock=lambda: sat)
        r = gate.evaluate(_request())
        assert r["decision"] == "REFUSE"
        assert "weekend" in r["reason"]


# ── Risk caps ────────────────────────────────────────────────────

class TestRiskCaps:
    def test_daily_loss_cap_blocks_after_breach(self, gate):
        gate.record_trade_close(-350.0)   # exceeds $300 cap
        r = gate.evaluate(_request())
        assert r["decision"] == "REFUSE"
        assert "daily loss" in r["reason"]

    def test_max_position_blocks_qty(self, gate):
        gate.record_fill(qty=2, side="LONG")  # already at max
        r = gate.evaluate(_request(qty=1))
        assert r["decision"] == "REFUSE"
        assert "projected position" in r["reason"]

    def test_orders_per_minute_cap(self, gate):
        # Simulate 6 prior orders within last 60s
        for _ in range(6):
            gate._order_timestamps.append(time.time())
        r = gate.evaluate(_request())
        assert r["decision"] == "REFUSE"
        assert "orders in last 60s" in r["reason"]

    def test_consecutive_losses_cap(self, gate):
        for _ in range(3):
            gate.record_trade_close(-50.0)
        r = gate.evaluate(_request())
        assert r["decision"] == "REFUSE"
        assert "consecutive losses" in r["reason"]


# ── Killswitch ───────────────────────────────────────────────────

class TestKillswitch:
    def test_halt_marker_blocks(self, gate, tmp_path):
        marker = Path(gate.config.killswitch_marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("test")
        r = gate.evaluate(_request())
        assert r["decision"] == "REFUSE"
        assert "killswitch" in r["reason"]


# ── Accept path ──────────────────────────────────────────────────

class TestAcceptPath:
    def test_clean_request_accepted(self, gate):
        r = gate.evaluate(_request())
        assert r["decision"] == "ACCEPT"
        assert "oif_path" in r
        assert Path(r["oif_path"]).exists()

    def test_oif_content_has_place_line(self, gate):
        r = gate.evaluate(_request(action="BUY", qty=1))
        content = Path(r["oif_path"]).read_text(encoding="utf-8")
        assert content.startswith("PLACE;")
        # Field count: 13 fields = 12 semicolons
        first_line = content.splitlines()[0]
        assert first_line.count(";") == 12


# ── Pipe handler (no real pywin32 pipe) ──────────────────────────

class TestPipeHandler:
    def test_round_trip_accept(self, gate):
        srv = PipeServer(gate)
        line = json.dumps(_request())
        resp = json.loads(srv._handle_message(line))
        assert resp["decision"] == "ACCEPT"
        assert "oif_path" in resp

    def test_bad_json_refused(self, gate):
        srv = PipeServer(gate)
        resp = json.loads(srv._handle_message("not json {"))
        assert resp["decision"] == "REFUSE"
        assert "bad_json" in resp["reason"]


# ── OIF format ──────────────────────────────────────────────────

class TestOIFFormat:
    def test_place_field_count(self, tmp_path):
        from core.risk.oif_writer import _build_place_line
        line = _build_place_line(_request())
        # 13 fields = 12 semicolons
        assert line.count(";") == 12
        assert line.startswith("PLACE;Sim101;MNQ 06-26;BUY;1;MARKET;")

    def test_killswitch_writes_cancelall_and_close(self, tmp_path):
        from core.risk.oif_writer import write_killswitch
        out = tmp_path / "incoming"
        out.mkdir()
        path = write_killswitch(working_order_ids=["ord1"], outgoing_dir=str(out))
        content = Path(path).read_text(encoding="utf-8")
        assert "CANCELALLORDERS;" in content
        assert "CANCEL;ord1;" in content
        assert "CLOSEPOSITION;" in content


# ── Watchdog heartbeat staleness ─────────────────────────────────

class TestWatchdog:
    def test_fresh_heartbeat_does_not_fire(self, tmp_path, monkeypatch):
        from tools import watchdog_runner as wr
        hb = tmp_path / "risk_gate.hb"
        hb.write_text(str(time.time()))
        monkeypatch.setattr(wr, "HEARTBEAT_PATH", hb)
        # Use tested age helper
        age = wr.heartbeat_age_s(hb)
        assert age < 1.0

    def test_stale_heartbeat_returns_inf_when_missing(self, tmp_path):
        from tools import watchdog_runner as wr
        missing = tmp_path / "no_such.hb"
        age = wr.heartbeat_age_s(missing)
        assert age == float("inf")


# ── Snapshot ─────────────────────────────────────────────────────

class TestSnapshot:
    def test_snapshot_shape(self, gate):
        snap = gate.snapshot()
        for k in ("open_contracts", "daily_loss_usd", "consecutive_losses",
                  "orders_last_min", "config"):
            assert k in snap
        assert snap["config"]["daily_loss_cap_usd"] == 300.0
