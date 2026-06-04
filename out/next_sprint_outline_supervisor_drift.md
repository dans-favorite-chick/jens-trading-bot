# Next-Sprint Outline — Dashboard Supervisor Drift

_Source: `out/bot_health_forensics_report_2026-06-04.md` §2 + §3 + §7. Read this for full evidence._

## Symptom + root cause

The dashboard's `/api/bot/stop` reports "not running" for bots that are actively trading because its `_bot_processes` registry only contains subprocesses spawned via `_start_bot()`. The status endpoint (`_bot_status`) detects external bots via bridge connection + recent state pushes, but `_stop_bot` only honors the registry unless `force=True`. When the operator launches the stack from a shell (round-2 PowerShell, `launch_all.bat`, manual `python bots/prod_bot.py`), the supervisor is never told about them. The 2026-05-13 commit (`8b471af`) made `force=False` the default deliberately, to protect operator cmd-window bots from watchdog auto-restart cycles — at the cost of operator stop actions being ineffective by default.

This is also the root cause shared with the multi-PID symptom: both come from "launches happen outside the supervisor's lifecycle, so the supervisor can't enforce launch discipline (interpreter path) either." Closing this sprint also makes the dashboard surface enough info to detect the WindowsApps-shim launch pattern.

## Proposed fix (3-5 bullets)

- Make `_stop_bot` symmetric with `_bot_status`: when registry pop returns `None`, queue a `shutdown` command for the bot via `_state["_commands_<name>"]` (same path Path 1 uses for graceful shutdown), then poll `_bot_status` until it returns `stopped` (≤ `_GRACEFUL_SHUTDOWN_TIMEOUT_S`). Fall back to psutil scan only if the command-queue path doesn't take.
- Add `dashboard adopt-detect` at startup: scan for already-running bot processes (psutil by command-line match) and, if found, populate `_bot_processes[name] = None` with a sentinel that means "external, command-queue managed." Subsequent `/api/bot/stop` calls then dispatch to the command queue instead of returning "not running."
- Flip the dashboard UI "Stop Bot" button to send `force=True` by default — the operator's mental model already expects "kill it." Watchdog calls continue to use `force=False` for safety.
- Surface `sys.executable` and the resolved process inventory in `/api/bot/status` so the dashboard can detect "I was started with the WindowsApps shim" and warn loudly. Closes the latent zombie-propagation risk noted in the watchdog observability outline.
- Add a unit test covering the externally-started-bot stop path (mock psutil, mock command queue, verify graceful shutdown round-trip).

## Files touched + protection

- `dashboard/server.py` — NOT in `.claude/PROTECTED_FILES.md`. Safe to edit without OA.
- `tests/test_dashboard_bot_lifecycle.py` (new) — tests are always safe.
- `dashboard/__init__.py` and `dashboard/panels.py` — read-only references, no edit expected.

## Operator OA needed?

**NO.** All edits are in `dashboard/` which is explicitly listed as safe ("doesn't touch execution") in `CLAUDE.md`. Behavior change is observability + stop-semantics, no risk-cap or live-canary touch.

## Effort: **M**

Mostly mechanical with one design-review point: the command-queue-based external-bot adopt pattern needs alignment on whether to introduce a sentinel value in `_bot_processes` or a separate `_external_bot_managers` dict. Pick during the sprint.

## Dependencies

- Pairs with `out/next_sprint_outline_multi_pid.md` (same root cause) and `out/next_sprint_outline_watchdog.md` (the observability addition). Operator may choose to roll all three into one PR.
- No dependency on PHANTOM-NT8 sprint.
- Round-2 remediation + confluence sprint must remain closed (already true per Phase 0).
