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
