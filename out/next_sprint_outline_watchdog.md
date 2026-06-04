# Next-Sprint Outline — Watchdog Observability Addition

_Source: `out/bot_health_forensics_report_2026-06-04.md` §4. Read this for full evidence._

## Symptom + root cause

The watchdog itself is **healthy** post-round-2. Log shows continuous "Bridge:OK NT8:live, prod:UP(2.7h), sim:UP(2.7h), Dash:OK" for 5+ minutes across the sample window, no restart events fired since the round-2 restart at 15:02 CDT. Delegation pattern is sound: `restart_bot()` calls `POST /api/bot/stop` then `POST /api/bot/start`, dashboard spawns with `sys.executable`, no chance of double-spawn or zombie creation when externally-started bots are already alive (`_start_bot` returns "already running" cleanly, watchdog accepts gracefully).

**The gap:** There is no audit hook recording **which interpreter** `sys.executable` resolved to when the dashboard process started. A misconfigured dashboard (started with the WindowsApps shim) would silently propagate the zombie pattern to every watchdog-triggered respawn. This is a latent observability hole, not an active bug.

## Proposed fix (3-5 bullets)

- Add `sys.executable` to the response of `dashboard/server.py:/api/bot/status` (alongside the existing `prod` / `sim` entries). 3-line change.
- Add a "launcher health" line to the watchdog's status print loop: `Bridge:OK NT8:live | dashboard_interp:pythoncore | prod:UP...`. Watchdog already queries `/api/bot/status` every 5s; just surface the new field.
- Have watchdog log a WARN if `dashboard_interp` matches `\WindowsApps\python.exe` — loud breadcrumb that the bad launch pattern is in use.
- Optional: have watchdog also publish a JSON event to `logs/disconnect_forensics.jsonl` on dashboard-interp WARN transitions so it shows up in the post-session debrief.

## Files touched + protection

- `dashboard/server.py` — NOT protected.
- `tools/watchdog.py` — NOT protected.

## Operator OA needed?

**NO.** Observability-only; no behavior change to bot start/stop semantics.

## Effort: **S**

~30 lines total across two files.

## Dependencies

- Belongs naturally bundled with `out/next_sprint_outline_supervisor_drift.md` (same `/api/bot/status` surface area + same root cause family). Operator may choose to treat as a one-line addition to that sprint rather than its own PR.
- Independent of PHANTOM-NT8 sprint.
