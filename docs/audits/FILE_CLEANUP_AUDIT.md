# Phoenix File Cleanup Audit — Post-Phase-13

**Date:** 2026-05-19
**Author:** Cleanup audit agent (post-Phase-13)
**Scope:** Enumerate every file under `phoenix_bot/` after Phase 13's research
explosion, classify CURRENT vs RESEARCH-ARCHIVE vs OBSOLETE vs CRUFT, apply
high-confidence cleanups, document medium-confidence proposals for operator
review.

**Operator's stated concern (paraphrase):**
> "I don't want them [the docs/tools/artifacts] getting confused."

This audit's goal is to make the boundary between CURRENT and HISTORICAL
visible at a glance, so future sessions and the operator both know what's
load-bearing vs what's just kept-for-reference.

---

## TL;DR — what changed in this audit

**Header labels added (committed):**
1. `docs/PHOENIX_BEST_PLAN.md` → `[CURRENT — SHIP PLAN]` block at top.
2. `docs/PHASE_13_IMPLEMENTATION_PLAN.md` → `[RESEARCH ARCHIVE — historical context]` block at top.
3. `docs/STRATEGY_DEEP_DIVE_2026-05-18.md` → `[RESEARCH ARCHIVE — superseded]` block at top.

**Disk-only cruft deleted (NOT in git — no `git rm` needed):**
- `backtest_results/_smoke_exit.csv` (16 KB)
- `backtest_results/_smoke_exit_summary.csv` (543 B)
- `backtest_results/_smoke_orb_v2.csv` (257 B)
- `backtest_results/phoenix_real_v1.csv` (1 KB, throwaway pre-real-pipeline run)

**Nothing tracked was deleted.** All "delete" actions were against gitignored
artifacts that exist only on disk. This audit is strictly conservative — see
"Proposals NOT applied" below for medium-confidence items deferred to operator.

---

## 1. Classification table

### 1.1 PRODUCTION (live code — keep, do not touch)

| Path | What it is |
|---|---|
| `bots/` | base_bot, prod_bot, sim_bot, lab_bot, daily_flatten |
| `bridge/` | bridge_server, oif_writer, footprint_builder, tradingview_webhook |
| `core/` | risk_manager, session_manager, exit_policies, entry_modes, etc. (~80 files) |
| `strategies/` | 22 strategy modules + base_strategy + `_nq_stop` helper |
| `config/` | settings, strategies, account_routing, regime_matrix, instrument_price_bands |
| `dashboard/` | Flask app |
| `ninjatrader/` | TickStreamer.cs + MarketDataBroadcasterV3.cs |
| `agents/`, `analysis/`, `data_feeds/`, `models/` | Supporting subsystems |
| `requirements.txt`, `pytest.ini`, `main.py`, `langgraph.json` | Top-level production glue |
| `KillSwitch.bat`, `PhoenixStart.bat`, `launch_*.bat` | Operator daily-driver scripts |
| `CLAUDE.md` | Project context file |

### 1.2 CURRENT RESEARCH (active references — keep, leave alone)

