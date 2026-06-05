"""Regression for FINDING-2026-06-05-SLOT-INTERLOCK-BYPASS-T-BRIDGE.

The T-BRIDGE protected-edit sprint at `bridge/bridge_server.py`
`_handle_trade_command` translates WS `action="EXIT"` with `qty > 0`
and a valid `direction` field into a sized `PARTIAL_EXIT_<dir>` OIF
(`PLACE;<account>;<instrument>;<side>;<qty>;MARKET;...`) instead of
the unbounded account-wide CLOSEPOSITION emit.

Three semantic invariants the swap MUST preserve:

  1. Kill-switch (qty=0): emit legacy CLOSEPOSITION.
  2. Backward-compat (qty key missing entirely): emit legacy
     CLOSEPOSITION + log warning. The bot should always send qty=N
     for normal exits; missing-qty is a sign of a malformed/older
     bot message that the bridge should still handle safely.
  3. Invalid direction: reject + log [SLOT-INTERLOCK] error, emit
     NOTHING. A typo-ed direction must NOT silently fall through
     to CLOSEPOSITION (because that would re-open the multi-strategy
     wipeout hole the swap closes).

Plus the byte-level sentinel: actually invoke `write_partial_exit`
against a tmp OIF_INCOMING + OIF_STAGING and assert the file
contents are `PLACE;Sim101;<INSTRUMENT>;SELL;1;MARKET;0;0;GTC`
(the canonical sized-LONG-close MNQ format).

These tests instantiate the real `BridgeServer` (its __init__ has
no external I/O) and exercise `_handle_trade_command` through
`asyncio.run`. `write_oif`, `write_partial_exit`, `check_latest_fill`,
and `asyncio.sleep` are monkeypatched at module level so the test
runs in <1s without touching real OIF directories.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Shared fixtures ───────────────────────────────────────────────────

@pytest.fixture
def bs_module(monkeypatch):
    """Import bridge_server, monkeypatch its slow / I/O internals,
    return the module. Tests then call srv._handle_trade_command via
    asyncio.run."""
    import logging
    from bridge import bridge_server as bs

    # Patch the 1-second post-write sleep — no value in a test.
    async def _no_sleep(*a, **kw):
        return None
    monkeypatch.setattr(bs.asyncio, "sleep", _no_sleep)

    # Patch the post-write fill check — returns None in tests.
    monkeypatch.setattr(bs, "check_latest_fill",
                         lambda since_time=0: None)

    # The "Trades" logger has propagate=False in production (writes to
    # logs/trades.log). Pytest's caplog captures via root, so flip
    # propagate ON for the duration of the test so [SLOT-INTERLOCK]
    # records are visible to caplog. monkeypatch resets on teardown.
    trade_logger = logging.getLogger("Trades")
    monkeypatch.setattr(trade_logger, "propagate", True)

    return bs


@pytest.fixture
def srv(bs_module):
    """A fresh BridgeServer with no external connections. The
    bot_connections dict is empty so the acknowledgement send is a
    no-op."""
    s = bs_module.BridgeServer()
    s.bot_connections = {}  # no ack send
    return s


def _capture():
    """Build a (mock, calls_list) pair where mock appends each call's
    (args, kwargs) to calls_list and returns a synthetic path list."""
    calls = []
    def fake(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        # Return a non-empty list so the bridge's `if not paths` branch
        # doesn't fire (legacy code reads len(paths) > 0 as success).
        return [f"/fake/oif/{kwargs.get('trade_id', 'noid')}.txt"]
    return fake, calls


# ── T-BRIDGE-1: qty=1, LONG → sized PARTIAL_EXIT ──────────────────────

def test_t_bridge_1_qty1_long_translates_to_partial_exit(bs_module, srv,
                                                          monkeypatch):
    fake_oif, oif_calls = _capture()
    fake_pe, pe_calls = _capture()
    monkeypatch.setattr(bs_module, "write_oif", fake_oif)
    monkeypatch.setattr(bs_module, "write_partial_exit", fake_pe)

    payload = {
        "action": "EXIT", "qty": 1, "direction": "LONG",
        "trade_id": "t_bridge_1", "account": "Sim101",
    }
    asyncio.run(srv._handle_trade_command("test_bot", payload))

    assert len(pe_calls) == 1, (
        f"T-BRIDGE-1: write_partial_exit must be called exactly once "
        f"for action=EXIT qty=1 LONG. got pe_calls={pe_calls!r} "
        f"oif_calls={oif_calls!r}"
    )
    assert len(oif_calls) == 0, (
        f"T-BRIDGE-1: legacy write_oif must NOT be called when the "
        f"sized swap fires. got: {oif_calls!r}"
    )
    kw = pe_calls[0]["kwargs"]
    assert kw["direction"] == "LONG"
    assert kw["n_contracts"] == 1
    assert kw["trade_id"] == "t_bridge_1"
    assert kw["account"] == "Sim101"


# ── T-BRIDGE-2: qty=3, SHORT → sized PARTIAL_EXIT ─────────────────────

def test_t_bridge_2_qty3_short_translates_to_partial_exit(bs_module, srv,
                                                           monkeypatch):
    fake_oif, oif_calls = _capture()
    fake_pe, pe_calls = _capture()
    monkeypatch.setattr(bs_module, "write_oif", fake_oif)
    monkeypatch.setattr(bs_module, "write_partial_exit", fake_pe)

    payload = {
        "action": "EXIT", "qty": 3, "direction": "SHORT",
        "trade_id": "t_bridge_2", "account": "Sim101",
    }
    asyncio.run(srv._handle_trade_command("test_bot", payload))

    assert len(pe_calls) == 1 and len(oif_calls) == 0, (
        f"qty=3 SHORT must hit write_partial_exit only. pe={pe_calls!r} "
        f"oif={oif_calls!r}"
    )
    kw = pe_calls[0]["kwargs"]
    assert kw["direction"] == "SHORT"
    assert kw["n_contracts"] == 3


# ── T-BRIDGE-3: qty=0 kill-switch → legacy CLOSEPOSITION ──────────────

def test_t_bridge_3_qty0_preserves_legacy_close_position(bs_module, srv,
                                                          monkeypatch):
    fake_oif, oif_calls = _capture()
    fake_pe, pe_calls = _capture()
    monkeypatch.setattr(bs_module, "write_oif", fake_oif)
    monkeypatch.setattr(bs_module, "write_partial_exit", fake_pe)

    payload = {
        "action": "EXIT", "qty": 0, "direction": "LONG",
        "trade_id": "t_bridge_3", "account": "Sim101",
    }
    asyncio.run(srv._handle_trade_command("test_bot", payload))

    # qty=0 is the canonical kill-switch path — MUST emit CLOSEPOSITION
    assert len(oif_calls) == 1, (
        f"T-BRIDGE-3: qty=0 kill-switch MUST use legacy CLOSEPOSITION "
        f"via write_oif. got oif={oif_calls!r} pe={pe_calls!r}"
    )
    assert len(pe_calls) == 0, (
        f"T-BRIDGE-3: write_partial_exit must NOT fire for qty=0. "
        f"got: {pe_calls!r}"
    )
    # action MUST still be "EXIT" passed through to write_oif
    assert oif_calls[0]["args"][0] == "EXIT"


# ── T-BRIDGE-4: missing qty key → legacy + warning ────────────────────

def test_t_bridge_4_missing_qty_preserves_legacy_and_warns(
    bs_module, srv, monkeypatch, caplog
):
    fake_oif, oif_calls = _capture()
    fake_pe, pe_calls = _capture()
    monkeypatch.setattr(bs_module, "write_oif", fake_oif)
    monkeypatch.setattr(bs_module, "write_partial_exit", fake_pe)

    # NO "qty" key at all — bot side should always send it; this is
    # the malformed-message backward-compat path.
    payload = {
        "action": "EXIT", "direction": "LONG",
        "trade_id": "t_bridge_4", "account": "Sim101",
    }
    import logging
    # bridge_server uses the "Trades" logger for these messages —
    # caplog default captures root, so target explicitly.
    with caplog.at_level(logging.WARNING, logger="Trades"):
        asyncio.run(srv._handle_trade_command("test_bot", payload))

    assert len(oif_calls) == 1, (
        f"T-BRIDGE-4: missing qty MUST fall through to legacy write_oif. "
        f"got oif={oif_calls!r} pe={pe_calls!r}"
    )
    assert len(pe_calls) == 0
    # And the warning should be visible so operators can catch the
    # malformed-message regression.
    warn_msgs = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "SLOT-INTERLOCK" in warn_msgs and "qty" in warn_msgs.lower(), (
        f"T-BRIDGE-4: expected [SLOT-INTERLOCK] warn about missing qty; "
        f"got logs: {[r.getMessage() for r in caplog.records]}"
    )


# ── T-BRIDGE-5: invalid direction → reject, emit nothing ──────────────

def test_t_bridge_5_invalid_direction_rejects_emit(
    bs_module, srv, monkeypatch, caplog
):
    fake_oif, oif_calls = _capture()
    fake_pe, pe_calls = _capture()
    monkeypatch.setattr(bs_module, "write_oif", fake_oif)
    monkeypatch.setattr(bs_module, "write_partial_exit", fake_pe)

    payload = {
        "action": "EXIT", "qty": 2, "direction": "INVALID",
        "trade_id": "t_bridge_5", "account": "Sim101",
    }
    import logging
    with caplog.at_level(logging.ERROR, logger="Trades"):
        asyncio.run(srv._handle_trade_command("test_bot", payload))

    assert len(oif_calls) == 0 and len(pe_calls) == 0, (
        f"T-BRIDGE-5: invalid direction MUST reject — no emit at all. "
        f"got oif={oif_calls!r} pe={pe_calls!r}"
    )
    err_msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "SLOT-INTERLOCK" in err_msgs and "direction" in err_msgs.lower(), (
        f"T-BRIDGE-5: expected [SLOT-INTERLOCK] error about direction; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


# ── T-BRIDGE-6: qty>0 but missing direction → legacy + warn ──────────
#
# Production reality: bots/_trade_exit.py, bots/sim_bot.py, and
# bots/base_bot.py all send WS EXIT payloads WITHOUT a `direction`
# field today (verified via grep at sprint Phase 0). The bridge swap
# must NOT reject those — that would silently break every normal
# exit. Instead the bridge falls through to legacy CLOSEPOSITION
# + emits a [SLOT-INTERLOCK] warn nudging the operator to update
# the bot WS payload. A separate follow-up sprint (unprotected,
# bots/* edits) will add `direction: pos.direction` to those WS
# sends so the swap actually engages in production.

def test_t_bridge_6_qty_positive_missing_direction_falls_back_with_warn(
    bs_module, srv, monkeypatch, caplog
):
    fake_oif, oif_calls = _capture()
    fake_pe, pe_calls = _capture()
    monkeypatch.setattr(bs_module, "write_oif", fake_oif)
    monkeypatch.setattr(bs_module, "write_partial_exit", fake_pe)

    # Production-shaped EXIT WS payload (matches current
    # bots/_trade_exit.py:134-140 shape — NO direction).
    payload = {
        "type": "trade", "trade_id": "t_bridge_6",
        "action": "EXIT", "qty": 2,
        "account": "Sim101", "reason": "test_target_hit",
    }
    import logging
    with caplog.at_level(logging.WARNING, logger="Trades"):
        asyncio.run(srv._handle_trade_command("test_bot", payload))

    assert len(oif_calls) == 1, (
        f"T-BRIDGE-6: missing direction with qty>0 MUST fall through "
        f"to legacy write_oif — silently rejecting would break every "
        f"production exit until bots are updated. "
        f"got oif={oif_calls!r} pe={pe_calls!r}"
    )
    assert len(pe_calls) == 0, (
        f"T-BRIDGE-6: write_partial_exit must NOT fire when direction "
        f"is missing. got: {pe_calls!r}"
    )
    # And the warn surface should mention direction for the operator
    # forensics nudge.
    warn_msgs = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "SLOT-INTERLOCK" in warn_msgs and "direction" in warn_msgs.lower(), (
        f"T-BRIDGE-6: expected [SLOT-INTERLOCK] warn mentioning "
        f"direction; got: {[r.getMessage() for r in caplog.records]}"
    )


# ── T-BRIDGE-string-qty: red-team scenario, qty arrives as string ─────

def test_t_bridge_string_qty_still_translates(bs_module, srv, monkeypatch):
    """Red-team scenario #2: WS payload with qty as string "2" instead
    of int 2. The defensive int() parse must accept it and fire the
    swap, not silently fall through to CLOSEPOSITION."""
    fake_oif, oif_calls = _capture()
    fake_pe, pe_calls = _capture()
    monkeypatch.setattr(bs_module, "write_oif", fake_oif)
    monkeypatch.setattr(bs_module, "write_partial_exit", fake_pe)

    payload = {
        "action": "EXIT", "qty": "2", "direction": "LONG",
        "trade_id": "t_bridge_str", "account": "Sim101",
    }
    asyncio.run(srv._handle_trade_command("test_bot", payload))

    assert len(pe_calls) == 1, (
        f"String '2' qty should parse via int() and fire the swap; "
        f"got pe={pe_calls!r} oif={oif_calls!r}"
    )
    assert pe_calls[0]["kwargs"]["n_contracts"] == 2


# ── Byte-level sentinel: actual write_partial_exit OIF bytes ──────────

def test_byte_level_sized_long_close_writes_correct_oif(tmp_path,
                                                         monkeypatch):
    """Byte-level pin: actually invoke `write_partial_exit` against a
    tmp OIF_INCOMING + OIF_STAGING and assert the file contents start
    with `PLACE;Sim101;<INSTRUMENT>;SELL;1;MARKET;0;0;GTC` — the
    canonical sized-LONG-close MNQ format documented in the
    `nq-trading-skills` rule sheet."""
    from bridge import oif_writer as ow
    from config.settings import INSTRUMENT

    incoming = tmp_path / "incoming"
    staging = tmp_path / "incoming.staging"
    outgoing = tmp_path / "outgoing"
    incoming.mkdir()
    staging.mkdir()
    outgoing.mkdir()

    monkeypatch.setattr(ow, "OIF_INCOMING", str(incoming))
    monkeypatch.setattr(ow, "OIF_STAGING", str(staging))
    monkeypatch.setattr(ow, "OIF_OUTGOING", str(outgoing))

    paths = ow.write_partial_exit(
        direction="LONG", n_contracts=1,
        trade_id="byte_test_long", account="Sim101",
    )

    assert paths, "write_partial_exit must return at least one path"
    oif_files = list(incoming.glob("oif*.txt"))
    assert oif_files, f"no oif* file created in {incoming}"

    content = oif_files[0].read_text(encoding="utf-8").strip()
    # The canonical sized-close MNQ format:
    # PLACE;Sim101;MNQM6;SELL;1;MARKET;0;0;GTC[;...]
    parts = content.split(";")
    assert parts[0] == "PLACE", f"expected PLACE; got {content!r}"
    assert parts[1] == "Sim101", f"account mismatch in {content!r}"
    assert parts[2].startswith(INSTRUMENT.split()[0]), (
        f"instrument mismatch — expected prefix {INSTRUMENT!r}; got {content!r}"
    )
    assert parts[3] == "SELL", (
        f"LONG close must be SELL side; got {content!r}"
    )
    assert parts[4] == "1", f"qty must be 1; got {content!r}"
    assert parts[5] == "MARKET", f"order type must be MARKET; got {content!r}"


def test_byte_level_sized_short_close_writes_buy_side(tmp_path,
                                                       monkeypatch):
    """Byte-level pin: SHORT close → BUY side at the OIF byte level."""
    from bridge import oif_writer as ow

    incoming = tmp_path / "incoming"
    staging = tmp_path / "incoming.staging"
    outgoing = tmp_path / "outgoing"
    incoming.mkdir()
    staging.mkdir()
    outgoing.mkdir()

    monkeypatch.setattr(ow, "OIF_INCOMING", str(incoming))
    monkeypatch.setattr(ow, "OIF_STAGING", str(staging))
    monkeypatch.setattr(ow, "OIF_OUTGOING", str(outgoing))

    paths = ow.write_partial_exit(
        direction="SHORT", n_contracts=3,
        trade_id="byte_test_short", account="Sim101",
    )

    assert paths
    content = list(incoming.glob("oif*.txt"))[0].read_text(encoding="utf-8").strip()
    parts = content.split(";")
    assert parts[3] == "BUY", (
        f"SHORT close must be BUY side; got {content!r}"
    )
    assert parts[4] == "3", f"qty must be 3; got {content!r}"
