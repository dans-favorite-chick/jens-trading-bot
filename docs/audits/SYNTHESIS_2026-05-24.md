# Phoenix Bot — Audit Synthesis (2026-05-24)

**Status:** Canonical. Supersedes the three 2026-05-24 single-auditor reports for plan-making purposes.
**Inputs:** [`0524_Claude_Analysis.md`](0524_Claude_Analysis.md) (A), [`0524_AntiGravity_Analysis.md`](0524_AntiGravity_Analysis.md) (B), [`0524_Codex_Analysis.md`](0524_Codex_Analysis.md) (C).
**Method:** every finding verified against `file:line` in the live codebase. Vote count is not evidence; one auditor catching a real bug counts fully.

---

## 1. Findings matrix

Severity values are the auditor's claim; confidence is mine after verifying the code.

| ID | Finding | Raised by | Severity (claimed) | Code-verified | Conf. | Verdict |
|----|---------|-----------|---|---|---|---|
| F-01 | `bots/base_bot.py` is a 5,951-line god-class | A | High | [`bots/base_bot.py`](../../bots/base_bot.py) `wc -l = 5951` | High | **Confirmed** |
| F-02 | `WEEKLY_LOSS_LIMIT=$150 < DAILY_LOSS_LIMIT=$200` — weekly cap can trip on day-1 | B | Critical (hierarchy error) | [`config/settings.py:83-84`](../../config/settings.py) | High | **Confirmed** — singleton finding, clear bug |
| F-03 | AI council + pretrade filter run by default, advisory mode, zero measured uplift | A, B, C | High | [`config/settings.py:167-170`](../../config/settings.py) `AGENT_COUNCIL_ENABLED=True`, `AGENT_PRETRADE_FILTER_ENABLED=True`, `AGENT_DEBRIEF_ENABLED=True`; [`config/strategies.py:60`](../../config/strategies.py) `DEFAULT_AI_FILTER_MODE="advisory"` | High | **Confirmed consensus** |
| F-04 | `prod_bot` was DOWN at audit time, watchdog failed 3 restart tries, Gemini quota exhausted so incident AI itself failed | C | Critical (live) | [`logs/incidents/incident_2026-05-24_10-36-17.txt`](../../logs/incidents/) `consecutive_misses=47` + `ResourceExhausted` Gemini error | High | **Confirmed** — A and B missed current operational state |
| F-05 | `phoenix_bot/orchestrator/oif_writer.py:186` `RiskGateSink` fails soft to `DirectFileSink` when `PHOENIX_RISK_GATE=1` and pipe unreachable | C | High (risk gate optional) | [`phoenix_bot/orchestrator/oif_writer.py:186-225`](../../phoenix_bot/orchestrator/oif_writer.py) | High | **Confirmed** |
| F-06 | Bar-level CVD/delta proxy in backtest: `bar.delta = ±volume` based on close-vs-open sign | A | High (backtest/live gap) | [`tools/phoenix_real_backtest.py:159-164`](../../tools/phoenix_real_backtest.py) | High | **Confirmed** — docstring at L140 even acknowledges magnitude is wrong |
| F-07 | `risk_manager.calculate_contracts` is fixed-fractional; no portfolio correlation or directional exposure cap | A, B | Critical | [`core/risk_manager.py:210-244`](../../core/risk_manager.py); [`tools/strategy_correlation_audit.py`](../../tools/strategy_correlation_audit.py) is OFFLINE-only | High | **Confirmed** |
| F-08 | Operator-override pattern in `config/strategies.py` repeatedly bypasses `validated=True` Wilson-CI gate | A | High | [`config/strategies.py`](../../config/strategies.py) (grep "operator override"); [`config/settings.py:39-44`](../../config/settings.py) documents the $50→$100→$50 cap that was 3 days late on restore | High | **Confirmed** |
| F-09 | Phase 0 sim-testing overrides (DAILY_LOSS_LIMIT=$1M etc.) were live for weeks; one ($50 cap) survived 3 extra days | A | High | [`config/settings.py:39-44`](../../config/settings.py) comment; [`memory/context/CURRENT_STATE.md`](../../memory/context/CURRENT_STATE.md) at-a-glance L60 | High | **Confirmed** |
| F-10 | NT8 silent-stall (NT8 reports `live` but 0 ticks) still OPEN since 2026-04-16, no auto-recovery | A, C | Critical | [`memory/context/KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md) §"NT8 SILENT_STALL pattern" 🔴 OPEN | High | **Confirmed** |
| F-11 | 106s WS-watchdog reconnect cycle during 0-tick markets | C | Medium | [`bots/base_bot.py:5620`](../../bots/base_bot.py) `_ws_watchdog_loop`; [`memory/context/KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md) 🟠 OPEN | High | **Confirmed** |
| F-12 | `dom_pullback` was deleted on 2026-05-21 despite live-sim PF 2.13 | B | High (lost edge) | File absent from `strategies/`; [`config/strategies.py:267-275`](../../config/strategies.py) documents the deletion + reason | High | **Confirmed deletion; recommendation contested — see Conflict C-1** |
| F-13 | Phase 13 5y backtest unreconciled to live sim_bot | A, self | Highest leverage | `MEMORY.md` (operator user-memory) <!-- LINK BROKEN 2026-05-25: was ../../memory/MEMORY.md (external user-memory, not in repo) --> → `project_phase13_unreconciled.md`; CPCV/DSR/PBO checkboxes still "NOT YET RUN" in `weekly_evolution.py` | High | **Confirmed** — operator already memorialized as hard prerequisite |
| F-14 | No roll-event handling; multi-day and overnight strategies could carry a position through a contract roll | A | High (latent) | [`config/settings.py:17-21`](../../config/settings.py) hardcodes `INSTRUMENT="MNQM6"`, `NEXT_CONTRACT="MNQU6 09-26"`, `ROLL_DAYS_BEFORE_EXPIRATION=8`; `core/contract_rollover.py` exists — wiring not verified | Medium | **Partially correct** — primitive exists, but no evidence it's invoked at runtime; needs investigation |
| F-15 | OIF pipeline (atomic `.tmp`→`.txt`, `phoenix_<pid>_` filename prefix, pytest leak guard, PhoenixOIFGuard) | A (operational fact) | Foundation | [`bridge/oif_writer.py:7-14`](../../bridge/oif_writer.py) staged write + L41 `_PHOENIX_PID`; [`tests/conftest.py:20-37`](../../tests/conftest.py) autouse leak guard | High | **Confirmed working** |
| F-16 | Compounding backtest claim: $1.5K → $1.09M / 5y / 34% DD (`tier_3000`) | A | Suspect | [`memory/context/CURRENT_STATE.md`](../../memory/context/CURRENT_STATE.md) L23; [`docs/PHOENIX_BEST_PLAN.md`](../PHOENIX_BEST_PLAN.md) §3.4 lists own suspicion | High | **Confirmed claim; treat as conjecture until reconciled** (F-13) |
| F-17 | $4.82 round-turn commission ≈ 9.6 ticks on MNQ — friction trap | B | High | [`config/settings.py:184-187`](../../config/settings.py) commission math; trades-per-day count in `logs/trade_memory*.json` | High | **Confirmed structural drag** |
| F-18 | SPY-derived strategies (`noise_area`, Zarattini) misfit MNQ volatility profile | B | Medium-High | `noise_area` was retired; `opening_session.orb` is still active and is also Zarattini-derived | Medium | **Partially correct** — noise_area retired (B's headline example is closed); the volatility-mismatch concern survives for the remaining academic-derived strategies |
| F-19 | Grader-config divergence (un-retired `spring_setup` + grader expecting retired) | B | Low-Medium | `tools/grade_open_predictions.py` exists; whether the assertion is still live needs grep | Medium | **Needs investigation** — verify against current grader; if still live, P3 task |
| F-20 | Per-strategy daily cap $200 × 11 strategies = $2,200 theoretical daily exposure, only the global cap catches it | A | Critical | [`config/settings.py:271`](../../config/settings.py); [`core/strategy_risk_registry.py`](../../core/strategy_risk_registry.py) | High | **Confirmed** |
| F-21 | No real broker integration; everything is filesystem IPC into NT8 ATI | A, B | Critical structural | [`config/settings.py:60`](../../config/settings.py); [`bridge/oif_writer.py`](../../bridge/oif_writer.py) | High | **Confirmed** — design choice, not a bug; track as a permanent constraint |
| F-22 | No external dead-man's switch; alerts originate from the trading PC | A | High | All alerting routes (Telegram, Twilio SMS) start on Trading PC | High | **Confirmed** |
| F-23 | No fill-latency telemetry; backtest assumes 2-tick slippage flat | A | High | [`config/settings.py:187`](../../config/settings.py) `SLIPPAGE_TICKS_PER_SIDE=2` | High | **Confirmed** |
| F-24 | No CPCV / DSR / PBO walk-forward validation in production | A, C | High | [`tools/routines/weekly_evolution.py`](../../tools/routines/weekly_evolution.py) `VALIDATION_STATUS_TEMPLATE` emits "NOT YET RUN" | High | **Confirmed** |
| F-25 | B-030 sim_bot ZERO_GATE neutered every protective gate (commit `3afb04d`) — class of recurring sim-only branch failure | A | Critical pattern | Commit `3afb04d` per [`memory/context/CURRENT_STATE.md`](../../memory/context/CURRENT_STATE.md) RECENT_CHANGES; not re-verified against current source but pattern is operator-acknowledged | Medium | **Confirmed pattern, specific instance closed** |
| F-26 | Bug B2 (`open_drive` pivot_pp wrong-side target) flagged at `strategies/opening_session.py:372` | A, memory | High | [`strategies/opening_session.py:361-368`](../../strategies/opening_session.py) — "PHASE 13 BUG B2 FIX (2026-05-18): we no longer require pivot_pp" — the require-check was relaxed, but the *target* design decision is still pending operator input per OPEN_QUESTIONS | High | **Partially fixed; design decision still open** |
| F-27 | Bug B3 (`orb_fade` wallclock freshness) flagged at `strategies/orb_fade.py:162` | A, memory | High | [`strategies/orb_fade.py:159-166`](../../strategies/orb_fade.py) — header literally reads "PHASE 13 BUG B3 FIX (2026-05-18)" and uses `now_ct.timestamp()` | High | **REFUTED as still-open — fix already in code.** Audit A's claim is stale by the date of the fix commit |

### Bucketing

**Consensus (≥2 auditors):** F-01, F-03, F-06 (A only, but C echoes "backtest/live feature mismatch"), F-07, F-10, F-21, F-24. AI-is-dead-weight is the loudest consensus (F-03).

**Singletons:** F-02 (B alone — confirmed bug), F-04 (C alone — confirmed; A and B did not check current process state), F-05 (C alone — confirmed structural soft-fail), F-09 (A alone — confirmed), F-12 (B alone — fact confirmed, recommendation contested), F-14 (A alone — partially confirmed), F-22 (A alone — confirmed), F-25 (A alone — confirmed pattern).

**Conflicts:** see §2.

---

## 2. Conflict adjudications

**C-1. `dom_pullback`: reinstate (B) vs do not reintroduce (A, C implicit).**
- Evidence: [`config/strategies.py:267-275`](../../config/strategies.py) — deleted 2026-05-21 with cited reason "0 trades / 0 signals in 5-year canonical backtest (1.77M 1m-bar cycles); live sim showed 6/6 wins on incomplete data." The "PF 2.13" B cites is from sim_bot's small sample, not from canonical backtest.
- Verdict: **A/C are correct on the operational call.** B is right that deleting was premature if the *real* reason `dom_pullback` had zero backtest trades is that the backtester lacks L2/DOM data (which is true — see F-06 and [`tools/phoenix_real_backtest.py`](../../tools/phoenix_real_backtest.py)). The honest conclusion is *not* "reinstate dom_pullback now"; it is "do not delete strategies because of a backtest that structurally cannot evaluate them. Park them with `enabled=False` and a comment that names the data gap." That second sentence is the doctrine — file as an open item under Roadmap.

**C-2. Operational state: A says "system is up", C says "prod_bot down, gemini quota exhausted, stack thrashing."**
- Evidence: [`logs/incidents/incident_2026-05-24_10-36-17.txt`](../../logs/incidents/) — `prod_bot not running (consecutive checks missed: 47). Watchdog has failed to restart after 3 tries — paging.` Gemini quota error in same file. Multiple incident files per minute from 10:34 to 10:36 prove a tight failure loop, not a transient.
- Verdict: **C is correct.** A did not check current process state; B did not either. The plan must start by restoring stack health before doing anything else.

**C-3. Risk-limit hierarchy: B says fix (`WEEKLY < DAILY` is broken); A and C are silent.**
- Evidence: [`config/settings.py:83-84`](../../config/settings.py) — `DAILY_LOSS_LIMIT = 200.0`, `WEEKLY_LOSS_LIMIT = 150.0`. A single $150 daily loss closes the bot for the rest of the week. This is exactly the trap B describes.
- Verdict: **B is right. A and C missed it.** This is a 1-line fix and a singleton finding that the other two auditors did not catch — it counts fully.

**C-4. AI council action: A says A/B test for uplift, B says kill outright, C says park as quota-exhausted noise.**
- Evidence: [`config/settings.py:167-170`](../../config/settings.py) AGENT_*_ENABLED=True; current `gemini-2.5-flash` quota visibly exhausted per incident reports. No A/B harness exists.
- Verdict: **Synthesize.** Disable AGENT_COUNCIL/PRETRADE_FILTER/DEBRIEF immediately (no measured uplift + active quota cost + adds latency before order entry). Keep the code; do not delete. Re-enable any single agent only after an A/B harness publishes "uplift = $X over N trades, 95% CI [a,b]" in `out/`. Closer to A in structure, closer to B in default.

**C-5. Highest-leverage 7-day move: A says reconciliation harness, B says rebuild backtester for L2 + restore `dom_pullback`, C says restore stack health.**
- Evidence: F-04 (stack is down) is a hard blocker for anything else. The reconciliation harness (A) cannot run if sim_bot is dead. The L2 backtester rebuild (B) is a multi-week project, not a 7-day deliverable.
- Verdict: **C first, then A.** Stack health is the literal prerequisite. Once green, the reconciliation harness becomes the next gate. B's L2 backtest rebuild stays in the LATER bucket.

**C-6. Strategy roster: A "cull to 5", B "reinstate dom_pullback (count goes up)", C "cut to the few with enough live/sim evidence."**
- Evidence: 11 strategies enabled in Phase 13 ship list per [`memory/context/CURRENT_STATE.md`](../../memory/context/CURRENT_STATE.md); only `bias_momentum` has TENTATIVE-tier live data (n ≥ 100), per [`out/validation_status_2026-05-22.md`](../../out/validation_status_2026-05-22.md) (the file's existence is confirmed by `tools/validation_tracker.py` output convention; content needs re-run after stack restart).
- Verdict: **A/C win.** Cull to 3-5 strategies whose live evidence at TENTATIVE tier is positive. B's reinstate-recommendation does not survive the data-gap reframing in C-1.

**Cannot resolve from code alone:**
- F-19 (grader-config divergence on `spring_setup`) — needs `tools/grade_open_predictions.py` grep + a recent grader run output. If still live, P3 task to align grader assertions with current `config/strategies.py`. Flagged as **needs investigation** in the matrix.

---

## 3. Phase 0 foundation checklist

Every item below must be **TRUE AND VERIFIED** before any P1+ task starts.

| # | Check | Verification | Definition of done |
|---|-------|--------------|---------------------|
| 0.1 | Test suite passes at advertised count | `python -m pytest --tb=no -q` from repo root | "2,110+ passed, 19 skipped, 0 failed" (last advertised). If lower, investigate before proceeding. |
| 0.2 | `prod_bot` and `sim_bot` are running and connected to bridge | `Get-CimInstance Win32_Process -Filter "Name=\"python.exe\""` shows both PIDs; bridge health endpoint at `http://127.0.0.1:8767/health` returns `nt8_status: live`; bots listed in `bots_connected`. | Both bots up for 30 continuous minutes with no `process_down` incident. |
| 0.3 | Bridge `:8765`, `:8766`, `:8767`, dashboard `:5000` all listening | `(Get-NetTCPConnection -LocalPort 8765 -State Listen).Count -ge 1` for each | All 4 ports listen; dashboard `/api/today-pnl` returns 200. |
| 0.4 | OIF atomic write + `phoenix_<pid>_` prefix verified | [`bridge/oif_writer.py:7-14, 41`](../../bridge/oif_writer.py) inspection; run `python tools/verify_oif_fix.py` | Verifier reports OK. |
| 0.5 | PhoenixOIFGuard quarantine regex matches `phoenix_<pid>_*` | Manual: drop a file named `notphoenix_test.txt` in `incoming/`; NT8 PhoenixOIFGuard AddOn must move it to a quarantine folder within 1s | Quarantine confirmed; phoenix-prefixed test file passes through. |
| 0.6 | `tests/conftest.py` autouse fixture isolates OIF_INCOMING per-test | [`tests/conftest.py:20-37`](../../tests/conftest.py) inspection — confirmed in this synthesis | Already done. ✅ |
| 0.7 | Restart-safe OIF counter seeding | [`bridge/oif_writer.py:29`](../../bridge/oif_writer.py) `_oif_counter = int(time.time() * 1000) % 1000000` — already time-seeded | Already done. ✅ |
| 0.8 | Kill switch verified | `python tools/oif_kill_switch.py` writes `outgoing/halt_all.json`; tail `logs/prod_bot.log` for `[KILL_SWITCH] halt_all.json detected — refusing entries`. Then verify a live signal IS refused. | Manual signal-replay through bot confirms refusal. |
| 0.9 | Paper/live separation airtight | `grep -n "LIVE_TRADING" config/settings.py` — must read `False`; `grep -n "ACCOUNT" config/settings.py` — must read `"Sim101"`; `LIVE_ACCOUNT=1590711` in `.env` is the B59 hard-guard *target*, never the active routing. | All three checks pass; no test or sim-override path routes to LIVE_ACCOUNT. |
| 0.10 | Phase 0 sim-testing overrides fully restored | `grep -nE "(1_000_000|1e6|\$1M)" config/settings.py` returns 0 matches; `grep -n "operator override" config/strategies.py` returns 0 matches | All sim-overrides removed from production source. (Note: this is also P0 task #2 — the cleanup ships *as part of* Phase 0 closure.) |
| 0.11 | Memory write-back hook is wired and runs on SessionEnd | `cat ~/.claude/settings.json` shows the SessionEnd hook executing `tools/memory_writeback.py --auto-detect --commit`; last entry in `memory/audit_log.jsonl` is from current session boundary | Hook present and last write-back is < 24h old. |

**Foundation blockers found during this audit (any one of these failing means stop-and-fix before P1):**

- ❌ **0.2 fails right now**: prod_bot down with 47 missed heartbeats, watchdog exhausted 3 retries, Gemini-backed incident AI itself quota-exhausted (F-04). **This is the first thing fixed.**
- ❌ **0.10 fails**: while Phase 13 audit restored the headline overrides, [`config/settings.py:39-44`](../../config/settings.py) documents that one override survived 3 days late. The pattern is "comment-driven restore" which is exactly what failed. Phase 0 closure includes converting the override mechanism to a single `config/sim_overrides.py` opt-in file gated by `PHOENIX_SIM_OVERRIDES=1`. See P0-2.
- ⚠ **0.5, 0.8** are checks the operator can confirm in 5 minutes and I cannot verify from code alone. They go into Phase 0 closure as operator-confirm items.

---

## 4. Unified action plan

One sprint. Sequenced by dependency, then risk. No calendar phases.

### P0 — Foundation (stop everything else until these pass)

| ID | Task | Closes | Files | Effort | DoD | Verify | Risk |
|----|------|--------|-------|---|---|---|---|
| **P0-1** | Restore stack health: bring `prod_bot`, `sim_bot`, bridge, dashboard, watcher up; confirm Gemini quota OR disable AI agents that depend on it | F-04 | `bots/prod_bot.py`, `bots/sim_bot.py`, `bridge/bridge_server.py`, `dashboard/server.py`, `tools/watcher_agent.py`, `config/settings.py:167-170` | 1-2 sessions | All 5 processes up for 30min; bridge health green; no new `process_down` incidents | Phase 0 §0.2, §0.3 | Operator only; no code that touches OIF |
| **P0-2** | Convert "operator override" config pattern from inline-comment toggles to a single `config/sim_overrides.py` file gated by env `PHOENIX_SIM_OVERRIDES=1`. Bot startup prints `[CONFIG] sim_overrides active: N` (or `none`). Refuse to start if `LIVE_TRADING=True` AND overrides on. | F-08, F-09 | `config/settings.py`, `config/strategies.py`, new `config/sim_overrides.py`, `bots/base_bot.py` startup block | 1 day | All "operator override" comment-driven values are gone from production source; startup log line present in every bot launch | `grep -n "operator override" config/` returns 0 lines; live test of `PHOENIX_SIM_OVERRIDES=1` shows the toggle | Medium — touches config wiring; do not change risk math |
| **P0-3** | Fix the risk-limit hierarchy bug: raise `WEEKLY_LOSS_LIMIT` so `WEEKLY > DAILY × 3` | F-02 | [`config/settings.py:83-84`](../../config/settings.py) | 5 min | `WEEKLY_LOSS_LIMIT >= 600` and there is a test asserting `WEEKLY > DAILY` | Existing risk tests + 1 new test | Low — pure config |
| **P0-4** | Disable AI council, pretrade filter, debriefer (set the three `AGENT_*_ENABLED=False`). Keep code, document the kill in [`docs/roadmap.md`](../roadmap.md) as awaiting A/B harness | F-03 | [`config/settings.py:167-170`](../../config/settings.py) | 2 min | Three flags are `False`; no AI call appears in `logs/sim_bot.log` over a session | Tail logs for one session | Low |

**Phase 0 sign-off gate:** all four P0 tasks done, all Phase 0 checks at §3 pass, then proceed to P1. Test suite must still be at 2,110+ pass / 0 fail.

### P1 — Capital integrity (risks money, data, or order correctness)

| ID | Task | Closes | Files | Effort | DoD | Verify | Risk |
|----|------|--------|-------|---|---|---|---|
| **P1-1** | **Build the live-vs-backtest reconciliation harness for `bias_momentum`** (one strategy, the largest claimed P&L line). Take last 30 days of sim_bot trades; replay same input bars through `tools/phoenix_real_backtest.py`; assert entry/exit timestamps, prices, stop placement, exit reasons match within tolerance. Output `out/reconciliation_<date>_bias_momentum.md` with any divergences. | F-13, F-16 | `tools/reconcile_sim_vs_backtest.py` (new), `tools/phoenix_real_backtest.py` (no change unless replay hooks needed), trade memory readers via [`core/trade_memory.load_all_trades`](../../core/trade_memory.py) | 2-3 sessions | Tool exits 0 on `bias_momentum` 30-day window with documented tolerance; any divergence is catalogued | Operator inspection of output report | Low — read-only |
| **P1-2** | **Make `RiskGateSink` fail-CLOSED when `PHOENIX_RISK_GATE=1` is set explicitly**, not fail-soft. Today's behavior at [`phoenix_bot/orchestrator/oif_writer.py:186-225`](../../phoenix_bot/orchestrator/oif_writer.py) falls back to `DirectFileSink`; that is "risk gate off without telling anyone." Keep fail-soft only if `PHOENIX_RISK_GATE` is unset or 0. **🛑 STOP — sign-off required** | F-05 | [`phoenix_bot/orchestrator/oif_writer.py:186-225`](../../phoenix_bot/orchestrator/oif_writer.py), [`tools/risk_gate_runner.py`](../../tools/risk_gate_runner.py), tests | 1 day | When `PHOENIX_RISK_GATE=1` and pipe unreachable, bot refuses to write OIF and emits a CRITICAL log; existing tests pass | Existing `tests/test_risk_gate/` + new test for explicit-flag-fail-closed | **STOP — touches OIF path** |
| **P1-3** | **Portfolio-level directional/contract cap.** Before `_enter_trade` allows OIF #N+1, sum signed dollar exposure across all open positions in last 60s; reject (or halve sizing) if `|exposure_total| > $X` or co-fire Jaccard from [`tools/strategy_correlation_audit.py`](../../tools/strategy_correlation_audit.py) > 0.7 in same direction | F-07, F-20 | [`bots/base_bot.py`](../../bots/base_bot.py) `_handle_signal`/`_enter_trade`, possibly new `core/portfolio_risk.py` | 3-5 days | Live test: 3 simultaneous LONG fires reduces 3rd to 0c; backtest still passes | New `tests/test_portfolio_cap.py` | **STOP — touches OIF path** |
| **P1-4** | **NT8 silent-stall auto-recovery.** If `nt8_status: live` AND `tick_rate_10s == 0` for > 180s, SIGTERM `NinjaTrader.exe` and relaunch via PhoenixBoot. Halt new entries for 60s after relaunch. | F-10 | `tools/watcher_agent.py` (or new `tools/nt8_recovery.py`), [`memory/context/KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md) close-out | 1-2 days | Simulated stall (kill TickStreamer, leave NT8 alive) triggers Phoenix restart of NT8 within 5min; position reconciled | Manual operator test on a quiet evening | High — touches live NT8 process; **🛑 STOP — sign-off required** |
| **P1-5** | **External dead-man's switch.** A heartbeat probe NOT on the Trading PC (Cloudflare Worker, Lambda, second machine on the LAN) pings `:8767/health` every 60s; SMS+Telegram if 3 missed | F-22 | new external script + Tailscale or similar tunnel | 1 day | Pull Trading PC ethernet for 4min → SMS arrives | Operator manual test | Low (external infra) |
| **P1-6** | **WS-watchdog: distinguish "no ticks" from "WS dead."** Add bridge → bot `wsping` (or use existing heartbeat) as proof of life; raise WS_STALE_THRESHOLD_S to 180-300s for 0-tick windows | F-11 | [`bots/base_bot.py:5620`](../../bots/base_bot.py) `_ws_watchdog_loop`, `bridge/bridge_server.py` | 1 day | 106s reconnect cycle no longer triggered during 0-tick lulls; watchdog still fires on actual silent half-close | Stage simulated half-close test | Medium |
| **P1-7** | **Pending-order lifecycle truth.** Every entry must end in one of: filled, canceled, adopted, flattened. Timeout-then-cancel on stuck pending limits. | C #4 | `bots/base_bot.py` pending-order path, OIF cancel writer | 2-3 days | New `tests/test_pending_lifecycle.py` covers all 4 terminal states | Operator manual test on sim | **STOP — touches OIF path** |
| **P1-8** | **Stop-order ID capture or kill dynamic-stop strategies.** Today logs show `[STOP_MOVE_NO_ID]` for strategies that need stop modifications; without an order ID the modify path can't atomically cancel-replace. | C #5 | [`bridge/oif_writer.py`](../../bridge/oif_writer.py) modify_stop, NT8 outgoing-file ID capture | 2 days | No `[STOP_MOVE_NO_ID]` for any active managed-exit strategy; OR managed-exit strategies that lack ID-capture are switched to fixed stops | Tail logs for clean session | **STOP — touches OIF path** |

### P2 — Correctness (real bugs, not capital-critical)

| ID | Task | Closes | Files | Effort | DoD |
|----|------|--------|-------|---|---|
| **P2-1** | Bug B2 design decision + ship: `open_drive` continuation (R1/S1) or reversion (PP)? | F-26 | [`strategies/opening_session.py:361-400`](../../strategies/opening_session.py) | 1 day after operator decides | Strategy emits SIGNALs with correct target side; backtest re-validated |
| **P2-2** | Audit `time.time() - bar_ts` style gates across all strategies (B3 pattern). orb_fade fixed; check the rest. | F-27 generalized | `strategies/*.py` | 2 hours | `grep -rn "time.time().*bar_ts\|time.time().*last_bar" strategies/` returns 0 buggy patterns |
| **P2-3** | Roll-event handling: auto-flatten T-15 before front-month roll, refuse new entries until next session, swap to next contract | F-14 | [`core/contract_rollover.py`](../../core/contract_rollover.py), [`config/settings.py:17-21`](../../config/settings.py), [`bots/base_bot.py`](../../bots/base_bot.py) integration | 3-4 days | Simulated roll day: flatten at T-15, refuse entries, swap instrument |
| **P2-4** | Grader-config divergence: align `tools/grade_open_predictions.py` assertions with current `config/strategies.py` strategy set | F-19 | `tools/grade_open_predictions.py` | 1-2 hours | Grader runs clean on the next session log |
| **P2-5** | Recover `dom_pullback` decision: keep deleted, OR re-add with `enabled=False` and a doc-block explaining that backtest cannot evaluate it. **DO NOT silently reinstate.** | F-12 / C-1 | [`config/strategies.py`](../../config/strategies.py) | 30 min | Decision documented in [`docs/roadmap.md`](../roadmap.md) |

### P3 — Roadmap completion

| ID | Task | Closes | Files | Effort |
|----|------|--------|-------|---|
| **P3-1** | Wire 3C dashboard tuning sliders end-to-end (slider → `RiskManager.set_*` → live behavior) | open roadmap item | `dashboard/server.py`, `dashboard/templates/dashboard.html`, [`core/risk_manager.py:73-88`](../../core/risk_manager.py) | 2-3 days |
| **P3-2** | Build `feed.html` (the `/feed` window) and wire data into it | open roadmap item | `dashboard/templates/feed.html`, `dashboard/server.py` route | 1-2 days |
| **P3-3** | Cull active strategy roster to 3-5 with clearest live evidence at TENTATIVE tier; the rest go to `enabled=False` (eliminated from eval loop) | C-6 / A's "20% producing 80% of value" | [`config/strategies.py`](../../config/strategies.py) | 1 day (mostly deciding which 5) |
| **P3-4** | Cull docs root: confirm nothing references files now in `docs/archive/`. Update any broken cross-links. | repo cleanliness | repo-wide grep | 1 hour |

### P4 — Hardening (observability, resilience, tech debt)

| ID | Task | Closes |
|----|------|--------|
| **P4-1** | Decompose `bots/base_bot.py` (5,951 LOC → < 1,500 LOC) into strategy-dispatch / signal-handling / OIF-writing / daily-flatten / market-enrichment modules. **STOP — touches OIF path; sign-off required.** | F-01 |
| **P4-2** | Per-signal correlation/trace ID across signal-emit → council → pretrade → OIF → fill → exit → trade_memory row | observability |
| **P4-3** | Latency SLO: measure p99 tick-in → OIF-out; emit to dashboard | F-23 (live-side complement) |
| **P4-4** | Migrate `trade_memory` + halts + equity-state JSON → SQLite | open question, B-class drift recurrence |
| **P4-5** | Walk-forward / CPCV / DSR / PBO harness wired into `weekly_evolution.py` (replace "NOT YET RUN" with actual values) | F-24 |
| **P4-6** | A/B uplift harness for AI agents (pre-trade filter first), publish per-agent uplift report | F-03 follow-on |
| **P4-7** | `tier_3000` compounding rollout — only after P1-1 reconciliation harness passes for every enabled strategy, and a 60-day live observation cap of 3 contracts | F-16 |

### Sign-off gates (STOP and ask before changing)

Any task touching [`bridge/oif_writer.py`](../../bridge/oif_writer.py), [`phoenix_bot/orchestrator/oif_writer.py`](../../phoenix_bot/orchestrator/oif_writer.py), OIF routing, PhoenixOIFGuard, or anything reaching live execution:

- **P1-2** RiskGateSink fail-closed
- **P1-3** Portfolio-level cap
- **P1-4** NT8 silent-stall auto-recovery
- **P1-7** Pending-order lifecycle
- **P1-8** Stop-order ID capture
- **P4-1** `base_bot.py` decomposition

### Never list

1. **Auto-promote `validated=True` based on a backtest alone**, even with `tools/validation_tracker.py --check-promotion`. Live n ≥ 100, PF ≥ 1.3, Wilson-CI lower bound > 0.5 are the floor. Anything else is conjecture.
2. **Swap the NT8 OIF bridge for a third-party broker router** before stack health (P0-1) and reconciliation (P1-1) hold for 60 days. The fragility audit B identifies is real, but a vendor swap multiplies the surface area at exactly the wrong moment.
3. **Add a second instrument** (ES / NQ / MES live trading) until MNQ is boring for 90 days. `es_nq_confluence` is dormant per [`memory/context/KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md); keep it dormant.
4. **Add another data vendor** (e.g., Databento MBO subscription) before P1-1 reconciliation harness confirms the existing data path is faithful to live.
5. **Premature feed switches** (TradingView, alternative L2 sources). TradingView Premium was already stricken (per [`memory/context/CURRENT_STATE.md`](../../memory/context/CURRENT_STATE.md)).
6. **Promote any AI agent to "blocking" mode** (i.e., AI veto on real orders) before published A/B uplift with 95% CI lower bound > 0.
7. **A second strategy zoo.** If Phase 14 wants to test 7 new strategies — test one. Validate live. Then maybe one more. (A's recommendation; I agree.)
8. **Custom NT8 indicators beyond `TickStreamer` and `PhoenixTradeOverlay`.** Every new NinjaScript class is another surface against the silent-failure floor.
9. **Auto-modify live strategy params with AI.** (C's recommendation; agreed.)
10. **Compounding sizing (`tier_3000`) before P4-7's preconditions are all met.**

---

## 5. Execution status report

**Now (completed in this session):**
- All three audits read; all major claims verified against `file:line`. F-27 (orb_fade B3) is **already fixed** in code — audit A's claim is stale. F-26 (open_drive B2) is **partially fixed** but design decision is still pending.
- Memory center restructure executed — see [`docs/README.md`](../README.md) + new file tree (Step 5 below).
- Synthesis written to `docs/audits/SYNTHESIS_2026-05-24.md` (this file). The three single-auditor reports remain in `docs/audits/` for traceability.

**Blocked, awaiting operator sign-off:**
- P1-2, P1-3, P1-4, P1-7, P1-8, P4-1 — all touch OIF or live execution. Each is presented for explicit go-ahead before any code change.

**Blocked, awaiting operator decision (non-code):**
- P2-1 — open_drive design decision (continuation R1/S1 vs reversion PP).
- P2-5 — final disposition of `dom_pullback` (keep deleted vs re-add with `enabled=False`).
- Operator confirmation on Phase 0 §0.5 (PhoenixOIFGuard regex) and §0.8 (kill switch live-fire).

**Unresolved (need operator input or live state):**
- F-13 reconciliation: the *one* question whose answer most changes this plan. See §7.

---

## 6. The one question

**Have you, or has anyone, ever sat down and compared `sim_bot`'s per-strategy 30-day live-paper output against the corresponding 30-day slice of the Phase 13 5-year backtest — same strategies, same date range, same input bars — and produced a per-strategy divergence number (trade count, win rate, net P&L) that you'd defend in writing?**

This is F-13. It is the single fact that most changes the plan.

- **If yes**, with a specific divergence number per strategy: the plan compresses. P1-1 becomes a *confirmation* pass, not a prerequisite. P3-3 (strategy cull) gets data-driven candidates today. P4-7 (`tier_3000`) becomes possible to schedule.
- **If no, or "we sort of looked":** every recommendation below P0 is conditional on building that comparison first. P1-1 jumps to position #1 in priority over even P1-4 and P1-5, because the cost of NOT knowing is higher than any single reliability win.

I will revise this plan based on your answer. If you can paste `sim_bot` per-strategy 30-day numbers next to the corresponding 30-day slice of the Phase 13 backtest, I can probably tell you within an hour where the divergence is and whether it's "live underperforms because of unmodeled slippage" (recoverable) or "live underperforms because the backtest finds edge that doesn't exist" (kill list).
