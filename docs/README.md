# Phoenix Bot — docs/

**This is the START HERE file. A fresh AI session should read this first.**

Last restructured: 2026-05-24 (Sunday). Replaces the prior sprawl of build-maps,
prompts, and dated reports scattered through the root and `docs/`. Older files
were not deleted — they live under `docs/archive/`.

---

## What Phoenix is, in one paragraph

Phoenix is a one-operator, locally-hosted, Python + NinjaTrader 8 trading bot for
MNQ (Micro E-mini Nasdaq-100) futures. NT8 runs a custom C# Indicator
([`ninjatrader/TickStreamer.cs`](../ninjatrader/TickStreamer.cs)) that connects out
to a Python bridge ([`bridge/bridge_server.py`](../bridge/bridge_server.py)) which
fans market data to bot processes ([`bots/prod_bot.py`](../bots/prod_bot.py),
[`bots/sim_bot.py`](../bots/sim_bot.py)) and writes Order Instruction Files (OIFs)
that NT8's ATI picks up for execution. There is an AI advisory stack (Council,
PreTrade filter, SessionDebriefer, HistoricalLearner) that is currently advisory
and not on the critical path. As of this writing the system is in paper-trading;
real account is $300 and `LIVE_TRADING=False` until ≥ $2,000.

---

## How to read this directory

Read in this order:

1. **[architecture.md](architecture.md)** — How the pieces fit together. Data
   flow, components, ports, paths, the immutable technical rules.
2. **[runbook.md](runbook.md)** — How to start, stop, kill, recover. The
   operator playbook.
3. **[roadmap.md](roadmap.md)** — What's done, what's in flight, what's next.
   This file references the audits and the synthesis for the *why*.
4. **[incidents.md](incidents.md)** — Verbatim, dated incident history. Treat
   this as immutable: every dated entry stays, every commit hash stays.
5. **[audits/](audits/)** — Single-auditor reports + the canonical synthesis.
   The synthesis adjudicates conflicts between the auditors and is the file to
   read for plan-making.

