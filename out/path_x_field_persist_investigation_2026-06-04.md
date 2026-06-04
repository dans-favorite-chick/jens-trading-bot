# Path X — Strategy-Blocking Field Persistence Investigation

_Drafted: 2026-06-04 — confluence-firing sprint, Phase 1 deliverable._
_Branch: `weekly-evolution/2026-05-24` · HEAD: `08fa958` · FREEZE_ACTIVE = True._

---

## TL;DR — Verdict

**Option D — "Something else": the persistence wiring is ALREADY in place and
shipping correctly as of commit `856f317` (2026-05-28). No Path X code fix
is needed. The operator's "10 sampled May trades empty" evidence comes from
pre-fix trades. Current-code trades carry all 4 fields.**

This sprint should:
1. **Skip Phase 3a** (no code fix needed — already shipped 7 days ago).
2. **Still proceed to Phase 4** (regression test that asserts the wiring
   stays in place — defense-in-depth against future drift).
3. **Proceed to Phase 2** (Path Y threshold-drift diagnostic) as planned.

Two RESIDUAL gaps are surfaced below as **observations** (not in-scope fixes):

- **R1** — `bots/_strategy_dispatch.py` (prod path) never sets
  `market["es_nq_rs"]`; only `bots/sim_bot.py`'s override does. Effect on
  prod is minimal because `core/confluence_gates.py:74` grace-degrades to
  None and doesn't reject. Worth filing as a separate finding.
- **R2** — `core/history_logger.py:log_eval` (line 162-205) only logs
  `cr_verdict` from the 4 fields; `day_type`/`cvd_health`/`es_nq_rs` are
  not surfaced in eval events. They DO get logged on entry events
  (line 239-240, full market dict). Observability gap, not persistence gap.

---

## Phase 1.1 — Lifecycle bucketing

Grep across `core/ bots/ strategies/ agents/` for the 4 fields returned
46 files. Bucketed:

### COMPUTE-SITE
- `core/day_classifier.py` → day_type
- `core/cvd_trend_health.py` → cvd_health
- `core/continuation_reversal.py` → cr_verdict
- `core/market_intel.py` (read by sim_bot._latest_intel) → es_nq_rs

### WRITE-SITE (writes into the `market` dict)
- `bots/_strategy_dispatch.py:205` — `market["cvd_health"] = self.bot.cvd_health.assess("LONG")`
- `bots/_strategy_dispatch.py:366` — `market["cr_verdict"] = _cr.verdict`
- `bots/_strategy_dispatch.py:400` — `market["day_type"] = _day.day_type`
- `bots/_strategy_dispatch.py:911` — `self.bot._last_enriched_market = dict(market)` (stash for `_trade_entry`)
- `bots/sim_bot.py:605-661` — sim_bot OVERRIDE that mirrors the base dispatch path AND adds `es_nq_rs` (line 627)
- `bots/sim_bot.py:789` — sim_bot stash mirror of dispatch:911

### MERGE-SITE (copies stash into market before persisting)
- `bots/_trade_entry.py:177-186` — iterates over `("day_type", ..., "es_nq_rs", "intermarket", "advisor_guidance", "mq_direction_bias")` and merges from `_last_enriched_market` into `market` with the `_k not in market` guard (preserves fresh ATR/price)

### PERSIST-SITE (writes to disk)
- `bots/_trade_entry.py:1297-1320` — `self.bot.positions.open_position(..., market_snapshot=market, ...)`
- `core/history_logger.py:239-240` — full market dict written to today's history JSONL on entry event
- `core/trade_memory.py` — `position_manager.close_position()` eventually writes the row via canonical reader/writer (this side I read for context only; protected file)

### READ-SITE (consumers in strategies)
- `core/confluence_gates.py:74` — reads `market.get("es_nq_rs")`, falls back to `intermarket.nq_es_relative_strength`, returns None on miss (grace-degrade)
- `strategies/bias_momentum.py:17,105` — uses `tf60m_es_gate` and reads `advisor_guidance` with `or {}` fallback
- Other strategies (`vwap_pullback`, `vwap_pullback_v2`, `dom_pullback`, `spring_setup`, `vwap_band_reversion`, `nq_lsr`) read via same gate helpers — all grace-degrade

---

## Phase 1.2 — Write happens BEFORE evaluate() returns

In `_strategy_dispatch.py`:
- `evaluate()` runs every enrichment block (lines 178-405) BEFORE the
  strategy loop at line 600.
