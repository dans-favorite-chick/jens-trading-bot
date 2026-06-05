# Phase 0 — Prereq + stale audit findings (2026-06-04)

Scratch notes consumed by Phase 1's `out/winning_conditions_data_2026-06-04.md`.

## 0.1 Branch + HEAD
- Branch: `weekly-evolution/2026-05-24` ✓
- HEAD: `cfee0b8` (memory session changes; ancestor of c43cbb7 confirmed via git merge-base)
- Recent commits: cfee0b8, 2791b9f, 3c75364 (all `memory: Session changes`)
- Uncommitted working-tree changes: heavy (Phase 13 WFA shards, archived backtest results, untracked Python/, agents/market_analyst.py, OneDrive .fuse files). **NOT BUNDLED** — Phase 14 will use explicit-filename `git add -f` only.

## 0.2 Data prereqs
- `data/historical/databento_tbbo/mnq_ticks_clean.parquet` — 438,366,586 bytes (438MB) ✓
- Companion files present: mnq_ticks.parquet, mnq_ticks_slim.parquet, mnq_tbbo_2026-03-17_2026-05-17.dbn.zst (977MB), mnq_footprint_5m.csv, mnq_tbbo_..._footprint_sparse.parquet
- TBBO date window: 2026-03-17 → 2026-05-17
- `logs/volumetric_history.jsonl` — 15,216,764 bytes (~15MB), mtime 2026-06-04 ✓
- `load_all_trades()` returns 1,550 total records ✓

## 0.3 Python packages
- python 3.14.3
- pandas 3.0.2
- numpy 2.4.4
- matplotlib 3.10.8
- scipy 1.17.1

All present at recent versions.

## 0.4 Parallel-safety
- No Confluence Path X conflict checked into git tree at the file-overlap level. No file overlap between this sprint's `out/winning_conditions_*` outputs and Confluence sprint outputs. Both can coexist.

## 0.5 FREEZE + RECONCILIATION banner state
- `config/strategies.py:52` → `FREEZE_ACTIVE: bool = True` ✓
- Lift requires P1-1 reconciliation harness passing for at least bias_momentum + operator sign-off on divergence tolerances.

**FREEZE_BANNER applies:** `[FREEZE-BLOCKED — RESEARCH OUTPUT ONLY. NOT DEPLOYABLE UNTIL FREEZE-LIFT SPRINT SHIPS. See config/strategies.py:40-45 for lift preconditions.]`

Reconciliation report inventory in `out/`:
- `reconciliation_2026-05-24_bias_momentum.{md,json}` (exists)
- `reconciliation_2026-05-28_bias_momentum.{md,json}` (exists)
- `reconciliation_2026-05-28_bias_momentum_decision_fidelity.md` (exists)
- `reconciliation_2026-06-02_bias_momentum.md` + `_smoke.md` (exist)
- `reconciliation_2026-06-04_bias_momentum.md` ← MOST RECENT, today
- `reconciliation_harness_spec_2026-06-05.md` (forward-dated spec)
- `reconciliation_inspect_2026-05-24.md`

Inspecting the most recent (2026-06-04) bias_momentum reconciliation:
- Window: 2026-05-25 → 2026-05-27 (UTC), 5 sim trades
- **Verdict: FAIL** — backtester emitted 0 direction-matched signals
- 5 / 5 classified `SIM_ONLY` ("no signal in ±5m window around …")
- No replayed trades, no divergence numbers computed
- NOT operator-signed in any sense

**RECONCILIATION_BANNER applies:** `[PRE-RECONCILIATION — Phase 5 counterfactual PnL and Phase 13 envelope dollar numbers are DIRECTIONAL ONLY until the sim↔backtest reconciliation harness runs and produces a defensible per-strategy divergence number. See config/strategies.py:40-45 freeze-lift precondition #1.]`

For opening_session: NO reconciliation reports exist at all → banner applies trivially.

`OUTPUT_BANNER` = FREEZE_BANNER + "\n" + RECONCILIATION_BANNER (both non-empty, both required on every Phase 5 + Phase 13 deliverable, both to chat and to disk).

## 0.6 Snapshot completeness — REVISED VERDICT

Initial audit reported 0/111 winners had all required fields → would have deferred Phase 6 entirely. Inspection of actual snapshot schema reveals the audit was checking for field NAMES that don't exist:

**Actual snapshot has 107 distinct fields per record.** Hit rates on last 30 bias_momentum trades:

| Spec-required field | Actual schema match | Hit rate |
|---|---|---|
| `day_type` | `day_type` | 30/30 (100%) |
| `cr_verdict` | `cr_verdict` | 29/30 (96.7%) |
| `cvd_health` | `cvd_health` (dict) | 30/30 (100%) |
| `regime` | `regime` | 29/30 (96.7%) |
| `momentum_score` | **NOT stored — proxy `cr_mom_score`** | proxy 29/30 |
| `confluence_score` | **NOT stored at all** | **0/30** |
| `tf_bias_1m` | not stored separately | 0/30 |
| `tf_bias_5m` | not stored separately | 0/30 |
| `tf_bias` (consolidated) | `tf_bias` + `tf_votes_bullish`/`tf_votes_bearish` | 29/30 |
| `ema9` | `ema9` | 29/30 |
| `ema21` | `ema21` | 29/30 |
| `vwap` | `vwap` | 29/30 |
| `price` | `price` | 29/30 |

Bonus snapshot fields available beyond spec: `mq_direction_bias`, `vol_climax_ratio`, `vsa_signal_5m`, `dom_imbalance`/`dom_bid_heavy`/`dom_ask_heavy`, `prior_day_poc`/`vah`/`val`, `pivot_pp`/`r1`/`r2`/`s1`/`s2`, `es_nq_rs`, `bar_delta`/`bar_buy_vol`/`bar_sell_vol`, `delta_history_5m`, `cr_at_resistance`/`cr_at_support`, `microstructure`, `fill_latency_ms`.

**Revised Phase 0.6 verdict — environmental completeness: 96.7% → Phase 6 PROCEEDS but with PARTIAL retroactive evaluation:**
- Can retroactively evaluate: `regime_veto`, `session_block_window`, `tf_bias` (consolidated) direction match
- Partial: momentum threshold via `cr_mom_score` proxy (different scale; document caveat)
- **Cannot retroactively evaluate `min_confluence`** — field literally not persisted to trade memory. Note prominently in report.

## 0.7 Holdout window split
- Today (UTC): 2026-06-04
- Rich-data window: 2026-03-06 → 2026-06-04 (90 days)
- **DERIVATION SET**: 2026-03-06 → 2026-05-05 (oldest 60 days)
- **HOLDOUT SET**: 2026-05-05 → 2026-06-04 (most recent 30 days)
- DERIVATION trade counts: bias_momentum=234, opening_session=2
- HOLDOUT trade counts: bias_momentum=143, opening_session=6

TBBO coverage overlap:
- TBBO window = 2026-03-17 → 2026-05-17
- DERIVATION ∩ TBBO = 2026-03-17 → 2026-05-05 (~50 of 60 derivation days have TBBO; first ~11 days fall back to volumetric_history)
- HOLDOUT ∩ TBBO = 2026-05-05 → 2026-05-17 (~12 of 30 holdout days have TBBO; remaining ~17 days fall back)

## Sample-size verdicts vs spec targets
- bias_momentum: 392 closed non-RECONCILED trades — **OK** (target ≥ 100) → TENTATIVE tier per CLAUDE.md
- opening_session: **8** closed non-RECONCILED trades — **WAY BELOW TARGET** (target ≥ 500) → INSUFFICIENT_SAMPLE tier

**This is the biggest data-reality divergence from the sprint plan.** Spec assumed ~3,700 opening_session trades over the 90d window (Phase 13.6.6). Actual = 8 total, all-time. Implications:

- Phase 2/3 statistical tests on opening_session are not viable (n=8 cannot pass Mann-Whitney with any post-correction p-threshold)
- Phase 4 entry-timing distributions on opening_session are descriptive only
- Phase 9 charts: cannot produce top-5 winners + top-5 losers cleanly (8 total trades; possibly all winners or all losers); will produce what exists
- Phase 13 envelope for opening_session must be LOW-confidence on every parameter
- Phase 13.6 in-window WFA for opening_session will trip INSUFFICIENT_SAMPLE per its own n_trades_oos ≥ 20 criterion

The opening_session strategy may be:
- Recently added / rarely-firing (sim/prod gate rejecting heavily)
- Or stored under a different name (no other obvious candidate in the strategy roster — `high_precision_only` and `spring_setup` are different strategies)

**Recommendation embedded in plan execution:** bias_momentum gets full statistical + counterfactual + envelope treatment. opening_session gets a descriptive section + a single-line verdict: "INSUFFICIENT_SAMPLE — n=8 closed trades total, statistical analysis declined, no envelope recommendation possible."

---

Phase 0 verdict: GO with stipulations above. Banners ready. Holdout boundary locked. Phase 6 partial; opening_session demoted to descriptive-only.
