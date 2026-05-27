# Phoenix Bot — Phases E–H Parallel Sprint

**Prepared for: Claude Code (orchestrator + sub-agents via Task tool)**
**Prepared by: Claude (chat, 2026-04-21)**
**Owner: Jennifer (Frisco, TX)**
**Repo: `C:\Trading Project\phoenix_bot\`**
**Prerequisite: Phase C (sim_bot live flip) MERGED.**

---

## 🚀 MISSION

Execute Phases E, F, G, and H in a maximally parallel sprint. Use the Task tool to spawn sub-agents for independent workstreams. **Do not wait on Jennifer for anything you can legitimately decide yourself.** Document assumptions in code, commit small and often, and push progress continuously so partial work is always recoverable.

## 🧠 OPERATING PHILOSOPHY — read carefully, this is the whole point

Jennifer wants velocity. Her explicit direction: **"no stop signs, full go, only stop when my input is explicitly needed to move forward."**

Translate that as:

1. **If you can make a reasonable engineering judgment, make it.** Document the decision as a code comment and move on. Do NOT pause to ask Jennifer for style preferences, naming choices, or "should I refactor X while I'm here" questions.

2. **If a task is genuinely blocked by missing resources** (API key, credential, access you can't obtain), do one of these — NOT stop:
   - Build the code against a documented placeholder, mark the blocker in a `BLOCKERS.md` file at repo root, and move to the next task
   - Find an alternate path (e.g., for B32 Alpaca VIX, research free alternative data sources and implement against one you CAN use)
   - Ship the feature with a clearly-logged `[BLOCKER: <reason>]` runtime warning

3. **Bundle all your questions for the final report, not mid-execution.** Jennifer reads a single end-of-sprint summary, not 40 interruptions.

4. **Parallelism is mandatory where files don't conflict.** Spawn sub-agents via Task tool. See "Stream Map" below for lanes.

5. **Commit every logical unit of work.** Push after each commit. If the sprint is interrupted, partial progress survives.

6. **Exceptions where you MUST still stop:**
   - Pre-flight check fails (broken baseline before you started)
   - A change you're about to make would delete production trading code
   - Tests that were passing start failing and you can't fix them within 30 minutes
   - A commit that would exceed 2,000 lines net change

That's it. Everything else: decide and go.

---

## 🎯 WHAT YOU'RE BUILDING

### Phase E — B33 Gamma Integration (MenthorQ)

**E-tactical** (~5 min): Document the daily gamma-paste ritual in `docs/daily_ritual.md` — specifically that Jennifer must update `data/menthorq/menthorq_daily.json` alongside her `_levels.txt` paste each morning.

**E-defensive** (~15 min): Add startup warning to `menthorq_feed.py` (or wherever the gamma loader lives) — if `menthorq_daily.json` is >24h old, log a CRITICAL-level warning with the file's actual age in hours. Don't block startup.

**E-strategic** (~45 min): Rewire `score_menthorq_gamma` (wherever it lives — find it) to read `market_snapshot["gamma_regime"]` directly. Retire Path A (the stale JSON-reading path). Tighten the "overclaiming" warning text in `menthorq_feed.py` — current message lists `spring_setup` and `gamma_flip_detector` as affected but they have zero menthorq refs (verified). Fix the message to list only strategies that actually consume the score.

### Phase F — B15 Test Backlog Cleanup

Six tests currently failing on the `feature/knowledge-injection-systems` branch:

| Test | File | Likely root cause |
|---|---|---|
| `test_close_long_pnl` | `test_position_manager.py` | B13 commission integration changed PnL math |
| `test_close_short_pnl` | `test_position_manager.py` | Same as above |
| `test_cooloff_after_3_consecutive_losses` | `test_risk_manager.py` | Cooloff semantics changed |
| `test_prod_window_at_close` | `test_trading_pipeline.py` | Session-window logic drift |
| `test_bias_momentum_uses_regime_overrides` | `test_trading_pipeline.py` | Regime override contract changed |
| `test_non_golden_regime_has_tighter_gates` | `test_trading_pipeline.py` | Gate tightness contract changed |

For each: investigate the actual current behavior, determine if the TEST is wrong (update it) or if there's a real regression (fix the code). Default assumption: tests are stale because the code has evolved correctly.

If after 30 minutes of investigation you genuinely cannot determine the right answer, mark it `@pytest.mark.xfail(reason="...")` with a clear reason and open a memo in `docs/tech-debt/test-F-<testname>.md` for Jennifer to review later. That's a valid completion state for this phase.

### Phase G — Known Open Bugs

**G-B26 — empty-value parser robustness** (~30 min)
Located in the MenthorQ parser. Edge cases: empty string, missing key, NaN, negative zero. Add defensive parsing and round-trip tests.

**G-B32 — Alpaca VIX API 401** (RESEARCH + IMPLEMENT, do NOT wait on API key)
- Path A: Research and implement a free VIX data source that doesn't require auth (CBOE direct, Yahoo Finance yfinance, FRED API with free key, etc.)
- Path B: If Alpaca is strictly required, build graceful degradation — on 401, log once/hour, continue without VIX regime classification, flag in health endpoint
- Ship SOMETHING that makes the bot resilient to VIX unavailability. Current behavior (crash or log-spam) is not acceptable.

**G-B36 — PID 40908 silent crash** (PARKED — just add telemetry)
Don't try to diagnose a crash that hasn't recurred. Instead, add defensive telemetry:
- On bot startup, log Python version, websockets version, asyncio loop policy, available memory
- On bot shutdown (normal or abnormal), write a final `logs/shutdown_<pid>.json` with reason
- Watchdog: on restart, read the previous shutdown file and include in restart log
This way if it recurs we have data to diagnose.

**G-B37 — 4C integration test gap** (~1 hr)
Write `tests/test_4c_integration.py` covering: OIF written end-to-end through account routing → `_require_account` guard fires correctly on unmapped strategy → account string survives serialization → fallback path works. Use actual routing map, not mocks where possible.

**G-B38 — gamma_regime missing from log_eval** (~15 min)
In `core/history_logger.py`, `log_eval()` builds a snapshot dict. Add `gamma_regime` to it (already populated in `log_entry`). One-line fix + test.

### Phase H — AI Learning System (Phase 4 agents)

Build all five agents. This is the largest chunk.

**H-Infrastructure first** (~2 hrs) — `agents/base_agent.py`:
- Async AI client wrapper (Gemini Flash + Gemini Pro + Claude)
- Timeout handler — every agent call defaults to a safe pass-through value on timeout/error
- Structured prompt templates stored in `agents/prompts/`
- Response parser with JSON-mode for structured outputs
- Retry with exponential backoff (max 3 attempts)
- Cost/latency logging to `logs/agents/YYYY-MM-DD_agent_calls.jsonl`
- Env var check at import: `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY` — if missing, log critical warning but don't crash

**H-4A — Council Gate** (`agents/council_gate.py`, ~2 hrs):
- 7 sub-agents running Gemini Flash, each with a distinct "voting persona" (trend-follower, mean-reverter, vol-watcher, gamma-reader, intermarket-analyst, session-historian, contrarian)
- Each votes BULLISH / BEARISH / NEUTRAL with a brief rationale
- Gemini Pro orchestrator synthesizes: "Council: BULLISH 6/7 — <1-sentence summary>"
- Runs at session open (8:30 AM CT) and on major regime shifts (regime change detected in session_manager)
- Writes result to `logs/council/YYYY-MM-DD.json`
- Exposes `get_current_bias()` for bots to consult (optional filter, not blocking)

**H-4B — Pre-Trade Filter** (`agents/pretrade_filter.py`, ~1.5 hrs):
- Single Gemini Flash call before each entry
- 3-second hard timeout → defaults to `CLEAR` (NEVER blocks a trade on AI failure)
- Input: signal context (strategy, direction, confluences, market snapshot, recent trade history for this strategy)
- Output: `CLEAR` | `CAUTION` (log warning, let trade proceed) | `SIT_OUT` (strategy-configurable whether to respect)
- Per-strategy config in `strategies.py`: `"ai_filter_mode": "advisory"` (log only) or `"blocking"` (respect SIT_OUT)
- Default: advisory — collect data before ever blocking a trade

**H-4C — Session Debriefer** (`agents/session_debriefer.py`, ~2 hrs):
- Runs at 4:00 PM CT (right after the flatten, pre-globex-reopen)
- Reads today's `logs/history/YYYY-MM-DD_sim.jsonl` + `logs/trade_memory.json`
- Claude call (uses ANTHROPIC_API_KEY) with rich prompt including:
  - Trade-by-trade breakdown
  - Regime distribution
  - Per-strategy P&L
  - Confluences that worked / didn't
- Writes `logs/ai_debrief/YYYY-MM-DD.md` with sections: Summary, Wins, Losses, Patterns, Questions for Tomorrow
- Optionally emails or Telegrams the debrief (use existing infra if present, don't build new)

**H-4D — Historical Learner** (`agents/historical_learner.py`, ~3 hrs):
- Weekly run (Sunday night or first-tick-after-midnight-Sunday)
- Loads last N days of `logs/history/*_sim.jsonl` (default N=14)
- Loads `logs/trade_memory.json`
- For each strategy, computes: WR per regime, PF per time-of-day, confluence effectiveness, worst-hour statistics, best-hour statistics
- Claude call with aggregated stats: "Generate 3-7 specific, testable hypotheses about what's working and what isn't, grouped by strategy. Each hypothesis should suggest a concrete config change."
- Writes `logs/ai_learner/weekly_YYYY-MM-DD.md`
- Writes structured recommendations to `logs/ai_learner/pending_recommendations.json` — this is 4E's input

**H-4E — Adaptive Params** (`agents/adaptive_params.py`, ~2 hrs):
- Reads `logs/ai_learner/pending_recommendations.json`
- For each recommendation: validates against safety bounds (can't suggest risk-per-trade > $100, can't suggest disabling risk gates, etc.)
- Writes a human-readable proposal file `logs/ai_learner/proposals/proposal_<timestamp>.md` with:
  - Current value
  - Proposed value
  - Reasoning
  - Expected impact
  - Rollback instructions
- Human approval via `tools/approve_proposal.py <proposal_id>` which:
  - Creates a new git branch `ai-proposal/<proposal_id>`
  - Applies the change to `config/strategies.py`
  - Runs full test suite
  - Opens a summary for Jennifer to merge manually
- **CRITICAL:** never auto-applies. Always routes through the tool + git branch + Jennifer's merge.

---

## 🗺️ STREAM MAP — sub-agent lane assignments

Spawn these as parallel Task sub-agents (use the Task tool). Each sub-agent gets a single stream; files are non-overlapping within a wave.

### Wave 1 — spawn all four in parallel immediately

| Stream | Agent label | Files in-scope | Files forbidden |
|---|---|---|---|
| **S1: Small-fixes** | `stream-small-fixes` | `data/menthorq/menthorq_daily.json` docs, `menthorq_feed.py` (warning only), `core/history_logger.py` (B38 gamma_regime), `core/position_manager.py` (B26 parser robustness if it lives here — else MenthorQ parser), new `docs/daily_ritual.md` | anything in `agents/`, anything in `bots/`, `config/account_routing.py` |
| **S2: Test-backlog** | `stream-test-cleanup` | `tests/test_position_manager.py`, `tests/test_risk_manager.py`, `tests/test_trading_pipeline.py`, new `tests/test_4c_integration.py`, new `docs/tech-debt/test-F-*.md` | anything outside `tests/` unless fixing a true regression; if fixing code, scope ≤50 lines |
| **S3: E-strategic** | `stream-gamma-rewire` | `score_menthorq_gamma` (find it first), `menthorq_feed.py` overclaiming warning, related tests | `core/history_logger.py`, `tests/test_menthorq_feed.py` overlap — coordinate with S1 via comments |
| **S4: Agent-infra** | `stream-agent-infra` | NEW `agents/__init__.py`, `agents/base_agent.py`, `agents/prompts/*`, `agents/config.py`, `tests/test_agent_base.py` | nothing outside `agents/` and `tests/test_agent_*` |

**Merge point after Wave 1:** all four streams land on `feature/phases-e-h` branch (create it off current HEAD). Run full test suite. Green → proceed to Wave 2.

### Wave 2 — spawn all five in parallel after Wave 1 merges

| Stream | Agent label | Files in-scope |
|---|---|---|
| **S5: 4A Council Gate** | `stream-council` | `agents/council_gate.py`, `agents/prompts/council_*`, `tests/test_council_gate.py` |
| **S6: 4B Pretrade** | `stream-pretrade` | `agents/pretrade_filter.py`, `agents/prompts/pretrade.md`, `tests/test_pretrade_filter.py`, one-line hook in `bots/base_bot.py` entry path (COORDINATE) |
| **S7: 4C Debriefer** | `stream-debriefer` | `agents/session_debriefer.py`, `agents/prompts/debrief.md`, `tests/test_debriefer.py`, scheduled-task registration in `bots/base_bot.py` (COORDINATE) |
| **S8: 4D Learner** | `stream-learner` | `agents/historical_learner.py`, `agents/prompts/learner.md`, `tests/test_learner.py`, CLI tool `tools/run_weekly_learner.py` |
| **S9: 4E Adaptive** | `stream-adaptive` | `agents/adaptive_params.py`, `agents/prompts/adaptive.md`, `tests/test_adaptive.py`, `tools/approve_proposal.py`, `tools/list_proposals.py` |

**Conflict resolution:** Streams S6 and S7 both touch `bots/base_bot.py`. S6 adds one-line pre-entry hook. S7 adds one-line session-close scheduled-task. Orchestrator merges S6 first, then S7 rebases and continues.

### Wave 3 — integration & end-to-end test (orchestrator serial, no sub-agents)

- Integration test: synthetic session runs all 5 agents in sequence (council at open → filter per entry → debriefer at close → learner + adaptive as scheduled)
- Dashboard hooks: minimal — just ensure agents write to predictable file paths for a future Phase D (observability) to pick up
- Deployment notes: `docs/phase-eh-deployment.md` — how to start, monitor, tune the agent layer

---

## 🔨 GIT DISCIPLINE

- Work on single branch: `feature/phases-e-h` (create off `feature/knowledge-injection-systems` HEAD after Phase C merges)
- Commit subject format: `feat|fix|test|docs|chore(<stream>): <verb> <object>` — e.g., `feat(4a-council): implement 7-agent vote tallying`
- Push after every commit
- No squash — preserve stream history
- If a sub-agent's work conflicts at merge time: orchestrator rebases on main-stream, sub-agent's commits stay intact

## 🧪 TEST DISCIPLINE

- Every new module → unit tests in `tests/test_<module>.py`
- Every agent → integration test with mocked AI client
- No stream is "done" until its tests pass
- Target: final sprint ends with ≥ 513 + new tests all green (current baseline is 513 after Phase C)
- Real AI calls are NOT required in tests — use a `FakeAIClient` fixture that returns canned responses

## 🌐 ENVIRONMENT & CREDENTIALS

- Expect `.env` at repo root with `GOOGLE_API_KEY` and `ANTHROPIC_API_KEY`
- If either missing at import time of an agent: log `[AGENT_INIT] missing key <name> — agent will run in DEGRADED mode` and have the agent short-circuit to safe pass-through (CLEAR / empty recommendation / etc.)
- Never crash the bot on missing AI credentials
- Do NOT put real keys in code or tests. Use env vars. If testing, mock the client.

## 📝 ASSUMPTIONS LOG — DOCUMENT DON'T ASK

Maintain `docs/phase-eh-assumptions.md` with a line for each judgment call you made. Example format:

```
## 2026-04-22 14:30 — Stream S5 (Council Gate)
- Chose Gemini Flash 2.0 (not 1.5) for voter agents — rationale: better function-calling reliability for structured JSON votes. Rollback: change model string in agents/config.py:COUNCIL_MODEL.
- Persona temperature = 0.3 — low enough for consistency, high enough for viewpoint diversity.
- Tie-break rule: 3-3-1 ties → "NEUTRAL, council uncertain" rather than defaulting bullish/bearish.
```

Jennifer will read this in one pass at end-of-sprint. Do NOT ping her for these decisions.

## 🛑 LEGITIMATE BLOCKERS — the ONLY reasons to stop

Things you genuinely cannot proceed on:

1. Phase C is not yet merged — branch doesn't exist, `sim_bot.py` doesn't exist. Stop, tell Jennifer, wait.
2. `.env` file is missing AND you'd need to create it (don't guess credentials)
3. A sub-agent reports a fundamental architectural misunderstanding in this doc
4. Running-process damage — if you somehow caused a crash of her live bot (which you shouldn't be touching)
5. A test regression you can't fix within 30 minutes that was GREEN at sprint start

Everything else: decide, document, move. Even "should this be named X or Y" is your call — commit to one, add a note, move on.

## 📤 END-OF-SPRINT REPORT

When the sprint completes (or when you hit a true blocker), produce a single `docs/phase-eh-report.md` with:

- **What shipped** — list of commits, grouped by stream, with line counts
- **Test results** — pass/fail counts, any new xfails
- **Assumptions made** — summary of `phase-eh-assumptions.md`
- **Blockers hit** — what, why, what you did instead
- **Questions for Jennifer** — ONLY questions that genuinely need her input now
- **Recommended next steps** — what you'd do next if she greenlights

## ⚡ GO SIGNAL

Pre-flight:
```
cd "C:\Trading Project\phoenix_bot"
git fetch
git log --oneline -3                                  # confirm Phase C merged
$env:PYTHONPATH = 'C:\Trading Project\phoenix_bot'
python -m pytest --tb=no -q                           # baseline
python tools/verify_routing.py                        # sanity
git checkout -b feature/phases-e-h
```

If pre-flight passes, spawn Wave 1 immediately. Wave 2 after Wave 1 merges. Wave 3 after Wave 2 merges. Report at the end.

**GO.** 🚀
