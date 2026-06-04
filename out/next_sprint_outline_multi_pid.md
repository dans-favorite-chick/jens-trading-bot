# Next-Sprint Outline — Multi-PID / WindowsApps Shim Zombies

_Source: `out/bot_health_forensics_report_2026-06-04.md` §3 + §7. Read this for full evidence._

## Symptom + root cause

Bridge_server currently runs as PID 172436 (pythoncore-3.14, owning ports 8765/8766/8767) parented to PID 176512 (WindowsApps shim). The shim's parent (PID 46372) is gone from the process table. Some launcher — almost certainly a dev/IDE tool, not one of the `launch_*.bat` scripts — invoked plain `python bridge/bridge_server.py` from a PATH context where `python` resolved to `C:\Users\Trading PC\AppData\Local\Microsoft\WindowsApps\python.exe`. The shim is the Microsoft Store `PythonSoftwareFoundation.PythonManager` stub: it re-execs into the real pythoncore-3.14 interpreter and blocks waiting on the child's exit. It is a parent wrapper, not a true zombie. The round-2 PowerShell script intentionally left the bridge alone, so the pair survives.

Severity is **LOW** because (a) the shim doesn't actively contend for the listening ports — its child does, (b) atomic save via `FINDING-2026-06-02-A` eliminated the JSON-clobber-via-inherited-handles concern, and (c) the existing `launch_*.bat` files and the round-2 PowerShell script already enforce the safe explicit-pythoncore-path pattern. The remaining bridge zombie is a one-off from a long-dead launcher.

The launcher-discipline angle is the same root cause as the supervisor-drift symptom. Closing the supervisor-drift sprint (which surfaces `sys.executable` in `/api/bot/status`) plus a one-time bridge restart resolves this fully.

## Proposed fix (3-5 bullets)

- **One-time:** Restart the bridge using `launch_bridge.bat` (uses the explicit pythoncore path). Verify with `Get-CimInstance Win32_Process` that no WindowsApps-shim PID is parent of the new bridge PID. Confirm port ownership transfers cleanly.
- Add a launcher fingerprint check to `tools/watchdog.py` start-up: read its own `sys.executable`, check it against the WindowsApps shim path, log a loud WARN if so. Provides early detection if anyone re-introduces the bad launch pattern.
- (Bundled with `next_sprint_outline_supervisor_drift.md`) — surface `sys.executable` on `/api/bot/status` so the dashboard UI shows a banner when the interpreter is the shim path.
- Document in `memory/` the WindowsApps shim diagnosis with the specific signature (parent PID has `\WindowsApps\python.exe` exec path, child has `\pythoncore-3.14-64\python.exe` exec path, double-space in argv hint). `MEMORY.md` index already references `windows_subprocess_zombie.md` but the file is missing.
- Optional: add an integration check in `tools/daily_session_summary.py` that flags any python process whose exec path matches `\WindowsApps\python.exe`. Surfaces drift in the daily report.

## Files touched + protection

- `tools/watchdog.py` — NOT protected. Safe to edit.
- `tools/daily_session_summary.py` — NOT protected. Safe to edit.
- `memory/windows_subprocess_zombie.md` (new) — safe to create.
- `dashboard/server.py` — bundled with supervisor-drift sprint, not touched here standalone.
- **`bridge/bridge_server.py` — PROTECTED. Will NOT be edited.** The bridge restart is operational, not code change.

## Operator OA needed?

**NO** for the code changes. The one-time bridge restart should be confirmed with the operator (it's a state-affecting action: brief disconnect of bots from bridge, potential mid-session). Schedule for off-hours.

## Effort: **S**

Mostly diagnostic + a one-time restart. The watchdog fingerprint check is ~10 lines.

## Dependencies

- **Pairs with `out/next_sprint_outline_supervisor_drift.md` — same root cause.** Recommend rolling both into one PR.
- **Pairs with `out/next_sprint_outline_watchdog.md` — same observability addition (`sys.executable` surfacing).**
- The one-time bridge restart should happen during a session-close window (after RTH, before Globex re-open), not mid-trading-session.
