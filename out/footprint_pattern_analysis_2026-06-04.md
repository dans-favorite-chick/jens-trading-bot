# Phoenix Footprint Pattern Analysis & CVD Strategy Audit — 2026-06-04

_Phases 2 + 3 + 4 deliverable of the Footprint / CVD / DOM Feasibility Research sprint._

---

## Phase 2 — Existing CVD/footprint/DOM strategy audit

### Tier-summary of all 5y backtested strategies (sorted by edge × significance)

Source: `backtest_results/phoenix_real_5year_2026-06-02_summary.csv`. Strategies marked `n=0` produced NO backtest trades — the 5y backtester (`tools/phoenix_real_backtest.py`) cannot simulate them because their inputs are not in the 5y data warehouse (volumetric or DOM history don't exist before 2026-05-04).

| Strategy | CVD/footprint role | n (5y) | tier | PF | WR | Annual PnL trend | WFA-robust? |
|---|---|---:|---|---:|---:|---|---|
| **bias_momentum** | `cvd_health` veto + `cvd` chop-gate + `bar_delta` explosive-bypass | 28501 | HIGH_CONF | 1.56 | 0.42 | +$20k → +$88k (5y profitable every year) | ✅ True |
| **opening_session** | CVD confluence in entry score | 3719 | HIGH_CONF | 1.79 | 0.44 | +$7k → +$13k (5y profitable every year) | ✅ True |
| **es_nq_confluence** | Cross-market lift, CVD touch | 131 | TENTATIVE | 3.38 | 0.46 | tiny but uniformly positive | ✅ True (PF 335 in WFA — overfit-suspicious) |
| **ib_breakout** | CVD confirmation | 185 | TENTATIVE | 1.15 | 0.48 | scratchy: small +/− | ❌ False |
| **vwap_band_pullback** | uses VWAP+CVD context | 295 | TENTATIVE | 1.15 | — | not enough years to confirm trend | ✅ True |
| **a_asian_continuation** | (Phase 13 new) | 340 | TENTATIVE | 11.88 | — | small but uniformly positive | ❌ False (despite high PF) |
| **e_multi_day_breakout** | (Phase 13 new) | 622 | VALIDATED | 5.30 | — | — | ✅ True |
| **g_inside_bar_breakout** | (Phase 13 new) | 973 | HIGH_CONF | 4.33 | — | — | ✅ True |
| **raschke_baseline** | trend follow, no CVD | 801 | HIGH_CONF | 3.87 | — | — | ❌ False |
| **nq_lsr** | CVD ratio as PRIMARY signal | 967 | HIGH_CONF | **0.83** | 0.22 | LOSING every year, −$1.5k 5y | n/a |
| **orb_v2** | CVD veto | 1 | INSUFFICIENT | — | — | dead — likely a config bug | n/a |
| **orb_fade** | CVD veto | not in 5y CSV | — | — | — | — | ❌ False (in WFA, OOS PF 0.68) |
| **footprint_cvd_reversal** | **CVD/footprint as primary** | **0** | n/a | n/a | n/a | NEVER BACKTESTED | n/a |
| **dom_pullback** | **DOM as primary signal** | **0** | n/a | n/a | n/a | NEVER BACKTESTED | ❌ False (OOS PF 0.0) |

### Disable history for `footprint_cvd_reversal`

Commit `b9a3b2e` (2026-05-21, Jennifer Brennan):
> "kill: disable 4 unvalidated strategies entirely (enabled=False) … footprint_cvd_reversal: dormant pending volumetric NT8 feed. Logs DATA_NOT_AVAILABLE 100% of the time but still loaded every tick by sim_bot."

The disable was **procedural, not analytical**:
- The strategy code is intact and shipped fully featured (1,679 lines, 4-confluence IQS scoring).
- It was never backtested because the backtester can't replay volumetric data (which started recording only 2026-05-04 — vs the 5y backtest window of 2021-2026).
- The disable rationale explicitly says: _"These stay killed until each has a clean 5y backtest. To re-enable for genuine lab-collection work, flip enabled=True AND add to WINNERS_BEYOND_PLAN."_

In other words: the strategy never got the chance to fail. The kill was a hygiene action to stop log noise from a data-starved strategy in `sim_bot`'s evaluation loop.

### What the Databento commit (3 days earlier, 2026-05-18) did

Commit `0b773aa` shipped `tools/databento_footprint_download.py` + walkthrough doc. Three modes (estimate / download / convert). **TBBO data for 2026-03-17 → 2026-05-17 has already been downloaded** and is sitting in `data/historical/databento_tbbo/`:

```
mnq_footprint_5m.csv                                  (per-5m-bar summary)
mnq_tbbo_2026-03-17_2026-05-17.dbn.zst                (raw TBBO compressed)
mnq_tbbo_2026-03-17_2026-05-17_footprint_sparse.parquet (per-bar/price detail)
mnq_ticks.parquet / mnq_ticks_slim.parquet / mnq_ticks_clean.parquet
```

This means:
- ~60 days of TRUE footprint data exist for the Mar 17 → May 17 window.
- Plus 31 days of live-recorded volumetric for May 4 → Jun 4.
- ~14 days of OVERLAP (May 4 → May 17) for sanity-checking live vs Databento.
- Total unique-day coverage: ~78 days.
- This is enough to validate `footprint_cvd_reversal` against a Wilson-CI n≥100 over the available days IF firing rate ≥ ~1.5 sigs/day on average (the legacy lab claimed ≥1/day).

### Cross-strategy CVD pattern analysis

Three patterns emerge from the WINNERS (CVD as filter/lift) vs LOSERS (CVD as primary signal):

| Pattern | Strategies that use it | Verdict |
|---|---|---|
| **CVD as health-check veto** (`cvd_health.assess() → veto=True`) | bias_momentum, opening_session | ✅ Both HIGH_CONFIDENCE and profitable. Suggests CVD slope-disagreement is a real filter when stacked on an otherwise validated signal. |
| **CVD as informational confluence (no score weight)** | bias_momentum (post B-033 demotion), vwap_pullback, vwap_pullback_v2 | ✅ Demotion to informational didn't kill the host strategies. CVD-as-confluence might not add real lift over what `cvd_health` veto already provides. |
| **CVD-derived ratio as the PRIMARY signal** | nq_lsr | ❌ HIGH_CONFIDENCE LOSER, PF 0.83 across 5 years. Clear evidence that CVD-as-signal alone doesn't have an edge in MNQ — only CVD-as-filter does. |
| **DOM imbalance as primary** | dom_pullback | ❓ Untestable in backtest (n=0), and the operator-only live run was 6 losses in a row (kill commit reason). Not promising. |
| **Footprint as scoring engine** (`footprint_cvd_reversal` IQS) | only this one strategy | ❓ Never tested at all. |

### Phase 2 verdict

The disable of `footprint_cvd_reversal` does **not** constitute evidence the strategy lacks edge. It constitutes evidence the operator was unable to validate it within the existing backtest harness. Now that Databento footprint data exists for a contiguous 60-day window AND live volumetric covers a further 30 days, the strategy IS testable for the first time — but that test has not been run yet.

The HARDER signal from this audit is from the WORKING strategies: bias_momentum and opening_session both use CVD as a HEALTH-CHECK VETO (not as a primary signal), and both are HIGH_CONFIDENCE WFA-robust 5y winners. nq_lsr — the one strategy that uses CVD-derived data as the primary signal — is a HIGH_CONFIDENCE 5y LOSER (PF 0.83). The pattern is "CVD filters help; CVD signals don't."

That pattern is **directly relevant to the operator's question**. If the goal is to use footprint data to improve outcomes, the evidence points to **footprint-as-filter on existing winners** rather than **footprint-as-new-strategy**. `footprint_cvd_reversal` is in the latter category (footprint scoring IS the entry signal), so re-enabling it would be a bet against the pattern in the existing data.

---

## Phase 3 — Pattern analysis on volumetric_history

### Method

1. Load all closed sim+prod trades 2026-05-04+ via `load_all_trades()` → 397 trades.
2. For each trade, extract `cvd_health.assess(direction)` at `entry_time` via `RecordedCVDProvider`. This is the byte-identical replay of what the live bot's `cvd_health` would have produced.
3. From `volumetric_history.jsonl`, extract a ±5-minute window around `entry_time` and compute per-trade footprint features (delta in window, sum signed_delta, mean max_imbalance_ratio, stacked-imbalance majority side, POC drift).
4. Bucket by outcome: WIN if `pnl_dollars_net > 0`, else LOSS.
5. Mann-Whitney U (non-parametric, two-sided) per feature, Bonferroni-correct.
6. Random-baseline counterfactual in Phase 4.

(Concrete numbers populated by the analysis script below — Phase 3 results section.)

### Phase 3 results

**Setup:**
- Filtered trades (closed, non-RECONCILED, sim+prod, 2026-05-04+, has snapshot+pnl): **397**
- Sample by strategy: bias_momentum 149, vwap_pullback 64, dom_pullback 46, spring_setup 46, vwap_pullback_v2 28, noise_area 10, big_move_signal 7, opening_session 6, vwap_band_reversion 5, e_multi_day_breakout 5, ib_breakout 1, plus `_reconciled_*` legacy buckets (30).
- Trades with replayable `cvd_health.assess()` via `RecordedCVDProvider`: **167 / 397 (42.1%)** — the other 58% predate the recorded volumetric window for their specific minute or fall outside same-session prior-minute reach.

**Outcome baseline (30 days of live operating):**
| Bucket | N | WR | mean PnL | total PnL |
|---|---:|---:|---:|---:|
| Original | 397 | 36.5% | $+0.11/trade | $+43.98 |
| WIN | 145 | 100.0% | $+43.04 | $+6,240.80 |
| LOSS | 252 | 0.0% | $−24.59 | $−6,196.68 |

The 30-day "all strategies, all sim+prod" operating account is essentially flat (+$44 over 397 trades).

**Per-feature Mann-Whitney U + Bonferroni (K=14 features, α=0.05 → raw p must be < 0.0036):**

| feature | n_win | n_loss | win_mean | loss_mean | U | p (raw) | p (Bonf) | sig |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| **delta_aligned_ratio_5m** | 145 | 252 | 0.310 | 0.452 | 13664 | 0.0000 | 0.0004 | **✅ SIG** |
| price_vs_vwap_aligned | 145 | 252 | 34.561 | 40.660 | 15204 | 0.0054 | 0.0750 |  |
| dom_heavy_aligned | 145 | 252 | 0.248 | 0.405 | 15411 | 0.0094 | 0.1316 |  |
| vol_climax_ratio | 145 | 252 | 1.042 | 1.189 | 17136 | 0.3032 | 1.0000 |  |
| cvd_health_agreement | 59 | 108 | 0.055 | −0.008 | 2921 | 0.3749 | 1.0000 |  |
| bar_delta_aligned | 145 | 252 | 5,544,684 | 3,754,239 | 17308 | 0.3819 | 1.0000 |  |
| delta_sum_5m_aligned | 145 | 252 | 2,636,568 | 7,063,848 | 17376 | 0.4167 | 1.0000 |  |
| dom_imb_aligned | 145 | 252 | 0.540 | 0.560 | 17574 | 0.5269 | 1.0000 |  |
| cvd_aligned | 145 | 252 | 109,133,451 | 39,152,274 | 17682 | 0.5933 | 1.0000 |  |
| dom_heavy_against | 145 | 252 | 0.269 | 0.290 | 17892 | 0.7310 | 1.0000 |  |
| vsa_aligned | 145 | 252 | 0.021 | 0.040 | 17923 | 0.7526 | 1.0000 |  |
| vsa_absorption | 145 | 252 | 0.000 | 0.012 | 18052 | 0.8434 | 1.0000 |  |
| cvd_health_veto | 59 | 108 | 0.339 | 0.333 | 3168 | 0.9519 | 1.0000 |  |
| cvd_health_slope | 59 | 108 | 0.000 | 0.000 | 3186 | 1.0000 | 1.0000 |  |

**Phase 3 finding (the surprise):**

**ONE feature** survives Bonferroni correction: `delta_aligned_ratio_5m` (Bonferroni p=0.0004).

But the **direction is the OPPOSITE of intuitive expectation:**
- WIN trades had `delta_aligned_ratio_5m` = **0.310** (only 31% of the last 5 5m bar deltas aligned with the trade direction)
- LOSS trades had `delta_aligned_ratio_5m` = **0.452** (45% aligned)
- Winners win MORE when delta is OPPOSING the trade direction at entry. Losers fired when delta was already going their way.

Two reasonable interpretations:
1. **Phoenix's strategy stack is biased toward REVERSAL setups** (vwap_pullback, spring_setup, opening_session at extremes). These fire when delta is going the "wrong" way and they're betting on mean reversion.
2. **Continuation strategies fire LATE** — by the time `delta_aligned_ratio_5m ≥ 0.6`, the move's already cooked; entering then = chasing the exhaustion phase.

Both interpretations have the same actionable consequence: **the conventional "wait for delta agreement before entering" filter would have made the operating account WORSE, not better.**

Other near-misses worth flagging (not Bonferroni-significant but raw-p < 0.01):
- `price_vs_vwap_aligned`: WIN mean +34.56, LOSS mean +40.66. Winners entered slightly CLOSER to VWAP. Same inverted-from-intuition direction.
- `dom_heavy_aligned`: WIN mean 24.8%, LOSS mean 40.5%. Winners were LESS likely to enter when DOM was already heavy on their side. (Yet another inversion: DOM-heavy entries look like late chase.)

The convergent pattern across the top-3 (raw p) features: **trades where the order flow / footprint / DOM was ALREADY agreeing with the entry direction tended to LOSE more often than not.** This is the classic "everyone is on the same side of the boat" reversal signature.

---

## Phase 4 — Counterfactual filter test

### Naïve forward filter (Phase 4.1) — for the strongest surviving feature

Filter: **KEEP** trades where `delta_aligned_ratio_5m ≥ median(winners) = 0.200`.
(I.e., trades where at least 1 of the last 5 5m deltas aligns with the trade direction.)

| | N | WR | expectancy | total PnL |
|---|---:|---:|---:|---:|
| Original | 397 | 36.5% | $+0.11 | $+43.98 |
| **Kept (filter ≥)** | 296 | 27.7% | **$−5.78** | **$−1,711.72** |
| Rejected (filter <) | 101 | **62.4%** | **$+17.38** | **$+1,755.70** |

The naive filter **destroys** the operating account. The rejected-side group — the trades the filter would have thrown away — is where ALL the profit lived. WR 62.4% on the rejected group is more than 2x baseline WR.

### Phase 4.3 — Monte Carlo random-rejection baseline (1000 trials, same kept-N)

- Random-kept expectancy:  mean $+0.16 | p5 $−3.42 | p95 $+2.94
- **Actual filtered kept expectancy: $−5.78**

→ **Filter expectancy is BELOW random p5.** The filter is actively HARMFUL. The INVERTED filter (reject when feature ≥ threshold) would have generated +$1,755 vs +$44 baseline — a 40× improvement, but this is overfitting on the same data the threshold was selected from.

### Inverted-filter sanity check on bias_momentum (largest n strategy)

Hypothesis: bias_momentum wins when price is CLOSER to VWAP (per the inverted Phase 3 finding for `price_vs_vwap_aligned`). Filter: **KEEP** if `price_vs_vwap_aligned ≤ threshold`.

| Threshold quantile | thr | N kept | kept WR | kept exp$ | kept total$ | rejected exp$ |
|---|---:|---:|---:|---:|---:|---:|
| q=0.25 | 1.42 | 38 | **55.3%** | **$+23.36** | $+887.80 | $−2.97 |
| q=0.50 | 20.58 | 75 | 38.7% | $+10.41 | $+781.00 | $−3.02 |
| q=0.75 | 84.10 | 112 | 37.5% | $+7.10 | $+795.70 | $−6.43 |

Filtering to "only fire bias_momentum when price is very close to VWAP" (q=0.25) keeps 38/149 trades and grows total PnL from $+557.82 baseline to $+887.80, with WR jumping from 33% to 55%. **That's the operationally relevant finding.**

But the **Monte Carlo random baseline** at q=0.50: random p5 = $−4.52, p95 = $+11.91. Actual filter exp $+10.41 is **within the p5–p95 band** — indistinguishable from random rejection at this sample size.

### Combined filter on bias_momentum: vwap-near AND delta-not-aligned

| Filter | N kept | WR | exp$ | total$ |
|---|---:|---:|---:|---:|
| Combined: `price_vs_vwap_aligned ≤ 6.44` AND `delta_aligned_ratio_5m ≤ 0.40` | 37 | 54.1% | $+22.27 | $+824.20 |

vs random sample of equal size: p5=$−6.89, p95=**$+23.88**, actual = $+22.27. **Right at the random p95 boundary.**

### Phase 4 verdict (with self-critique)

Phase 3's Bonferroni-significant finding (`delta_aligned_ratio_5m` is inversely related to win-rate, p_bonf=0.0004) is **statistically real** in the available data. But Phase 4's Monte Carlo says the filter that exploits this finding, when applied with conservative rejection rates, is **indistinguishable from random rejection of the same volume of trades.**

Both findings can be true simultaneously: there IS a relationship (small effect size, large N), but at the operating sample sizes (37–149 trades for bias_momentum) the lift is in the noise band.

What this means in practical terms:
- The DATA does support the operator's intuition that footprint primitives carry information. The convergent inversion pattern across three independent features (delta-alignment, vwap-distance, dom-heavy-alignment) is unlikely to be pure noise — three independent features all pointing the same wrong-way-from-expectation direction is itself a finding.
- But the data DOES NOT yet support deploying a footprint-based filter on existing strategies and claiming statistical lift over random rejection. With more trades (and probably with the Databento 60-day backfill to roughly triple the per-strategy N), the lift would either crystallize or wash out.

### Strategy-specific drills (raw-p exploratory, NOT Bonferroni)

**`bias_momentum` (N=149, WR 33%, total $+558, exp $+3.74):**
- `price_vs_vwap_aligned`  win=+35.85, loss=+64.79, raw p=0.0029 ← winners enter closer to VWAP
- `delta_aligned_ratio_5m`  win=+0.347, loss=+0.494, raw p=0.0113 ← inverted, consistent with pool
- `dom_imb_aligned`  win=+0.600, loss=+0.524, raw p=0.0663 ← marginal, winners had slightly heavier DOM on their side

**`vwap_pullback` (N=64, WR 62.5%, total $−356, exp $−5.57):**
- All raw-p > 0.15. No clean footprint feature distinguishes winners from losers, and the strategy itself is a 30-day LOSER despite high WR (avg loss > avg win).

**`spring_setup` (N=46, WR 43.5%, total $−82, exp $−1.78):**
- `price_vs_vwap_aligned`  win=+115.31, loss=+30.27, raw p=0.0089 ← winners enter MORE distant from VWAP (opposite of bias_momentum — makes sense, spring_setup IS a level-distant reversal play)
- `cvd_aligned`  win=+545M, loss=+210M, raw p=0.0177 ← winners had stronger CVD alignment (normal direction)
- Suggests spring_setup wants the CONVENTIONAL "delta agrees" alignment, while bias_momentum wants the inverse. Strategy-specific filter design would be required.

**`dom_pullback` (N=46, WR 4.3%, total $−321, exp $−6.97):**
- Only 2 winners → Mann-Whitney can't run.
- Independently confirms the kill commit b9a3b2e was justified. WR 4.3% over 30 days is "no edge" territory.

---

## Phase 3 + 4 self-critique (mandatory)

1. **Sample size:** Even the largest single-strategy bucket (bias_momentum at N=149) sits in the PRELIMINARY tier per Phoenix's `validation_tracker` rubric. The pooled N=397 is bigger but is a Frankenstein mix of strategies with different edges — any pooled filter ALSO becomes "best on average, possibly wrong for each strategy individually."
2. **cvd_health coverage:** Only 167/397 trades (42%) had replayable `cvd_health` from `RecordedCVDProvider`. The minute-resolution + same-session prior-minute fallback already does its best — but trades early in a session or after volumetric gaps drop through. The `cvd_health_*` features are therefore under-sampled and may have failed-to-survive Bonferroni for power reasons, not effect-size reasons.
3. **Multiple-testing discipline:** K=14 features is the Bonferroni multiplier. If I were less honest and tested K=5 "most promising" features, two more would survive (`price_vs_vwap_aligned` at adjusted p ≈ 0.027, `dom_heavy_aligned` at ≈ 0.047). The right answer is to PRE-REGISTER hypotheses before testing more data — Phase 6.5 outlines this.
4. **The "inverted relationship" finding is data-mining-vulnerable.** Three features all pointing the same inverted direction in a single 30-day live window is suggestive, but the Databento overlap window (~14 days) and 60-day backfill (Phase 5) are the proper out-of-sample replications.
5. **Phoenix's strategy mix is biased toward reversal in this window.** vwap_pullback, spring_setup, opening_session, and even bias_momentum at extremes all behave as countertrend at the entry level. The pattern that "trades-against-delta-win-more" may be a strategy-mix property, not an order-flow law of nature. A different strategy mix would likely show a different pattern.

