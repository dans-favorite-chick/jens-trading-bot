# Phase B+ Log

A simple chronological log of Phase B+ work as it happens. Newest entries go on top.

## 2026-04-25 EOD -- Sprint 2: Routines + remaining §3 + git push

After the morning skeleton sprint, a second wave shipped with greenlight on
§2.2 / §2.3 / §3.5 / §3.6 / §4.1 / §4.3 / §4.4. Defaults remain SAFE.

### What shipped

- **§2.2 FRED macros (real client)** -- `core/fred_client.py` polls FFR /
  CPI / UNRATE / T10Y2Y, caches at `data/cache/fred/`, emits regime-shift
  events when any series moves outside its rolling band.
- **§2.3 Finnhub (real client)** -- REST + WebSocket dual-path with token
  bucket and re-connect logic. Key was already in `.env` (the earlier
  blocker note was incorrect).
- **§3.1 TradingView webhook -- STRICKEN** -- Premium $59.95/mo not
  approved. HMAC-SHA256 scaffolding removed from active roadmap. Routes
  not imported anywhere; will not load on bot start.
- **§3.4 Phoenix-specific skills -- DEFERRED** -- empty `.claude/skills/`
  directory created and allowlisted. Future-ready, no skills yet.
- **§3.5 OIF kill-switch** -- `tools/oif_kill_switch.py`. Drops
  `outgoing/halt_all.json`; `prod_bot` watches and refuses new entries
  until cleared. One-command manual halt for the operator.
- **§3.6 Phoenix Routines** -- three deterministic autonomous routines
  with verdict-only AI (commentary in appendix, never influences verdict):
  - `tools/routines/morning_ritual.py` (06:30 CT Mon-Fri) -- 7 checks:
    processes, ports, NT8 single-stream, FMP drift, MQ staleness,
    watcher heartbeat, OIF markers.
  - `tools/routines/post_session_debrief.py` (16:05 CT Mon-Fri) -- chains
    PhoenixGrading; computes Sharpe / max DD / profit factor / win rate;
    scans logs for new error signatures vs 7-day baseline; drains
    DigestQueue into ONE consolidated Telegram.
  - `tools/routines/weekly_evolution.py` (Sun 18:00 CT) -- aggregates
    week's grades, runs adaptive_params, AI review, auto-creates
    `weekly-evolution/YYYY-MM-DD` branch (NEVER auto-pushes). Commit body
    MUST include CPCV / DSR / PBO checkboxes -- enforced by
    `VALIDATION_STATUS_TEMPLATE` constant + 3 unit tests.
  - Shared scaffolding `tools/routines/_shared.py`: `RoutineReport`,
    `DigestQueue` (file-backed FIFO at `out/digest_queue.jsonl`), AI
    fail-soft wrappers, Telegram dispatch (now + consolidated), PDF
    assembly via reportlab, `stack_health_snapshot()`.
  - **Three Jennifer amendments locked in:** verdict determinism,
    validation checkboxes, consolidated digest. Each enforced by tests
    in `tests/test_routines/`.
- **§4.1 / 4.3 / 4.4 Strategy fixes (A-F)** -- 20 regression tests
  committed at `tests/test_lock_in_epic_v1/`:
  - ORB ATR-adaptive stops
  - bias_momentum SHORT mirror + VCR=1.2 threshold
  - noise_area silent cadence + band_mult=0.7
  - ib_breakout 10-minute window
  - compression min_squeeze_bars=12
  - spring_setup retired

### Scheduled task lattice (5 new register scripts)

- `scripts/register_phoenix_grading_task.ps1` -- 16:00 CT Mon-Fri
- `scripts/register_risk_gate_task.ps1` -- on-boot, gated by `PHOENIX_RISK_GATE=1`
- `scripts/register_morning_ritual_task.ps1` -- 06:30 CT Mon-Fri
- `scripts/register_post_session_debrief_task.ps1` -- 16:05 CT Mon-Fri
- `scripts/register_weekly_evolution_task.ps1` -- Sun 18:00 CT

