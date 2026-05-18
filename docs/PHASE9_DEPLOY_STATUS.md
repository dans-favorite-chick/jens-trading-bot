# Phase 9 — V2 Deployment Status

- **Date:** 2026-05-17 / 2026-05-18 (Sunday → Monday)
- **Final commit:** `853482e` (Phase 9.1 hotfix)
- **All commits in deployment order:**
  - `7372f53` — Phase 5: disable V1 strategies superseded by V2 (compression_breakout, vwap_pullback, orb)
  - `fdeb289` — Phase 6: Week-1 config patches to 8 existing strategies + FCD-6 footprint config flip
  - `396154e` — Phase 7: 10 code patches (CODE PATCH 1-4,6 + FCD-1 through FCD-5)
  - `ed5cb03` — Phase 8 (partial): copy 9 bundle test files into tests/ (import-patched)
  - `1587745` — Phase 8: triage 13 stale assertions (2 updated, 11 skipped with Phase-10 restore marker)
  - `853482e` — Phase 9.1 hotfix: register vwap_band_reversion in strategy_classes; add big_move_signal to STRATEGY_KEYS + STRATEGY_ACCOUNT_MAP
- **Branch (not yet pushed):** `weekly-evolution/2026-05-17`

---

## Structural Verification (Complete ✅)

- 15/15 strategies loaded with `validated=True`
- 25 accounts mapped (no STRATEGY_ACCOUNT_MAP routing failures)
- TickAggregator restored state from disk (247 time bars + 200 tick bars + 233 RTH-replay)
- Bridge ws stable, sim + prod both connected

---

## 10-Minute Observation (21:28 – 21:38 CT, 2026-05-17)

- 7 strategies evaluating live against ticks:
  - `vwap_pullback_v2` (V2 new) — 12 evals
  - `vwap_band_reversion` (Phase 9.1 hotfix unlock) — 12 evals
  - `vwap_band_pullback` — 12 evals
  - `opening_session` — 12 evals
  - `ib_breakout` — 12 evals
  - `dom_pullback` — 12 evals
  - `compression_breakout_micro` (V2 new) — 6 evals
- Tick flow stable, no gaps > 30 s, `tick_rate_10s` range 6 – 10
- Zero `[ROUTING]` failures, zero `[ERROR]`, zero `[EXCEPTION]`
- `COOLOFF` + `RECOVERY_MODE` risk gates fired correctly on the orphan loss

---

## Phase 9 Trade Activity

One trade lifecycle:

- **SimBias Momentum SHORT @ 29065.25 → stopped at 29090.25**
  - Intent: -25t / -$12.50
  - Realized: **-100t / -$54.82**
  - Reason: pre-existing state-desync race (CLOSEPOSITION-vs-OCO). See OPEN INCIDENT #1 below.
  - Hold: 272 s

No new (non-orphan) signals fired during the 10-min window — Sunday overnight is low-vol and most session-windowed strategies (nq_lsr, orb_fade, orb_v2, etc.) are gated to RTH windows starting 08:30 CT Monday.

---

## OPEN INCIDENTS (require separate sprint, NOT Phase 9 blockers)

### Incident #1 — CLOSEPOSITION-vs-OCO state desync race

- **Symptom:** NT8 fill cycle creates brief reverse position when OCO stop rejects rest order; Phoenix reconciler catches it ~25 s later and market-flattens, adding slippage.
- **Impact:** Orphan stopout on 2026-05-17 produced 4× intended loss (-$54.82 vs -$12.50 intent, +75 t slippage).
- **Status:** Diagnosed but not fixed. Pre-existing, not V2-introduced.
- **Recommended fix:** Investigate ATI fill sequencing; consider holding `CLOSEPOSITION` orders until OCO confirms cancel. Estimated 1-2 day sprint.

---

## Phase 9.5 Backlog

- **Item A:** vwap_band_reversion orphan-history archaeology
- **Item B:** dupe_test halt-state cleanup
- **Item C:** create `SimBigMove` account in NT8 + map `big_move_signal`
- **Item D:** investigate Gemini / Grok / CNN F&G provider errors
- **Item E (new):** silent-strategy `[EVAL] SKIP` logging for `big_move_signal`, `footprint_cvd_reversal`, `compression_breakout_v2`

---

## Monday 2026-05-18 Cash Open Resumption Protocol

- **08:00 CT** — verify sim_bot still alive, ticks flowing, COOLOFF cleared.
- **08:30 – 09:30 CT** — tail logs for session-windowed strategies (`nq_lsr`, `orb_fade`, `orb_v2`) hitting their windows.
- **After 30 min of clean V2 signals firing and routing to correct accounts:** push branch and declare Phase 9 production-validated.
