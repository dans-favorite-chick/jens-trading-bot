"""Regression for FINDING-2026-06-05-T-BRIDGE-ENGAGEMENT-GAP.

The bridge swap shipped at 607ac74 (`bridge/bridge_server.py`,
OPERATOR-APPROVED 2026-06-05) translates WS `action="EXIT"` payloads
that carry both `qty > 0` AND a valid `direction` into a sized
`PLACE;<account>;<instrument>;<side>;<qty>;MARKET;...` PARTIAL_EXIT
OIF, instead of the unbounded account-wide CLOSEPOSITION emit.

The swap is correct on disk but INERT until the three bot-side WS
EXIT senders include `direction` in their payload. Without
`direction`, the bridge's backward-compat branch logs
``[SLOT-INTERLOCK] EXIT missing direction — falling back to
CLOSEPOSITION (legacy behavior)`` (preserved by T-BRIDGE-6) and the
sized translation never runs.

This file pins the engagement contract at the THREE bot WS EXIT
sites. Each test inspects the SERIALIZED JSON string actually passed
to ``websocket.send`` (the bytes the bridge sees), NOT the
pre-serialization dict — per the master-prompt R3 quality bar.

Run: pytest tests/test_bot_ws_exit_includes_direction.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ─────────────────────────────────────────────────────────────────
# Shared serialized-payload capture
#
# The 3 sites all use the literal pattern
#     await ws.send(json.dumps({..., "action": "EXIT", ...}))
# wrapped in
#     try: ... except Exception as e: logger.error(...)
# We must inspect the JSON STRING (not the dict) because the
# acceptance bar is "what bytes does the bridge see". We use a
# BaseException subclass so the outer `except Exception` doesn't
# swallow it — the test sees the raised sentinel + a captured
# payload, and the downstream OIF-fallback / position-close paths
# never fire (no monkeypatching of trade_memory, telegram, etc.).
# ─────────────────────────────────────────────────────────────────

class _CaptureSendAbort(BaseException):
    """Sentinel raised AFTER ws.send captures the JSON payload.

    BaseException — not Exception — so it propagates THROUGH the
    `except Exception:` in each call site (the production code logs
    and falls back); the test catches it directly via pytest.raises.
    """


def _capturing_ws(captures: list[str]):
    """Build a fake ws with an async `send` method that records the
    first argument as a JSON string and then raises the abort
    sentinel to short-circuit the rest of the call site."""
    async def _send(payload, *a, **kw):
        captures.append(payload)
        raise _CaptureSendAbort()

    ws = MagicMock()
    ws.send = _send
    return ws


def _parse_captured(captures: list[str]) -> dict:
    """Assert exactly one capture and return the parsed JSON dict."""
    assert len(captures) == 1, (
        f"Expected exactly one ws.send call before abort, got "
        f"{len(captures)}: {captures!r}"
    )
    # The captured value MUST be a json.dumps string — confirm it
    # parses cleanly. This is the contract the bridge depends on.
    assert isinstance(captures[0], str), (
        f"ws.send received non-string payload: type={type(captures[0]).__name__}"
    )
    return json.loads(captures[0])


# ─────────────────────────────────────────────────────────────────
# Site 1 — bots/_trade_exit.py:135 (TradeExit.exit_trade)
# ─────────────────────────────────────────────────────────────────

def _build_trade_exit_bot(pos):
    """Minimal mock bot for TradeExit.exit_trade.

    exit_trade does many things AFTER ws.send (rider state reset,
    portfolio-gate exit hook, telegram notifier, etc.) — we abort via
    _CaptureSendAbort BEFORE those run so we only need the attributes
    that exist in the pre-send path.
    """
    bot = MagicMock()
    # exit_trace prelude — get_position(trade_id) is called twice
    # (once for trace, once for the active position).
    bot.positions.get_position.return_value = pos
    bot.positions.position = pos
    # Early-return guard at _trade_exit.py:62 — MagicMock would
    # default this to a truthy Mock; force False so the test
    # progresses to the WS send.
    bot.positions.is_flat = False
    bot._last_exit_send_ts = {}      # mutable real dict the prelude writes to
    bot.status = ""
    return bot


def test_trade_exit_ws_send_includes_direction():
    """Site 1: bots/_trade_exit.py:135 — TradeExit.exit_trade.

    Exit reasons other than `stop_loss` / `target_hit` reach the WS
    send (the OCO-handled-reasons branch was added 2026-05-17 for the
    Phase 9.5 race fix and intentionally skips WS for those two).
    Use `manual_exit` to land on the send path.
    """
    from bots._trade_exit import TradeExit

    captures: list[str] = []
    ws = _capturing_ws(captures)
    pos = SimpleNamespace(
        trade_id="tid_te_1",
        direction="SHORT",
        contracts=2,
        account="Sim101",
        rider_mode=False,
    )
    bot = _build_trade_exit_bot(pos)
    te = TradeExit(bot)

    with pytest.raises(_CaptureSendAbort):
        asyncio.run(te.exit_trade(ws, 30315.0, "manual_exit",
                                  trade_id="tid_te_1"))

    payload = _parse_captured(captures)
    assert payload.get("action") == "EXIT"
    assert payload.get("trade_id") == "tid_te_1"
    assert payload.get("qty") == pos.contracts
    # The contract this test pins:
    assert "direction" in payload, (
        "TradeExit.exit_trade WS payload missing `direction` field. "
        "Bridge T-BRIDGE swap falls back to legacy CLOSEPOSITION "
        "without it (see FINDING-2026-06-05-T-BRIDGE-ENGAGEMENT-GAP)."
    )
    assert payload["direction"] == pos.direction
    # Type check: bridge T-BRIDGE-1 expects an uppercase 'LONG' /
    # 'SHORT' string. Anything else would fall through the bridge's
    # invalid-direction branch and emit nothing (preserving the gap).
    assert payload["direction"] in ("LONG", "SHORT")


# ─────────────────────────────────────────────────────────────────
# Site 2 — bots/sim_bot.py:516 (SimBot._get_ws_send_fn)
# ─────────────────────────────────────────────────────────────────

def test_sim_bot_daily_flatten_ws_send_includes_direction():
    """Site 2: bots/sim_bot.py:518 — the `_send` closure returned by
    `SimBot._get_ws_send_fn` (per-strategy account routing
    daily-flatten sender).

    We instantiate the factory on a bare object via the factory
    method's `__get__` descriptor protocol so we don't need to
    construct a real SimBot (which would require WS / config / etc.).
    """
    from bots.sim_bot import SimBot

    captures: list[str] = []
    ws = _capturing_ws(captures)
    pos = SimpleNamespace(
        trade_id="tid_sim_1",
        direction="LONG",
        contracts=3,
        account="SimBias Momentum",
        sub_strategy=None,
    )

    bot = MagicMock()
    bot._ws = ws
    bot.positions.get_position.return_value = pos
    # Call the factory bound to `bot` — exactly mirrors the live path
    # where `self._get_ws_send_fn()` returns the closure
    # that DailyFlattener invokes.
    send_factory = SimBot._get_ws_send_fn.__get__(bot, SimBot)
    _send = send_factory()
    assert _send is not None, "factory must return a sender (bot._ws is set)"

    with pytest.raises(_CaptureSendAbort):
        asyncio.run(_send("tid_sim_1", reason="daily_flatten_1554CT"))

    payload = _parse_captured(captures)
    assert payload.get("action") == "EXIT"
    assert payload.get("trade_id") == "tid_sim_1"
    assert payload.get("qty") == pos.contracts
    assert "direction" in payload, (
        "SimBot daily-flatten WS payload missing `direction`. Bridge "
        "T-BRIDGE swap inert (FINDING-2026-06-05-T-BRIDGE-ENGAGEMENT-GAP)."
    )
    assert payload["direction"] == pos.direction
    assert payload["direction"] in ("LONG", "SHORT")


# ─────────────────────────────────────────────────────────────────
# Site 3 — bots/base_bot.py:2193 (BaseBot._get_ws_send_fn)
# ─────────────────────────────────────────────────────────────────

def test_base_bot_daily_flatten_ws_send_includes_direction():
    """Site 3: bots/base_bot.py:2195 — the default `_send` closure
    returned by `BaseBot._get_ws_send_fn`. SimBot overrides
    this for per-strategy routing; the base implementation is the
    fallback used by prod_bot and any subclass that doesn't override.
    """
    from bots.base_bot import BaseBot

    captures: list[str] = []
    ws = _capturing_ws(captures)
    pos = SimpleNamespace(
        trade_id="tid_base_1",
        direction="SHORT",
        contracts=1,
        account="Sim101",
        sub_strategy="open_drive",  # base reads via getattr(., None)
    )

    bot = MagicMock()
    bot._ws = ws
    bot.positions.get_position.return_value = pos
    send_factory = BaseBot._get_ws_send_fn.__get__(bot, BaseBot)
    _send = send_factory()
    assert _send is not None, "factory must return a sender (bot._ws is set)"

    with pytest.raises(_CaptureSendAbort):
        asyncio.run(_send("tid_base_1", reason="daily_flatten_1554CT"))

    payload = _parse_captured(captures)
    assert payload.get("action") == "EXIT"
    assert payload.get("trade_id") == "tid_base_1"
    assert payload.get("qty") == pos.contracts
    assert "direction" in payload, (
        "BaseBot daily-flatten WS payload missing `direction`. Bridge "
        "T-BRIDGE swap inert (FINDING-2026-06-05-T-BRIDGE-ENGAGEMENT-GAP)."
    )
    assert payload["direction"] == pos.direction
    assert payload["direction"] in ("LONG", "SHORT")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
