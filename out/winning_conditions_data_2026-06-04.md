# Winning Conditions Sprint — Data Report
*Phase 1 deliverable | 2026-06-04*

This document records the assembled analysis dataset for the
2026-06-04 Winning Conditions Reverse-Engineering sprint. The dataset
is the foundation for all Q1–Q6 analyses (Phases 2–13).

---

## 1 · Phase 0 audit summary (carried forward)

| Item | Verdict |
|---|---|
| Branch | `weekly-evolution/2026-05-24` ✓ |
| HEAD | `cfee0b8` (ancestor of c43cbb7 confirmed) ✓ |
| TBBO clean parquet | 438 MB, ts range 2026-03-17 → 2026-05-15 ✓ |
| `logs/volumetric_history.jsonl` | 15 MB, mtime 2026-06-04 ✓ |
| Python stack | 3.14.3 + pandas 3.0.2 + numpy 2.4.4 + matplotlib 3.10.8 + scipy 1.17.1 ✓ |
| `FREEZE_ACTIVE` | `True` (`config/strategies.py:52`) → **FREEZE_BANNER required** on Phase 5 + 13 outputs |
| Reconciliation status | bias_momentum reports exist but most-recent `reconciliation_2026-06-04_bias_momentum.md` verdict = FAIL (0/5 direction-matched); no opening_session report → **RECONCILIATION_BANNER required** on Phase 5 + 13 outputs |
| **DERIVATION SET** | 2026-03-06 → 2026-05-05 (oldest 60 d of the 90-d rich-data window) |
| **HOLDOUT SET** | 2026-05-05 → 2026-06-04 (most recent 30 d) |
| Phase 6 retroactive eval | **PARTIAL** — `confluence_score` not persisted to trade memory; can evaluate `regime_veto`, `session_block_window`, `tf_bias` direction, momentum via `cr_mom_score` proxy |

---

## 2 · Dataset specification

**Source loader:** `core.trade_memory.load_all_trades()` (canonical reader; never raw-open `logs/trade_memory*.json`).

**Closed non-RECONCILED filter:**
- exit_price OR exit_time present
- status ≠ "RECONCILED"; `reconciled` flag ≠ True
- source / origin ≠ "reconciliation"
- **result ≠ "NO_FILL"** (PHANTOM-NT8 phantom-rejection records have no entry_time and contaminate winner stats)
- entry_time non-null

**Scope:** `strategy ∈ {bias_momentum, opening_session}`.

**Win/loss classification:** `result == "WIN"` → True, `result == "LOSS"` → False (100 % populated). Fallback to `pnl_dollars_net` sign for any record lacking `result`.

**Net PnL extraction (fallback chain):**
1. `pnl_dollars_net` (modern format, 38.8 %)
2. `pnl_dollars` (canonical legacy alias, 100 %)
3. `pnl_dollars_gross − commission_dollars` (older format, 93 %)
4. Price-derived: `(exit − entry) × dir × contracts × 2.0 − commission` (MNQ $2/pt)

Result: `pnl_dollars_net` is 100 % computable across the 385-row dataset.

**R-multiple:** stored `r_multiple` is only 23.1 % populated (modern format). Computed `r_multiple_computed = pnl_dollars_net / (|entry − stop| × 2.0 × contracts)` is 72.7 % populated. Phases 2.5 / 3.5 top-5 selection uses `r_multiple_computed` with `pnl_dollars_net` tie-break.

---

## 3 · Final row counts

| Strategy | DERIVATION | HOLDOUT | Total | Tier (per CLAUDE.md) |
|---|---:|---:|---:|---|
| `bias_momentum` | 234 | 143 | **377** | TENTATIVE (100–384) |
| `opening_session` | 2 | 6 | **8** | **INSUFFICIENT_SAMPLE (<30)** |

**Excluded:** 15 NO_FILL bias_momentum records (PHANTOM-NT8 phantom rejections, no entry_time). These appear in operational counts but cannot contribute to winner/loser analysis.

### Opening_session reality check
Sprint plan expected ~500+ closed trades (Phase 0.2 target) and "~3,700 opening_session trades over ~90 days" (Phase 13.6.6). Actual = **8 trades all-time**. Implications carried forward into every downstream phase:

- **Phase 2/3 statistical tests on opening_session: declined.** n=8 cannot pass Mann-Whitney with any Bonferroni-survivable p-threshold.
- **Phase 4 entry-timing distributions on opening_session: descriptive only.**
- **Phase 9 charts on opening_session: produce whatever exists** (cannot do clean top-5 wins + top-5 losses; will surface all 8).
- **Phase 13 envelope for opening_session: LOW confidence on every parameter.**
- **Phase 13.6 in-window WFA for opening_session: trips its own INSUFFICIENT_SAMPLE per n_trades_oos ≥ 20 criterion.**

Every opening_session conclusion in this sprint is labeled accordingly. The sprint's depth lands on bias_momentum.

### Win/loss balance
| Strategy | WIN | LOSS | win_rate |
|---|---:|---:|---:|
| bias_momentum | 111 | 266 | 29.4 % |
| opening_session | (per-record disclosed in Phase 2) | | |

---

## 4 · Feature list with computability rates

The dataset has 144 columns. Categorized:

### Identity (all 100 %)
`trade_id`, `strategy`, `sub_strategy`, `bot_id`, `account` (62.9 %), `entry_time_iso`, `entry_time_epoch`, `exit_time_iso`, `exit_time_epoch`, `direction`, `entry_price`, `exit_price`, `stop_price`, `target_price`, `entry_reason`, `exit_reason`, `result`, `result_label`, `tier` (0 % — not stored on older records), `contracts`, `commission_dollars` (41 %), `exchange_fees_dollars` (41 %), `slippage_dollars` (41 %), `pnl_dollars_gross` (93 %), `pnl_dollars_net` (100 % via fallback), `pnl_dollars_raw` (100 %), `pnl_ticks` (96 %), `r_distance` (23 %), `r_multiple` (23 %), `r_multiple_computed` (73 %), `r_distance_dollars` (73 %), `mae_price`/`mae_ticks` (23 %), `mfe_price`/`mfe_ticks` (23 %), `mfe_capture_pct` (23 %), `hold_time_s` (100 %), `win` (100 %), `split` (100 %), `entry_in_tbbo_window` (100 %), `session_phase_ct` (100 %).

### Snapshot environmental fields (mostly ≥ 90 %)
Hit rate annotated:

- `regime` 90.4 %, `vwap` 90.4 %, `ema9` 90.4 %, `ema21` 90.4 %, `ema5` 69.4 %
- `atr_1m` / `atr_5m` / `atr_15m` / `atr_60m` 90.4 %, `atr_tick` 81.3 %
- `cvd` 90.4 %, `bar_delta` 90.4 %, `bar_buy_vol`/`bar_sell_vol` 90.4 %, `cvd_method` 90.4 %
- `tf_votes_bullish`/`tf_votes_bearish` 90.4 %, `tf_bias_tick` 81.3 %, `tf_bias` 0 % (consolidated field not persisted — only the per-TF vote counters are)
- `dom_imbalance`/`dom_bid_heavy`/`dom_ask_heavy`/`dom_depth` 90.4 %; `dom_signal` 0 %
- MACD family (`macd_line` etc.) 66 %
- `vwap_std`/`vwap_upper1`/`vwap_lower1`/`avwap_pd_*` 64.4 %
- `vsa_signal_5m` 55.3 %, `vol_climax_ratio` 55.3 %
- `ema9_15m` / `ema21_15m` 55.3 %
- Pivot family (`pivot_pp`/`r1`/`r2`/`s1`/`s2`) 53 %; prior_day_* 53 %
- `pmh`/`pml`/`rth_open_price`/`rth_*_high`/`*_low` 17–25 %
- `opening_type`/`opening_holds_outside_at_845`/`orb_first_break_direction` 17–34 %
- **Recent-instrumentation fields (16.1 %):** `day_type`, `day_type_reason`, `cr_verdict`, `cr_mom_score`, `cr_confidence`, `cr_direction`, `cr_at_resistance`, `cr_at_support`, `mq_direction_bias`, `cvd_health_*`, `es_nq_rs`
- `signal_price` 90.4 %, `fill_latency_ms` 61.6 %, `now_ct` 54.5 %
- `cvd_health_short`/`ms_*` 0 % (not present in this strategy's snapshot)

### TBBO tick-window features (77.4 % within window)
- `tick_count_5m_before` / `tick_count_5m_after`
- `bid_volume_5m_before` / `ask_volume_5m_before`
- `delta_aligned_ratio_5m` ← **the contrarian signal flagged by the earlier footprint feasibility study**
- `spread_avg_5m` / `spread_max_5m`
- `price_5m_high` / `price_5m_low` / `price_range_5m_ticks`
- `cvd_slope_5m_per_min` / `cvd_slope_1m` / `cvd_acceleration`

### Entry-timing features (Q3 inputs)
- `price_position_in_5m_bar` (77.4 %, 0 = bar low, 1 = bar high)
- `distance_from_5m_high_ticks` / `distance_from_5m_low_ticks` (77.4 %)
- `distance_from_ema9_ticks` / `distance_from_vwap_ticks` (90.4 %, snapshot-derived)
- `adverse_pre_move_ticks` (76.4 %, signed against direction in t-5m → t-2m window)
- `pullback_flag` (76.4 %, True if `adverse_pre_move_ticks > 0.5 × atr_5m`)

### Regime-detail features
- `session_phase_ct` (100 %, derived from UTC + −5 h CT offset; bins: PREMARKET/OPEN/MORNING/LUNCH/AFTERNOON/CLOSE/OVERNIGHT)

---

## 5 · Coverage map (TBBO vs volumetric_history)

| Window | Trades (bias_mom) | Source for tick features |
|---|---:|---|
| `< 2026-03-17` | 0 | (no DERIVATION trades pre-TBBO) |
| `2026-03-17 → 2026-05-05` (DERIVATION ∩ TBBO) | 234 | TBBO clean parquet |
| `2026-05-05 → 2026-05-15` (HOLDOUT ∩ TBBO) | ~30–40 | TBBO clean parquet |
| `2026-05-16 → 2026-06-04` (HOLDOUT, post-TBBO) | ~100+ | volumetric_history.jsonl fallback (or null) |

TBBO ends 2026-05-15 23:59:59 UTC. The remaining HOLDOUT trades (~17 days, ~100 trades) have no TBBO data. For those rows the tick-window/entry-timing features are `NaN`. Phase 13.6 in-window WFA uses these trades with snapshot-derived features only — the regime/EMA/VWAP/ATR signals are still present at 90.4 %.

The 22.6 % miss rate on TBBO-window trades (= 1 − 298/385) breaks down as:
- 8 opening_session trades — only 2 in TBBO window, mostly post-2026-05-15
- ~80 bias_momentum trades dated post-2026-05-15 in HOLDOUT

For DERIVATION ∩ TBBO (Phases 2–8's primary statistical engine), TBBO enrichment coverage is effectively 100 % on bias_momentum (234/234 trades fall in window).

---

## 6 · Saved artifact

- Path: `C:\tmp\winning_conditions\wc_dataset.parquet` (not committed; reproducible from script + repo data)
- Builder script: `C:\tmp\winning_conditions\phase1_build_dataset.py`
- Shape: (385, 144)
- Format: pandas DataFrame → pyarrow parquet (object columns coerced to nullable string)

---

## 7 · Caveats locked in for downstream phases

1. **NO_FILL exclusion.** 15 bias_momentum NO_FILL records were excluded from the analysis dataset. They represent operational PHANTOM-NT8 failure modes (the live phantom-fill issue documented in `OPEN_QUESTIONS.md`) and have no entry-time market state. Phase 5 counterfactual notes them separately under the "actual fills vs counterfactual fills" reconciliation.
2. **opening_session insufficient sample.** Every opening_session conclusion downstream is labeled INSUFFICIENT_SAMPLE / descriptive-only / LOW confidence.
3. **`confluence_score` not persisted.** Phase 6 retroactive evaluation of the `min_confluence` gate is **declined** — no proxy field exists. Other gates (`regime_veto`, `session_block_window`, `tf_bias`, momentum via `cr_mom_score`) are testable.
4. **`day_type` only 16.1 % populated.** Phase 7 day_type fingerprint uses `regime` (90.4 %) as the primary day-bucket axis and `day_type` as a secondary lens restricted to the recent ~60-trade subset. The "TREND vs VOLATILE" distinction is reframed through regime + atr_5m intraday volatility quantiles where day_type is missing.
5. **`tf_bias_1m` / `tf_bias_5m` not stored as separate fields.** Only the consolidated `tf_bias_tick` (81 %) and the vote-count pair `tf_votes_bullish` / `tf_votes_bearish` (90 %) are persisted. Phase 6 SHORT-asymmetric tf_bias check uses the vote-count differential as the proxy.
6. **Sample-period banner.** All Phase 5 + Phase 13 deliverables receive the FREEZE_BANNER + RECONCILIATION_BANNER (`OUTPUT_BANNER`) prefix verbatim, both to disk and to chat.
