# CLAUDE.md — Phoenix Trading Bot (PROPOSED v2)

> **STATUS: PROPOSED — operator review/merge required.**
> This file is a hardened superset of `phoenix_bot/CLAUDE.md`. It adds five
> top-of-file blocks per the 2026-05-24 Gemini anti-regression guidance.
> Original "System Architecture" and "Daily Monitoring Workflow" sections
> below are preserved verbatim. Do NOT overwrite live `CLAUDE.md` directly;
> diff this against the live file and merge.

---

## 1. TRADING GUARDRAILS (CRITICAL & NON-NEGOTIABLE)

These limits exist because Phoenix runs against real (and soon-real) money on
NinjaTrader 8 Sim101 / live. Violations are not warnings — they have cost the
operator real dollars (see `memory/lessons_learned.md`, `code_changes_dont_auto_deploy.md`).

- **`MAX_ACTUAL_STOP_DOLLARS_PER_TRADE = 50.0`** in `config/settings.py:45`.
  Hard per-trade dollar cap on the *actual* stop distance × tick value × contracts.
  **NEVER raise this without explicit operator sign-off AND a justification line
  in the commit message** (e.g. `Raised per-trade stop cap to $60: operator
  approved 2026-05-XX because <reason>`).

- **`DAILY_LOSS_LIMIT = 200.0`** and **`PER_STRATEGY_DAILY_LOSS_CAP = 200.0`**
  in `config/settings.py`. Restored 2026-05-20 after Phase 0 sim-override
  pollution. **NEVER set these to sim-only values (e.g. $20/$45) in production
  source.** If you need looser caps for a sim run, override via env var or a
  separate sim config — do not edit the production constants. Enforced by
  `tests/test_risk_hierarchy.py`.

- **`LIVE_TRADING = False`** in `config/settings.py`. Stays False until the
  live account is ≥ **$2,000** (currently ~$300). Do not flip this flag.
  Any PR that touches the literal `LIVE_TRADING = ` line must STOP and ask.

- **Hard stops on every order.** Stop-Market for stop-loss execution
  certainty; Limit for profit targets. Per OIF universal rules (see
  `bridge/oif_writer.py` and `docs/OIF_*.md`). No "soft" stops, no
  mental stops, no stops-that-cancel-on-fill.

- **Daily flatten cascade — NEVER disable any stage:**
  - 15:53 CT — Python-side polite flatten request
  - 15:54 CT — Python escalation
  - 15:54:45 CT — Python final
  - 15:55 CT — NT8 indicator backstop (TickStreamer-driven)
  - 16:00 CT — CME hard floor (exchange-enforced)
  Removing or shifting any of these later than its current time is a
  guardrail violation and requires operator sign-off in the commit message.

- **Kill switch path is sacred.** `tools/oif_kill_switch.py` writes
  `outgoing/halt_all.json`, which every bot polls. **NEVER comment out the
  detection path, the file check, or the halt-on-flag behavior in any bot
  or in `core/risk_manager.py`.** If a test is failing because of the kill
  switch, fix the test or the trigger, not the detection.

- **Telegram + Twilio SMS alerts** are wired through `watcher_agent` and the
  notification stack. **NEVER remove notification hooks during refactors**,
  even "temporarily" — silent failures (`memory/feedback_silent_failures.md`)
  are Phoenix's #1 historical failure mode. If a notification call is in your
  way, route it, do not delete it.

---

## 2. ANTI-REGRESSION POLICY (per Gemini, 2026-05-24)

Adopted after the "whack-a-mole" bug cycle of Sprint J-K where fixes to one
file silently re-broke adjacent unchanged behavior.

- **No monolithic file overwrites.** Do not regenerate a whole file to fix
  a single function. Isolate the exact broken function, method, or class and
  edit only that. If a full rewrite is genuinely needed, say so and ask first.

