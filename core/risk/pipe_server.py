"""
Windows named-pipe server for the RiskGate.

Wire protocol (line-delimited JSON):

  request  (in):  {"v":1,"id":"<uuid>","op":"PLACE", ... }
  response (out): {"v":1,"id":"<uuid>","decision":"ACCEPT","oif_path":"..."}
                  {"v":1,"id":"<uuid>","decision":"REFUSE","reason":"..."}

Single-instance pipe (`PIPE_ACCESS_DUPLEX`, `FILE_FLAG_OVERLAPPED` not
used). Each connection is handled in a thread; gate evaluate() is
internally locked. PowerShell test client:

    $p = New-Object System.IO.Pipes.NamedPipeClientStream(".",
        "phoenix_risk_gate",
        [System.IO.Pipes.PipeDirection]::InOut)
    $p.Connect()
    $w = New-Object System.IO.StreamWriter($p)
    $r = New-Object System.IO.StreamReader($p)
    $w.WriteLine('{"v":1,"id":"x","op":"PLACE",...}')
    $w.Flush()
    $r.ReadLine()
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

from .risk_gate import RiskGate

logger = logging.getLogger("RiskPipe")


class PipeServer:
    """Windows named-pipe RPC server. Built so the bulk of the logic is
    test-friendly: `_handle_message(payload_str)` is pure (no pywin32),
    while `serve_forever()` is the pywin32-backed event loop.

    The ``pipe_factory`` ctor arg lets tests inject a fake pipe class —
    if provided, ``serve_forever`` will use it in place of the real
    pywin32 ``CreateNamedPipe``/``ReadFile``/``WriteFile`` machinery.
    See ``tests/test_risk_gate/test_pipe_protocol.py`` for the pattern.
    """

    def __init__(
        self,
        gate: RiskGate,
        pipe_path: Optional[str] = None,
        pipe_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.gate = gate
        self.pipe_path = pipe_path or gate.config.pipe_path
        self.pipe_factory = pipe_factory
        self._stop = threading.Event()
        self._started = False

    # ── Public lifecycle ──────────────────────────────────────────
    def start(self) -> None:
        """Spawn the serve loop in a background thread. Returns once
        the listener is accepting connections (best-effort)."""
        if self._started:
            return
        self._started = True
        t = threading.Thread(target=self.serve_forever, daemon=True,
                             name="risk_pipe_server")
        t.start()

    # ── Pure handler (no pywin32) ─────────────────────────────────
    def _handle_message(self, raw_line: str) -> str:
        """Parse one JSON line, evaluate, return JSON response line."""
        raw_line = (raw_line or "").strip()
        if not raw_line:
            return json.dumps({"v": 1, "decision": "REFUSE",
                               "reason": "empty_message"}) + "\n"
        try:
            req = json.loads(raw_line)
        except json.JSONDecodeError as e:
            return json.dumps({"v": 1, "decision": "REFUSE",
                               "reason": f"bad_json: {e}"}) + "\n"
        if not isinstance(req, dict):
            return json.dumps({"v": 1, "decision": "REFUSE",
                               "reason": "request_not_object"}) + "\n"
        # Special op: snapshot (read-only, doesn't go through evaluate())
        if req.get("op") == "SNAPSHOT":
            return json.dumps({"v": 1, "id": req.get("id", "?"),
                               "decision": "INFO",
                               "snapshot": self.gate.snapshot()}) + "\n"
        # Default: PLACE through the check chain
        resp = self.gate.evaluate(req)
        return json.dumps(resp) + "\n"

    # ── pywin32 event loop ────────────────────────────────────────
    def serve_forever(self) -> None:
        """Block forever; spawn a handler thread per connection."""
        import win32pipe  # type: ignore
        import win32file  # type: ignore
        import pywintypes  # type: ignore

        logger.info(f"PipeServer listening on {self.pipe_path}")
        while not self._stop.is_set():
            try:
                handle = win32pipe.CreateNamedPipe(
                    self.pipe_path,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_WAIT,
                    1,                      # max instances (single)
                    65536, 65536,          # out/in buffer sizes
                    300,                    # default timeout (ms)
                    None,                   # security attrs (default)
                )
                # Wait for client; ConnectNamedPipe blocks
                win32pipe.ConnectNamedPipe(handle, None)
                # One thread per connection
                threading.Thread(
                    target=self._serve_one, args=(handle,), daemon=True
                ).start()
            except pywintypes.error as e:
                logger.warning(f"pipe loop error: {e!r}")
                if self._stop.is_set():
                    break

    def _serve_one(self, handle) -> None:
        import win32file  # type: ignore
        import pywintypes  # type: ignore
        try:
            while not self._stop.is_set():
                try:
                    rc, data = win32file.ReadFile(handle, 65536)
                except pywintypes.error:
                    break
                if not data:
                    break
                payload = data.decode("utf-8", errors="ignore")
                # Could be multiple lines in one read
                resp_lines = []
                for line in payload.splitlines():
                    if line.strip():
                        resp_lines.append(self._handle_message(line))
                resp = "".join(resp_lines)
                if resp:
                    win32file.WriteFile(handle, resp.encode("utf-8"))
        finally:
            try:
                import win32pipe  # type: ignore
                win32pipe.DisconnectNamedPipe(handle)
                win32file.CloseHandle(handle)  # type: ignore
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
