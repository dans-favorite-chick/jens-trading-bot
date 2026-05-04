# Phoenix Bot — Current State

_Last updated: 2026-04-25 ~17:40 CDT (full scheduled-task lattice operational, SMS verified end-to-end)_
_Next Claude session: read this FIRST for situational awareness_

## ✅ Saturday EOD — full operational state

**The entire Phoenix automation lattice is now live and verified:**

| Layer | Status | Evidence |
|---|---|---|
| Bridge `:8765` single-stream enforcement | ✅ ON | `PHOENIX_BRIDGE_SINGLE_STREAM=1`, 3 unit tests + live spy verification |
| Bridge `:8765` peer-MAD validator | ✅ ON | `PHOENIX_STREAM_VALIDATOR=1` (was 0 all weekend until today) |
| Multi-account close isolation | ✅ Tested | 7 unit tests; closes only target account, never cross-cancels |
| Twilio SMS escalation | ✅ E2E verified | `sid=SMba9bbf84b5866fdefa0ae9587b898aa0` delivered to phone |
| Telegram alerts | ✅ Live | Watcher logged `[Alerter] Telegram ready` |
| Gemini AI investigator | ✅ Live | Watcher logged `[Investigator] Gemini client ready` |
| 11 scheduled tasks under `TradingPC\Trading PC` | ✅ Registered | See task table below |
| 7 Phoenix processes | ✅ Running | prod_bot, sim_bot, bridge, dashboard, watcher, finnhub, fred |
| Test suite | ✅ Green | 1,231 passing, 4 skipped, 0 failing |
| Repo | ✅ Pushed | `5cf0d3d` on `origin/main` |

## Scheduled task lattice (final state, all under Trading PC user)

| Task | Trigger | Currently | Notes |
|---|---|---|---|
| `PhoenixBoot` | AtLogOn | Ready | Auto-launches stack via PhoenixStart.bat at logon (was broken with dbren Principal until today) |
| `PhoenixWatcher` | AtLogOn (daemon) | **Running** | SMS/Telegram escalation, 3-strike rule, NT8 SILENT_STALL detection |
| `PhoenixFinnhubNews` | AtLogOn (daemon) | **Running** | News feed; WS connected (free-tier limited, REST fallback active) |
| `PhoenixFredMacros` | AtLogOn (daemon, --interval-min 60) | **Running** | FFR/CPI/UNRATE/T10Y2Y poller, Telegram on regime shift |
| `PhoenixGrading` | 16:00 CT Mon-Fri | Ready | Daily prediction grader |
| `PhoenixMorningRitual` | 06:30 CT Mon-Fri | Ready | Pre-market 7-check, deterministic verdict |
| `PhoenixPostSessionDebrief` | 16:05 CT Mon-Fri | Ready | Consolidated digest (Telegram) |
| `PhoenixWeeklyEvolution` | Sun 18:00 CT | Ready | Auto-PR with adaptive params + CPCV/DSR/PBO checkboxes |
| `PhoenixRiskGate` | AtBoot/AtLogOn | Ready | Fail-closed gate (gated by `PHOENIX_RISK_GATE=0`, off by default) |
| `PhoenixRiskWatchdog` | AtBoot/AtLogOn | Ready | Heartbeat watchdog for risk gate |
| `PhoenixLearner` | 23:30 CT daily | Ready | Historical learner (only one that survived all reboots — was correctly principled originally) |

## Saturday afternoon root-cause + cleanup summary

After the morning Phase B+ Sprint 2 work, the afternoon was a 4-hour
incident response on the dual-stream pollution bug. Final root cause
turned out to be 3 layered failures (see KNOWN_ISSUES.md for full
playbook):

1. Two legacy NT8 source files were still compiled in `NinjaTrader.Custom.dll`:
   - `Indicators\JenTradingBotV1DataFeed.cs` (V1-era WS indicator)
   - `Strategies\OLDDONTUSEMarketDataBroadcasterv2.cs` (V2-era strategy)
2. NT8 `<ShowDefaultWorkspaces>true</ShowDefaultWorkspaces>` auto-loaded a
   workspace with **9 hidden MNQM6 charts** (`IsWindowVisible=false`,
   invisible in taskbar, no Window menu in this NT8 version)
3. `PHOENIX_STREAM_VALIDATOR=0` (default) — bridge defense was off

**Defense layers added today (preventing recurrence):**

