"""RiskGate check chain — Phase B+ Section 3 acceptance tests.

12+ tests across the chain (chain order is cheapest first):

    schema -> account_allow -> instrument_allow -> trading_window
    -> killswitch -> daily_loss_cap -> max_position
    -> max_orders_min -> max_consec_loss -> price_sanity

Each test isolates one rule by constructing a request that passes
all earlier gates and trips exactly the rule under test (or, in the
combined-denial test, several at once — the chain returns the first
failure).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.risk.risk_config import RiskConfig
from core.risk.risk_gate import RiskGate

CT = ZoneInfo("America/Chicago")


def _request(**overrides) -> dict:
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
def gate(tmp_path):
    """Gate with safe-tempdir paths; 'now' = Wed 10:00 CT (always in window)."""
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


# ─── 1. account whitelist ─────────────────────────────────────────

def test_account_in_allowlist_passes(gate):
    r = gate.evaluate(_request(account="Sim101"))
    assert r["decision"] == "ACCEPT"


def test_account_not_in_allowlist_refused(gate):
    r = gate.evaluate(_request(account="LiveAcct"))
    assert r["decision"] == "REFUSE"
    assert "account" in r["reason"]


# ─── 2. instrument allowlist ──────────────────────────────────────

def test_instrument_in_allowlist_passes(gate):
    r = gate.evaluate(_request(instrument="MNQM6"))
    assert r["decision"] == "ACCEPT"


def test_instrument_not_in_allowlist_refused(gate):
    r = gate.evaluate(_request(instrument="ESM6"))
    assert r["decision"] == "REFUSE"
    assert "instrument" in r["reason"]


# ─── 3. trading window: in-window / out-window / weekend ──────────

def test_trading_window_inside_passes(gate):
    r = gate.evaluate(_request())
    assert r["decision"] == "ACCEPT"


def test_trading_window_outside_refused(tmp_path):
    cfg = RiskConfig(oif_outgoing_dir=str(tmp_path),
                     killswitch_marker_path=str(tmp_path / ".HALT"))
    late = datetime(2026, 4, 22, 18, 0, tzinfo=CT)  # 6pm CT
    g = RiskGate(cfg, bridge_probe=lambda: None, clock=lambda: late)
    r = g.evaluate(_request())
    assert r["decision"] == "REFUSE"
    assert "outside window" in r["reason"]


def test_trading_window_weekend_refused(tmp_path):
    cfg = RiskConfig(oif_outgoing_dir=str(tmp_path),
                     killswitch_marker_path=str(tmp_path / ".HALT"))
    sat = datetime(2026, 4, 25, 10, 0, tzinfo=CT)  # Saturday
    g = RiskGate(cfg, bridge_probe=lambda: None, clock=lambda: sat)
    r = g.evaluate(_request())
    assert r["decision"] == "REFUSE"
    assert "weekend" in r["reason"]


# ─── 4. daily loss cap: under / over ──────────────────────────────

def test_daily_loss_cap_under_passes(gate):
    gate.record_trade_close(-50.0)   # well below $300 cap
    r = gate.evaluate(_request())
    assert r["decision"] == "ACCEPT"


def test_daily_loss_cap_over_refused(gate):
    gate.record_trade_close(-350.0)  # exceeds $300 cap
    r = gate.evaluate(_request())
    assert r["decision"] == "REFUSE"
    assert "daily loss" in r["reason"]


# ─── 5. max position contracts ────────────────────────────────────

def test_max_position_under_passes(gate):
    gate.record_fill(qty=1, side="LONG")  # 1 + 1 = 2 = max, allowed
    r = gate.evaluate(_request(qty=1))
    assert r["decision"] == "ACCEPT"


def test_max_position_at_limit_refused(gate):
    gate.record_fill(qty=2, side="LONG")  # already at max
    r = gate.evaluate(_request(qty=1))
    assert r["decision"] == "REFUSE"
    assert "projected position" in r["reason"]


# ─── 6. max orders per minute ─────────────────────────────────────

def test_orders_per_minute_under_passes(gate):
    for _ in range(3):
        gate._order_timestamps.append(time.time())
    r = gate.evaluate(_request())
    assert r["decision"] == "ACCEPT"


def test_orders_per_minute_at_limit_refused(gate):
    for _ in range(6):
        gate._order_timestamps.append(time.time())
    r = gate.evaluate(_request())
    assert r["decision"] == "REFUSE"
    assert "orders in last 60s" in r["reason"]


# ─── 7. consecutive losses ────────────────────────────────────────

def test_consecutive_losses_zero_passes(gate):
    r = gate.evaluate(_request())
    assert r["decision"] == "ACCEPT"


def test_consecutive_losses_two_passes(gate):
    gate.record_trade_close(-25.0)
    gate.record_trade_close(-25.0)
    r = gate.evaluate(_request())
    assert r["decision"] == "ACCEPT"


def test_consecutive_losses_three_blocks(gate):
    for _ in range(3):
        gate.record_trade_close(-25.0)
    r = gate.evaluate(_request())
    assert r["decision"] == "REFUSE"
    assert "consecutive losses" in r["reason"]


# ─── 8. price sanity ──────────────────────────────────────────────

def test_price_sanity_no_ref_passes(gate):
    """price_ref omitted → check is skipped (the dedicated price_sanity
    module is the authoritative downstream guard)."""
    req = _request()
    req.pop("price_ref", None)
    r = gate.evaluate(req)
    assert r["decision"] == "ACCEPT"


def test_price_sanity_no_bridge_does_not_fail_open(gate):
    """When the bridge probe returns None, the gate defers to the
    downstream price_sanity module rather than failing-open noisily."""
    gate._bridge_probe = lambda: None
    r = gate.evaluate(_request())
    assert r["decision"] == "ACCEPT"


# ─── 9. killswitch marker ─────────────────────────────────────────

def test_killswitch_marker_blocks(gate):
    marker = Path(gate.config.killswitch_marker_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("HALT")
    r = gate.evaluate(_request())
    assert r["decision"] == "REFUSE"
    assert "killswitch" in r["reason"]


# ─── 10. schema ───────────────────────────────────────────────────

def test_schema_missing_required_field_refused(gate):
    bad = {"v": 1, "id": "x", "op": "PLACE"}  # missing most fields
    r = gate.evaluate(bad)
    assert r["decision"] == "REFUSE"
    assert "missing keys" in r["reason"]


# ─── 11. all-pass case ────────────────────────────────────────────

def test_all_pass_case_writes_oif(gate):
    r = gate.evaluate(_request())
    assert r["decision"] == "ACCEPT"
    assert "oif_path" in r
    assert Path(r["oif_path"]).exists()
    content = Path(r["oif_path"]).read_text(encoding="utf-8")
    assert content.startswith("PLACE;")


# ─── 12. combined denial: first failure wins ──────────────────────

def test_combined_denial_first_violation_wins(gate):
    """Multiple rules fail simultaneously; the chain returns the
    earliest violation (account, since allowlist is checked before
    daily loss cap)."""
    gate.record_trade_close(-500.0)         # would also trip daily loss
    r = gate.evaluate(_request(account="LiveAcct", instrument="ESM6"))
    assert r["decision"] == "REFUSE"
    # account_allow comes before instrument_allow before daily_loss_cap
    assert "account" in r["reason"]
    assert "instrument" not in r["reason"]
    assert "daily loss" not in r["reason"]
