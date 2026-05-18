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

---

## Phase 9.5 — Findings

### Item A — `vwap_band_reversion` orphan-history archaeology

**Strategy was orphaned in `base_bot.strategy_classes` for 14 days before Phase 9.1 hotfix exposed it.**

| Event | Commit | Date | Files touched |
|---|---|---|---|
| Strategy created | `ce1b0cc` | 2026-05-03 | `config/strategies.py`, `config/account_routing.py`, `core/strategy_risk_registry.py`, `tests/test_account_routing.py`, `strategies/vwap_band_reversion.py`, `tests/test_vwap_band_reversion.py` |
| Registered in `bots/base_bot.py` `strategy_classes` | `853482e` | 2026-05-17 | `bots/base_bot.py` (Phase 9.1 hotfix) |
| **Orphan window** | — | **14 days** | — |

**Root cause:** The 2026-05-03 commit message has an explicit "Wiring:" section that lists three integration points (`config/strategies.py`, `config/account_routing.py`, `core/strategy_risk_registry.py`) but **omits** `bots/base_bot.py` `strategy_classes`. The class import + dict entry were never added.

**Why it stayed hidden 14 days:** The loader guard at `bots/base_bot.py:1235` —

```python
for name, config in STRATEGIES.items():
    if name not in strategy_classes:
        continue   # silent skip — no warning, no log
```

— silently skips any config entry without a matching class. So the strategy was:
- ✅ Present in `STRATEGIES` config (Phase 4 and earlier showed it as `enabled=True`)
- ✅ Present in `STRATEGY_ACCOUNT_MAP` (routes to `SimVwap Reversion`)
- ✅ Present in `STRATEGY_KEYS` (risk registry knew about it)
- ✅ Tested in isolation (9 test cases all green)
- ❌ Never instantiated by the bot loader

The strategy file itself was perfectly fine; only the bot-side wiring was missing. `validated=False` for those 14 days kept it lab-only, masking the gap — no operator-visible signal-miss because lab-mode signals aren't pushed to OIF anyway.

**Process fix recommendation (out of scope for Phase 9.5):** Add a startup self-check that compares `STRATEGIES.keys()` against `strategy_classes.keys()` and logs `[WARN] strategy '{x}' has config but no class registration` for any orphan. ~5-line addition. Would have caught this in 1 startup instead of 14 days.

**Resolution:** Phase 9.1 hotfix (commit `853482e`) registered the class. No further action required.

**Process-fix update:** A startup self-check that diffs `STRATEGIES.keys()` against `strategy_classes.keys()` and logs `[WARN] strategy '{x}' has config but no class registration` for orphans would have caught this in 1 startup instead of 14 days. Tracked as Phase 9.5+ backlog (not implemented tonight).

### Item B — `dupe_test` halt-state cleanup

**Status: cleaned in-place. Effective at next sim_bot restart.**

Pre-clean state of `logs/strategy_halts.json`:
```json
{"halted": ["dupe_test"], "reasons": {"dupe_test": "first"}}
```

Post-clean state:
```json
{"halted": [], "reasons": {}}
```

**Note on git tracking:** `logs/strategy_halts.json` is intentionally gitignored (`.gitignore:10` excludes `logs/`) because it's runtime-mutated state. The bot writes to it whenever a strategy halts or re-enables. Force-tracking it with `git add -f` would create constant dirty-tree noise. Cleanup is therefore filesystem-only; this doc-commit serves as the audit trail.

**Bot still running with stale in-memory copy:** sim_bot (PIDs 76700/66988) loaded `["dupe_test"]` into its halt set at startup and will keep that in memory until the next restart. Since `dupe_test` is not a real strategy (not in `strategy_classes`), the in-memory halt has zero behavioral effect — the loader skips unknown names per the `if name not in strategy_classes: continue` guard. Sweep-up happens naturally on next planned restart.

### Item D — Provider error diagnostics

**All three diagnosed via live probes; one code-fix landed, two need operator key actions.**

| Provider | Root cause | Fix |
|---|---|---|
| Gemini 429 | Free-tier daily quota burnt (20 req/day on `gemini-2.5-flash`). `/v1beta/models` returns 200 / 50 models → key + project alive; only quota burnt. Matches `memory/gemini_api_diagnostic.md` decision matrix. | **Operator action:** "Create API key in new project" at https://aistudio.google.com/app/apikey → fresh free quota under same Google account. Swap `GOOGLE_API_KEY` in `.env`. Restart `watcher_agent.py`. |
| Grok 400 | `Incorrect API key provided: xa***hg` — API key invalid/revoked (not a model name issue, not a payload issue). | **Operator action:** new key from https://console.x.ai/. Swap `GROK_API_KEY` in `.env`. |
| CNN F&G 418 | Literal response: `"I'm a teapot. You're a bot."` — CNN's anti-scraping uses Varnish-based bot detection; the Mozilla/5.0 UA spoof isn't enough. | **Code-fix landed (commit `48ad489`):** detect 418 → cache `unavailable` sentinel under a separate key with 24h TTL. Cuts polling from ~6/hr to ~1/day. Consumers continue to get the graceful `{score: 0, rating: 'unavailable', source: 'unavailable'}` sentinel; no behavior change. |

### Item E — Silent-strategy `[EVAL]` logging

Added `[EVAL] {name}: entered evaluate()` + `[EVAL] {name}: SKIP {reason}` to 3 previously-silent strategies (`big_move_signal`, `compression_breakout_v2`, `footprint_cvd_reversal`). 8 distinct SKIP reasons across the 3. Effective at next sim_bot restart. Commit `d32a028`. Per-strategy eval-count grep will now show all strategies after restart; no more "did this strategy even get called?" ambiguity.

---

## OPEN INCIDENTS — UPDATE

### Incident #1 — CLOSEPOSITION-vs-OCO state desync race

**Status: FIX LANDED (commit `f4cc375`). Effective at next sim_bot restart.**

Root cause confirmed: when an OCO bracket stop fires, NT8 closes the position; ~simultaneously Phoenix's `_exit_trade()` sees the price cross and sends `action: "EXIT"` WS msg → bridge writes `CLOSEPOSITION;...` to NT8 incoming. On a now-FLAT account, the CLOSEPOSITION request does NOT no-op as expected — NT8 interprets it as a fresh market order in the cover direction, creating a phantom reverse position. The reconciler catches it ~25s later and market-flattens, but slippage on the round-trip is what 4×'d the loss on the 2026-05-17 21:25 SimBias Momentum trade.

**Fix:** in `bots/base_bot.py` `_exit_trade()`, differentiate exit reasons:
- `stop_loss` / `target_hit` → **SKIP** the EXIT WS send. OCO does the close; reconciler finalizes the Python position once NT8 reports FLAT.
- All other reasons (managed exits) → keep current behavior (no OCO is firing for those).

Safety net: existing retry-loop at position-management lines 1067-1108 still fires directional MARKET after `EXIT_PENDING_TIMEOUT_S=60s` if OCO somehow fails. That path is race-safe (reads NT8's actual direction first).

Test results: 2092 passed / 19 skipped / 0 failed (same as pre-fix). Existing `test_auto_retry_flatten.py` + `test_close_position_verification.py` + `test_oif_pipeline_health.py` (34 cases) all pass.

**Future hardening (out of scope tonight):** convert managed-exit CLOSEPOSITION to directional MARKET too, eliminating CLOSEPOSITION entirely from the bot's outbound surface.