| Path | What it is | Why CURRENT |
|---|---|---|
| `docs/PHOENIX_BEST_PLAN.md` | **Canonical ship plan** | Now labeled `[CURRENT — SHIP PLAN]` |
| `docs/PHASE_13_IMPLEMENTATION_PLAN.md` | Full research record | Now labeled `[RESEARCH ARCHIVE]`, but actively cited from PHOENIX_BEST_PLAN.md |
| `docs/BUGS_AND_TODOS.md` | Running issue list | Created 2026-05-20 per operator instruction |
| `docs/TICK_LEVEL_EXIT_VERIFICATION.md` | Section U.1 source | Cited by PHOENIX_BEST_PLAN.md exit policy column |
| `docs/TICK_LEVEL_ENTRY_VERIFICATION.md` | Section U.2 source | Cited by PHOENIX_BEST_PLAN.md entry mode column |
| `docs/ENTRY_RETEST_ANALYSIS.md` | Section V.1 source | Cited (retest mode) |
| `docs/EARLY_REVERSAL_EXIT_ANALYSIS.md` | Section V.2 source | Cited (negative finding kept) |
| `docs/SR_ZONE_STRATEGY.md` | Section V.3 source | Cited (negative finding kept) |
| `docs/SR_VETO_BIAS_MOMENTUM.md` | Sprint A source | Cited |
| `docs/SR_CONFLUENCE_SPRING_SETUP.md` | Sprint B source | Cited |
| `docs/FAILED_HOLD_STRATEGY.md` | Sprint C source | Cited |
| `docs/obi_feature/` (DECISIONS.md, DESIGN.md, PHASE_0A.md, README.md) | OBI feature design (not started) | Forward-looking work |
| `docs/STRATEGY_DEEP_DIVE_2026-05-18.md` | Initial deep-dive | Now labeled `[RESEARCH ARCHIVE — superseded]`, but kept because it introduced the CSV-backed pipeline + several Phase 13 verdicts |
| `docs/architecture.html` | Live architecture diagram | Loaded by dashboard |
| `docs/daily_ritual.md` | Operator daily checklist | Active |
| `docs/oif_killswitch_runbook.md` | Operator runbook | Active |
| `docs/chicago_vps_migration_plan.md` | Forward-looking plan | Active |
| `docs/tradingview_webhook_setup.md` | Operator setup doc | Active |
| `docs/PHASE9_DEPLOY_STATUS.md` | Phase 9 wrap-up | Recent (May 17) deployment record |
| `docs/STRATEGY_SPECIFICATIONS.md` | Per-strategy spec | Section Q deliverable |
| `docs/DATABENTO_FOOTPRINT_WALKTHROUGH.md` | Databento workflow doc | Active |
| `docs/exit_methodology_per_strategy.md` | Exit-policy reference | Recent |
| `tools/phoenix_real_backtest.py` | Real CSV-backed Phoenix backtest | Primary backtest tool |
| `tools/phoenix_compounding_backtest.py` | Compounding engine | Section I primary deliverable |
| `tools/phoenix_new_strategy_lab.py` | A-G strategy lab | Source for 3 new winners (asian_continuation, multi_day_breakout, inside_bar_breakout) |
| `tools/phoenix_*` lab files (15+) | Each tied to a Phase 13 section | Re-runnable; CSV outputs gitignored |
| `tools/tbbo_cache_builder.py` | Canonical tick cache builder | Authoritative — see `data/historical/databento_tbbo/README.md` |
| `tools/exit_methodology_v3.py`, `tools/backtest_v3_sweep.py` | Phase 12C ES/NQ sweep | Their CSV outputs are CITED in `strategies/es_nq_confluence.py` docstring |
| `tools/memory_writeback.py`, `tools/validation_tracker.py`, `tools/daily_session_summary.py` | Daily operator workflow tools | CURRENT — listed in CLAUDE.md |
| `tools/routines/`, `tools/graders/`, `tools/log_parsers/` | Sub-packages for routines | Active |
| `tools/window_layout.json`, `tools/*.ps1` | Operator desktop helpers | Active |
| `tools/verification_2026_04_18/` | Trade-plan + test bundle | Reference — kept |
| `memory/` (entire tree) | Memory subsystem | Active per CLAUDE.md |

### 1.3 ARCHIVE (kept-for-reference, already in archive directories)

| Path | What it is |
|---|---|
| `archive/pre_load_sigma_open/` | Sigma-open warmup pre-load artifacts (referenced by dashboard) |
| `archived/menthorq_2026-05-05/` | MenthorQ data — retired in Sprint J, archived for reference |
| `Phoenix Rising Project/` | Older project dir, gitignored |

### 1.4 ARCHIVE-WORTHY (label or move proposals — NOT applied)

These are tracked, legacy, but still serve as historical context. They should
get an `[ARCHIVE]` header note OR move to `docs/archive/` subdir. Listed
for operator decision:

| Path | Reasoning | Proposed action |
|---|---|---|
| `BUILD_MAP.md` (root) | 52 KB Apr 22 architectural map | Add `[HISTORICAL]` note; or move to `docs/archive/` |
| `PHOENIX_ROADMAP_v4.md` (root) | Apr 21 v4 roadmap, superseded by PHOENIX_BEST_PLAN.md | Add `[HISTORICAL]` note OR delete |
| `phoenix_action_plan_v2_post_migration.md` (root) | Apr 21 plan | Add `[HISTORICAL]` note OR delete |
| `REBUILD_PLAN.md` (root) | Apr 21 rebuild plan | Add `[HISTORICAL]` note OR delete |
| `PROJECT_EXPORT_PROMPT.md`, `PHOENIX_PROJECT_PROMPT.md`, `STRATEGY_KNOWLEDGE_INJECTION_PROMPT.md`, `Epic Update v1 Prompt.md` | Old Anthropic.ai handoff prompts | Move to `docs/archive/handoff_prompts/` |
| `AI Trading Analysis System Research.md` (root) | Apr 21 background research | Move to `docs/archive/` |
| `audit_report.md` (root) | Apr 25 audit | Move to `docs/archive/` |
| `trading_journal_2026-04-04.txt` (root) | One-off journal | Move to `docs/archive/` |
| `OPERATOR_TODO.md` (root) | Apr 25 — stale? | Verify with operator; archive if stale |
| `SCRATCH_DIRS.md`, `SKILLS.md` (root) | Project metadata | Keep at root if still used |
| `docs/ACTION_PLAN_V2_1_DELTAS.md` | V2.1 deltas — pre-Phase-13 | Add `[HISTORICAL]` note |
| `docs/TODO_RETIRE_LEGACY_REPO.md` | Legacy repo retirement | Verify if action complete; archive if so |
| `docs/exit-sprint-*.md`, `docs/final-sprint-*.md`, `docs/phase-*` (8 files) | Pre-Phase-13 sprint reports | Move to `docs/archive/sprints_2026_04/` |
| `docs/hard-audit-2026-04-22.md` | Apr 22 audit | Add `[HISTORICAL]` note |
| `docs/phase_b_plus_roadmap.md`, `docs/phase_c_architecture.md`, `docs/momentum_days.md`, `docs/opening_strategies_silence.md` | Older phase docs | Add `[HISTORICAL]` notes |
| `docs/cvd_usage_audit.md`, `docs/stop_target_math_audit.md`, `docs/target_fire_audit.md`, `docs/trailing_stop_audit.md`, `docs/guaranteed_loss_audit.md` | Older audits | Likely superseded by Section U tick-level work; check before archiving |
| `docs/nt8_multi_stream_recovery.md` | NT8 recovery note | Check if still relevant |
| `backtest_results/phoenix_real_5year_BROKEN_pre_bugfix.csv` (2.8 MB, gitignored) | Pre-bugfix BROKEN baseline | **KEEP** — explicitly cited in PHASE_13 as "kept for reference" |

