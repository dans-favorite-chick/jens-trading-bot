# Backtester Fidelity — Full Field-Level Decomposition & Fixes

**Date:** 2026-05-28 (autonomous session) · **Freeze:** `FREEZE_ACTIVE = True` (UNCHANGED) · **No strategy code touched.** All fixes behind the `real_enrichment` flag — default 5-year backtest path unchanged.

## Method (the key reframe)

The backtester runs the **identical** `strategy.evaluate()` as live, so "does backtest
match live?" reduces to "does the backtester's reconstructed `market` dict match live's?"
I built a **field-by-field enrichment audit** (`tools/replay_enrichment/enrichment_audit.py`)
comparing the backtester's market vs the live-recorded values (`logs/history/*_prod.jsonl`),
minute by minute. This needs **no new data** and bypasses the broken-cr DECISION contamination.

## Field-level audit (05-06..05-15), and the fix stack

| Field | databento bars | + recorded bars | + regime fix | verdict |
|---|---|---|---|---|
| price | 0.01% / corr .999 | **0.00% / corr 1.000** | — | ✅ |
| vwap | 0.07% / corr .979 | **0.00% / corr 1.000** | — | ✅ |
| tf_bias 1m | 57.1% | **99.88%** | — | ✅ fixed |
| tf_bias 5m | 49.8% | **97.40%** | — | ✅ fixed |
| regime | 33.9% | 34.1% | **100.00%** | ✅ fixed |
| atr_1m / 5m | ~7% / ~9% | ~6% / ~8% | — | ◑ ok |
| cr_verdict | 0% | 0% | 0% | ✗ live bug (see below) |
| dom_imbalance | 100% off | 100% off | — | ✗ not in bar data |
| cvd | corr .25 | n/a (scale) | — | ✗ tick-level, not bars |

**Four tooling gaps found and fixed** (in order of discovery):
1. **Warmup starvation** (harness only evaluated matched minutes) → `--warmup-days` + seed_history + evaluate-every-cycle. (`noise_area` 0/6→4/6.)
2. **tf_bias algorithm** (ema-spread vs live's 2-of-3 bar vote) → `_vote_tf_bias()`.
3. **tf_bias BAR SOURCE** (databento ≠ the bot's NT8-built bars; the 2-of-3 vote is hypersensitive to sub-tick close differences). PROVEN: the vote on the bot's OWN recorded closes reproduces live tf_bias at **99.97%**. → recorded-bar replay (`recorded_bars.py`, `--bar-source recorded`). tf_bias 50%→**99.88%**, price/vwap→**100%**.
4. **regime taxonomy** (backtester used "LUNCH" etc.; live uses `SESSION_WINDOWS` via `core.session_manager`) → de-stub now classifies regime via the live `SessionManager`. **34%→100%**.

## Gate-strategy result: bias_momentum

Working-cr window (05-26..05-27), recorded bars, all fixes:
- decision agreement **98.19%**, signal recall **3/8** (3/3 direction-matched), over-fire 27→**17**.
- Progression of over-fire as fixes landed (05-13/14 window): **108 → 29 → (recorded bars) → (regime)**.

## The residual — NOT backtester defects

- **cr_verdict / day_type:** live recorded `UNKNOWN` cr **1046×** even in the post-fix 05-26/05-27 window (overnight / data-gap artifact), and the broken-cr B2-3 bug made the entire ≤05-15 window `UNKNOWN`. cr reconstruction also depends on the cross-session momentum file. So cr/day_type can't be matched cleanly — partly live-data artifact, partly the momentum-feed (wireable, `recorded_day_cr.isolated_momentum_file`, future work).
- **cvd (tick-level) and dom (not in databento or recorded bars):** fundamentally unreconstructable from bar data; both are confluences, not hard gates, for bias_momentum.

## Bottom line

The sim-vs-backtest discrepancy was a **stack of backtester-fidelity bugs**, now decomposed
and the major ones **fixed**: the backtester's enrichment matches live on price/vwap (100%),
tf_bias (99.9%/97.4%), and regime (100%) when fed the bot's own recorded bars. Gate-strategy
decision agreement is **98.2%**. The remaining ~2% is cr/day_type (live-data artifact +
momentum-feed) and cvd/dom (not in bar data) — not defects we can engineer away.

**This also removes the databento dependency for the forward reconcile:** replay the bot's
**own recorded bars** (it logs them continuously) → faithful enrichment → clean reconcile as
field-carrying sim trades accumulate. No databento purchase, no historical-window contamination.
`FREEZE_ACTIVE` stays True pending that reconcile + operator sign-off.
