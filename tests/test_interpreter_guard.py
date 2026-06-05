"""Regression test for FINDING-2026-06-04-MULTI-PID interpreter guard.

The WindowsApps Python shim (`PythonSoftwareFoundation.PythonManager`)
is a re-execution wrapper: when invoked, it spawns the real pythoncore
interpreter and BLOCKS waiting on the child's exit. This produces the
MULTI-PID zombie pattern documented in `out/bot_health_forensics_report
_2026-06-04.md` §3 (e.g. bridge_server PID 172436 child of WindowsApps
shim PID 176512 on 2026-06-04 14:47 CDT).

All `launch_*.bat` files + `restart_round2_*.ps1` already use the
explicit pythoncore-3.14 path. This guard is a RUNTIME defense against
regressions: any script that imports + calls `is_shim_chain()` will
detect the shim chain (via parent-process inspection) and can refuse
to continue.

Detection mechanism:
  - sys.executable of the CURRENT python process. After shim re-exec
    this is the real pythoncore — so checking sys.executable alone
    isn't enough. (But we check it anyway for the unusual case where
    the shim itself runs the script in-process — rare but cheap.)
  - psutil.Process(os.getpid()).parent().exe(). If parent's exe
    matches `\WindowsApps\`, we're in the shim chain.

Tests:
  T1 — pythoncore interpreter, pythoncore parent → safe (False)
  T2 — explicit WindowsApps interpreter → flagged (True)
  T3 — pythoncore interpreter, WindowsApps parent → flagged (True)
  T4 — behavioral: real subprocess invocation of tools/check_interpreter.py
       in the test runner's environment → exit code 0 (we're on
       pythoncore per Phase 0 baseline)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = REPO_ROOT / "tools" / "check_interpreter.py"

SAFE_INTERPRETER = (
    r"C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe"
)
SHIM_INTERPRETER = (
    r"C:\Users\Trading PC\AppData\Local\Microsoft\WindowsApps\python.exe"
)


def test_t1_pythoncore_safe(monkeypatch):
    """Direct invocation of pythoncore-3.14 with non-shim parent → safe."""
    import tools.check_interpreter as guard

    monkeypatch.setattr(sys, "executable", SAFE_INTERPRETER)
    monkeypatch.setattr(guard, "parent_executable",
                        lambda: r"C:\Windows\System32\cmd.exe")

    assert guard.is_shim_chain() is False


def test_t2_explicit_shim_interpreter(monkeypatch):
    """sys.executable itself is the WindowsApps shim → flagged."""
    import tools.check_interpreter as guard

    monkeypatch.setattr(sys, "executable", SHIM_INTERPRETER)
    monkeypatch.setattr(guard, "parent_executable", lambda: None)

    assert guard.is_shim_chain() is True


def test_t3_pythoncore_with_shim_parent(monkeypatch):
    """After shim re-exec: child sys.executable is pythoncore but parent
    is the shim. This is the SHIPPED zombie pattern. → flagged."""
    import tools.check_interpreter as guard

    monkeypatch.setattr(sys, "executable", SAFE_INTERPRETER)
    monkeypatch.setattr(guard, "parent_executable",
                        lambda: SHIM_INTERPRETER)

    assert guard.is_shim_chain() is True


def test_t4_behavioral_subprocess_exit_code():
    """Real subprocess invocation of the guard script. Exit code 0
    confirms the test runner's interpreter (sys.executable) is safe.
    This is the canonical 'does this actually work' test."""
    assert GUARD_PATH.exists(), f"guard script missing: {GUARD_PATH}"
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"interpreter guard rejected the test runner's interpreter "
        f"({sys.executable!r}). stderr={result.stderr}"
    )


def test_t5_behavioral_shim_path_simulated(tmp_path):
    """Use a deliberately-spoofed env var so the guard rejects with
    exit code 2. Confirms the LOUD-WARN path is wired and the script
    returns the documented non-zero exit code."""
    assert GUARD_PATH.exists()
    env = os.environ.copy()
    env["PHOENIX_FAKE_SHIM_FOR_TESTS"] = "1"
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert result.returncode == 2, (
        f"expected exit code 2 (shim detected via test hook); "
        f"got {result.returncode}. stderr={result.stderr}"
    )
    assert "WindowsApps" in result.stderr, (
        f"expected explanatory stderr; got: {result.stderr!r}"
    )