- **Preserve all existing developer comments, docstrings, and log statements.**
  Comments encode hard-won context (see almost every file in `memory/`).
  Stripping them is a regression even if behavior is unchanged.

- **After any edit to strategy or risk code, surface a `git diff`** of the
  changed range and ask for explicit confirmation that no unrelated logic
  was dropped. Do not assume the edit was clean just because the test passed.

- **When fixing a bug, do NOT also "clean up" or "optimize" adjacent code.**
  Two-purpose commits are the failure mode this policy exists to prevent.
  File a follow-up task for the cleanup if it's real; don't smuggle it.

- **STOP-AND-ASK files (sign-off required before any edit):**
  - `bridge/oif_writer.py`
  - `phoenix_bot/orchestrator/oif_writer.py` (if/when it lands)
  - `core/risk_manager.py`
  - `bots/base_bot.py`

  Touching any of these without prior sign-off is a process violation,
  regardless of whether the change is "obviously safe". These four files
  carry the load-bearing OIF, risk, and bot-lifecycle invariants.

---

## 3. ENVIRONMENT & BUILD COMMANDS

- **Runtime:** Python 3.11+ on Windows 11 (PowerShell 7 is the shell).
- **Install deps:** `pip install -r requirements.txt`
- **Run paper (sim):** `python bots/sim_bot.py` — spawns 16 sim sub-accounts.
- **Run prod (paper-only currently):** `python bots/prod_bot.py`
- **Run lab:** `python bots/lab_bot.py`
- **Bridge:** `python bridge/bridge_server.py`
- **Dashboard:** `python dashboard/server.py` → `http://localhost:5000`
- **Full test suite:** `python -m pytest --tb=no -q`
  Must stay at **2,110+ pass / 0 fail**. A red main is an all-hands stop.
- **Targeted test (preferred per-edit):** `python -m pytest tests/test_<area>.py -q`
- **Lint:** no enforced linter yet (no `pyproject.toml`, `.ruff.toml`, `setup.cfg`,
  or `.flake8` at project root as of 2026-05-24). If you add one, document it here.
- **Kill switch:** `python tools/oif_kill_switch.py` — flatten everything now.
- **Verify OIF round-trip:** `python tools/verify_oif_fix.py`
- **Daily session summary:** `python tools/daily_session_summary.py`
- **Validation tracker:** `python tools/validation_tracker.py --check-promotion`

---

## 4. STATE MANAGEMENT

Every restart-survivable fact must be persisted to disk. In-memory-only
critical state is a guardrail violation.

- **`logs/trade_memory_<bot>.json`** — per-bot trade memory.
  **NEVER raw-open** these files (or the legacy `logs/trade_memory.json`).
  Always go through `core.trade_memory.load_all_trades()` which merges the
  legacy file + per-bot files and handles the Unix-vs-ISO date-filter gotcha.
  See `memory/trade_memory_canonical_reader.md` (12-file audit shipped
  `c9099d7`, 2026-05-13).

- **`data/equity_state.json`** — equity tracking for `tier_3000` sizing.
  Currently **dormant** (live account < $2K). Do not delete; bot reads on boot.

- **`logs/strategy_halts.json`** — per-strategy halt persistence across restarts.
  Audit `except ...: pass` patterns around this file before adding logic
  (see `memory/index_error_pass_silent_pattern.md`).

- **`outgoing/halt_all.json`** — kill-switch flag file. Existence = halt.
  Every bot polls this; the detection path is on the STOP-AND-ASK list above.

- **`memory/audit_log.jsonl`** — append-only Phoenix memory event log,
  source of truth. Derived files (`CURRENT_STATE.md`, `RECENT_CHANGES.md`,
  `lessons_learned.md`) are regenerable from this. SessionEnd hook
  appends + commits.

---

## 5. CLAUDE CODE SPECIFIC DIRECTIONS

Tactics for working in this repo without breaking it.