All scripts use:
- `New-ScheduledTaskTrigger -Weekly -DaysOfWeek Mon,Tue,Wed,Thu,Fri ...`
  (NOT `-Daily` -- only `-Weekly` exposes `DaysOfWeek`).
- Robust python resolver (`pythoncore-3.14-64`, `py.exe`, `Program Files`,
  `Get-Command python.exe` non-WindowsApps fallback).
- All em-dashes replaced with `--` ASCII (cp1252 compatibility).
- `Get-ScheduledTask -ErrorAction SilentlyContinue` instead of
  `schtasks /Query` (PS7 `$ErrorActionPreference = "Stop"` was treating
  native command stderr as terminating).

### Plugins + skills

- 5 new plugins installed via `claude plugin install` CLI:
  machine-learning-ops, incident-response, pyright-lsp, document-skills,
  example-skills.
- `tools/skills_digest.py` regenerates `SKILLS.md` (72 skills across 9
  plugin namespaces). SessionStart hook runs it on every new session.

### git + .gitignore work

- `.gitignore` hardened with allowlist patterns for
  `phoenix_bot/orchestrator/`, `.claude/commands/`, `.claude/skills/`,
  `.claude/agents/`, `.claude/settings.json`, `out/baselines/`. Re-ignore
  patterns prevent `**/__pycache__/` and `**/*.pyc` from sneaking in via
  greedy allowlist `**`.
- Stale `.pyc` files removed via `git rm --cached`.
- GitHub auth: `gh auth logout` + `gh auth login` -- swapped from
  Statechamp76 (403 push) to dans-favorite-chick.
- Push succeeded at HEAD `c2dcdc8`.

### Test count

- Start of day (Friday EOD): 989
- After morning skeleton sprint: 1,081
- After Sprint 2 EOD: **1,221 passing / 0 failing**

### Known operational note

The 14:31 CDT TeamViewer-initiated reboot dropped the four newly
registered scheduled tasks (`PhoenixGrading`, `PhoenixRiskGate`,
`PhoenixMorningRitual`, `PhoenixPostSessionDebrief`,
`PhoenixWeeklyEvolution`). `PhoenixLearner` survived. Re-run the
five `register_*.ps1` scripts as Administrator to restore.

---

## 2026-04-25 -- Skeleton sprint day

Six skeleton items shipped today; defaults remain SAFE.

- **Multi-stream detector** -- `core/bridge/stream_validator.py` + `tools/nt8_stream_quarantine.py` deliver a price-band / peer-MAD / tick-grid validator. Default OFF behind `PHOENIX_STREAM_VALIDATOR=1`.
- **Fail-closed risk gate** -- `core/risk/risk_gate.py` + `tools/risk_gate_runner.py` + `tools/watchdog_runner.py` ship the named-pipe gate, OIFSink shim, atomic OIF writer, and heartbeat watchdog. Default `PHOENIX_RISK_GATE=0`; `base_bot.py` is unchanged.
- **FinBERT sentiment skeleton** -- `core/sentiment_finbert.py` + `agents/sentiment_flow_agent.py`. Council voter wired at `DEFAULT_WEIGHT = 0.0` (observation only, persists to ChromaDB).
- **Chicago VPS migration plan** -- `docs/chicago_vps_migration_plan.md` + `tools/verify_jsonl_continuity.py`. Plan-only; no infra changes performed.
- **SKILLS auto-digest** -- `tools/skills_digest.py` generates `SKILLS.md` and is now wired into the SessionStart hook so each new Claude session starts with a fresh inventory.
- **Dashboard Grades + Logs tabs** -- new tabs surface the output of yesterday's `tools/grade_open_predictions.py` grading harness directly in the Flask dashboard.

Supporting fixes:

- `.gitignore` updated so the new `models/finbert_onnx_int8/` directory and `logs/risk_gate/*.jsonl` heartbeats are not tracked.

Locked-in tactical strategy fixes (A-F) from 2026-04-24 also got their dedicated regression tests committed under `tests/test_lock_in_epic_v1/`. Test count went from 986 -> 1081 passed.
