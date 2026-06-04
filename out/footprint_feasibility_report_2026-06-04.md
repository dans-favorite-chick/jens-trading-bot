# Phoenix Footprint / CVD / DOM Feasibility Report — 2026-06-04

_Final deliverable of the Footprint / CVD / DOM Feasibility Research sprint._
_Branch: `weekly-evolution/2026-05-24`. Operator: Jennifer Brennan._
_Researcher: Claude Opus 4.7 (Cowork session)._

---

## 1. Executive Summary

**The question.** The operator asked: _"what are our options for footprint/DOM/CVD for backtesting / finding the best entries?"_ — with the framing that the bot isn't trading enough and one path under consideration is replacing or filtering existing strategies with a footprint-driven approach. This sprint produces a verdict on whether that direction has analytical support in the data Phoenix already has, before any code ships.

**The verdict.** **MIXED — Phase 3 found a Bonferroni-significant signal (`delta_aligned_ratio_5m`, p=0.0004) that discriminates winners from losers across 397 trades, in the OPPOSITE direction from intuition: Phoenix's strategies win MORE often when entering AGAINST recent order flow, not WITH it.** This is a real statistical finding, robust to multiple-testing correction. However, Phase 4's counterfactual filter test shows the lift from acting on this finding is INDISTINGUISHABLE from random rejection at the operating sample sizes (37–149 trades per strategy). The signal exists; the filter that exploits it isn't yet provable above noise. With 60 more days of footprint data already available via the operator's pre-purchased Databento backfill, both questions become testable out-of-sample for the first time.

**The recommended next action.** Run the `new_strategy_buildout.md` outline first (plumb the existing-but-disabled `footprint_cvd_reversal` strategy into a Databento-aware backtest harness over the 80-day combined window — definitively answer whether the strategy has edge). Run the `filter_integration.md` outline second (validate the inverted-direction filter on `bias_momentum` against the Databento window). DO NOT abandon footprint analysis — the evidence is too suggestive — but DO NOT deploy live yet either. The Databento backfill is the bridge between "promising signal in 30 days of data" and "confirmed lift in 80+ days."

---

## 2. What Phoenix already has

(See [`out/footprint_data_inventory_2026-06-04.md`](footprint_data_inventory_2026-06-04.md) for full details.)

- **Live computation:** `core/tick_aggregator.py` derives `cvd` (tick-aggressor classified, session-resetting), `bar_delta` (5m), `delta_history_5m` (deque of 10), `vsa_signal_5m`, `vol_climax_ratio`, `dom_imbalance` / `dom_bid_heavy` / `dom_ask_heavy` / `dom_signal` (iceberg/absorption). Then `core/cvd_trend_health.py` produces per-minute `cvd_health.assess()` dicts (slope, agreement, veto). 10 of 14 strategies consume some of these inputs.
- **Persisted:** `logs/volumetric_history.jsonl` — 14.47 MB, 10,787 records, covers 2026-05-04 → 2026-06-04 (31 days). Per-1500-tick bar with full footprint primitives: per-price-level imbalance list (`bid_vol`, `ask_vol`, `ratio`, `side`), stacked-imbalance flags, `max_imbalance_ratio` (up to 75×), POC, signed `delta`, `cvd_session`.
- **Already purchased:** `data/historical/databento_tbbo/mnq_footprint_5m.csv` (+ sparse parquet) — Databento TBBO for 2026-03-17 → 2026-05-17 (60 days). Operator commit `0b773aa` (2026-05-18) shipped the downloader; data exists on disk; ~14-day overlap with live volumetric for sanity-checking.
- **Replay surface:** `tools/replay_enrichment/recorded_cvd.py` (`RecordedCVDProvider.health_at(ts, direction)`) — gives historical `cvd_health.assess()` dicts byte-identical to what live would have computed.
- **Missing:** DOM-depth snapshots over time (live-only, not persisted) → fillable with ~1 day of engineering on `bridge_server.py`.

**Conclusion of inventory:** Phoenix has 80% of what professional footprint platforms have. The most-frequently-cited "we have no footprint data" framing is incorrect.

---

## 3. What the disabled `footprint_cvd_reversal` strategy taught us

(See Phase 2 in [`out/footprint_pattern_analysis_2026-06-04.md`](footprint_pattern_analysis_2026-06-04.md) for full details.)

The disable was **procedural, not analytical**. Commit `b9a3b2e` (2026-05-21) reads:

> _"footprint_cvd_reversal: dormant pending volumetric NT8 feed. Logs DATA_NOT_AVAILABLE 100% of the time but still loaded every tick by sim_bot."_
>
> _"These stay killed until each has a clean 5y backtest. To re-enable for genuine lab-collection work, flip enabled=True AND add to WINNERS_BEYOND_PLAN."_

The strategy was disabled because the 5y backtester can't replay volumetric data (only available 2026-05-04+), AND the live feed wasn't reaching the strategy in production at the time. Three days earlier the operator had shipped the Databento downloader — the two events are linked, and the operator clearly anticipated returning to this question once data existed.

The 1,679-line strategy is FULLY IMPLEMENTED (4-confluence Institutional Quality Score: HTF level + CVD divergence + footprint confirmation + CVD compression). It has never been tested AT ALL — neither validated nor disproven.

**This matters for the decision:** if we judge "footprint as primary signal" by the 5y backtest of `footprint_cvd_reversal`, that 5y backtest doesn't exist. The kill commit is not a verdict on the strategy's edge — only on its operational readiness.

---

## 4. Pattern analysis findings (statistical, n=397)

(See Phase 3 in [`out/footprint_pattern_analysis_2026-06-04.md`](footprint_pattern_analysis_2026-06-04.md) for full details.)

- 397 closed sim+prod trades, 2026-05-04 → 2026-06-04, 14 footprint features tested per trade, Mann-Whitney U with K=14 Bonferroni correction.
- WR 36.5%, mean WIN $+43.04, mean LOSS $-24.59, total PnL $+43.98 (essentially flat over 30 days).
- **1 feature survives Bonferroni:** `delta_aligned_ratio_5m` (p_bonf = 0.0004).

**The surprise — direction is inverted from intuition:**

| feature | WIN mean | LOSS mean | Direction |
|---|---:|---:|---|
| `delta_aligned_ratio_5m` (Bonferroni-sig) | 0.310 | 0.452 | WINS at LOWER delta-alignment ⚠️ |
| `price_vs_vwap_aligned` (raw p=0.005) | 34.56 | 40.66 | WINS at LOWER vwap distance ⚠️ |
| `dom_heavy_aligned` (raw p=0.009) | 0.248 | 0.405 | WINS at LOWER dom-heavy-on-side ⚠️ |

Three independent features all pointing the same WRONG-WAY-FROM-INTUITION direction is itself a finding. The pattern: **trades that fired when the order flow / footprint / DOM was ALREADY agreeing with the entry direction tended to LOSE more often than not.** The likely cause: Phoenix's strategy mix is reversal-biased (vwap_pullback, spring_setup, opening_session at extremes, bias_momentum at extremes), and these strategies are SUPPOSED to fire against recent flow — confirming flow at entry typically means "the move's already cooked, you're chasing exhaustion."

Strategy-specific raw-p (exploratory, NOT Bonferroni-corrected):
- **bias_momentum (N=149, WR 33%, +$558 total)**: `price_vs_vwap_aligned` raw p=0.003 — winners enter CLOSER to VWAP, losers chase far from VWAP. `delta_aligned_ratio_5m` raw p=0.011 — same inverted pattern as the pool.
- **vwap_pullback (N=64, WR 63%, −$356 total)**: high WR loser. Avg loss > avg win. No clean footprint discriminator at raw p < 0.15.
- **spring_setup (N=46, WR 44%, −$82 total)**: `price_vs_vwap_aligned` raw p=0.009 — winners enter FURTHER from VWAP (correct direction for a reversal-from-level play). `cvd_aligned` raw p=0.018 — winners had stronger CVD alignment (conventional direction).
- **dom_pullback (N=46, WR 4.3%, −$321 total)**: only 2 winners. The kill commit was justified independently.

---

## 5. Counterfactual filter test (Phase 4)

(See Phase 4 in [`out/footprint_pattern_analysis_2026-06-04.md`](footprint_pattern_analysis_2026-06-04.md) for full details.)

### Naïve forward filter on the pooled 397 trades

Filter: **KEEP** if `delta_aligned_ratio_5m ≥ 0.20` (median of winners).

| | N | WR | exp$ | total$ |
|---|---:|---:|---:|---:|
| Original | 397 | 36.5% | $+0.11 | $+43.98 |
| Kept (≥) | 296 | 27.7% | $−5.78 | $−1,711.72 |
| Rejected (<) | 101 | 62.4% | $+17.38 | $+1,755.70 |

Random-rejection Monte Carlo (1000 trials, same kept-N): mean $+0.16, p5 $−3.42, p95 $+2.94. **Actual filtered exp $−5.78 is BELOW random p5** → filter is actively HARMFUL. The INVERTED filter (reject when feature ≥ threshold) would have grown PnL from $+44 to $+1,756 — but that's IN-SAMPLE on the very data the threshold was selected from.