For **live operational truth** (what's true right now), do NOT rely on this
directory. Read:

- **[memory/context/CURRENT_STATE.md](../memory/context/CURRENT_STATE.md)** —
  Auto-loaded by the SessionStart hook. The bot's own real-time snapshot.
- **[memory/context/RECENT_CHANGES.md](../memory/context/RECENT_CHANGES.md)** —
  Dated change log, newest first.
- **[memory/context/KNOWN_ISSUES.md](../memory/context/KNOWN_ISSUES.md)** —
  Open issues, status.
- **[memory/context/OPEN_QUESTIONS.md](../memory/context/OPEN_QUESTIONS.md)** —
  Decisions waiting on the operator.
- **[memory/semantic/lessons_learned.md](../memory/semantic/lessons_learned.md)** —
  Durable hard-won knowledge.

The `memory/` system has a write-back hook on `SessionEnd` that appends to
`memory/audit_log.jsonl` and commits memory changes; do not edit those files by
hand without going through the hook unless you know what you're doing.

---

## File map (after 2026-05-24 restructure)

```
docs/
├── README.md                       # this file — entry point
├── architecture.md                 # system design
├── incidents.md                    # verbatim dated incident log
├── roadmap.md                      # done / in flight / next
├── runbook.md                      # start, stop, kill, recovery
├── PHOENIX_PROJECT_PROMPT.md       # operator-facing architecture notes (still current)
├── PHOENIX_BEST_PLAN.md            # Phase 13 source plan (active reference)
├── PHASE_13_IMPLEMENTATION_PLAN.md # active Phase 13 deliverable
├── STRATEGY_SPECIFICATIONS.md      # per-strategy specs (active)
├── STRATEGY_DEEP_DIVE_2026-05-18.md
├── PHOENIX_ENTRY_SIGNAL_DOCTRINE.md
├── OPERATOR_BRIEF_PT2.md           # active reference brief
├── OPERATOR_BRIEF_PT5_PT7.md
├── OPERATOR_BRIEF_PT8_ADDENDUM.md
├── OPERATOR_MORNING_BRIEF.md
├── BUGS_AND_TODOS.md               # active running list
├── audits/
│   ├── SYNTHESIS_2026-05-24.md     # ← canonical synthesis of the 3 audits
│   ├── 0524_Claude_Analysis.md
│   ├── 0524_AntiGravity_Analysis.md
│   ├── 0524_Codex_Analysis.md
│   ├── 2026-04-25_plugin_skill_audit.md
│   ├── hard-audit-2026-04-22.md
│   ├── FILE_CLEANUP_AUDIT.md
│   └── STRATEGY_SHIP_AUDIT.md
└── archive/                        # superseded plans, prompts, one-shot notes
    ├── PHOENIX_ROADMAP_v4.md
    ├── REBUILD_PLAN.md
    ├── phoenix_action_plan_v2_post_migration.md
    ├── BUILD_MAP.md
    ├── OPERATOR_TODO.md
    ├── ACTION_PLAN_V2_1_DELTAS.md
    ├── TODO_RETIRE_LEGACY_REPO.md
    ├── PROJECT_EXPORT_PROMPT.md
    ├── STRATEGY_KNOWLEDGE_INJECTION_PROMPT.md
    ├── Epic_Update_v1_Prompt.md
    ├── SCRATCH_DIRS.md
    ├── AI_Trading_Analysis_System_Research.md
    ├── chicago_vps_migration_plan.md  # STRICKEN — Phoenix stays on Trading PC
    ├── tradingview_webhook_setup.md   # STRICKEN — Premium not approved
    ├── phase-0-*.md, phase-eh-*.md
    ├── phase_b_plus_roadmap.md
    ├── exit-sprint-*.md, final-sprint-*.md
    └── ... (24 files total — see ls)
```

Active operational docs in the `docs/` root (not moved): `BUGS_AND_TODOS.md`,
`CONFLUENCE_VOTER_RESEARCH_2026-05-21.md`, `DATABENTO_FOOTPRINT_WALKTHROUGH.md`,
`EARLY_REVERSAL_EXIT_ANALYSIS.md`, `ENTRY_RETEST_ANALYSIS.md`,
`FAILED_HOLD_STRATEGY.md`, `OPERATOR_BRIEF_*`, `PHASE9_DEPLOY_STATUS.md`,
`PHASE_13_IMPLEMENTATION_PLAN.md`, `PHOENIX_BEST_PLAN.md`,
`PHOENIX_ENTRY_SIGNAL_DOCTRINE.md`, `SR_*`, `STRATEGY_*`,
`TICK_LEVEL_*_VERIFICATION.md`, `cvd_usage_audit.md`, `daily_ritual.md`,
`exit_methodology_per_strategy.md`, `guaranteed_loss_audit.md`,
`momentum_days.md`, `nt8_multi_stream_recovery.md`, `oif_killswitch_runbook.md`,
`opening_strategies_silence.md`, `phase-c-deployment.md`,
`phase_c_architecture.md`, `stop_target_math_audit.md`, `target_fire_audit.md`,
`trailing_stop_audit.md`. These are topic-specific references that still get
hit; they live alongside the canonical files rather than being inlined.

---

## What the 2026-05-24 restructure changed

- **5 audit/analysis files** moved from `analysis/` to `docs/audits/`.
- **24 stale build-maps, one-shot prompts, and superseded phase reports** moved
  from the root and `docs/` into `docs/archive/`. Nothing deleted.
- **5 canonical entry-point files created**: this README, `architecture.md`,
  `incidents.md`, `roadmap.md`, `runbook.md`.
- **The synthesis** of the three 2026-05-24 audits lives at
  [`audits/SYNTHESIS_2026-05-24.md`](audits/SYNTHESIS_2026-05-24.md) and is
  canonical for plan-making.
- **Root-level files preserved**: `README.md` (project README, now points here),
  `CLAUDE.md` (Claude Code session bootstrap), `SKILLS.md` (auto-generated by
  `tools/skills_digest.py`).
- **Not touched**: `memory/`, `agents/prompts/`, `ninjatrader/`, `archive/`,
  `archived/`. Each of these is code-adjacent or operational.

---

## If you are the next AI session reading this

1. Read this file (you're here).
2. Read [`architecture.md`](architecture.md) and [`runbook.md`](runbook.md).
3. Skim [`memory/context/CURRENT_STATE.md`](../memory/context/CURRENT_STATE.md).
4. Skim [`memory/context/KNOWN_ISSUES.md`](../memory/context/KNOWN_ISSUES.md).
5. Read [`audits/SYNTHESIS_2026-05-24.md`](audits/SYNTHESIS_2026-05-24.md) for
   the current plan and the *one question* that gates everything.
6. Then ask the operator what they want to work on.

Do **not** read the three single-auditor reports as primary plan inputs — they
were the inputs; the synthesis is the output. If you suspect the synthesis is
wrong on a specific point, go to the cited `file:line` in the actual codebase
and verify — that's the ground truth.