- `bridge/bridge_server.py::handle_nt8_tcp` — rejects 2nd+ NT8 connection
  at socket-accept layer (`PHOENIX_BRIDGE_SINGLE_STREAM=1`)
- `tools/nt8_unhide_all_windows.ps1` — Win32 EnumWindows + ShowWindow
  to surface hidden NT8 chart windows; required because newer NT8
  builds have no Window menu
- `tools/diagnose_nt8_client.py` — spy bot that classifies the
  connected NT8 client by message-shape fingerprint
- `tools/_patch_register_scripts.py` — idempotent helper that
  retroactively fixed all 8 register scripts to use `$TaskUser`
  (`TradingPC\Trading PC`) instead of `$env:USERDOMAIN\$env:USERNAME`

## .env state (relevant flags only — secrets redacted)

```
PHOENIX_STREAM_VALIDATOR=1          # peer-MAD validator (NEW today)
PHOENIX_BRIDGE_SINGLE_STREAM=1      # socket-accept reject (NEW today)
SENTIMENT_FLOW_ACTIVE=false         # FinBERT voter (deferred)
SENTIMENT_FLOW_WEIGHT=0.10          # ignored while ACTIVE=false
LIVE_ACCOUNT=1590711                # B59 hard-guard target
SIM_ACCOUNT=Sim101                  # prod_bot default routing
```

Twilio + Telegram + Gemini + Google + Finnhub + FRED + Anthropic +
OpenAI + Groq + Grok + Alpaca + MenthorQ keys all populated and
verified loadable by `dotenv` (after a brief mid-afternoon
.env corruption incident — em-dash byte 0x97 from PowerShell
`Add-Content`, fixed in-place by re-encoding as ASCII/UTF-8).

## Operator runbook (next session)

**Sunday 17:00 CT — market reopen first test:**
1. NT8 reconnects → bridge log shows ONE `NT8 client connected from`
2. `(Get-NetTCPConnection -LocalPort 8765 -State Established).Count` = 1
3. Stream validator silently approves real ticks
4. Watcher's "bridge_down" false-alarm clears

**Monday 06:30 CT — first scheduled-task fire:**
1. `PhoenixMorningRitual` → `out/morning_ritual/2026-04-27.md`
2. Order round-trip test: `python tools/verify_oif_fix.py`
3. `PhoenixGrading` 16:00 CT → `PhoenixPostSessionDebrief` 16:05 CT
   consolidated Telegram

**Deferred (not blocking):**
- `PHOENIX_RISK_GATE=1` — flip when ready to intercept every OIF
- `SENTIMENT_FLOW_ACTIVE=true` — flip after shadow data validates
- CPCV/DSR/PBO harness — Phase C dependency



## ⚠️ Today's NT8 dual-stream incident — RESOLVED (this afternoon)

**Symptom:** bridge `:8765` had 2+ established TCP connections all weekend.
PriceSanity logged 27,000+ tick rejections over Friday→Saturday with corrupt
~7,196-class prices alongside real ~27,440 MNQ prices ("phantom $40K trade"
signature).

**Real root cause** (turned out to be 3 layered issues, not the simple
"single TickStreamer dupe" we thought):

1. **Two legacy NT8 source files were still installed and compiled:**
   - `Indicators\JenTradingBotV1DataFeed.cs` — V1-era WebSocket indicator
     with `IsSuspendedWhileInactive=false`, broadcasting synthetic
     mom/prec/conf fields plus a secondary data series whose price scale
     was the source of the corrupt 7,196 stream.
   - `Strategies\OLDDONTUSEMarketDataBroadcasterv2.cs` — V2-era WebSocket
     strategy, also targeting `:8765`, with its own ATI write path.
2. **NT8 auto-loaded a bloated workspace via
   `<ShowDefaultWorkspaces>true</ShowDefaultWorkspaces>`** — `Jen's Fav.xml`
   and/or `Jen's indicators.xml` brought up **9 hidden MNQM6 charts** plus
   ESM6/AUDUSD/SuperDOM windows (`IsWindowVisible=false`). Charts were
   alive in NT8 memory holding TickStreamer instances + TCP connections,
   but invisible — not in taskbar, no Window menu in this NT8 build to
   reveal them.
3. **The system was operating one PriceSanity edge case away from a real
   loss the entire weekend.** PriceSanity caught all corrupt ticks at the
   bot level; the OIF builders' price-sanity guard would have caught any
   that slipped past. But `PHOENIX_STREAM_VALIDATOR=0` meant the bridge-
   level defense built specifically for this scenario was off.

