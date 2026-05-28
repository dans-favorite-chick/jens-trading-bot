# Backtester Fidelity — Root Cause & Fix (autonomous session)

**Date:** 2026-05-28 · **Freeze:** `FREEZE_ACTIVE = True` (UNCHANGED) · **No strategy code modified.**

## What was wrong, and what I fixed

The sim-vs-backtest decision divergence traced to **two tooling bugs**, both now fixed
(tooling only — strategies untouched, fixes gated behind the `real_enrichment` flag so the
default 5-year backtest path is unchanged):

### Bug 1 — warmup starvation (caused the noise_area "0/6")
`tools/replay_enrichment/fidelity_vs_eval_logs.py` only called `strategy.evaluate()` on minutes
that had a live-eval match. Strategies that build rolling internal tables during `evaluate()`
(e.g. `noise_area.sigma_open_table`) therefore **never warmed up** during the pre-window warmup
period, so they bailed at their warmup gate.
**Fix:** `--warmup-days` (default 20), live-parity `seed_history()` from `data/sigma_open_table.json`
(matches `base_bot.py:1759-1764`), and call `evaluate()` on **every** cycle (score only matched
minutes). **Result: `noise_area` signal recall 0/6 → 4/6** (4/4 direction-matched).

### Bug 2 — tf_bias algorithm mismatch (the bias_momentum over-fire driver)
The backtester computed `tf_bias` from an **`ema9 − ema21` spread**; the live bot uses a
**2-of-3 vote on the last 3 bars' closes** (`core/tick_aggregator.py:574-599`). Different
algorithms → they disagreed on **108/108** over-fire minutes, with the backtester reading more
decisive/bullish than live, so `bias_momentum`'s `tf_bias` gate passed where live's rejected.
**Fix:** `_vote_tf_bias()` recomputes `tf_bias` live-style in the de-stub path (exact for the
gating 1m/5m timeframes; 60m proxied from 15m and is context-only). **Result: `bias_momentum`
over-fire 108 → 29; decision agreement 86.0% → 96.2%.**

## The residual 29 — NOT a backtester defect

| Live reject reason | count | nature |
|---|---|---|
| "RANGE day" | 18 | live `day_type` derived from the **broken `cr_verdict`** (B2-3 bug, fixed 2026-05-25). This window (CSV ≤ 05-15) is entirely inside the broken-cr period, so live itself was mis-classifying. Matching it would mean reproducing a bug. |
| SHORT extra-gate | 8 | directional edge cases, driven by residual tf_bias off-by-one |
| CVD veto | 2 | live tick-CVD vetoed; recorded bar-delta CVD didn't (tick-vs-bar) |
| EMA distance | 1 | minor |

Underlying: 26/29 still have a **one-notch `tf_bias` difference** — the **fundamental tick-vs-bar limit**
(live builds bars from the NT8 tick stream; databento 1-min closes differ slightly, occasionally
flipping the 2-of-3 vote). A minor regime-boundary difference (BT time-based vs live session
manager) also exists but is **not** the binding reject reason in any of these 29.

## Bottom line

The two **fixable** backtester-fidelity bugs are fixed (warmup + tf_bias), cutting the measured
`bias_momentum` divergence by ~73%. The remaining divergence on this window is **not** a backtester
defect — it is (a) **live's own broken `cr_verdict`** in the only CSV-covered window, and (b)
**tick-vs-bar granularity** we cannot reconstruct from databento bars.

**A clean `bias_momentum` reconciliation is therefore impossible on current data** — it requires
**post-2026-05-25 databento MNQ/MES coverage** (working live CR) plus the field-carrying sim trades
now accumulating. With those, re-run `reconcile_sim_vs_backtest.py --real-enrichment` for a real
real-vs-real number. Until then: **freeze stays True.**