### 1.5 OBSOLETE / CRUFT (proposed for cleanup)

| Path | Status | Action taken or proposed |
|---|---|---|
| `backtest_results/_smoke_exit.csv` | Disk-only, untracked | **DELETED** (this audit) |
| `backtest_results/_smoke_exit_summary.csv` | Disk-only, untracked | **DELETED** (this audit) |
| `backtest_results/_smoke_orb_v2.csv` | Disk-only, untracked | **DELETED** (this audit) |
| `backtest_results/phoenix_real_v1.csv` (1 KB) | Disk-only, untracked; zero references | **DELETED** (this audit) |
| `base_bot_check.pyc.1864215936528` (root, 213 KB) | Old .pyc remnant, gitignored | Proposed for deletion — operator decision |
| `data/historical/MNQ 06-26.Last`, `MES 06-26.Last` | NT8 raw .Last tick files, gitignored | Keep — possibly useful for investigation |
| `data/historical/databento_tbbo/mnq_ticks.parquet` (417 MB) | Legacy schema, REGENERATED from canonical | **Keep** — README explicitly documents back-compat reason; `tools/phoenix_tick_trail_verification.py` reads this schema |
| `data/historical/databento_tbbo/mnq_ticks_slim.parquet` (395 MB) | Legacy schema, REGENERATED from canonical | **Keep** — same reason; `tools/phoenix_tick_entry_quality.py` reads this schema |
| `data/historical/databento_tbbo/mnq_ticks_clean.parquet` (418 MB) | **CANONICAL** | Keep — single source of truth |

**On the "old Agent A/B parquet" question from the audit prompt:**
The README at `data/historical/databento_tbbo/README.md` makes the legacy
parquets explicit back-compat shims regenerated FROM the clean source.
They are NOT stale Agent-A/B leftovers — they are intentional dual-format
caches with documented existing consumer tools. Deleting them would break
two tick-level tools without buying any correctness. **Do not delete.**

---

## 2. Documentation hierarchy (after this audit)

```
docs/
├── PHOENIX_BEST_PLAN.md           [CURRENT — SHIP PLAN]   ← read first
├── PHASE_13_IMPLEMENTATION_PLAN.md [RESEARCH ARCHIVE]      ← full WHY
├── STRATEGY_DEEP_DIVE_2026-05-18.md [RESEARCH ARCHIVE — superseded]
├── BUGS_AND_TODOS.md              [CURRENT]               ← running issue list
├── FILE_CLEANUP_AUDIT.md          [CURRENT]               ← this file
│
├── TICK_LEVEL_EXIT_VERIFICATION.md ─┐
├── TICK_LEVEL_ENTRY_VERIFICATION.md │  Phase 13 Section U/V
├── ENTRY_RETEST_ANALYSIS.md         │  research reports.
├── EARLY_REVERSAL_EXIT_ANALYSIS.md  │  Cited by PHOENIX_BEST_PLAN.md.
├── SR_ZONE_STRATEGY.md              │  All are reference docs;
├── SR_VETO_BIAS_MOMENTUM.md         │  no header relabel needed
├── SR_CONFLUENCE_SPRING_SETUP.md    │  (PHOENIX_BEST_PLAN.md cites them
├── FAILED_HOLD_STRATEGY.md         ─┘  explicitly).
│
├── obi_feature/                    [CURRENT — design phase]
│
├── architecture.html                [CURRENT — operator-facing]
├── daily_ritual.md                  [CURRENT — operator runbook]
├── oif_killswitch_runbook.md        [CURRENT — operator runbook]
├── chicago_vps_migration_plan.md    [CURRENT — forward plan]
├── tradingview_webhook_setup.md     [CURRENT — operator setup]
├── PHASE9_DEPLOY_STATUS.md          [CURRENT — recent deploy log]
├── STRATEGY_SPECIFICATIONS.md       [CURRENT — Section Q output]
├── DATABENTO_FOOTPRINT_WALKTHROUGH.md [CURRENT — operator workflow]
├── exit_methodology_per_strategy.md  [CURRENT — exit-policy reference]
│
└── (older sprint reports, audits, V2.1 deltas — see "ARCHIVE-WORTHY" above)
```

