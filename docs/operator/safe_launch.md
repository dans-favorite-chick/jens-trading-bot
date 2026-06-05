# Phoenix Safe-Launch Pattern

_The Phoenix stack must NEVER be invoked via the bare `python` command on Windows. This document explains why and how to avoid it._

---

## Why bare `python` is unsafe on this machine

Windows ships a `python.exe` "Application Execution Alias" at

    C:\Users\Trading PC\AppData\Local\Microsoft\WindowsApps\python.exe

owned by the Microsoft Store's `PythonSoftwareFoundation.PythonManager` app. When the shim is invoked it:

1. Reads the user's "Default Python" setting.
2. Spawns the actual Python interpreter as a child process.
3. **Blocks waiting on the child's exit code** so it can return that exit code to the caller.

The result: every bot launched through the shim runs as TWO processes — the real pythoncore-3.14 (PID A) plus the shim wrapper (PID B, parent of A). Process inventories list both. Watchdog observability becomes confusing. Resource accounting overcounts. And in the worst case (documented in `out/bot_health_forensics_report_2026-06-04.md` §3), a stale shim parent from a long-gone launcher persists indefinitely as a zombie wrapper, holding inheriting file handles from whatever started it.

The real bug isn't the shim itself — it's the foot-gun nature of `python` as a PATH-resolved command. If the operator's PATH happens to put the WindowsApps entry first, every bare `python <script>` invocation produces the zombie pattern. The fix is to NEVER use bare `python` in Phoenix launch paths.

---

## The safe pattern

Always set `PY` to the explicit pythoncore-3.14 path, with a fallback to `python` only for portability:

```bat
set PY="%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist %PY% set PY=python
```

Then invoke as `%PY% <script>`. The same pattern lives in:

- `launch_bridge.bat`
- `launch_dashboard.bat`
- `launch_prod.bat`
- `launch_all.bat`
- `launch_watcher.bat`
- `out/restart_round2_2026-06-04.ps1`

If you write a new launcher, use this pattern verbatim.

---

## The runtime guard

`tools/check_interpreter.py` is a 1-second runtime guard that refuses to let the stack come up under the shim. Every `launch_*.bat` file (as of commit landing this doc) invokes it BEFORE launching the actual script:

```bat
%PY% tools\check_interpreter.py
if errorlevel 1 (
    echo ERROR: Phoenix interpreter check FAILED.
    pause
    exit /b 1
)
%PY% <your_script.py>
```

The guard checks two conditions:

1. `sys.executable` of the current Python process — catches the rare case where the shim executes the script in-process.
2. Parent process exe via `psutil.Process(os.getpid()).parent().exe()` — catches the common case where the shim re-execs into pythoncore and the script sees a safe `sys.executable` but the parent is the shim wrapper.

If either matches `\WindowsApps\`, the guard prints a loud stderr message + exits with code 2. The launcher BAT propagates the failure and the operator sees the message in the cmd window.

Programmatic callers can import and check directly:

```python
from tools.check_interpreter import is_shim_chain

if is_shim_chain():
    logger.error("WindowsApps shim launch — refusing")
    sys.exit(2)
```

---

## What to do if you see "interpreter check FAILED"

The launcher BAT prints the offending interpreter paths to stderr. Most likely cause: `%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe` doesn't exist on this machine — the `if not exist %PY% set PY=python` fallback then resolves to the shim.

Fix:

1. Confirm the real Python install path: open a NEW cmd window and run `where python`. The output should include `C:\Users\Trading PC\AppData\Local\Python\pythoncore-3.14-64\python.exe` somewhere.
2. If it doesn't, re-install Python 3.14 from python.org (NOT from the Microsoft Store) and ensure the install path matches what the BAT files expect.
3. As a last resort, edit the launcher BAT to point `PY` at the actual install path.

DO NOT bypass the guard. The MULTI-PID zombie pattern is hard to debug after the fact (PIDs persist, parent processes vanish, root cause is invisible without process-tree archaeology). The 2026-06-04 bridge zombie that survived round-2 remediation took an hour of forensics work to attribute. Spending 5 minutes fixing the Python install is cheaper than another hour of zombie hunting.

---

## Background reading

- `out/bot_health_forensics_report_2026-06-04.md` §3 — full root-cause analysis of the zombie pattern.
- `out/next_sprint_outline_multi_pid.md` — the sprint plan that led to this guard.
- `tests/test_interpreter_guard.py` — the regression sentinel keeping the guard honest.
- `docs/findings_tracker.md` row `FINDING-2026-06-04-MULTI-PID` — the audit ledger entry.
