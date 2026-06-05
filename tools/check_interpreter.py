"""
Phoenix Bot — Interpreter Safety Check (FINDING-2026-06-04-MULTI-PID)

Detects whether the calling python process was launched via the
WindowsApps `PythonSoftwareFoundation.PythonManager` shim. The shim
is a re-execution wrapper that spawns the real pythoncore-3.14
interpreter and BLOCKS waiting on the child's exit — producing the
MULTI-PID zombie pattern documented in:

    out/bot_health_forensics_report_2026-06-04.md §3
    docs/operator/safe_launch.md

Exit codes:
    0  — safe interpreter (pythoncore-3.14 direct, no shim parent)
    2  — shim chain detected; caller should NOT proceed

Usage from a launcher BAT (Windows):

    set PY="%LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe"
    if not exist %PY% set PY=python
    %PY% tools\\check_interpreter.py
    if errorlevel 1 (
        echo Phoenix interpreter check FAILED. See stderr.
        exit /b 1
    )
    %PY% <your_script.py>

Programmatic use (from any phoenix module):

    from tools.check_interpreter import is_shim_chain
    if is_shim_chain():
        logger.error("shim launch — refusing to continue")
        sys.exit(2)
"""
from __future__ import annotations

import os
import re
import sys

# Broad match: any \WindowsApps\ in the path flags it. The actual
# shim lives under
# C:\Users\<u>\AppData\Local\Microsoft\WindowsApps\python.exe and
# also under C:\Program Files\WindowsApps\...PythonManager...\python.exe
# Both contain the segment.
_SHIM_RE = re.compile(r"\\WindowsApps\\", re.IGNORECASE)


def parent_executable() -> str | None:
    """Best-effort fetch of the parent process's exe path.

    Uses psutil if available; returns None otherwise. Exposed as a
    module-level function so tests can monkeypatch it.
    """
    try:
        import psutil  # type: ignore

        parent = psutil.Process(os.getpid()).parent()
        if parent is None:
            return None
        return parent.exe()
    except Exception:
        return None


def is_shim_chain() -> bool:
    """True if this process or its parent is the WindowsApps shim.

    Checks both:
      1. `sys.executable` of THIS process (catches the unusual case
         where the shim runs the script in-process without re-exec).
      2. The parent process's exe path (catches the SHIPPED pattern:
         shim spawned pythoncore as child, child sees itself as
         pythoncore but parent is shim).
    """
    # Test-only hook so the behavioral test can exercise the
    # rejection path without an actual shim invocation. The hook is
    # documented in tests/test_interpreter_guard.py.
    if os.environ.get("PHOENIX_FAKE_SHIM_FOR_TESTS") == "1":
        return True

    if _SHIM_RE.search(sys.executable or ""):
        return True

    parent = parent_executable()
    if parent and _SHIM_RE.search(parent):
        return True

    return False


def main(argv: list[str]) -> int:
    if is_shim_chain():
        parent = parent_executable()
        sys.stderr.write(
            "ERROR: Phoenix process invoked via the WindowsApps "
            "Python shim.\n"
            "  This produces the MULTI-PID zombie pattern documented in\n"
            "  out/bot_health_forensics_report_2026-06-04.md §3 and\n"
            "  docs/operator/safe_launch.md.\n"
            "\n"
            "  Use the explicit pythoncore-3.14 path instead:\n"
            "    set PY=\"%LOCALAPPDATA%\\Python\\pythoncore-3.14-64"
            "\\python.exe\"\n"
            "    %PY% <script.py>\n"
            "\n"
            f"  Detected interpreter : {sys.executable!r}\n"
            f"  Detected parent exe  : {parent!r}\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