### Inverted bias_momentum filter (the operationally relevant one)

Filter: **KEEP bias_momentum trades** if `price_vs_vwap_aligned ≤ 1.42` (q25 of bias_momentum trades — i.e. price very close to VWAP).

| | N | WR | exp$ | total$ |
|---|---:|---:|---:|---:|
| bias_momentum baseline | 149 | 32.9% | $+3.74 | $+557.82 |
| Kept (price close to VWAP) | 38 | **55.3%** | **$+23.36** | **$+887.80** |
| Rejected | 111 | 25.2% | $−2.97 | $−329.98 |

The filter keeps 25% of bias_momentum trades but DELIVERS 159% of baseline PnL. WR jumps from 33% to 55%.

**BUT — Monte Carlo at q=0.50 threshold:** random p5 $−4.52, p95 $+11.91; actual $+10.41 = **within random band**. At q=0.25 with N=38 the random p95 is wider and the actual still doesn't clear cleanly. Combined `price_vs_vwap` AND `delta_aligned_ratio` filter: actual $+22.27 sits AT random p95 $+23.88 boundary.

### Phase 4 honest summary

The Phase 3 statistical signal is REAL (Bonferroni-significant, three convergent features). The Phase 4 actionable filter is at the EDGE of Monte Carlo significance — close enough to be interesting, not far enough to act on with the current 30-day sample. The Databento 60-day backfill (already on disk) is the obvious way to break this tie.

---

## 6. Cost-benefit if pursuing

### Data scope situation

| Source | Window | Status | Cost |
|---|---|---|---|
| `logs/volumetric_history.jsonl` (live) | 2026-05-04 → present | ACTIVELY GROWING | $0 (existing infrastructure) |
| `data/historical/databento_tbbo/` | 2026-03-17 → 2026-05-17 (60d) | DOWNLOADED, never integrated | $0 incremental (already paid) |
| `data/historical/glbx-mdp3-*.csv` (5y OHLCV) | 2021-05 → 2026-05 | EXISTING, no footprint primitives | $0 (already paid) |
| Future Databento backfill | extend further back | Available | ~$10-30/month, $100-300/year per the walkthrough doc |

**No new data purchase is required to validate the Phase 3 finding** — operator already has 80 unique days of true footprint data with ~14 days of overlap. The bottleneck is INTEGRATION effort, not data acquisition.

### Integration effort (per follow-on outlines)

| Outline | Files | Effort | Operator approval needed |
|---|---|---|---|
| [`filter_integration.md`](footprint_followup_outlines/filter_integration.md) — add inverted-direction filter to bias_momentum | `core/footprint_filter.py` (new), `strategies/bias_momentum.py`, `config/strategies.py`, `tools/phoenix_real_backtest.py` (Databento mode) | S–M (~3–5 days) | YES (canary strategy edit) |
| [`new_strategy_buildout.md`](footprint_followup_outlines/new_strategy_buildout.md) — validate `footprint_cvd_reversal` on 80-day Databento+live | `tools/databento_to_volumetric.py` (new), `tools/phoenix_real_backtest.py` extension, eventual `config/strategies.py` flip | M (~1–1.5 weeks) | YES at 2 points (harness change + enable flip) |
| [`data_collection_uplift.md`](footprint_followup_outlines/data_collection_uplift.md) — persist DOM history | `bridge/bridge_server.py` (protected), `tools/dom_snapshot_recorder.py` (new) | S (~1–2 days work + 30 days wait) | YES (protected file) |

### Out of scope (intentional non-recommendations)

- DO NOT build a new "footprint engine" from scratch — the primitives already exist.
- DO NOT subscribe to Databento MBO (~$500/yr) — TBBO is sufficient at $10-30/month.
- DO NOT introduce ML model / multi-feature ensemble until single-feature filters have demonstrably failed OOS — YAGNI.

---

## 7. Recommended next action

**RUN `new_strategy_buildout.md` FIRST.** It answers the most decision-relevant question: does the strategy that was specifically designed for footprint signals have edge in the 80-day combined window? If YES, that's a directly-deployable strategy (after Wilson n≥100 + WFA + canary). If NO, that closes the "footprint-as-primary-signal" question definitively, freeing the operator from re-asking it.

**RUN `filter_integration.md` SECOND** (or in parallel if engineering capacity allows). It tests the lighter-touch question: can we make existing winners better with a footprint-derived filter? Phase 3's inverted-direction finding is the specific filter to validate, on the specific strategy (bias_momentum) with the largest sample, against the same 80-day window.

