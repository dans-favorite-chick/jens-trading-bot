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
