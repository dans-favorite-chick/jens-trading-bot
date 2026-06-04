[FREEZE-BLOCKED — RESEARCH OUTPUT ONLY. NOT DEPLOYABLE UNTIL FREEZE-LIFT SPRINT SHIPS. See config/strategies.py:40-45 for lift preconditions.]

[PRE-RECONCILIATION — Phase 5 counterfactual PnL and Phase 13 envelope dollar numbers are DIRECTIONAL ONLY until the sim↔backtest reconciliation harness runs and produces a defensible per-strategy divergence number. See config/strategies.py:40-45 freeze-lift precondition #1. The most recent reconciliation attempt (out/reconciliation_2026-06-04_bias_momentum.md) produced verdict=FAIL — zero direction-matched signals.]

# Winning Conditions Sprint — Consolidated Report
*Phases 12 + 13 deliverable | 2026-06-04*

This report consolidates the Winning Conditions Reverse-Engineering sprint's findings into a single narrative + actionable parameter envelope. Source deliverables: [data](winning_conditions_data_2026-06-04.md), [stats](winning_conditions_stats_2026-06-04.md), [counterfactual](winning_conditions_counterfactual_2026-06-04.md), [regimes](winning_conditions_regimes_2026-06-04.md).

---

## 1 · Executive summary

**What we found.** bias_momentum's winners in the DERIVATION window (2026-03-06 → 2026-05-05, n=54 of 220 = 24.5 % WR) entered with a slight pullback signature — median `adverse_pre_move_ticks` = -15 (against direction) vs losers at -7 (Cliff δ = -0.16, p ≈ 0.07 — does NOT survive Bonferroni). The single feature surviving the K=32 Mann-Whitney Bonferroni panel is `tf_votes_bullish` (δ=+0.318, p=0.00027), but the Phase 11 red-team and R10.1 stats audit both flagged this as **almost certainly a direction confound** — winners are 87 % LONG; the apparent signal is "LONG bias in a bullish tape", not strategy edge. **No reliable single-feature discriminator has been identified after correction.** The strongest robust pattern is regime-conditional: bias_momentum loses money in 7 of 8 regimes including CLOSE_CHOP (0 % WR, n=10) and AFTERNOON_CHOP (14.3 % WR, n=14); only PREMARKET_DRIFT (n=5, +$79.30 total) has positive expectancy in DERIVATION.

**What the counterfactual says.** Relaxing the recently-added `session_block_window 04:00-04:59 CT` gate (Category A, 58 simulated candidates at pre-Apr-18 params) returns -$229.50 over the 8-day eval-log window — INDISTINGUISHABLE from the n=58 random baseline (median identical at $-9.00, win-rate 22.4 % vs baseline 29.3 %). Re-simulating the 31 actual SIGNAL fires at `target_rr=1.5` instead of current `2.5` returns -$99.00 (25.8 % WR). **Both counterfactual relaxations LOSE money** vs the current configuration. The operator's hypothesis "we tightened too much" is not supported by the 8-day eval-log evidence — the post-Apr-18 tightening + recent gate additions appear to be filtering correctly. Caveat: 8 days is a small window, and the random-baseline P95 at n=58 is one trade away from a different verdict (red-team finding 3 — DECLINE survives but evidence is thin).

**What the regime detector adds.** `core/day_classifier.py` already runs the classifier every bar (not at session start, not after-N-bars) and tracks `flip_count` for telemetry. The classifier CAN switch mid-day if cr_verdict or ATR drift across thresholds. Phase 8's intraday stability cannot be measured from DERIVATION data (day_type field 0 % populated in DERIVATION; only the 60+ recent trades have it). A 30-min realized-volatility-expansion detector is proposed as a heuristic for future intraday switch alerts but is unvalidated on a real sample.

**Recommended action — single sentence.** Run no parameter changes in this sprint; the data converge on "current tightening is doing its job"; if anything, the next experiment should be a regime-conditional `bias_momentum.enabled = False` flip for CLOSE_CHOP / AFTERNOON_CHOP / OVERNIGHT_RANGE (those buckets account for ~$272 of historical bias_momentum loss with zero offsetting wins).

---

## 2 · Q1 — Win profile  →  [stats md §Phase 2](winning_conditions_stats_2026-06-04.md#phase-2--win-conditions)

Median DERIVATION winner entered at ATR(5m)=18.99 pt, +452.8 ticks vs VWAP, +51.1 ticks vs EMA9, at 5m-bar-position 0.62 (slightly above bar middle). 5-min `delta_aligned_ratio_5m` median = 0.468 (BELOW 0.5 = aggressor pressure AGAINST direction — slight contrarian footprint). Most common regime: AFTERHOURS (36.9 %), session_phase OVERNIGHT (46.2 %).

---

## 3 · Q2 — Loss profile + discriminators  →  [stats md §Phase 3](winning_conditions_stats_2026-06-04.md#phase-3--loss-conditions--discriminators)

K=32 numeric features Mann-Whitney + Bonferroni (α/K = 0.00156). **One survivor: `tf_votes_bullish`** (winners median 2.5 vs losers 2.0, Cliff δ=+0.318, p=0.00027). Direction-confound caveat per R10.1 + red-team: re-test this as `tf_votes_aligned_with_direction` before drawing any conclusion. The footprint feasibility cross-check on `delta_aligned_ratio_5m` shows the contrarian effect at the per-strategy level but p=0.177 (does NOT survive Bonferroni at the per-strategy slice; full-portfolio Bonferroni had p=0.0004 — the signal exists at portfolio level but not strongly within bias_momentum alone).

---

## 4 · Q3 — Entry timing  →  [stats md §Phase 4](winning_conditions_stats_2026-06-04.md#phase-4--entry-timing-q3-signal-fire-vs-pullback)

`bias_momentum` code fires IMMEDIATELY on signal (no pullback wait in the gate stack — see `strategies/bias_momentum.py:64-340`). The data show winners and losers are NOT cleanly separated by 5m-bar position; the slim signal that exists is winners had ~15-tick adverse pre-move vs losers ~7. **Suggestive only — does not survive correction.** A small-bore additive gate of the form `adverse_pre_move_ticks ≤ -0.5 × atr_5m` belongs in Phase 13 as a LOW-confidence A/B proposal.

---

## 5 · Q4 — Regime analysis + detector  →  [regimes md](winning_conditions_regimes_2026-06-04.md)

**Per-regime expectancy on bias_momentum DERIVATION** (red-team finding #4: conditional on day_type clearance — RANGE-day vetoed evals are excluded):

| regime | n | WR % | total $ | E[V] $ |
|---|---:|---:|---:|---:|
| `AFTERHOURS` | 78 | 30.8 | -178.66 | -2.29 |
| `LATE_AFTERNOON` | 40 | 30.0 | -116.80 | -2.92 |
| `MID_MORNING` | 29 | 27.6 | -115.90 | -4.00 |
| `OPEN_MOMENTUM` | 25 | 20.0 | -35.04 | -1.40 |
| `OVERNIGHT_RANGE` | 19 | 10.5 | -89.28 | -4.70 |
| `AFTERNOON_CHOP` | 14 | 14.3 | -88.08 | -6.29 |
| `CLOSE_CHOP` | 10 | 0.0 | -94.20 | -9.42 |
| `PREMARKET_DRIFT` | 5 | 20.0 | **+79.30** | **+15.86** |

**Negative expectancy in 7 of 8 regimes.** PREMARKET_DRIFT is the only positive, but n=5 is below the INSUFFICIENT_SAMPLE floor (CLAUDE.md tier table: <30 = INSUFFICIENT). The CLOSE_CHOP and AFTERNOON_CHOP rows alone account for $-182 of loss with 2 winners across 24 trades.

**Day-type detector:** `core/day_classifier.py` algorithm documented. Stateless per-bar classification. The TREND/RANGE/VOLATILE bucket can change mid-day. A regime-switch alert based on 30-min realized-vol expansion (`vol_30m(t) ≥ 1.8 × baseline + |Δclose| ≥ 0.15 %`) is proposed in the regimes deliverable; unvalidated.

---

## 6 · Q5 — Counterfactual PnL  →  [counterfactual md](winning_conditions_counterfactual_2026-06-04.md)

| scenario | n | total $ | win-rate | verdict |
|---|---:|---:|---:|---|
| Category A (relax `session_block 04:00-04:59 CT`) | 58 | -$229.50 | 22.4 % | DECLINE — indistinguishable from random baseline at n=58 |
| Category B (re-sim actual fires at `target_rr=1.5`) | 31 | -$99.00 | 25.8 % | DECLINE — lowering target_rr loses |
| Random baseline | 58 | -$139.50 | 29.3 % | (control) |

The earlier draft of Phase 6 reported a `+$981.84` "implied lift" — **retracted per red-team finding #1** as an accounting artifact. The honest signal remains the per-gate win-vs-loss rejection-rate gap (80.1 % losers vs 70.2 % winners filtered by ≥1 current gate). See the counterfactual deliverable for the per-gate table and fill-realism caveats.

---

## 7 · Q6 — Recommended parameter envelope (Phase 13)

**Banner prefix on every numeric proposal below:** the FREEZE_BANNER + RECONCILIATION_BANNER at the top of this document applies. Nothing in this section is deployable; it is a research output for operator review. Per the freeze policy at `config/strategies.py:40-45`, ANY parameter retune justified by backtest is forbidden until the reconciliation harness produces a defensible divergence number for at least bias_momentum.

### 7.1 · bias_momentum recommended envelope

| param | current | recommended range | confidence | evidence |
|---|---|---|---|---|
| `min_confluence` | 5.5 | **HOLD at 5.5** | HIGH | counterfactual Phase 5 shows no threshold-level rejections in 8-day eval window; lowering would not produce more trades; data report flags `confluence_score` is not even persisted so cannot honestly retune |
| `min_momentum` | 80 | **HOLD at 80** | MEDIUM | same as above; persisted `cr_mom_score` proxy is at 16 % coverage |
| `target_rr` | 2.5 | **HOLD at 2.5** | MEDIUM | counterfactual Category B at `target_rr=1.5` loses $99 vs current; current is performing better |
| `max_hold_min` | 60 | **HOLD at 60** | MEDIUM | hold_time_s winner median = 34.7 s, loser median = 16.85 s (winners hold longer — current cap is not binding) |
| `min_tf_votes` | 2 | **HOLD at 2** | MEDIUM | the `tf_votes_bullish` discriminator is a direction confound (red-team); pre-Apr-18 `=3` would have rejected more trades but no evidence the rejected trades were losers |
| `stop_method`, `stop_atr_mult` | atr_anchored 2.0 × | **HOLD** | HIGH | no data in this sprint analyzed stop sizing; existing B14 / V2 deployment params untouched |
| `session_block_windows` | [(04:00, 04:59)] | **HOLD** | MEDIUM | counterfactual Category A: relaxing this LOSES money (-$229.50 over 58 candidates); the gate is doing its job |
| `regime allowlist` | none (regime-veto only) | **PROPOSE explicit veto add: `CLOSE_CHOP`, `AFTERNOON_CHOP`** | MEDIUM-LOW | per-regime DERIVATION expectancy: CLOSE_CHOP n=10 WR=0 % E[V]=-$9.42; AFTERNOON_CHOP n=14 WR=14.3 % E[V]=-$6.29; combined ~$182 historical loss with 2 wins. Sample size is borderline INSUFFICIENT_SAMPLE per CLAUDE.md tier; flag the operator decision accordingly |
| `OVERNIGHT_RANGE veto` | already vetoed (2026-05-22) | **HOLD veto active** | HIGH | regime data confirms: 19 trades, WR 10.5 %, total -$89.28. Current veto is right |
| anti-chase `max_ema_dist_60t` | 60 ticks (outside golden window) | **HOLD** | MEDIUM | Phase 6 per-gate table: rejects winners at non-trivial rate but red-team finding #1 retracts the dollar lift |
| LOW-confidence A/B proposal | — | `adverse_pre_move_ticks ≤ -0.5 × atr_5m` additive gate | **LOW** | Phase 4 effect size Cliff δ ≈ -0.16, p ≈ 0.07 — directional only; needs out-of-sample test |

**No HIGH-confidence change is recommended.** The strongest defensible action is "ratify the existing config and add CLOSE_CHOP / AFTERNOON_CHOP to the regime veto list" — and even that is MEDIUM-LOW because the per-regime n is borderline.

### 7.2 · opening_session recommended envelope

**INSUFFICIENT_SAMPLE** — n=8 closed non-RECONCILED trades all-time. No envelope can be defensibly recommended. The sprint plan's 7.x for opening_session is declined.

Recommendation: opening_session stays at current params until n ≥ 100 (PRELIMINARY tier per CLAUDE.md) is reached, or a sim-mode burn-in produces ≥ 30 trades within 90 days.

### 7.3 · 5-year WFA cross-reference

From `backtest_results/portfolio_framework/wfa_summary.csv`:

| strategy | n_windows | mean_is_pf | mean_oos_pf | median_oos_pf | pct_degraded | robust? |
|---|---:|---:|---:|---:|---:|---|
| `bias_momentum` | 3 | 1.33 | 1.34 | 1.34 | 0.0 | **True** |
| `opening_session` | 3 | 2.10 | 1.94 | 1.97 | 0.33 | **True** |

Both strategies are classified WFA-robust in the existing 5-year analysis. The proposed envelope above does NOT change parameters from the values used in that WFA run (we are recommending HOLD), so the existing robust verdict carries forward. No envelope value extrapolates outside the WFA-tested range.

### 7.4 · In-window WFA on HOLDOUT (Phase 13.6)

**Status: DEFERRED.** Honest execution of Phase 13.6 requires replaying `bias_momentum` + `opening_session` strategy code at the PROPOSED envelope over the HOLDOUT 30-day window. The existing `tools/reconcile_sim_vs_backtest.py` infrastructure (per spec 13.6.3.a) can do this, but:

1. The most recent reconciliation attempt (`out/reconciliation_2026-06-04_bias_momentum.md`) **FAILED** with verdict "backtester emitted zero direction-matched signals across the sim window." Until the reconciliation harness itself produces a defensible per-strategy divergence number (the freeze-lift precondition #1), running an in-window WFA on top of it produces numbers that cannot be defended.
2. The proposed envelope is mostly HOLD — there is no parameter change to validate at OOS. The exception is the CLOSE_CHOP / AFTERNOON_CHOP veto add, which is regime-conditional rather than a numeric threshold.

**Recommended follow-up:** before any envelope change ships, the operator should commission:

1. A reconciliation-harness debug sprint that gets `reconcile_sim_vs_backtest.py` producing matched signals (current state: 0/5).
2. THEN an in-window WFA on the proposed CLOSE_CHOP / AFTERNOON_CHOP veto add, scoped to the HOLDOUT 30-day window.

A short outline of the follow-up sprint is recorded at the end of this section (per spec 13.6.8).

### 7.5 · Conditional follow-up outline (spec 13.6.8)

IF the operator decides to ship the CLOSE_CHOP / AFTERNOON_CHOP veto add despite the freeze + reconciliation gap, a one-shot validation sprint should:

- Scope: bias_momentum only (opening_session is INSUFFICIENT_SAMPLE).
- Parameter delta: add CLOSE_CHOP and AFTERNOON_CHOP to the `regime_veto` list at `strategies/bias_momentum.py:79`.
- Backtest range: re-run the existing 5-yr WFA at the modified config using `tools/portfolio_backtest/wfa.py`.
- Estimated compute: ~hours per WFA shard; the existing `wfa_summary.csv` infrastructure handles the orchestration.
- Acceptance criteria (per existing wfa conventions): `mean_oos_pf ≥ 1.5` per strategy, `pct_windows_degraded ≤ 33 %` — same bar as the 5yr WFA already passing for bias_momentum.

---

## 8 · Subagent verdicts (Phase 10)

- **R10.1 Statistical Methodology Auditor:** PASS-WITH-CAVEATS. Two CRITICAL findings: (1) `tf_votes_bullish` is a direction confound; (2) random baseline n=58 P95 is unreliable. Recommendation: re-test `tf_votes_bullish` as `tf_votes_aligned_with_direction` and bootstrap the baseline before any deployment.
- **R10.2 Counterfactual Audit:** Conclusion correct (-$229.50 arithmetically sound). Two integrity issues flagged: (1) 5-hour timestamp mislabel in CSV (CT-naive labeled UTC — bar lookup is internally consistent, human-readable times are off); (2) "first blocking gate cleared" assumption in script comment not actually verified in code — Category A is an upper bound on lift, true counterfactual is strictly less.
- **R10.3 Visual Sanity Check:** Initial verdict NOT publication-quality (uint32 overflow in panels 2/3 → 4.3e9 sentinel values). **FIXED post-audit** (`size` column cast to int64 before arithmetic, xlim locked to [-15m, +30m]). Re-rendered charts verified visually before consolidation. Title/entry/stop/target/exit markers were always correct.

---

## 9 · Red-team verdict (Phase 11)

**PASS-WITH-CAVEATS.** No CRITICAL-HALT triggered. Seven findings:

1. **CRITICAL-MAJOR — RETRACTED:** the `+$981.84` "implied lift" in Phase 6 is an accounting artifact. **Number struck from report; per-gate table retained.**
2. **MAJOR:** Phase 6 winner-rejection is partially tautological (rejected-trade fills exist because the trade fired; slot-interlock not modeled). **Fill-realism caveat added.**
3. **MAJOR:** Phase 5 Category A and baseline medians are identical at $-9.00; DECLINE verdict survives but evidence is thin. **Caveat added to falsification section.**
4. **MEDIUM:** Phase 7 regime breakdown is conditional on day_type clearance (RANGE-day vetoed evals invisible). **Section retitled.**
5. **MEDIUM:** Top-5 LOSS chart selection was 100 % stop-clamp artifacts (sub-tick stops). **Re-ranked with stop > 0.5 pt filter; charts re-rendered.**
6. **MINOR:** opening_session declined throughout — compliant.
7. **MINOR:** Banner repetition on Phase 13 outputs — enforced.

The headline conclusion ("tightening is doing its job, no envelope change recommended") does NOT flip after red-team review. Loses its dollar anchor; gains methodological honesty.

---

## 10 · Self-critique (5 sentences)

The dataset's sample-size honesty caps everything: 220 bias_momentum trades in DERIVATION fits the TENTATIVE tier (CLAUDE.md), so every claim is provisional and the LOW-confidence proposals should not be acted on without 2× more data; opening_session at n=8 is genuinely uninformative. The data CAN'T tell us whether the post-Apr-18 tightening was the optimal point on a curve or just AN improvement — without a controlled A/B at the boundary thresholds, we only know "today's config beats the 8-day naive relaxations tested." The most fragile conclusion is the regime-conditional vetoes (CLOSE_CHOP n=10, AFTERNOON_CHOP n=14) — these would resolve with another 30 trades in each bucket, and the operator should consider running with the proposed vetoes "shadow" mode (still firing, tagged for separate accounting) for 30 days before any hard veto adds. What would resolve fragility: (a) instrumentation to persist `confluence_score` on the snapshot so Phase 6 has actual data; (b) ≥ 100 closed opening_session trades; (c) a working reconciliation harness so the in-window WFA can run honestly. Best counter-argument from an adversarial reviewer: "your 8-day eval-log counterfactual is the wrong test — you should be running the full pre-Apr-18 strategy code over the full 60-day window and comparing AGAINST a deliberately tightened mirror, not patching today's strategy with relaxed thresholds and pretending that's a fair contest" — which we partly acknowledge but cannot execute without the reconciliation harness producing matched signals first.

---

## 11 · Chart index (Phase 9)

All 10 charts in `out/charts/winning_conditions/`. 3-panel: price+markers / aggressor footprint / cumulative CVD. TBBO-sourced (DERIVATION ∩ TBBO window). xlim locked to [-15m, +30m] around entry. Loss charts re-ranked post red-team to exclude sub-tick-stop artifacts.

| rank | kind | trade_id | regime | file |
|---:|---|---|---|---|
| 1 | WIN | `4271b190` | AFTERHOURS | [bias_momentum_win_1_4271b190.png](charts/winning_conditions/bias_momentum_win_1_4271b190.png) |
| 2 | WIN | `b85286b1` | AFTERHOURS | [bias_momentum_win_2_b85286b1.png](charts/winning_conditions/bias_momentum_win_2_b85286b1.png) |
| 3 | WIN | `ba967bc5` | PREMARKET_DRIFT | [bias_momentum_win_3_ba967bc5.png](charts/winning_conditions/bias_momentum_win_3_ba967bc5.png) |
| 4 | WIN | `b4db1d47` | AFTERHOURS | [bias_momentum_win_4_b4db1d47.png](charts/winning_conditions/bias_momentum_win_4_b4db1d47.png) |
| 5 | WIN | `e7c7b03d` | AFTERHOURS | [bias_momentum_win_5_e7c7b03d.png](charts/winning_conditions/bias_momentum_win_5_e7c7b03d.png) |
| 1 | LOSS | `9b8532e3` | CLOSE_CHOP | [bias_momentum_loss_1_9b8532e3.png](charts/winning_conditions/bias_momentum_loss_1_9b8532e3.png) |
| 2 | LOSS | `b8e2a3bf` | OVERNIGHT_RANGE | [bias_momentum_loss_2_b8e2a3bf.png](charts/winning_conditions/bias_momentum_loss_2_b8e2a3bf.png) |
| 3 | LOSS | `0a6c6c16` | LATE_AFTERNOON | [bias_momentum_loss_3_0a6c6c16.png](charts/winning_conditions/bias_momentum_loss_3_0a6c6c16.png) |
| 4 | LOSS | `e9676605` | AFTERNOON_CHOP | [bias_momentum_loss_4_e9676605.png](charts/winning_conditions/bias_momentum_loss_4_e9676605.png) |
| 5 | LOSS | `06ae6e1c` | OVERNIGHT_RANGE | [bias_momentum_loss_5_06ae6e1c.png](charts/winning_conditions/bias_momentum_loss_5_06ae6e1c.png) |

opening_session charts skipped — n=8 doesn't merit individual chart treatment. See the dataset CSVs at `C:\tmp\winning_conditions\`.