---

## 3. New directory structure (proposed but NOT applied)

The audit prompt floated `docs/research/`, `docs/current/`, `tools/research/`,
`tools/production/`. **My recommendation: do NOT do this.**

Rationale:
1. PHOENIX_BEST_PLAN.md already serves as the single CURRENT entry point.
2. Header labels (added in this audit) cheaply solve the "which doc wins"
   problem without breaking any existing reference path.
3. Moving files breaks every cross-reference in committed docs, audit logs,
   `memory/audit_log.jsonl`, and operator muscle memory.
4. `tools/` files are flat-named with clear prefixes (`phoenix_*`, `diagnose_*`,
   `validate_*`, `backfill_*`) — that's already a soft classification.

**If reorganization happens later, do it in one atomic commit that updates all
referencing docs in the same commit.** Don't do it piecemeal.

---

## 4. Proposals NOT applied (operator decision needed)

### Medium-confidence (low blast radius)
1. Add `[HISTORICAL]` headers to root-level pre-Phase-13 plans:
   `BUILD_MAP.md`, `PHOENIX_ROADMAP_v4.md`, `phoenix_action_plan_v2_post_migration.md`,
   `REBUILD_PLAN.md`. Visible win for the operator's "I don't want to be
   confused" goal.
2. Add `[HISTORICAL]` headers to `docs/ACTION_PLAN_V2_1_DELTAS.md`,
   `docs/hard-audit-2026-04-22.md`, `docs/exit-sprint-*.md`,
   `docs/final-sprint-*.md`, `docs/phase-*.md` (group of ~8 older sprint files).
3. Delete `base_bot_check.pyc.1864215936528` (gitignored, 213 KB old .pyc).

### Lower-confidence (worth a verify pass)
4. Move root-level prompt files (`Epic Update v1 Prompt.md`,
   `PROJECT_EXPORT_PROMPT.md`, `PHOENIX_PROJECT_PROMPT.md`,
   `STRATEGY_KNOWLEDGE_INJECTION_PROMPT.md`) to a `docs/archive/handoff_prompts/`
   subdir. They're old Anthropic.ai handoff fixtures.
5. Verify `OPERATOR_TODO.md` (Apr 25) — is it actively maintained or stale?
6. Verify `docs/TODO_RETIRE_LEGACY_REPO.md` — is the legacy-repo retirement
   complete? If yes, archive.
7. The 5 older audit docs (`cvd_usage_audit.md`, `stop_target_math_audit.md`,
   `target_fire_audit.md`, `trailing_stop_audit.md`, `guaranteed_loss_audit.md`) —
   probably superseded by Section U tick-level work but each should be skimmed
   before archiving.

### Out of scope (do not touch)
- All `core/`, `bots/`, `bridge/`, `config/`, `strategies/` files. Even ones
  that *look* old (e.g., `core/chart_patterns_v1.py`) may have live consumers.
  Production code cleanup is a separate, riskier audit.
- All `tools/` Python files. They're all flat-named and each addresses a real
  research question; we have no signal that any are dead.
- All gitignored tick data and backtest CSV outputs (other than the smoke files
  + v1 throwaway we deleted). They're regenerable but actively re-read by the
  research workflow.

---

## 5. Verification

```
git status (after this audit, this file + 3 header additions + 0 deletions tracked):
  M docs/PHASE_13_IMPLEMENTATION_PLAN.md     (added [RESEARCH ARCHIVE] header)
  M docs/PHOENIX_BEST_PLAN.md                (added [CURRENT — SHIP PLAN] header)
  M docs/STRATEGY_DEEP_DIVE_2026-05-18.md    (added [RESEARCH ARCHIVE — superseded] header)
  ?? docs/FILE_CLEANUP_AUDIT.md              (this file)
```

Disk cleanup (untracked, no git impact):
- 4 files removed (smoke tests + v1 throwaway), ~18 KB recovered.

No production code touched. No tracked file deleted. All cleanup is reversible
via undoing the header text additions; the disk deletes are gitignored
regenerables.
