# Phoenix Bot — Recent Changes

_Dated log of what's been changed, by whom, why. Newest first._
_Auto-appended by `tools/memory_writeback.py` via SessionEnd hook._

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
