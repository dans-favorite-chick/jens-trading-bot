# Phase E-H Assumptions Log (Stream S1 — small-fixes)

Judgment calls made during the S1 parallel sprint. Each entry: what was
assumed, why, and where to revisit if the assumption turns out wrong.

## 2026-04-21

### A1 — `menthorq_daily.json` path
- **Assumption:** the canonical path is
  `C:\Trading Project\phoenix_bot\data\menthorq_daily.json` (as used by
  `core/menthorq_feed.py::DATA_FILE`), not the `data/menthorq/` sub-dir
  referenced in the S1 scope note.
- **Why:** `data/menthorq/` is already populated with per-day
  `*_levels.txt` / `*_blind.txt` paste files (plus `gamma/`). The live
  `menthorq_feed` reads `data/menthorq_daily.json`. Moving the file now
  would break the running bot. Daily ritual doc and staleness check both
  target the real path.
- **Revisit if:** someone intentionally migrates the regime JSON into
  `data/menthorq/` — update `DATA_FILE` and re-point the ritual doc.

### A2 — Staleness threshold for `menthorq_daily.json`
- **Assumption:** CRITICAL log fires when mtime age > 24h. Uses
  `logger.critical`. Never blocks startup (matches B11 philosophy for
  the bridge file).
- **Why:** Task scope specified "> 24h old" and "don't block startup."

### A3 — `gamma_regime` mirroring in `log_eval`
- **Assumption:** `gamma_regime` on the `log_eval` snapshot must be
  sourced from `eval_record` if present, else `market` (mirrors what
  `log_entry` already does). Enum values are flattened via `.value`.
- **Why:** The field is already in the dict at line 131 as
  `market.get("gamma_regime")`. Task says it's "already in log_entry —
  mirror it." The S1 change is making it a first-class field (lifted
  out of the generic market snapshot) and normalizing enum→str.

### A4 — Parser hardening scope
- **Assumption:** "G-B26 parser robustness" means the `load_bridge_levels`
  and `load` JSON ingestion paths in `core/menthorq_feed.py`. Harden
  against empty strings, missing keys, NaN, -0.0, None.
- **Why:** That file is the only MenthorQ parser in `core/`. The
  existing `_f()` helper is close but doesn't reject NaN.

### A5 — Test file creation
- **Assumption:** `tests/test_menthorq_feed.py` does not exist (only
  `test_menthorq_gamma.py` and `test_b11_menthorq_bridge_health.py`
  exist). Creating new file with round-trip parser tests.
- **Why:** Glob confirmed absence. Scope says "create if absent."

## S3 / B33 — Phase E-strategic (2026-04-21)

**Scorer location**: `core/structural_bias.py` — `score_menthorq_gamma()` at line ~275.

**Path A retired**: old scorer read `mq_context.get("gex_regime")`, `hvl`, `call_resistance_all`, `put_support_all` from `market_snapshot["menthorq"]` (sourced from stale `data/menthorq/menthorq_daily.json`). New scorer reads `market_snapshot["gamma_regime"]` directly — a `GammaRegime` enum populated by `bots/base_bot._enrich_market_with_gamma` via fresh `data/menthorq/gamma/*_levels.txt` parse + B27 `classify_regime()`.

**Signature change**: scorer now takes `market_snapshot` (full dict), not the `menthorq` sub-dict. Call site at line ~418 updated. Back-compat: also accepts a dict with just `{gamma_regime: ...}` for direct test invocation.

**Score mapping (6-value enum → points, clamped to ±15)**:
- POSITIVE_STRONG: +10 ; POSITIVE_NORMAL: +5
- NEUTRAL: 0 ; UNKNOWN: 0 (returns early)
- NEGATIVE_NORMAL: -5 ; NEGATIVE_STRONG: -10
- Optional `gamma_nearest_wall` (tuple from base_bot) within 5pt: ±5 based on call/put side.

**Overclaiming warning — strategy list corrected** (`core/menthorq_feed.py`):
- Removed: `spring_setup`, `gamma_flip` (zero menthorq refs — verified via grep; `spring_setup.py` only mentions "MQ" in a reject-reason string about direction bias, no gamma reads).
- Kept: `core.structural_bias.score_menthorq_gamma` (15-pt composite weight), `core.continuation_reversal` (uses `market.get("gamma_regime")` for context adjustment).

**Tests**: new `tests/test_scoring_menthorq_gamma.py` (20 tests) covers enum regimes, string back-compat, graceful missing/UNKNOWN handling, Path A poisoning (proves Path B dominates), `open()`-patch proves zero file IO from scorer, wall proximity, clamp bounds. Full run `tests/test_scoring_menthorq_gamma.py tests/test_menthorq_gamma.py tests/test_b11_menthorq_bridge_health.py` → 52 passed.