**Cleanup playbook (now embedded in `tools/nt8_unhide_all_windows.ps1`):**

1. Move both legacy `.cs` files to `.disabled_2026_04_25` quarantine.
2. Run `tools/nt8_unhide_all_windows.ps1` from elevated PS — uses
   Win32 `EnumWindows` + `ShowWindow(SW_SHOWNORMAL)` to surface every
   hidden NT8-owned window. Without this you cannot find the 9 ghost
   charts.
3. Manually close every chart not needed; keep one MNQM6 with
   TickStreamer attached.
4. Workspaces → Save As → `phoenix_clean_2026_04_25` (clean baseline).
5. Tools → Options → General → uncheck "Show default workspaces on
   startup" — prevents recurrence.
6. NT8 NinjaScript Editor → F5 to recompile (purges legacy classes
   from cached `NinjaTrader.Custom.dll`).
7. Full NT8 restart. Verify: with NT8 running but no chart open,
   `(Get-NetTCPConnection -LocalPort 8765 -State Established).Count`
   must return 0. Open one chart → count = 1. Close chart → back to 0.
8. **Set `PHOENIX_STREAM_VALIDATOR=1` in `.env`** — bridge-level
   defense for any future workspace pollution.

**Diagnostic insight that broke the case:** `nt8_last_heartbeat_age_s ≈ 2.8`
on bridge health endpoint matched **TickStreamer's `HEARTBEAT_MS=3000`**
timer exactly. Legacy V2 strategy uses `HEARTBEAT_BARS=30` (bars, not
milliseconds — silent on a closed Saturday market). That fingerprint
proved the connecting client was TickStreamer, but a Win32 window
enumeration revealed it was attached to one of nine hidden charts.

**Status as of 15:30 CDT:** Jennifer completed cleanup. NT8 is currently
closed; bridge confirms `nt8_status: disconnected`, 0 connections on
`:8765`. Next NT8 startup should bring 0 connections (until a chart is
manually opened).

## Bot operational state (as of Saturday 2026-04-25 EOD)

- **Prod bot:** UP, flat, Sim101 account (LIVE_TRADING=False). PID and uptime
  re-stabilized after the 14:31 CDT reboot (TeamViewer-initiated).
- **Sim bot:** UP, Phase C live-sim execution on 16 dedicated NT8 Sim
  accounts. 24/7 trading, per-strategy risk isolation, real OIF writes,
  10 strategies loaded.
- **Lab bot:** **DECOMMISSIONED** — paper-only flow ended 2026-04-21.
  `bots/lab_bot.py` preserved on disk as rollback safety net only.
- **Bridge:** UP on :8765 (NT8) + :8766 (bots) + :8767 (health)
- **Dashboard:** UP on :5000, with per-strategy risk panel, Grades tab,
  Logs tab, and (new today) sentiment-flow surface.
- **Watchdog:** UP, tracks prod + sim
- **NT8:** live, MNQM6, single client confirmed (multi-stream issue
  resolved per Sunday 2026-04-19 diagnostic).

## Today's sprint summary (2026-04-25)

A two-phase Saturday rebuild day. **All work shipped to `origin/main` at
commit `c2dcdc8`.** Defaults remain SAFE (`PHOENIX_RISK_GATE=0`,
`PHOENIX_STREAM_VALIDATOR=0`, `SENTIMENT_FLOW_ACTIVE=false`).

### Phase B+ skeleton sprint (morning)

Six items shipped behind off-by-default flags:

1. **NT8 stream validator** — `core/bridge/stream_validator.py` +
   `tools/nt8_stream_quarantine.py`. Price-band / peer-MAD / tick-grid
   checks. Gated by `PHOENIX_STREAM_VALIDATOR=1`.
2. **Fail-closed risk gate** — `core/risk/risk_gate.py` +
   `tools/risk_gate_runner.py` + `tools/watchdog_runner.py`. Named-pipe
   gate (`\\.\pipe\phoenix_risk_gate`), OIFSink shim, atomic OIF writer,
   heartbeat watchdog. Gated by `PHOENIX_RISK_GATE=1`.