- **Before refactoring `bots/base_bot.py`:** read
  `docs/audits/BASE_BOT_DECOMPOSITION_PLAN.md` first (TODO: write this plan
  if it doesn't exist yet — do not freelance a decomposition). Follow the
  staged plan; do not do it all in one PR.

- **Before adding any new strategy:** ship with `enabled=False` and
  `validated=False` in `config/strategies.py`. Promotion to `validated=True`
  requires live n ≥ 100 **and** Wilson-CI lower bound on win-rate > 0.5
  (see `memory/promotion_on_vibes_failure_mode.md`, Wilson guardrail
  shipped 2026-05-13 as commit `477e31d`). Run
  `tools/validation_tracker.py --check-promotion` before any flip.

- **When the user reports a bug:** read the cited `file:line` first. Print
  the actual current code (use the Read tool — do not paraphrase from
  memory). ONLY THEN propose a fix. Phoenix bugs are silent
  (`memory/feedback_silent_failures.md`); guessing without reading the
  current code is how regressions ship.

- **When implementing changes across multiple files:** list the planned
  file changes first as a bulleted plan, get operator sign-off, THEN
  execute. Do not interleave plan/edit/plan/edit — it bloats commits and
  hides intent.

- **Use the skills in `.claude/skills/`** as topic-loaded context. They
  encode Phoenix-specific workflows (OIF, risk, validation, etc.) and
  are cheaper to reload than re-deriving from the codebase.

- **After every behavior-affecting commit:** flag "prod needs restart"
  in the response, or ask permission to restart prod_bot. A long-running
  prod_bot keeps its in-memory code snapshot from process start; a commit
  is not a deploy (see `memory/code_changes_dont_auto_deploy.md`, cost
  the operator $-106 on 2026-05-14).

- **`git push origin <branch>` after any "save and commit"** unless the
  branch is `main`, the push is non-fast-forward, or it's a force-push.
  Operator standing instruction since 2026-05-14
  (`memory/feedback_auto_push_after_commit.md`).

---

## --- ORIGINAL CONTENT BELOW (PRESERVED VERBATIM) ---

# CLAUDE.md — Phoenix Trading Bot

## System Architecture

Phoenix is a local Python trading system for MNQ (Micro E-mini Nasdaq-100) futures, connected to NinjaTrader 8.

### Data Flow
```
NinjaTrader 8 (TickStreamer.cs indicator)
  → WebSocket CLIENT connects OUT to Python on :8765
  → bridge_server.py (WebSocket SERVER on :8765, fans out on :8766)
  → prod_bot.py / lab_bot.py (WebSocket CLIENTS on :8766)
  → Trade signals → OIF files → NT8 incoming/ folder → execution
```

### Critical Design Rules (DO NOT CHANGE)
1. **NT8 Indicator, not Strategy** — Strategies crash with ErrorHandling=Stop
2. **Python is WS SERVER, NT8 connects OUT** — reverse direction failed
3. **OIF files for trade execution** — file path is consistent and reliable
4. **NT8 data folder path is config-driven** — change `NT8_DATA_ROOT` in `config/settings.py`; migrated out of OneDrive 2026-04-18
5. **No Newtonsoft.Json in C#** — not bundled with NT8, use StringBuilder
6. **VWAP calculated in Python** — Order Flow+ license required in NT8

### Key Paths
- OIF incoming: `C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\`
- OIF outgoing: `C:\Users\Trading PC\Documents\NinjaTrader 8\outgoing\`
- File fallback: `C:\temp\mnq_data.json`
- NT8 indicators: `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\`

### Ports
- `:8765` — Bridge WS server (NT8 connects here)
- `:8766` — Bridge WS server (bots connect here)
- `:8767` — Bridge health HTTP endpoint
- `:5000` — Dashboard (Flask)

### Project Layout
```
phoenix_bot/
├── config/settings.py          # All config: ports, paths, limits, instruments
├── config/strategies.py        # Strategy params (toggleable, slider-friendly)
├── bridge/bridge_server.py     # WS server :8765 (NT8) + :8766 (bots)
├── bridge/oif_writer.py        # OIF trade file writer
├── ninjatrader/TickStreamer.cs  # Lean tick-only NT8 indicator
├── bots/base_bot.py            # Shared bot logic
├── bots/prod_bot.py            # Production bot (validated strategies)
├── bots/lab_bot.py             # Experimental bot (sandbox)
├── strategies/base_strategy.py # Strategy interface
├── strategies/*.py             # Individual strategy files
├── core/tick_aggregator.py     # Builds bars, ATR, VWAP, EMA, CVD from ticks
├── core/risk_manager.py        # Limits, VIX filter, recovery mode, sizing
├── core/session_manager.py     # 8 market regimes, time windows
├── core/position_manager.py    # Track positions, P&L, stop/target
├── core/trade_memory.py        # Trade log + learning data
├── dashboard/server.py         # Flask app, REST API
├── dashboard/templates/        # dashboard.html
├── agents/                     # Optional AI advisory (Council, pre-trade, debrief)
└── logs/
```

### Running
```bash
# 1. Start NinjaTrader 8, load TickStreamer on MNQM6 chart
# 2. Start bridge
python bridge/bridge_server.py

# 3. Start bot(s)
python bots/prod_bot.py    # Production
python bots/lab_bot.py     # Experimental (optional)

# 4. Open dashboard
python dashboard/server.py  # then visit localhost:5000
```

### Environment
```bash
pip install -r requirements.txt   # websockets, flask, numpy, aiofiles, python-dotenv, aiohttp
```

### Trading Parameters (defaults in config/settings.py)
- Instrument: MNQM6 06-26
- Account: Sim101 (LIVE_TRADING = False by default)
- Max loss per trade: $20
- Daily stop: -$45
- Recovery mode: -$30 daily → 50% size reduction
- Primary session: 8:30-10:00 AM CST
- Base RR: 1.5:1

## Daily Monitoring Workflow (Sprint C)

All four tools are read-only and write to `out/`. Run from project root.

| When | Command | Reads | Writes |
|------|---------|-------|--------|
| After each session | `python tools/daily_session_summary.py` | `logs/history/<today>_<bot>.jsonl` | `out/daily_summary_<today>.md` |
| Weekly (or after risk-code changes) | `python tools/verify_halt_signatures.py` | (synthetic triggers) | `out/halt_verify_<today>.md` |
| Weekly | `python tools/validation_tracker.py --post-b13-only` | `logs/trade_memory.json` | `out/validation_status_<today>.md` |
| As needed | `python tools/backfill_commissions.py` | `logs/trade_memory.json` | `out/historical_pnl_recompute_<today>.md` |

### Statistical tier reference

| Tier | Trades | Confidence | Decisions Allowed |
|------|-------:|-----------:|---|
| INSUFFICIENT_SAMPLE | < 30 | none | WATCH only |
| PRELIMINARY | 30–99 | ~70% | WATCH or KILL if PF<0.7 |
| TENTATIVE | 100–384 | ~90% | + GRADUATE candidate |
| VALIDATED | 385–665 | ~95% | + SCALE candidate |
| HIGH_CONFIDENCE | 666+ | ~99% | full confidence |

Phoenix's project 50-trade graduation gate sits inside PRELIMINARY —
enough to start making directional decisions, NOT enough to bet the
farm on. The validation_tracker tool surfaces this uncertainty
explicitly via Wilson 95% CI on win rate.

### Anomaly detection (daily_session_summary.py)

After each session, the tool flags two kinds of anomaly vs the
trailing 7-day baseline:

- `signal_volume_drop`: today's signals < 40% of the strategy's
  trailing average. **Early warning that a Sprint A gate may be
  rejecting too aggressively.**
- `silent_strategy`: trailing avg ≥ 1/day, today = 0. **Critical —
  investigate before next session.**
