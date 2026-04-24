# Phoenix Bot — Recent Changes

_Dated log of what's been changed, by whom, why. Newest first._
_Auto-appended by `tools/memory_writeback.py` via SessionEnd hook._

---

### 2026-04-24 12:20 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 12:17 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:56 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:55 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:55 Central Daylight Time — Session changes: 11 files modified

**Files changed:**
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-24 05:51 Central Daylight Time — Session changes: 12 files modified

**Files changed:**
- `bots/base_bot.py`
- `bots/sim_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`
- `core/startup_reconciliation.py`
- `dashboard/templates/dashboard.html`
- `launch_all.bat`
- `ninjatrader/PhoenixOIFGuard.cs`
- `strategies/bias_momentum.py`
- `strategies/dom_pullback.py`
- `tests/test_oif_filename_tagging.py`

---
### 2026-04-22 14:25 Central Daylight Time — Session changes: 3 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`

---
### 2026-04-22 14:22 Central Daylight Time — Session changes: 3 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`

---
### 2026-04-22 14:22 Central Daylight Time — Session changes: 3 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `core/position_manager.py`

---
## 2026-04-21 late evening — B41 TIF fix + B40 diagnosis + dashboard cleanup

**Critical production bug discovered during first sim_bot live trading:**

### B39/B40/B41 — silent phantom positions traced to TIF mismatch
- **B39 symptom**: Python PositionManager showed `active_positions=1` while
  NT8 showed no active trade on the routed Sim account. OIF files sitting
  unconsumed in `incoming/`.
- **B40 initial misdiagnosis**: suspected NT8 ATI multi-account config
  issue. Tried kill-switch `MULTI_ACCOUNT_ROUTING_ENABLED=False` — user
  rejected. Reverted.
- **B41 ROOT CAUSE** (from NT8 Log tab): `'time in force parameter not
  supported by this account "DAY"'`. Entry orders used TIF=DAY which the
  "My Coinbase"-style 24/7 connection rejects. Stops/targets already used
  GTC and were landing — so bracket orders were half-submitted.