3. **FinBERT sentiment skeleton** — `core/sentiment_finbert.py` +
   `agents/sentiment_flow_agent.py`. **Real INT8 ONNX model now installed
   under `models/finbert_onnx_int8/`** (downloaded via optimum-cli;
   gitignored). Council voter wired at `DEFAULT_WEIGHT = 0.0`.
4. **Chicago VPS migration plan** — `docs/chicago_vps_migration_plan.md`
   + `tools/verify_jsonl_continuity.py`. **STRICKEN per Jennifer
   2026-04-25** — Phoenix stays on the Trading PC. Doc preserved for
   reference; no infra moves planned.
5. **SKILLS auto-digest** — `tools/skills_digest.py` generates
   `SKILLS.md` and is wired into the SessionStart hook.
6. **Dashboard Grades + Logs tabs** — surface `tools/grade_open_predictions.py`
   output directly in the Flask dashboard.

### Phase B+ "remaining §3" sprint (afternoon → evening)

After greenlight on §2.2 / §2.3 / §3.5 / §3.6 / §4.1 / §4.3 / §4.4:

1. **§2.2 FRED macros** — real `core/fred_client.py` with regime-shift
   detection on FFR / CPI / UNRATE / T10Y2Y; cached at
   `data/cache/fred/`.
2. **§2.3 Finnhub real client** — REST + WebSocket dual-path with token
   bucket; key already present in `.env`.
3. **§3.1 TradingView webhook** — **STRICKEN** (Premium $59.95/mo not
   worth the cost). HMAC-SHA256 webhook scaffolding removed from active
   roadmap; placeholder code retained but not imported anywhere.
4. **§3.4 Phoenix-specific skills** — **DEFERRED**. `.claude/skills/`
   directory created (empty), allowlisted in `.gitignore`, future-ready.
5. **§3.5 OIF kill-switch** — `tools/oif_kill_switch.py`. One-command
   manual halt: writes `outgoing/halt_all.json`, prod_bot watches for
   it on every cycle, refuses new entries until cleared.
6. **§3.6 Phoenix Routines** — three deterministic routines:
   - `tools/routines/morning_ritual.py` (06:30 CT, Mon-Fri)
   - `tools/routines/post_session_debrief.py` (16:05 CT, Mon-Fri,
     chains PhoenixGrading at 16:00)
   - `tools/routines/weekly_evolution.py` (Sun 18:00 CT)
   - Shared `tools/routines/_shared.py` with: `RoutineReport`
     (verdict-deterministic), `DigestQueue` (file-backed FIFO at
     `out/digest_queue.jsonl`), AI wrappers (`call_claude` /
     `call_gemini` fail-soft), Telegram dispatch
     (`send_telegram_now` for RED, `send_consolidated_digest` for
     EOD), PDF assembly via reportlab, `stack_health_snapshot()`.
   - **Three Jennifer amendments locked in:**
     - Verdict is computed from deterministic checks ONLY; AI commentary
       is appendix and CANNOT influence GREEN/AMBER/RED.
     - Every weekly_evolution commit body MUST include CPCV / DSR / PBO
       checkboxes with status "NOT YET RUN (Phase C dependency)".
     - All routine output is queued to `out/digest_queue.jsonl` and
       drained as ONE consolidated Telegram at 16:05 (only RED-verdict
       items fire an immediate interrupting Telegram).
7. **§4.1 / 4.3 / 4.4 Strategy fixes (A-F)** — locked-in regression
   tests committed at `tests/test_lock_in_epic_v1/` (20 new tests):
   - ORB ATR-adaptive stops
   - bias_momentum SHORT mirror + VCR=1.2 threshold
   - noise_area silent cadence + band_mult=0.7
   - ib_breakout 10-minute window
   - compression min_squeeze_bars=12
   - spring_setup retired

### Tooling, infra, plugins

- **Plugins installed:** machine-learning-ops, incident-response,
  pyright-lsp, document-skills, example-skills (via `claude plugin
  install` CLI subcommand). Total: **10 plugins / 72 skills indexed**.
- **`.gitignore` hardened:** broad ignores for venv/.venv-ml/models/
  out/.claude/ etc. with allowlist patterns for
  `phoenix_bot/orchestrator/`, `.claude/commands/`, `.claude/skills/`,
  `.claude/agents/`, `.claude/settings.json`, `out/baselines/`. Re-ignore
  patterns to stop bytecode caches sneaking in via greedy `**` allowlists.