- `strat.evaluate(market, ...)` at line 625 sees the populated market dict
  (day_type, cr_verdict, cvd_health are present; es_nq_rs is NOT set on
  this base path — only on sim_bot's override).
- The stash at line 911 fires AFTER `best_signal` is picked, so `dict(market)`
  captures all enrichment.

In `sim_bot.py`:
- Override mirrors the base path: enrichment writes (lines 605-661) happen
  BEFORE the strategy loop at line 678.
- Stash at line 789 captures everything (including `es_nq_rs` from line 627
  when `_latest_intel` has been populated by the news scanner).

`_trade_entry.py:177-186` merge runs AFTER `aggregator.snapshot()` (line 168)
and BEFORE `positions.open_position(market_snapshot=market)` at line 1297.
The merge guard `if _k in self.bot._last_enriched_market and _k not in market`
correctly preserves fresh price/ATR.

**Conclusion: no temporal-order bug. Writes happen before reads.**

---

## Phase 1.3 — history_logger persists what it sees

`core/history_logger.py:log_entry()` at line 239-240 writes
`"market": {k: v for k, v in market.items() if k not in ("tf_bias", "gamma_levels", "gamma_regime")}`.

This is a full dump minus the 3 excluded keys. So if the strategy / merge step
populated the 4 fields in `market`, they reach disk.

`log_eval()` at lines 162-205 is narrower — only `cr_verdict` from the 4 fields
is surfaced. This is observability gap **R2** noted above.

`position_manager.open_position` (protected file; read-only inspection) takes
`market_snapshot=market` and stores it on the Position. On close, it writes the
trade row to trade_memory (canonical reader/writer at `core/trade_memory.py`).

**Conclusion: persistence is end-to-end if the merge populated the fields.**

---

## Phase 1.4 — Trade table (5 early May + 5 late May)

Sampled via `from core.trade_memory import load_all_trades`:

| Entry time (CT)    | trade_id       | n_keys | day_type | cr_verdict | cvd_health | es_nq_rs |
|--------------------|----------------|-------:|:--------:|:----------:|:----------:|:--------:|
| 2026-05-03 23:24   | 9598dc1b       |     95 | ✗        | ✗          | ✗          | ✗        |
| 2026-05-03 23:42   | 1dcafb43       |     95 | ✗        | ✗          | ✗          | ✗        |
| 2026-05-03 23:57   | d9c1b025       |     95 | ✗        | ✗          | ✗          | ✗        |
| 2026-05-04 00:14   | 045b7ac9       |     95 | ✗        | ✗          | ✗          | ✗        |
| 2026-05-04 00:47   | 410322e6       |     95 | ✗        | ✗          | ✗          | ✗        |
| 2026-05-26 14:06   | a95d1bcb       |    107 | ✓        | ✓          | ✓          | ✓        |
| 2026-05-26 14:31   | 13c6a1e9       |    107 | ✓        | ✓          | ✓          | ✓        |
| 2026-05-26 15:17   | 98016b37       |    107 | ✓        | ✓          | ✓          | ✓        |
| 2026-05-26 20:00   | f4918cbc       |    107 | ✓        | ✓          | ✓          | ✓        |
| 2026-05-26 20:42   | 9bb2f51b       |    107 | ✓        | ✓          | ✓          | ✓        |

Cutoff is commit `856f317` (2026-05-28 01:22:34 -0500), title
_"fix(reconcile): persist day_type/cr_verdict/cvd_health/es_nq_rs on sim trades"._

Window-level stats across all `bias_momentum` rows:

| Window               | Trades | All 4 present | Missing at least one |
|----------------------|-------:|--------------:|---------------------:|
| Early May (5/1–5/14) |     69 |             0 |                   69 |
| Mid May  (5/15–5/24) |     58 |            37 |                   21 |
| Late May (5/25–5/31) |     25 |            24 |                    1 |
| June     (6/1–6/4)   |      0 |             — |                    — |

Late-May had ~96% of trades carry all 4 fields. Mid-May has the transition
straddling the 856f317 ship date. June has zero entries — that's the
firing-rate problem (Path Y), not a persistence problem.

---

## Phase 1.5 — Live eval evidence (today, 2026-06-04)

Today's `logs/history/2026-06-04_sim.jsonl`:

- 1050 `eval` events; **1050/1050 carry `cr_verdict` populated (non-UNKNOWN)**
- 17 best-signal events on sim
- 0 entry events on sim or prod (firing-rate failure, downstream)

`log_eval` only surfaces `cr_verdict` of the 4 fields, so I cannot
conclude about day_type/cvd_health/es_nq_rs presence today from log
scraping alone. But the dispatch code at `_strategy_dispatch.py:205,366,400`
sets all 3 unconditionally before the strategy loop — if eval is running
(it is — 1050 events), those fields are in the market dict the strategies
see.

The "Reconciliation harness BLOCKED 3/3 in smoke test" the operator's
Cowork-Claude observed must have run against pre-fix historical sim trades.

---

## Phase 1.6 — STOP-CONDITION check (no fix needed)

The sprint's STOP condition gates a fix that touches a protected file.
My finding is more conservative: **no fix is needed at all**. The
canonical PROTECTED_FILES.md was respected — I only READ
`core/position_manager.py` and `core/trade_memory.py`; never edited.

`bots/_strategy_dispatch.py`, `bots/_trade_entry.py`, `bots/sim_bot.py`,
`core/history_logger.py` are all unprotected. Even if a fix were needed,
no protected edit would be required.

The operator's MAY-EDIT scope included `bots/_signal_router.py`; I
confirmed it exists (AI pre-trade filter dispatch, not on the enrichment
path) and is irrelevant to this finding.

---

## Residual gaps (observations, not in-scope fixes)

### R1 — Prod-path `es_nq_rs` gap

`_strategy_dispatch.py` (used by prod_bot via base_bot) sets day_type,
cr_verdict, cvd_health but **NOT** `es_nq_rs`. Only `bots/sim_bot.py`'s
override (line 617-627) sets it, sourced from `_latest_intel`
(news-scanner polled every ~2 min). The `agents.market_advisor.enrich_market_snapshot`
call at dispatch:275 also doesn't set `es_nq_rs` (it returns the merged
market dict but doesn't compute that field).

**Consequence:** prod_bot bias_momentum trades since 2026-05-24 carry
3 of 4 fields. Since `confluence_gates.py:74` grace-degrades on None
(falls back to `intermarket.nq_es_relative_strength`, then to None),
strategies don't silently reject. The reconciliation harness DOES
care, however — `tools/reconcile_sim_vs_backtest.py:88` lists `es_nq_rs`
as one of the 4 required fields for replay.

Suggested follow-up (not this sprint): file FINDING-PROD-ES_NQ_RS and
either (a) wire `_latest_intel` into base_bot the way sim_bot does, or
(b) compute es_nq_rs directly in `_strategy_dispatch.py` near the
intermarket enrichment at line 262.

### R2 — `log_eval` surface

`history_logger.log_eval` (lines 162-205) records cr_verdict, but not
day_type, cvd_health, or es_nq_rs at the eval level. This is observability
debt — operators inspecting eval JSONL can't see all 4 strategy-branching
fields. Adding them is safe (additive keys; old readers ignore unknown).

Suggested follow-up (not this sprint): a 4-line addition to `log_eval`'s
dict write, gated by an additive-only review.

### R3 — Today's zero-entries presentation

`2026-06-04_sim.jsonl` has 17 best-signals but 0 entries. That happens
downstream of `_strategy_dispatch` (which only stages `_pending_signal`)
in `_signal_router → _trade_entry`. Likely causes:
- Pre-entry filters in `_trade_entry` (no-new-entries window, roll gate,
  NT8 sink health, risk gate, tier_sizer halt, calendar block, portfolio cap,
  fill timeout, phantom guard) rejecting before `positions.open_position`
- Or PHANTOM-NT8 silent rejection (operator's reported issue today, NT8 ATI
  rejecting OIF; documented as `FINDING-2026-06-04-PHANTOM-NT8` OPEN in
  findings_tracker)

This is the Path Y / PHANTOM-NT8 territory — out of scope for Path X.

---

## What the sprint should do next

| Phase | Action | Rationale |
|---|---|---|
| 3a | **SKIP** | Wiring already shipped 2026-05-28 (`856f317`); no edit needed |
| 4 | Write regression test that asserts the wiring stays in place; run pytest baseline | Defense-in-depth; existing `tests/test_enriched_market_persistence.py` already covers most invariants. Sprint's required new test (`test_strategy_blocking_fields_persistence.py`) should add the entry-time persistence assertion that closes the loop end-to-end. |
| 2 | Proceed with Path Y diagnostic as planned | Threshold drift is now the *only* live theory for the firing-rate gap |
| 5/5.7/6/7 | Proceed as planned; tracker row for FIELD-PERSIST is STALE (already-fixed off-prompt) not RESOLVED-this-sprint | Findings_tracker has a "STALE" status for already-fixed findings — credit `856f317` |

The operator should be surfaced this verdict and given the chance to
either confirm the SKIP or ask me to file/audit something different
(e.g., R1 prod-path es_nq_rs gap as a parallel correctness fix). I will
NOT silently proceed to Phase 3a — that would burn a no-op commit.

---

## Self-check — what could be wrong with this verdict

1. **The "wiring works" inference relies on 24/25 late-May trades having the
   fields.** That's strong evidence but not proof for today. The June=0-trades
   sample means I can't verify directly. Phase 4 regression test should
   include a "today's run on a fresh signal would persist the fields" assertion.
2. **`es_nq_rs` is only verified for sim_bot trades** (prod-path doesn't set
   it — see R1). If the operator's Cowork-Claude was sampling prod trades,
   those would always lack es_nq_rs. But the sprint title said "sim
   bias_momentum trades" so this is consistent.
3. **The reconciliation harness sample of 3/3 BLOCKED** — I don't know which
   3 trades. If those were post-2026-05-28, my verdict is wrong. The
   operator should confirm the sample dates before fully accepting Verdict D.
4. **I did not run the reconcile harness against a recent trade** to verify
   end-to-end. The bot has no June bias_momentum entries to test against,
   so I can't run the harness on a fresh trade. Phase 5.4 (Live Smoke) plan
   should be adjusted: instead of "next sim trade", target the most recent
   late-May sim entry to confirm the harness now PASSES on a known-good trade.
