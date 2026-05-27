"""OIF emitter — extracted from base_bot.py 2026-05-24 (P4-1 Stage 3).

WARNING: CRITICAL EXECUTION PATH. These functions write Order Instruction
Files that NT8's ATI executes. Any change here must be byte-equivalent to
the original.

Wraps the OIF sink (DirectFileSink by default, RiskGateSink when
PHOENIX_RISK_GATE=1) with the small amount of bot-side logic that lives
between the strategy decision and the raw OIF write (direction parsing,
qty defaulting, trade_id handling, op dispatch).

Original location: bots/base_bot.py:523-645 (the 5 _sink_submit_*
functions) plus the helper bots/base_bot.py:513-520 (_get_oif_sink).

Function bodies are copied VERBATIM from base_bot.py. The only
difference is the leading underscore is dropped on the public surface
(this module IS the sink, so the `_sink_` prefix is redundant). The
old names are exported as aliases at the bottom for backward
compatibility.

NOTE on module-level state: there is ONLY ONE cached sink, and it
lives on `bots.base_bot` (`bb._OIF_SINK` / `bb._get_oif_sink()`).
Each submit_* function below lazy-imports `bb._get_oif_sink` at call
time so that:

  - `bb._OIF_SINK = None` followed by `bb._get_oif_sink()` still works
    correctly (single cache to reset).
  - Tests like `tests/test_risk_gate_migration.py:251` that mutate
    `bb._OIF_SINK` directly continue to control sink construction for
    every OIF write, regardless of whether the call originates from
    `bb._sink_submit_*` or from `_oif_emitter.submit_*`.
  - The PHOENIX_RISK_GATE env flag flip is honored from the one
    canonical accessor.

base_bot.py's `_sink_submit_*` names re-export this module's
`submit_*` functions (single-line `_sink_submit_X = _oif.submit_X`),
so the active path goes through this file while preserving the
existing public surface.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("OIFEmitter")


# ── Module-level emitters (verbatim from base_bot.py:523-645) ─────────
#
# Each function lazy-imports `_get_oif_sink` from `bots.base_bot` at
# call time. That makes `bots.base_bot._OIF_SINK` the single source of
# truth: any test or operator that resets `bb._OIF_SINK = None` will
# see the reset honored on the very next emitter call.

def submit_place(direction: str, qty: int, entry_type: str,
                       entry_price: float, stop_price: float,
                       target_price, trade_id: str, account: str,
                       strategy: str = "", sub_strategy=None) -> dict:
    """Sink-mediated bracket PLACE. Returns the sink response dict.
    On REFUSE, the caller should treat as a no-op and skip the entry."""
    from config.settings import INSTRUMENT
    from bots.base_bot import _get_oif_sink
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "PLACE",
        "strategy": strategy or "",
        "account": account or "",
        "instrument": INSTRUMENT,
        "action": "BUY" if str(direction).upper() == "LONG" else "SELL",
        "qty": int(qty),
        "order_type": str(entry_type).upper(),
        "tif": "GTC",
        "price_ref": float(entry_price or 0.0),
        "entry_price": float(entry_price or 0.0),
        "stop_price": float(stop_price) if stop_price is not None else None,
        "target_price": float(target_price) if target_price is not None else None,
        "trade_id": trade_id or "",
        "sub_strategy": sub_strategy,
    }
    return sink.submit(req)


def submit_protect(direction: str, qty: int, stop_price: float,
                         target_price: float, trade_id: str,
                         account: str) -> dict:
    """Sink-mediated post-fill OCO protection (PROTECT op)."""
    from config.settings import INSTRUMENT
    from bots.base_bot import _get_oif_sink
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "PROTECT",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        # PROTECT carries direction of the FILLED position; the legacy
        # writer derives the opposite side for stop+target.
        "direction": str(direction).upper(),
        "action": "SELL" if str(direction).upper() == "LONG" else "BUY",
        "qty": int(qty),
        "order_type": "OCO",
        "tif": "GTC",
        "stop_price": float(stop_price),
        "target_price": float(target_price),
        "trade_id": trade_id,
    }
    return sink.submit(req)


def submit_exit(qty: int, trade_id: str, account: str,
                      reason: str = "") -> dict:
    """Sink-mediated EXIT (CLOSEPOSITION)."""
    from config.settings import INSTRUMENT
    from bots.base_bot import _get_oif_sink
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "EXIT",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        "action": "SELL",  # not actually used for EXIT; legacy writer issues CLOSEPOSITION
        "qty": int(qty),
        "order_type": "MARKET",
        "tif": "GTC",
        "trade_id": trade_id,
        "reason": reason,
    }
    return sink.submit(req)


def submit_partial_exit(direction: str, n_contracts: int,
                              trade_id: str, account: str) -> dict:
    """Sink-mediated PARTIAL_EXIT (scale-out)."""
    from config.settings import INSTRUMENT
    from bots.base_bot import _get_oif_sink
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "PARTIAL_EXIT",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        "direction": str(direction).upper(),
        "action": "SELL" if str(direction).upper() == "LONG" else "BUY",
        "qty": int(n_contracts),
        "order_type": "MARKET",
        "tif": "GTC",
        "trade_id": trade_id,
    }
    return sink.submit(req)


def submit_modify_stop(direction: str, new_stop_price: float,
                             n_contracts: int, trade_id: str,
                             account: str, old_stop_order_id: str) -> dict:
    """Sink-mediated stop cancel+replace (MODIFY_STOP)."""
    from config.settings import INSTRUMENT
    from bots.base_bot import _get_oif_sink
    sink = _get_oif_sink()
    req = {
        "v": 1,
        "id": trade_id or "",
        "op": "MODIFY_STOP",
        "strategy": "",
        "account": account,
        "instrument": INSTRUMENT,
        "direction": str(direction).upper(),
        "action": "SELL" if str(direction).upper() == "LONG" else "BUY",
        "qty": int(n_contracts),
        "order_type": "STOPMARKET",
        "tif": "GTC",
        "new_stop_price": float(new_stop_price),
        "trade_id": trade_id,
        "old_stop_order_id": old_stop_order_id,
    }
    return sink.submit(req)


# ── Backward-compat aliases — drop in a future stage. ─────────────────
# Existing call sites in base_bot.py use the leading-underscore names;
# downstream importers may also reach for these by their old names.

_sink_submit_place = submit_place
_sink_submit_protect = submit_protect
_sink_submit_exit = submit_exit
_sink_submit_partial_exit = submit_partial_exit
_sink_submit_modify_stop = submit_modify_stop