- **GitHub auth fixed** — Statechamp76 → dans-favorite-chick mapping
  via `gh auth logout` + `gh auth login`. Push to `origin/main`
  succeeded at commit `c2dcdc8`.

## Test count

- **Before today:** 989 (Friday EOD 2026-04-24)
- **After Phase B+ skeleton sprint:** 1,081
- **After Routines + remaining §3 sprint:** **1,221 passing / 0 failing**

## Scheduled task state (2026-04-25 EOD)

After the 14:31 reboot, currently registered:

| Task | Schedule | Status |
|---|---|---|
| `PhoenixLearner` | 23:30 CT daily | ✅ REGISTERED (survived reboot) |
| `PhoenixGrading` | 16:00 CT Mon-Fri | ⏳ Script ready: `scripts/register_phoenix_grading_task.ps1` |
| `PhoenixRiskGate` | on-boot | ⏳ Script ready: `scripts/register_risk_gate_task.ps1` |
| `PhoenixMorningRitual` | 06:30 CT Mon-Fri | ⏳ Script ready: `scripts/register_morning_ritual_task.ps1` |
| `PhoenixPostSessionDebrief` | 16:05 CT Mon-Fri | ⏳ Script ready: `scripts/register_post_session_debrief_task.ps1` |
| `PhoenixWeeklyEvolution` | Sun 18:00 CT | ⏳ Script ready: `scripts/register_weekly_evolution_task.ps1` |
| `PhoenixWatcher` | AtLogOn (daemon) | ⏳ Script ready: `scripts/register_watcher_task.ps1` (added 2026-04-25 ~16:30 CDT — escalates RED_ALERT to Twilio SMS + Telegram; runs watcher_agent.py continuously with auto-restart) |
| `PhoenixFinnhubNews` | AtLogOn (daemon) | ⏳ Script ready: `scripts/register_finnhub_news_task.ps1` (added 2026-04-25 ~16:50 CDT — Finnhub WS+REST news feed, persists to logs/finnhub_news.jsonl) |
| `PhoenixFredMacros` | AtLogOn (daemon, --interval-min 60) | ⏳ Script ready: `scripts/register_fred_macros_task.ps1` (added 2026-04-25 ~16:50 CDT — FFR/CPI/UNRATE/T10Y2Y poller, regime-shift Telegram alerts) |

### `.env` flags flipped 2026-04-25 ~16:50 CDT (defense-in-depth)

- **`PHOENIX_STREAM_VALIDATOR=1`** — peer-MAD price-band validation at the
  bridge layer. Built specifically for today's dual-stream incident class.
- **`PHOENIX_BRIDGE_SINGLE_STREAM=1`** — explicit (default already 1)
  rejecting any 2nd+ NT8 connection at socket-accept.

**ACTION:** Re-run the four `register_*.ps1` scripts as Administrator
to restore the full schedule. Each script is idempotent (replaces any
existing task of the same name). The em-dash / schtasks / python-alias
issues are all fixed in the current versions.

## AI agent live status

- **Gemini (Council, Pre-Trade Filter):** ACTIVE.
- **Claude (Session Debriefer, Historical Learner):** ACTIVE — the
  ANTHROPIC_API_KEY issue was resolved 2026-04-21 via commit `eac5ae4`
  (`load_dotenv override=True`). The 108-char key has been verified
  end-to-end.

## Account state

- **Real live account balance:** $300 (small_account_mode active)
- **Live trading status:** PAUSED — prod stays Sim101 until account
  reaches $2,000.
- **Sim bot:** $2,000 × 16 strategies = $32,000 virtual pool.

## Ground rules locked in this weekend

- Phoenix stays on the Trading PC. No VPS migration.
- TradingView Premium not approved. Stricken from roadmap.
- Per-strategy risk isolation is the unit of accounting.
- AI is **advisory only** — every routine verdict is deterministic.
- Telegram is the ONLY runtime alert channel (Twilio SMS is escalation
  only, behind WatcherAgent).

## Repository state

- Branch: `main`
- HEAD: `c2dcdc8` (chore(gitignore): re-ignore __pycache__ in
  allowlisted dirs; drop stale .pyc; add phoenix_bot/__init__.py)
- Working tree: **clean**
- Pushed to `origin/main`: ✅
- 10 plugins installed via `claude plugin install`
- 72 skills indexed via `tools/skills_digest.py` → `SKILLS.md`

## Immediate to-dos for the next session