## 2026-04-21 — Stream S8 (4D historical learner)

### S8.1 — History filename suffix
- **Assumption:** history JSONL files are named `{date}_{prod|lab}.jsonl`
  (confirmed by `logs/history/` listing). Spec referenced `_sim.jsonl`, so
  the loader accepts `prod`, `lab`, AND `sim` suffixes to be future-proof
  without forcing a rename.

### S8.2 — Trade data source
- **Assumption:** `logs/trade_memory.json` is the authoritative source for
  closed-trade P&L, regime, strategy, confluences, entry_time. History
  JSONL is skimmed only for signal-count / regime-bar context. This split
  avoids double-counting and matches the shape of existing files (history
  events are `bar`/`eval`/`entry`/`exit` separately; trade_memory has the
  joined closed-trade record).

### S8.3 — Hourly buckets = CT, naive UTC-6
- **Assumption:** hour-of-day buckets use `UTC - 6` and ignore DST. The
  NY-session primary window (08:30-10:00 CT) is well away from the DST
  transition hour. Off-by-1 inside the DST ambiguity window is accepted.

### S8.4 — Recommendation schema enforcement
- **Assumption:** recs missing ANY of the six required fields
  (`strategy`, `param`, `current`, `proposed`, `rationale`,
  `expected_impact`) are dropped silently, not coerced. Final count may
  fall below 3-7 target — we accept whatever passes validation rather
  than fabricating fields for S9.

### S8.5 — Non-blocking outputs
- **Assumption:** both the markdown report and
  `pending_recommendations.json` are written even when Claude returns
  nothing (empty recs list + degraded message in the MD). S9 reading an
  empty `recommendations: []` must treat it as "no-op this week."

### S8.6 — `pending_recommendations.json` is mutable-single
- **Assumption:** overwritten each run (not appended / version-history).
  The dated `weekly_YYYY-MM-DD.md` is the archive; the JSON is strictly
  the current pending queue for S9.

## 2026-04-21 — Stream S4 (agent-infra)

### S4.1 — Extend, don't overwrite `agents/`
- **Assumption:** existing `agents/__init__.py`, `ai_client.py`,
  `council_gate.py`, etc. stay untouched. S4 adds NEW modules
  (`config.py`, `base_agent.py`, `prompts/`) and re-exports them from
  `__init__.py`.
- **Why:** mission scope says "extend, don't overwrite." The existing
  `ai_client.py` already provides tiered multi-provider routing (Groq /
  Gemini / Grok / Ollama); S4 `base_agent.py` is a narrower, Phase-E-H
  specific surface (Gemini + Claude only) sitting alongside it.

### S4.2 — Optional-dep strategy
- **Assumption:** `anthropic` and `aiohttp` are already in
  `requirements.txt` (verified). `google-generativeai` is NOT added —
  it is deprecated (per FutureWarning) in favour of `google-genai` used
  by the existing `ai_client.py`. `base_agent.AIClient` tries to import
  `google.generativeai` anyway (works if present) and falls back to
  `aiohttp` REST call if missing — so no new pin required. Sub-streams
  that want the newer SDK can use `agents.ai_client.ask_gemini`.
- **Revisit if:** a sub-stream needs a feature of the newer `google-genai`
  SDK in the base_agent surface — then switch the import shim to
  `from google import genai` and drop the old path.

### S4.3 — Defaults
- **Timeout:** 10s. **Retries:** 3. **Backoff:** 1s initial, 2x
  exponential. All overridable via env vars (`AGENT_TIMEOUT_S`,
  `AGENT_MAX_ATTEMPTS`, `AGENT_BACKOFF_INITIAL_S`, `AGENT_BACKOFF_FACTOR`).

### S4.4 — DEGRADED flag vs crash
- **Assumption:** missing `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` logs
  CRITICAL once and sets `agents.config.DEGRADED = True`. Agent code
  must check the flag (or just call `ask_*` which short-circuits and
  returns `default`). Importing `agents` in a no-key environment does
  NOT crash.