- **Fix (commits `65cc9d6` + `66a8e7a`)**: change all OIF TIFs from DAY
  to GTC — universally accepted per NT8 docs
  (https://ninjatrader.com/support/helpguides/nt8/timeinforce.htm).
  Paths fixed: `_build_entry_line`, `CLOSEPOSITION`, `PARTIAL_EXIT_LONG/SHORT`.
- **End-to-end validation**: direct OIF injection to `SimBias Momentum`
  (18:59 CDT) and `SimVwap Band Pullback` (19:02 CDT) — both FILLED.
  Position files confirmed: `LONG;1;26741.25` → flatten → `FLAT;0;0`.

### Dashboard cleanup (commits `97fdf98` + `46dd214`)
- Added Sim Bot health pill + tab to dashboard UI (fixed watchdog
  auto-restart path that was 400-rejecting `name=sim`)
- Removed retired Lab Bot from UI + `/api/bot/start|stop|status`
- `_state` dict + `/api/status` now include sim, exclude lab
- Watchdog docstring updated (default was already prod,sim)

### STRATEGY_KEYS parent aliases (commit `b8b505e`)
- Added `compression_breakout` + `opening_session` as parent-key aliases
  in `core/strategy_risk_registry.STRATEGY_KEYS` to suppress the
  `[RISK] unknown strategy key` warnings on every eval. Strategies
  register under bare names; registry needed the aliases.

### Sim banner clarity (commit `94ee5f5`)
- `[SIM] 10 strategies → 16 account destinations loaded` instead of
  just `10 strategies loaded`. `opening_session` dispatches to 6 subs;
  `compression_breakout` has 15m + 30m timeframes. All 16 destinations
  confirmed tracked by StrategyRiskRegistry.

### AI agent activation status
- **Gemini Flash + Gemini Pro agents (Council, Pretrade)**: live and
  working. Council auto-trigger wired for regime-shift only
  (commit `5c013f2`) per Jennifer decision. Pretrade hook in
  `bots/base_bot.py:1148` (commit `7788a62` + attribution `6259d90`).
- **Claude Sonnet agents (Debriefer, Learner)**: **DEGRADED** —
  `ANTHROPIC_API_KEY` is present in `.env` but empty (0 chars). Every
  Claude call today returned `outcome: degraded, error_msg:
  ANTHROPIC_API_KEY missing`. Fallback deterministic templates are
  being emitted instead of real Claude analysis. Fix requires Jennifer
  to paste a valid key into `.env`.
- **Daily learner scheduled** (Windows Task `PhoenixLearner`) runs at
  23:30 CT with `--days 7` rolling window. Will produce empty/fallback
  recommendations until Anthropic key is set.
- **Session Debriefer**: first real run fired at 17:22:56 then 18:19:43.
  Both wrote `logs/ai_debrief/2026-04-21.md` with the deterministic
  fallback template. Telegram dispatch is working.
- **Adaptive Params Telegram**: now accepts either `TELEGRAM_BOT_TOKEN`
  or `TELEGRAM_TOKEN` (commit `a7283fd`).

### Open follow-ups
- Paste valid `ANTHROPIC_API_KEY` into `.env` to unlock Claude-powered
  Session Debrief + Weekly Learner. Gemini agents are unaffected.
- `[INTERMARKET] Feed error: float() argument must be a string or a real
  number, not 'dict'` — pre-existing unrelated bug, not a Phases E–H
  regression. Logged for separate ticket.
- Update `data/menthorq_daily.json` daily per `docs/daily_ritual.md`.
  Currently 104h+ stale → regime fields using HVL proxy.

---

## 2026-04-21 evening — Phases E/F/G/H sprint merged to main

**Branch:** `feature/phases-e-h` → merged to `main` (merge commit `bdff605`).
**Test suite:** 566 passing + 6 failing → **700 passing / 0 failing**.

### Phase E — Gamma integration
- `docs/daily_ritual.md` — documents daily MenthorQ paste (both `menthorq_daily.json` + `gamma/*_levels.txt`)
- `core/menthorq_feed.py` — CRITICAL log if `menthorq_daily.json` >24h stale; parser hardened (`_coerce_float` handles empty/NaN/None/negative-zero) — this was G-B26
- `core/structural_bias.score_menthorq_gamma` — rewired Path A → Path B (reads `market_snapshot["gamma_regime"]` enum directly; retired stale-JSON read). Overclaiming warning corrected to list only real consumers.

### Phase F — B15 test backlog cleared
All 6 previously-failing tests now green. All were test-stale (B13 commission math, B13-era 3→2 cooloff threshold, 08:30–11:00 window, new `_REGIME_OVERRIDES` schema). Zero code regressions.

### Phase G — Bug fixes
- **B37**: new `tests/test_4c_integration.py` (12 tests, real routing map + OIF round-trip)
- **B38**: `core/history_logger.log_eval` now emits `gamma_regime` as first-class field

### Phase H — AI agent stack
Five agents + infrastructure:
- **Infra**: `agents/base_agent.py` (AIClient + BaseAgent + safe_call + JSONL logger), `agents/config.py`. Degraded mode on missing API key — bot never crashes.
- **4A Council Gate** (`agents/council_gate.py`) — 7 Gemini Flash voters + Gemini Pro orchestrator. Auto-trigger: **regime-shift only** (15-min debounce). Session-open fire dropped per Jennifer.
- **4B Pre-Trade Filter** (`agents/pretrade_filter.py`) — single Gemini Flash call, 3s timeout, fail-OPEN to CLEAR. All strategies default `ai_filter_mode="advisory"`. Hook in `bots/base_bot.py` ~L1148.
- **4C Session Debriefer** (`agents/session_debriefer.py`) — Claude Sonnet post-16:00-CT flatten, writes `logs/ai_debrief/YYYY-MM-DD.md`. Hook in `bots/sim_bot.py::_maybe_run_debrief`. Optional Telegram dispatch.
- **4D Historical Learner** (`agents/historical_learner.py` + `tools/run_weekly_learner.py`) — weekly Claude aggregate, writes `logs/ai_learner/weekly_*.md` + `pending_recommendations.json`.
- **4E Adaptive Params** (`agents/adaptive_params.py` + `tools/approve_proposal.py` + `tools/list_proposals.py`) — safety-bounded (risk_per_trade ≤$100, stops 4-200 ticks, no gate disables, no account_routing edits, no LIVE_TRADING flips). **Never auto-applies.** Always CLI → git branch → human merge. Telegram fires on new proposal count.

### Decisions (Jennifer 2026-04-21 evening):
- Council auto-trigger: **B only** (regime-shift). A (8:30 session-open) dropped.
- Pretrade filter does **NOT** consume council bias — would over-tighten and reduce trade count. Reverted commit `6549900`.
- Telegram fires on proposal creation. **Yes.**

### Docs shipped
- `docs/phase-eh-deployment.md` — runbook
- `docs/phase-eh-report.md` — end-of-sprint summary with assumption log
- `docs/phase-eh-assumptions.md` — ~20 judgment calls across 9 streams
- `docs/daily_ritual.md` — morning MenthorQ paste ritual

### Bot state unchanged
Sim bot still running from Phase C flip (PID 46996, 24/7). **Agent layer not yet activated on live sim** — requires API keys in `.env` and a bot restart.


---

### 2026-04-21 15:40 Central Daylight Time — Phase C sprint: Lab → Sim live flip

**Scope:** Transform lab_bot (paper-only) into sim_bot (live NT8 sim trading
on 16 dedicated sub-accounts, 24/7, per-strategy risk isolation). Merged
feature branch `feature/knowledge-injection-systems` → `main` at `4f444eb`.

**Commits landed (on feature, then merged to main):**
- `f5ee73f` — byte-exact NT8 account-name fix + compression 15m/30m split + top-level orb
- `33e5ad6` — test_account_routing updated to byte-exact names (34/34 pass)
- `e460bd2` — **PositionManager multi-position refactor** (dict storage keyed by trade_id,
  back-compat single-position API preserved, new `active_positions` / `is_flat_for(strategy)` /
  `check_exits_all()` / `close_all()` methods)
- `634bfe9` — **StrategyRiskRegistry** + Phase C settings constants
  (`PER_STRATEGY_ACCOUNT_SIZE=$2000`, `PER_STRATEGY_DAILY_LOSS_CAP=$200`,
  `PER_STRATEGY_FLOOR=$1500`, 16 strategy keys, halt persistence to
  `logs/strategy_halts.json`, 24/24 tests)
- `03687ef` — **sim_bot.py** + **daily_flatten.py** + **reenable_strategy.py** CLI
  (5 files, +989 lines, 20 new tests)
- `d6d318f` — multi-position tick-exit loop in base_bot + watchdog `--bots prod,lab,sim`
  + `docs/phase-c-deployment.md` (187 lines operator playbook)
- `d4fb979` — **Phase C follow-ups** (3): dashboard per-strategy risk panel
  (`/api/strategy-risk` + sortable table + halt highlighting),
  Telegram per-strategy routing (`TELEGRAM_STRATEGY_CHAT_OVERRIDES` + auto-tag,
  8 tests), base_bot rider/smart-exit/EoD/chandelier/managed iteration over
  active_positions (multi-position correctness)
- `4f444eb` — **Merge to main** (unrelated histories, --theirs strategy;
  feature content wins, 234 files, 66,988 insertions)

**Operational flip (15:38 CDT):**
- Killed lab_bot PIDs 42188 + 37424
- Killed old watchdog PIDs 41988 + 40408
- Started `python bots/sim_bot.py` → banner confirms 10 strategies loaded,
  LIVE execution, $2000/$200/$1500 limits, 16:00 CT flatten, 16 registry keys
- Started `python tools/watchdog.py` → tracking prod + sim (lab dropped from
  default --bots list since lab is deprecated)
- Bridge + prod + dashboard untouched throughout

**Test suite delta:**
- Baseline pre-sprint: 513 pass / 6 B15-backlog
- Post-sprint: 566 pass / 6 B15-backlog (+53 new tests, zero new failures)

**Files changed (high signal):**
- `bots/sim_bot.py` (NEW, 545 lines)
- `bots/daily_flatten.py` (NEW, 96 lines)
- `bots/base_bot.py` (multi-position iteration for rider/smart/EoD/chandelier/managed)
- `bots/lab_bot.py` (preserved on disk — rollback safety net)
- `core/strategy_risk_registry.py` (NEW, 261 lines)
- `core/position_manager.py` (dict-based multi-position, +286 lines)
- `core/telegram_notifier.py` (per-strategy routing + tagging)
- `core/history_logger.py` (unchanged — sim writes to `_sim.jsonl` via `bot_name="sim"`)
- `config/settings.py` (Phase C constants + Telegram overrides)
- `config/account_routing.py` (byte-exact names + split + top-level orb)
- `dashboard/server.py` (+`/api/strategy-risk` endpoint)
- `dashboard/templates/dashboard.html` (per-strategy panel)
- `tools/reenable_strategy.py` (NEW, 87 lines)
- `tools/watchdog.py` (tracks sim by default)
- `docs/phase-c-deployment.md` (NEW, 187 lines)
- 5 new test files: `test_strategy_risk_registry.py`, `test_daily_flatten.py`,
  `test_reenable_strategy_tool.py`, `test_telegram_routing.py`, position_manager
  tests expanded for multi-position invariants

---

### 2026-04-19 20:28 Central Daylight Time — Session changes: 8 files modified

**Files changed:**
- `bots/base_bot.py`
- `bridge/oif_writer.py`
- `ninjatrader/SiM_TickStreamer.cs`
- `ninjatrader/TickStreamer.cs`
- `strategies/base_strategy.py`
- `strategies/compression_breakout.py`
- `strategies/vwap_pullback.py`
- `tools/verification_2026_04_18/SESSION_2026_04_19.md`

---
### 2026-04-18 15:01 Central Daylight Time — Session changes: 14 files modified

**Files changed:**
- `agents/council_gate.py`
- `agents/expert_knowledge.py`
- `agents/pretrade_filter.py`
- `agents/session_debriefer.py`
- `bots/lab_bot.py`
- `bridge/bridge_server.py`
- `bridge/oif_writer.py`
- `config/strategies.py`
- `dashboard/templates/dashboard.html`
- `requirements.txt`
- `strategies/base_strategy.py`
- `strategies/ib_breakout.py`
- `strategies/spring_setup.py`
- `strategies/vwap_pullback.py`

---
### 2026-04-17 22:29 Central Daylight Time — Weekend evaluation complete. KEY FINDING: 697 live trades = 33.3% WR + negative P&L -,227.68. The architectural rebuild gave us the TOOLS to find strategy problems but did not SOLVE them. Scheduled 20/80 fix week for Apr 20-22 (replayable strategies + CI + decay alerts). Recommended LIVE_TRADING stays False until 90-day replay proves positive expectancy.

**Files changed:**
- `memory/context/EVALUATION_2026-04-18.md`

**Decisions:**
- Sim Option A verified harness deterministic + MC convergence
- Sim Option B surfaced THE critical finding: 33% WR with 5:1 config but net losing = ema_exit cutting winners short
- Option C 90-day sim deferred pending replayable strategy refactor
- 20/80 fix week scheduled Apr 20-22 with specific tasks per evening
- Recommendation: LIVE_TRADING=False indefinitely until 90-day replay validates a real strategy

---
### 2026-04-17 22:19 Central Daylight Time — Wire-up COMPLETE: all Saturday+Sunday modules now RUN in base_bot shadow mode. SwingState+VolumeProfile+ReversalDetector+SweepWatcher+GammaFlipDetector+Footprint1m/5m+DecayMonitor+TCA+CircuitBreakers all instantiated. Tick handler feeds footprint+volume profile+tick rate detector. 5m bar close feeds swing/climax/sweep/gamma flip/footprint. _evaluate_strategies computes full structural_bias composite every cycle with MenthorQ+VIX+OpEx+ES+Pinning enrichment. Dashboard endpoints (/api/structural-bias, /api/gamma-context, /api/footprint, /api/risk-mgmt, /api/all-signals) returning real data. Bots restarted clean, 0 errors, 61/61 tests still passing. Monday open = real shadow data flowing.

**Files changed:**
- `bots/base_bot.py`

**Decisions:**
- User pushed back on deferring wire-up: correctly pointed out shadow observation requires modules to RUN
- Wire-up adds ~220 lines to base_bot.py, all try/except guarded so shadow errors cannot break live trading
- All 13 new modules instantiated + hooked into proper lifecycle points
- Composite structural_bias runs every _evaluate_strategies cycle with full reasoning trail
- Dashboard API endpoints now return live data (confirmed via curl)
- Bots UP clean no errors watchdog healthy MQ flowing real values
- 2-week shadow observation window NOW STARTED (not delayed to future session)
- April 25 validation review has real data to work with

---
### 2026-04-17 22:11 Central Daylight Time — Sunday build COMPLETE: 6 new Sunday modules (footprint_builder, footprint_patterns, pinning_detector, opex_calendar, es_confirmation, structural_bias composite) + 6 dashboard API endpoints + 20 unit tests (61 total passing) + MONDAY_READINESS.md report. All signals shadow mode. Monday ships foundation fixes live (Telegram HTML, MQBridge running, hooks, memory system, contract rollover, emergency halt) while structural_bias/footprint/patterns run alongside old tf_bias for 2 weeks of shadow observation before activation.

**Files changed:**
- `bridge/footprint_builder.py`
- `core/footprint_patterns.py`
- `core/pinning_detector.py`
- `core/opex_calendar.py`
- `core/es_confirmation.py`
- `core/structural_bias.py`
- `dashboard/server.py`
- `tests/test_sunday_modules.py`
- `memory/context/MONDAY_READINESS.md`

**Decisions:**
- Task 1 complete: footprint pipeline reads existing tick stream no NT8 changes needed
- Task 2 gamma flip detector Saturday skeleton kept as-is requires live wiring (integration deferred)
- Task 3 complete: pinning detector last 90 min RTH + 0DTE strike proximity + breach detection
- Task 4 complete: OpEx calendar 3rd Friday detection + Triple Witching rules
- Task 5 complete: ES confirmation via manual daily file NQ vs ES gamma alignment
- Task 6 complete: structural_bias composite integrates 12 components with full reasoning trail
- Task 7 complete: dashboard API endpoints added (JSON ready html widgets can come later)
- Task 8 complete: WFO validation baseline 10.7pct risk of ruin break-even WR 47.6pct
- Task 9 complete: 61 tests passing MONDAY_READINESS.md written
- All new signals REMAIN SHADOW MODE for 2 weeks minimum before strategy gate activation
- April 25 session: reflector + strategy concentration review + Kelly activation gate

---
### 2026-04-17 19:17 Central Daylight Time — Saturday build complete: 11 new core modules + 3 procedural YAMLs + emergency halt tool + 41 passing unit tests. Signal foundation (swing ATR-ZigZag, volume profile POC/HVN/LVN/VAH/VAL + TPO-lite, climax reversal with mandatory secondary-test entry, liquidity sweep detector). Risk management (decay monitor rolling 30d Sharpe, TCA tracker with slippage analysis, anomaly circuit breakers with observe-mode default). Chart patterns v1 wrapper with context weighting (bull/bear flag + H&S/inverse H&S from existing detector). VIX term structure (CBOE-ready interface, yfinance fallback). Gamma flip detector skeleton. Session tagger for lab 24/7. Emergency halt tool. All modules dual-write mode or shadow, no live wiring yet.

**Files changed:**
- `memory/procedural/small_account_config.yaml`
- `memory/procedural/regime_matrix.yaml`
- `memory/procedural/regime_params.yaml`
- `core/swing_detector.py`
- `core/volume_profile.py`
- `core/reversal_detector.py`
- `core/liquidity_sweep.py`
- `core/strategy_decay_monitor.py`
- `core/tca_tracker.py`
- `core/circuit_breakers.py`
- `core/chart_patterns_v1.py`
- `core/vix_term_structure.py`
- `core/gamma_flip_detector.py`
- `core/session_tagger.py`
- `tools/emergency_halt.py`
- `tests/test_new_modules.py`

**Decisions:**
- Saturday 2F YAML configs complete small_account + regime_matrix + regime_params codify 60pct WR target
- Saturday 2B signal foundation complete: swing detector ATR-ZigZag volume profile climax secondary-test liquidity sweep
- Saturday 2A risk mgmt complete: decay monitor TCA tracker circuit breakers all shadow mode
- Saturday 2C chart patterns v1 uses existing 745-line detector with context weighting wrapper
- Saturday 2D VIX term structure CBOE primary yfinance fallback CBOE plug-in ready when credentials available
- Saturday 2G gamma flip skeleton pending Sunday integration with regime_matrix reload
- Saturday 2E lab bot parity via session_tagger module ASIA LONDON US_PRE US_RTH US_CLOSE PAUSE
- Emergency halt tool creates memory .HALT marker circuit breakers detect on next check ~5s
- 41 unit tests all passing 0.137s 13 modules covered minimum 3 tests each
- Everything remains shadow mode Sunday ties it all together with composite bias + WFO validation

---
### 2026-04-17 19:04 Central Daylight Time — Friday Session 1 complete: MQBridge deployed + verified (55 draw objects, real levels flowing), Telegram HTML fix, memory architecture scaffolded with atomic writes + hooks, NT8 arrow + contract rollover + Level 2 all diagnosed, git tag v-pre-rebuild-2026-04-17 + rollback runbook, WFO replay harness (multi-window + Monte Carlo + cost model), simple_sizing.py with loss-streak cooldown, bias_momentum hotfix VERIFIED working, BOM fix for utf-8-sig MQ bridge file read, bots restarted cleanly with real MQ values flowing

**Files changed:**
- `core/telegram_notifier.py`
- `core/menthorq_feed.py`
- `core/simple_sizing.py`
- `core/contract_rollover.py`
- `config/settings.py`
- `tools/memory_writeback.py`
- `tools/replay_harness.py`
- `memory/context/CURRENT_STATE.md`
- `memory/context/RECENT_CHANGES.md`
- `memory/context/KNOWN_ISSUES.md`
- `memory/context/OPEN_QUESTIONS.md`
- `memory/context/ROLLBACK_RUNBOOK.md`
- `memory/semantic/lessons_learned.md`
- `memory/procedural/targets.yaml`
- `memory/procedural/strategy_params.yaml`
- `memory/audit_log.jsonl`
- `~/.claude/settings.json (hooks)`
- `~/.claude/projects/C--Trading-Project/memory/MEMORY.md`

**Decisions:**
- All 10 Friday Tier 1 items complete
- MQBridge verified with 55 draw objects writing today real MQ levels
- Telegram now HTML instead of Markdown fixes 22 of 29 dropped lab messages
- Memory architecture operational with atomic writes file lock audit log
- Hooks installed: SessionStart auto-loads memory SessionEnd auto-writeback Stop checks pending
- Git tag v-pre-rebuild-2026-04-17 created as rollback baseline
- WFO harness tested: placeholder strategy 50% WR 10.8% risk of ruin correctly identifies overfitting OOS
- simple_sizing uses fixed 1-contract 80 conviction threshold 5 min loss-streak cooldown
- bias_momentum hotfix verified 0 errors 109 clean rejections correct gates firing
- BOM fix for utf-8-sig MenthorQ bridge file fixed zero-values bug at restart

---
### 2026-04-17 18:43 Central Daylight Time — Test write — memory architecture bootstrap

**Files changed:**
- `core/telegram_notifier.py`
- `memory/context/CURRENT_STATE.md`

**Decisions:**
- Switched Telegram to HTML parse mode
- Memory architecture scaffolded

---
## 2026-04-17 — Friday rebuild Session 1 (in progress)

### 17:30 CDT — Telegram notifier: Markdown → HTML

**What:** `core/telegram_notifier.py` converted from `parse_mode="Markdown"` to `parse_mode="HTML"`. All 5 formatters (entry, exit, daily summary, council, alert) updated to use `<b>` and `<code>` HTML tags instead of `*bold*` / `` `code` ``.

**Why:** 22 of 29 lab trade messages today were silently dropped by Telegram API returning 400 "can't parse entities" on underscores in strategy names (e.g., `bias_momentum`, `high_precision_only`). Created survivorship bias — user saw winning trades but not losing ones.

**Effect:** Next bot restart (tonight at session close) — all trade notifications will be delivered reliably.

### 17:20 CDT — MQBridge.cs redeployment instructions delivered

**What:** Diagnosed that `MQBridge.cs` source exists at `C:\Trading Project\phoenix_bot\ninjatrader\MQBridge.cs` but is NOT installed in NT8 Indicators folder (`C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\bin\Custom\Indicators\`). Walked user through NT8 NinjaScript Editor reinstall procedure.

**Why:** `C:\temp\menthorq_levels.json` has not been updated by NT8 since 2026-04-15 — indicator was removed/uninstalled from NT8. Every morning since has used stale gamma levels.

**Status:** User deploying. Verify timestamp updates post-install.

### 11:09 CDT — Hotfix: bias_momentum missing `price` and `vwap` variables

**What:** Added 2 lines in `strategies/bias_momentum.py` (near line 66):
```python
price = market.get("close", 0.0)
vwap = market.get("vwap", 0.0)
```

**Why:** Variables referenced throughout the method but only defined inside the non-TREND `else` branch. On TREND days, code crashed with `NameError: name 'price' is not defined`. Had been crashing continuously since the bot started today.

**Effect:** Bot back to operational. Zero signals fired in secondary window 13:00-14:30 but also zero errors — either no qualifying setups or needs further investigation (see `OPEN_QUESTIONS.md`).

### 11:07 CDT — Manual write of MQ levels for today

**What:** Directly wrote today's MenthorQ values to `C:\temp\menthorq_levels.json`:
- HVL 25,290, CR 26,500, PS 24,000, Day range 26,172-26,802
- GEX 1-10 levels from today's dashboard analysis

**Why:** NT8 MQBridge indicator not running (see above). Bot needs today's regime to operate properly.

**Status:** Temporary workaround. Permanent fix is MQBridge redeployment.

---

## Earlier entries will be appended above this line as SessionEnd hook runs.