1. Re-run all five `register_*.ps1` scripts as Administrator to restore
   the scheduled task lattice after the 14:31 reboot.
2. Verify `PhoenixMorningRitual` fires Monday 2026-04-27 at 06:30 CT —
   look for `out/morning_ritual/2026-04-27.md` + a non-RED verdict.
3. Verify `PhoenixPostSessionDebrief` consolidated digest arrives on
   Telegram Monday 16:05 CT containing the morning_ritual snippet.
4. First floor-kill test still pending — manually trigger a strategy
   to -$500 cumulative to validate halt + persistence + Telegram alert
   path.
5. CPCV / DSR / PBO validation harness implementation when Phase C
   data depth allows (currently the weekly_evolution checkboxes read
   "NOT YET RUN").

## Sprint C — Observability + Validation Hardening (2026-05-03)

Six commits shipped on top of Sprint A:

| Commit  | Tool / change |
|---------|---------------|
| `c78d6f6` | feat(b13-stats): backfill quality assessment |
| `e56c522` | fix(dashboard): pre/post-B13 backwards-compat |
| `2d216a1` | feat(daily): tools/daily_session_summary.py |
| `05d2af4` | feat(validation): tools/validation_tracker.py |
| `f3d73d3` | feat(halt-verify): tools/verify_halt_signatures.py |
| (this)    | docs: update CURRENT_STATE + CLAUDE.md |

### New tools (all read-only, all produce markdown reports in out/)

- `python tools/backfill_commissions.py` — historical net-P&L recompute
  + baseline quality flags. Run once, then again any time
  trade_memory.json grows significantly. Output:
  `out/historical_pnl_recompute_<today>.md`.
- `python tools/daily_session_summary.py [--date YYYY-MM-DD] [--bot sim|prod|both]`
  — daily heartbeat. Run after every session. Includes 7-day-baseline
  anomaly detection (silent strategies, signal volume drops). Output:
  `out/daily_summary_<today>.md`.
- `python tools/validation_tracker.py [--since YYYY-MM-DD] [--post-b13-only]`
  — statistical-tier decision support. Wilson 95% CI on WR. Run weekly.
  Output: `out/validation_status_<today>.md`.
- `python tools/verify_halt_signatures.py` — synthetic halt path
  end-to-end test. Run after any risk_manager / registry change.
  Output: `out/halt_verify_<today>.md`.

### Statistical tier reference (key insight)

Phoenix's project 50-trade graduation gate is **PRELIMINARY** by
published research standards:

| Tier | Trades | Confidence |
|---|---:|---:|
| INSUFFICIENT_SAMPLE | < 30 | none |
| PRELIMINARY | 30–99 | ~70% |
| TENTATIVE | 100–384 | ~90% |
| VALIDATED | 385–665 | ~95% |
| HIGH_CONFIDENCE | 666+ | ~99% |

`validation_tracker.py` surfaces this in the Decision column —
GRADUATE / SCALE recommendations require TENTATIVE tier (n ≥ 100)
at minimum, KILL_CANDIDATE only fires at PF < 0.7 with n ≥ 30.

### Live findings from first Sprint C run

Backfill (1,105 trades, 20-day span):
- Avg `|gross|/trade` = $108.45 with 1.00 contracts/trade — strongly
  suggests legacy NQ data has been mixed into the baseline (1 NQ tick
  = $5 vs 1 MNQ tick = $0.50). **Use `--post-b13-only` for clean
  validation comparisons** once enough post-B13 trades accumulate.
- `_reconciled` strategy n=3, net=−$80,216 — manual cleanup outliers,
  not real strategy trades. Excluded from validation comparisons.

Validation tracker:
- `high_precision_only` n=557 VALIDATED tier: WR 29% (CI 25–33%) — at
  KILL_CANDIDATE threshold.
- `bias_momentum` n=225 TENTATIVE: WR 28% (22–34%) — Sprint A's gate
  fixes (skip_on_stop_clamp, rsi_div_hard_gate, trend_stall_grace_s)
  should improve this on go-forward data; track via `--post-b13-only`.

Halt verification: ALL 4 SIGNATURES PASS — Sprint A's Fix E logging
reaches the production call chain end-to-end.

### Daily / weekly workflow during validation window