**DEFER `data_collection_uplift.md`** until at least one of the two above produces a positive result. DOM persistence is real engineering work for a question we don't yet need answered.

If both `new_strategy_buildout` and `filter_integration` come back negative on the OOS Databento window, the honest answer is: **Phoenix's existing strategy stack does NOT have a footprint-derived path to better performance.** Pivot to other improvement directions (Confluence sprint, exit-policy tuning, regime-aware sizing).

The directly-relevant alternative to footprint work is: the operator's separate Confluence sprint, which addresses signal-firing frequency via Path X (strategy-blocking field persistence) + Path Y (live-vs-backtest threshold tightening). Those are CONFIRMED bugs with confirmed fixes. Footprint is a maybe-bigger-but-unproven lift.

---

## 8. Self-critique

1. **Sample size:** Even after Bonferroni correction, ONE feature surviving out of 14 with N=397 should be treated with caution. With more data (the Databento window roughly doubles or triples per-strategy N depending on firing rate), the result could either crystallize OR wash out as a Type I error. Phase 4 already shows the actionable filter is within the random-rejection band — saying "the data SUGGESTS we should pursue this" is honest; saying "we have proven it" is not. Out-of-sample replication on Databento data is the only thing that earns the latter claim.

2. **What the data can't tell us:** This sprint cannot tell us how footprint signals behave in HIGH VIX regimes (the 30-day window is mostly normal volatility) or during major events (FOMC, NFP, geopolitical shocks). The Databento 60-day window picks up some events but is still not 5 years. Strategy promotion to live should require regime-aware backtest decomposition, not just total-window PF.

3. **Where the conclusion is most fragile:** The inverted-direction finding could be ARTIFACT of Phoenix's reversal-heavy strategy mix in this 30-day window. The 5y backtest record shows bias_momentum is profitable across ALL years 2021–2026 — so the strategy itself has multi-regime edge. But the FILTER built on top of it has only been tested in one 30-day window. A regime where the strategy mix shifts (e.g. ORB plays back in rotation) could flip the inverted-direction sign.

4. **Whether more data would resolve it:** YES. Phase 4's Monte Carlo result is borderline-statistically-distinguishable — the actual lift at q=0.50 bias_momentum filter ($+10.41) sits right at the boundary of the random p95 ($+11.91). Roughly doubling N (which the Databento backfill provides) would cut the Monte Carlo confidence interval by ~30%, almost certainly resolving the borderline either way. The operator already paid for this data; it's the cheapest possible disambiguation.

5. **The best adversarial counter-argument:** _"You're cherry-picking the Bonferroni-surviving feature because it's the only one that survived. The base rate for 'one feature out of 14 surviving Bonferroni at α=0.05' is roughly the false-positive rate of Bonferroni itself when no real effect exists — about 5%. Combined with the data-mining issue that the Phase 4 inverted-filter PnL ($+1,755) was selected after seeing the WIN/LOSS direction, the entire finding could be a Type I error masquerading as a discovery."_ The right rebuttal to this is **not** in this sprint's data — it's in the Databento out-of-sample replication. If `delta_aligned_ratio_5m` shows the same inverted direction on the 60-day Databento window with a freshly-fit threshold, it's a real effect. If it flips or vanishes, it was a Type I. There is no way to settle this without the OOS test.

---

## 9. The verdict and what comes next

**Verdict:** MIXED-PROMISING. Statistically real signal (Bonferroni-significant), but operationally borderline. Decisive resolution requires running the OOS test on the operator's already-purchased Databento data.

**Recommended next Cowork action:** expand [`out/footprint_followup_outlines/new_strategy_buildout.md`](footprint_followup_outlines/new_strategy_buildout.md) into a full master prompt. After operator approval, run that sprint to definitively validate (or close) the `footprint_cvd_reversal` strategy on 80 days of combined Databento + live volumetric data. THEN return to [`filter_integration.md`](footprint_followup_outlines/filter_integration.md) regardless of `new_strategy_buildout`'s outcome — the inverted-direction filter is a separate question with its own actionable value.

**Recommended NOT to:**
- Re-enable `footprint_cvd_reversal` based on this sprint alone (we have a signal in different data; the strategy itself still hasn't been tested).
- Modify any existing strategy on the strength of Phase 4's borderline-significance lift (the Databento OOS test is cheap; just do it).
- Subscribe to additional market data — the existing Databento + live data is sufficient to resolve the current question.

**No bot trades, no config changes, no strategy edits result from this sprint.** The decision to act on this verdict is a separate operator decision.
