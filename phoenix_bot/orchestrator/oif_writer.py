"""
OIFSink Protocol + DirectFileSink + RiskGateSink + get_default_sink().

Thin abstraction over "where does an OIF go?". With `PHOENIX_RISK_GATE`
unset or `0` (the default), every caller of `get_default_sink()` gets
DirectFileSink which writes straight to NT8's `incoming/` folder via
the existing `bridge.oif_writer` functions. That path is byte-for-byte
identical to the pre-migration call sites — the Sink shim is invisible
when the flag is off. With `PHOENIX_RISK_GATE=1`, callers get
RiskGateSink which forwards the request to the gate over the named
pipe and waits for ACCEPT/REFUSE.

Sink Protocol contract:
    submit(req: dict) -> dict
where the response always carries:
    {"v": 1, "id": <id>, "decision": "ACCEPT"|"REFUSE",
     "oif_path": <first-written path>            # ACCEPT only
     "reason":   <short string>,                  # REFUSE only
     "sink":     "direct"|"risk_gate"|"fallback"} # debug
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Optional, Protocol


logger = logging.getLogger("OIFSink")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class OIFSink(Protocol):
    """A place where an OIF request can be sent. Implementations decide
    whether the request is risk-gated, persisted to disk directly,
    routed through a network broker, etc."""

    def submit(self, request: dict) -> dict:
        """Send `request` (a PLACE-shaped dict). Return a response dict
        with at least `decision: ACCEPT|REFUSE` and either `oif_path` or
        `reason`."""
        ...


# ---------------------------------------------------------------------------
# DirectFileSink — wraps the legacy bridge.oif_writer path
# ---------------------------------------------------------------------------

# Map gate-shaped action -> direction for write_bracket_order.
_ACTION_TO_DIRECTION = {
    "BUY": "LONG",
    "ENTER_LONG": "LONG",
    "SELL": "SHORT",
    "ENTER_SHORT": "SHORT",
}


class DirectFileSink:
    """Legacy path: emit OIFs directly via bridge.oif_writer. This sink
    is the default and matches base_bot.py's pre-migration behavior
    when PHOENIX_RISK_GATE is unset or 0.

    submit() interprets the gate-shaped request dict and dispatches to
    the appropriate bridge.oif_writer entrypoint:
      - PLACE with stop+target -> write_bracket_order
      - PLACE without target   -> write_bracket_order(target=None)
      - PROTECT                -> write_protection_oco
      - EXIT                   -> write_oif("EXIT", ...)
      - PARTIAL_EXIT           -> write_partial_exit
      - MODIFY_STOP            -> write_modify_stop
    """

    def __init__(self, outgoing_dir: Optional[str] = None):
        # outgoing_dir is informational only — the legacy writer pulls
        # OIF_INCOMING from config.settings (and tests redirect via
        # conftest's _isolate_oif_incoming fixture).
        self.outgoing_dir = outgoing_dir or r"C:\Users\Trading PC\Documents\NinjaTrader 8\incoming"

    def submit(self, request: dict) -> dict:
        rid = request.get("id") or str(uuid.uuid4())
        op = str(request.get("op", "PLACE")).upper()
        try:
            if op == "PLACE":
                paths = self._submit_place(request)
            elif op == "PROTECT":
                paths = self._submit_protect(request)
            elif op == "EXIT":
                paths = self._submit_exit(request)
            elif op == "PARTIAL_EXIT":
                paths = self._submit_partial_exit(request)
            elif op == "MODIFY_STOP":
                paths = self._submit_modify_stop(request)
            else:
                return {"v": 1, "id": rid, "decision": "REFUSE",
                        "reason": f"unknown_op: {op!r}", "sink": "direct"}
        except Exception as e:
            return {"v": 1, "id": rid, "decision": "REFUSE",
                    "reason": f"writer_exception: {e!r}", "sink": "direct"}

        if not paths:
            return {"v": 1, "id": rid, "decision": "REFUSE",
                    "reason": "writer_returned_empty", "sink": "direct"}
        return {"v": 1, "id": rid, "decision": "ACCEPT",
                "oif_path": paths[0], "oif_paths": list(paths),
                "sink": "direct"}

    # -- op dispatch helpers -------------------------------------------------

    def _submit_place(self, req: dict) -> list:
        from bridge.oif_writer import write_bracket_order
        action = str(req.get("action", "")).upper()
        direction = _ACTION_TO_DIRECTION.get(action)
        if direction is None:
            raise ValueError(f"PLACE action {action!r} not mappable to LONG/SHORT")
        qty = int(req["qty"])
        order_type = str(req.get("order_type", "MARKET")).upper()
        # price_ref is the entry price reference (LIMIT or stop trigger).
        # MARKET orders pass 0.0 — write_bracket_order handles that.
        entry_price = float(req.get("price_ref") or req.get("entry_price") or 0.0)
        stop_price = float(req["stop_price"]) if req.get("stop_price") is not None else None
        target_price = float(req["target_price"]) if req.get("target_price") is not None else None
        trade_id = req.get("trade_id") or req.get("id", "")
        account = req.get("account")
        return write_bracket_order(
            direction=direction,
            qty=qty,
            entry_type=order_type,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            trade_id=trade_id,
            account=account,
        ) or []

    def _submit_protect(self, req: dict) -> list:
        from bridge.oif_writer import write_protection_oco
        direction = str(req.get("direction", "")).upper()
        return write_protection_oco(
            direction=direction,
            qty=int(req["qty"]),
            stop_price=float(req["stop_price"]),
            target_price=float(req["target_price"]),
            trade_id=str(req.get("trade_id") or req.get("id", "")),
            account=str(req["account"]),
        ) or []

    def _submit_exit(self, req: dict) -> list:
        from bridge.oif_writer import write_oif
        return write_oif(
            "EXIT",
            qty=int(req.get("qty", 1)),
            trade_id=str(req.get("trade_id") or req.get("id", "")),
            account=req.get("account"),
        ) or []

    def _submit_partial_exit(self, req: dict) -> list:
        from bridge.oif_writer import write_partial_exit
        return write_partial_exit(
            direction=str(req["direction"]).upper(),
            n_contracts=int(req.get("qty", 1)),
            trade_id=str(req.get("trade_id") or req.get("id", "")),
            account=req.get("account"),
        ) or []

    def _submit_modify_stop(self, req: dict) -> list:
        from bridge.oif_writer import write_modify_stop
        return write_modify_stop(
            direction=str(req["direction"]).upper(),
            new_stop_price=float(req["new_stop_price"]),
            n_contracts=int(req.get("qty", 1)),
            trade_id=str(req.get("trade_id") or req.get("id", "")),
            account=req.get("account"),
            old_stop_order_id=str(req.get("old_stop_order_id", "")),
        ) or []


# ---------------------------------------------------------------------------
# RiskGateSink — fail-soft over Windows named pipe
# ---------------------------------------------------------------------------

class RiskGateSink:
    """Forward to RiskGate over a Windows named pipe. Used when
    `PHOENIX_RISK_GATE=1`. The pipe-client open is per-call so a
    crashed gate doesn't leak handles.

    Fail-soft: if the named pipe is unreachable (gate process not
    running), log a one-shot WARN and fall back to DirectFileSink
    so the bot keeps trading. Operators flipping the flag without
    starting the gate process should see a single visible warning,
    not a crash."""

    _fallback_warned = False  # class-level so warning fires once per process

    def __init__(self, pipe_path: str = r"\\.\pipe\phoenix_risk_gate",
                 timeout_s: float = 2.0):
        self.pipe_path = pipe_path
        self.timeout_s = timeout_s
        self._fallback = DirectFileSink()

    def submit(self, request: dict) -> dict:
        if "id" not in request:
            request["id"] = str(uuid.uuid4())
        line = json.dumps(request) + "\n"
        try:
            return self._call_pipe(line)
        except Exception as e:
            # Fail-soft: log once, fall back to DirectFileSink so
            # the bot doesn't lose its execution path. The B59 + price
            # sanity guards downstream still apply.
            if not RiskGateSink._fallback_warned:
                logger.warning(
                    "[RISK_GATE] pipe unreachable (%r) — falling back to "
                    "DirectFileSink. Start tools/risk_gate_runner.py to "
                    "engage the gate.", e,
                )
                RiskGateSink._fallback_warned = True
            resp = self._fallback.submit(request)
            # Tag the response so callers can tell the gate didn't run.
            resp["sink"] = "fallback"
            return resp

    def _call_pipe(self, line: str) -> dict:
        # Imported at call-time so unit tests can monkeypatch _call_pipe.
        import pywintypes  # type: ignore
        import win32file   # type: ignore
        try:
            handle = win32file.CreateFile(
                self.pipe_path,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
        except pywintypes.error as e:
            raise IOError(f"cannot open pipe {self.pipe_path}: {e}")
        try:
            win32file.WriteFile(handle, line.encode("utf-8"))
            rc, data = win32file.ReadFile(handle, 65536)
            text = data.decode("utf-8", errors="ignore").splitlines()[0]
            return json.loads(text)
        finally:
            try:
                win32file.CloseHandle(handle)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Default-sink selector
# ---------------------------------------------------------------------------

def get_default_sink() -> OIFSink:
    """Returns the configured default sink based on PHOENIX_RISK_GATE.

    PHOENIX_RISK_GATE unset or "0" -> DirectFileSink (legacy path,
    behavior identical to pre-migration code).

    PHOENIX_RISK_GATE = "1" -> RiskGateSink (gate engaged; fails soft
    to DirectFileSink if the gate process isn't running).
    """
    if os.environ.get("PHOENIX_RISK_GATE", "0") == "1":
        return RiskGateSink()
    return DirectFileSink()