```
Morning (pre-session, optional):
  python tools/validation_tracker.py --post-b13-only

After session close:
  python tools/daily_session_summary.py
  # Read out/daily_summary_<today>.md
  # If anomalies present -> investigate before next session

Weekly:
  python tools/backfill_commissions.py     # refresh historical baseline
  python tools/verify_halt_signatures.py   # re-confirm halt paths intact
  python tools/validation_tracker.py --post-b13-only
```

### Test suite delta

Sprint A baseline: 1,295 passing
Sprint C final:    1,339 passing (+44)
0 failing, 4 skipped throughout.

## Sprint D — Stuck-exit fixes + alert noise reduction (2026-05-04)

Six commits over the morning:
- `e6129d8` fix(stuck-position): bulletproof auto-retry flatten + OIF guard
- `9c3e74b` fix(stuck-position): cover-action uses NT8 direction, not Python
- `2a7aae6` fix(exits): defensive observability + reconciliation tools
- `00e5a68` fix(alerts): EXIT_TIMEOUT one-shot + hourly rollup + RESOLVED
- `c7486c7` fix(alerts): RECOVERY MODE one-shot per session day
- `7b5144e` fix(alerts): watchdog 60s disconnect grace + restart threshold
- `1c39b8b` feat(alerts): low-priority alert digest (4h or 10-msg flush)

Estimated alert noise reduction: ~30/day → ~5-7 actionable/day.

## Sprint E — Daily roadmap calibration + indicator audit (2026-05-04)

Two commits:
- `9cf9b70` feat(quin): tools/quin_roadmap_log.py — daily roadmap capture
  + post-session calibration. Read-only labeled-dataset builder for
  regime-conditional gating evaluation. After 10-15 days of calibration
  data with avg score >= 0.70 → regime classifier earns authority over
  strategy gating (Sprint F territory).
- `561e61d` feat(audit): tools/indicator_audit.py — predictive-value ranking

### New tool: indicator_audit

`python tools/indicator_audit.py [--discover] [--post-b13-only]
[--since YYYY-MM-DD] [--strategy NAME] [--min-sample N]`

Reads logs/trade_memory.json. Computes per-indicator lift over base rate
with Wilson 95% CIs and significance flags. Writes
`out/indicator_audit_<today>.md`.

**Two design choices worth knowing:**

1. `result` and `exit_reason` are EXCLUDED from feature extraction —
   they're post-hoc outcomes, not pre-trade predictors. Including them
   yielded tautological 100% lift rows.
2. Boolean confluence features (e.g. `conf:VWAP reclaim`) are compared
   "with feature vs without feature at all" (different populations)
   instead of the within-feature pivot, which collapses to nothing for
   binary-presence features.

### Interpretation guide

1. **Top Predictive** — confluences/features that fire more on winning
   trades, statistically significant. Boost weight or make required.
2. **Top Contra-Indicators** — fire MORE on losing trades. Either remove
   as confluences (noise) or invert sign (bot was reading backwards).
3. **Tier Classifier Validation** — verifies whether A++/A/B/C ordering
   matches outcome. If NOT predictive → defer Sprint B's tier-sizing
   proposal; tier feature is currently noise, not signal.
4. **Per-Strategy** — drives strategy-specific config decisions.
5. **Per-Regime** — evidence for regime-conditional gating (Sprint F+).

### Workflow

```bash
# Weekly during validation window:
python tools/validation_tracker.py --post-b13-only
python tools/indicator_audit.py --post-b13-only

# Run full lifecycle once enough post-B13 data exists:
python tools/indicator_audit.py             # all data (quick view)
python tools/indicator_audit.py --post-b13-only  # clean baseline
```

Findings at PRELIMINARY tier are *hypotheses*. Findings at TENTATIVE+
with non-overlapping CIs are *evidence*. Don't act on hypotheses.

### Live audit findings on current trade_memory (1,123 trades, mixed eras)

All findings at PRELIMINARY tier — informational only:
- `market.pivot_s2=Q1` → WR 54.5% (lift +36.7pp, n=55)
- `account=SimBias Momentum` → WR 16.5% (lift -29.9pp, n=91)
  - Consistent with Sprint C validation_tracker flagging bias_momentum
    as KILL_CANDIDATE territory.

Post-B13-only run yielded 17 trades — too few for significance. The
audit will sharpen as the validation window fills.

### Test suite delta

Sprint C final:  1,339 passing
Sprint D final:  1,418 passing (+79)
Sprint E final:  1,468 passing (+50)
0 failing, 4 skipped throughout.