### S4.5 — Call log location
- **Path:** `logs/agents/YYYY-MM-DD_agent_calls.jsonl` relative to
  project root. Overridable via `AGENT_LOG_DIR`. Dir auto-created on
  first write. Log-write failures are swallowed with a WARNING (never
  affect the caller's return value).

## S2 test-cleanup (2026-04-21)

- **test_close_long/short_pnl_correct** — stale; `pnl_dollars` is NET of commission (B13). Updated tests to assert `gross_pnl` equals raw tick P&L and `pnl_dollars == gross - commission`.
- **test_record_trade_triggers_cooloff_after_3_consecutive_losses** — stale; `COOLOFF_AFTER_CONSECUTIVE_LOSSES` moved 3→2 in config. Renamed test and parametrized it against the config constant so further tuning doesn't re-break it.
- **test_prod_window_at_close** — stale; primary prod window is now 08:30–11:00 CST (was 08:30–10:00). Rewrote to assert closed at 12:00 (primary/secondary gap) and 11:00 (exclusive at primary close).
- **test_bias_momentum_uses_regime_overrides / test_non_golden_regime_has_tighter_gates** — stale; `_REGIME_OVERRIDES` schema changed. No `min_tf_votes` key anymore (direction gate is hardcoded in `evaluate()`); golden regimes now gate at `min_momentum=80`, off-hours at 60. Tests rewritten to assert contract (keys present per regime) and the invariant (off-hours < live) rather than specific numeric thresholds, so future recalibrations don't re-break them.
- **G-B37 4C integration test** — added `tests/test_4c_integration.py` (12 tests, ~170 lines). Uses real `STRATEGY_ACCOUNT_MAP`; monkeypatches only `oif_writer.OIF_INCOMING` to a tmp dir. Covers guard rejection, nested+flat routing through `write_bracket_order`, byte-exact survival of account strings (incl. spaces/mixed case) in the semicolon OIF format, and the Sim101 default-fallback path.

## S5 — 4A Council Gate (2026-04-21)

- **Coexistence:** `agents/council_gate.py` already contained a legacy `run_council`/`council_to_dict`/`CouncilResult` surface consumed by `bots/base_bot.py` (S6/S7 territory, forbidden to touch). Rather than replace, the S5 spec surface was **appended** to the same file: new `CouncilGate(BaseAgent)` class, `COUNCIL_PERSONAS`, `get_current_bias()`. Both APIs export from the module.
- **Personas (7):** trend-follower, mean-reverter, vol-watcher, gamma-reader, intermarket-analyst, session-historian, contrarian. Each has a one-paragraph `lens` field driving its vote.
- **Models:** voters → `agent_config.MODEL_GEMINI_FLASH` @ temp 0.3, 5s timeout, 200 max_tokens. Orchestrator → `agent_config.MODEL_GEMINI_PRO` @ temp 0.2, 8s timeout, 300 max_tokens. All routed through `AIClient.ask_gemini` with `default=None`.
- **Tie-break:** deterministic `_deterministic_verdict()` is the source of truth. Verdict = BULLISH if BULL >= 4 and > BEAR (same for bearish); otherwise NEUTRAL. 3-3-1 → NEUTRAL (score = max tally / 7). Orchestrator output is trust-but-verified against this tally — if it disagrees with a majority check, we override to deterministic.
- **Timeout/error fallback:** voters use `BaseAgent.safe_call` + `AIClient.parse_json` — any failure yields `{"vote":"NEUTRAL","rationale":"default (timeout or error)"}`. Orchestrator failure falls back to the deterministic verdict with a synthesized summary.
- **Logging:** `logs/council/YYYY-MM-DD.json` — a JSON **array** so multiple intraday runs (session open + regime shifts) append to the same day-file. Overridable via `COUNCIL_LOG_DIR` env var.
- **`get_current_bias()`:** module-level `_CURRENT_BIAS` dict updated after each run; returns copy with `verdict/score/summary/timestamp` (UTC ISO). Bots consult as optional filter — never a hard gate.
- **Prompts:** `agents/prompts/council_voter.md` (parameterized `{persona_name}`, `{persona_lens}`, `{market_json}`) and `agents/prompts/council_orchestrator.md` (`{votes_json}`). Missing-file fallback strings baked into module so tests never touch disk for prompts.

## S7 — 4C Session Debriefer (2026-04-21)
- **Bot name assumption**: scheduled hook reads `{date}_sim.jsonl` — spec said `sim.jsonl`, confirmed `DailyFlattener` is only wired into `sim_bot.py` so "sim" matches the file produced.
- **Dispatch trigger**: piggybacked on the existing 30s `_daily_flatten_loop` poll rather than adding a separate scheduler — same cadence, fires once per day after 16:00 CT via a `_debrief_fired_for` date guard.
- **Telegram**: default-on when `TELEGRAM_BOT_TOKEN` env is set AND `core.telegram_notifier` importable; uses existing `send_sync`. No new module built.
- **Fallback discipline**: Claude failure (exception, None, or missing sections) → deterministic 5-section markdown still written. Header tagged `source=fallback` so downstream tooling can detect degraded output.
- **Preserved legacy path**: existing Gemini-based `run_debrief()` function kept intact for back-compat; new `SessionDebriefer` class added alongside.
- **Hook marker**: used `# [AI-DEBRIEF-HOOK]` in `bots/sim_bot.py::_daily_flatten_loop`. No `# [AI-PRETRADE-HOOK]` marker observed; safe for S6 co-edit.

## S9 — Adaptive Params (2026-04-21)

### A-S9.1 — Proposal ID format
- **Assumption:** `<YYYYMMDD>_<HHMMSS>_<strategy_slug>_<param_slug>` (UTC).
  Matches task spec example `20260421_163022_bias_momentum`.
- **Revisit if:** S8 learner emits its own ID scheme — switch to its IDs.

### A-S9.2 — Source-of-truth for safety bounds
- **Assumption:** Hardcoded in `agents/adaptive_params.py::SafetyBounds`.
  Bounds can only be widened via a human code edit + code review, not at
  runtime. This is intentional — the whole point of S9 is that AI cannot
  loosen its own leash.

### A-S9.3 — Edit strategy for `config/strategies.py`
- **Assumption:** targeted regex-within-block replacement of the literal
  value after the matching `"param":` key, scoped to the matching
  strategy's dict (or to `STRATEGY_DEFAULTS` top-level). AST-verified
  re-parse guards against syntax errors. If the key can't be uniquely
  located, we ABORT rather than guess.
- **Revisit if:** strategies.py grows nested dicts where the same param
  name appears at multiple depths within one strategy's block.

### A-S9.4 — Stop-tick bound detection
- **Assumption:** any param whose name contains both `stop` and `tick`
  is a stop-distance in ticks (so min/max-ticks bounds apply). Covers
  `min_stop_ticks`, `max_stop_ticks`, `stop_fallback_ticks`, etc.

### A-S9.5 — approve_proposal never merges
- **Assumption:** tool leaves the new branch checked out and prints the
  `git merge` command for the human. Auto-merge would defeat the whole
  human-approval design.

## S6 — 4B Pre-Trade Filter (2026-04-21)

- **Replaced legacy implementation**: `agents/pretrade_filter.py` was a pre-S4 Groq-based module with fail-CLOSED (SIT_OUT on any failure). S6 mission spec requires **fail-OPEN** (CLEAR on any failure) so trading never blocks on AI outage. The module was rewritten on S4 infra (`AIClient` / `BaseAgent`). The module-level `check()` function and `FilterVerdict.action` alias were preserved so the existing `bots/base_bot.py` integration keeps working.
- **Model**: Gemini Flash (`MODEL_GEMINI_FLASH`) hard-coded; `model=` kwarg on the legacy shim is accepted and ignored.
- **Timeout**: 3 s hard, applied twice — `ask_gemini(timeout_s=3.0)` plus an outer `asyncio.wait_for(timeout=3.5)` belt-and-braces guard so a misbehaving inner call can't stall the tick loop.
- **`ai_filter_mode` config**: added `DEFAULT_AI_FILTER_MODE = "advisory"` to `config/strategies.py` and a bottom-of-file loop that backfills every entry in `STRATEGIES` with `"ai_filter_mode": "advisory"`. One line per strategy was rejected as noisy for 10+ entries; the loop accomplishes the same thing with one source of truth.
- **Hook in `bots/base_bot.py`**: the rich context-gathering block (news/MQ/RAG injection, lines ~1065-1120) was kept — it is production wiring other streams consume. Only the verdict-handling block (previously ~14 lines including RAG near-miss logging on SIT_OUT) was replaced with a 5-line mode-aware gate tagged `# [AI-PRETRADE-HOOK]` at line ~1148.
- **Default verdict on failure**: `CLEAR` with `source="default"`. Advisory mode means SIT_OUT is logged but the trade still proceeds; blocking mode respects SIT_OUT. CAUTION is logged at WARNING level; sizing reduction is already handled downstream in `_enter_trade` via `self._filter_verdict`.
- **Cache**: none. Every signal triggers one fresh Gemini Flash call.
- **Test strategy**: monkey-patched `AIClient.ask_gemini` via a `FakeAIClient` subclass for behavior-driven tests (clear/caution/sit_out/fenced/junk/none/raise/timeout). JSONL-write test stubs the lower-level `_gemini_once` to exercise the real retry/log path.
