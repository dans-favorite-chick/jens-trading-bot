---
name: backtest_expert
description: Phoenix backtest layer. Triggers when working with backtests, the strategy lab, replay tools, phoenix_real_backtest, or any reconciliation work between live and historical data. Read this before modifying tools/phoenix_real_backtest.py, tools/phoenix_new_strategy_lab.py, tools/phoenix_trend_pullback_lab.py, or any backtest results CSV.
---

# Layer: Backtest Expert

## What this layer does
Runs Phoenix's actual strategy classes against historical Databento MNQ + MES data so we can produce 5y P&L curves, exit-methodology comparisons, and per-strategy regime profiles BEFORE strategies graduate to live.

## Canonical backtester
`tools/phoenix_real_backtest.py` — tests Phoenix's ACTUAL strategy classes (not canonical approximations) against 5 years of Databento data. Reconstructs the `market` dict with the 50+ enriched fields that `core/tick_aggregator.snapshot()` + `bots/base_bot._evaluate_strategies()` would produce.

**Coverage matrix** (see file docstring for full list):
- Fully testable (data + enrichment supported): `es_nq_confluence`, `compression_breakout_v2`, `compression_breakout_micro`, `orb_v2`, `orb_fade`, `vwap_pullback_v2`, `vwap_band_pullback`, `vwap_band_reversion`, `noise_area`, `ib_breakout`, `spring_setup`
- Partial (fields stubbed; results approximate): `bias_momentum`, `big_move_signal`, `opening_session`
- Cannot test (data not in CSVs): `footprint_cvd_reversal` (needs volumetric stream), `nq_lsr` (needs liquidity_levels + TPO + volume_profile_lsr context)
- Deleted 2026-05-21: `dom_pullback` (0 trades / 5y; class + config removed)

## Known gotcha: bar-level CVD/delta proxy (F-06)
**Live uses tick-level aggressor side; backtester uses `bar.delta = volume × sign(close - open)`.** This is a rough proxy that:
- Works for "is delta positive vs negative" gates
- **Understates magnitude on inside bars** (where direction is ambiguous)
- Causes strategies that strictly compare CVD to thresholds to see FEWER signals in backtest than live

This means a backtest miss is not always a real miss — it can be the proxy under-reporting magnitude. Reference: `tools/phoenix_real_backtest.py:159-164` (docstring acknowledges this at L140).

## Open work: P1-1 reconciliation harness
**Status: NOT YET BUILT.** SYNTHESIS_2026-05-24.md (P1-1) specifies building `tools/reconcile_sim_vs_backtest.py`:
- Take last 30 days of sim_bot trades
- Replay same input bars through `tools/phoenix_real_backtest.py`
- Assert entry/exit timestamps, prices, stop placement, exit reasons match within tolerance
- Output `out/reconciliation_<date>_bias_momentum.md` with any divergences

`bias_momentum` is the first target — it is the largest claimed P&L line.

**Until this harness passes for at least one strategy**, the operator has confirmed (2026-05-24) the Phase 13 5y backtest is NOT tied to live-paper sim_bot results within a defensible tolerance. Reconciliation is a HARD PREREQUISITE before any Phase 13 production change (kill list, new strategy promotions, `tier_3000` sizing flip, branch push).

## Suspect compounding curve (F-16)
`tier_3000`: claimed $1.5K → $1.09M / 5y / 34% DD. Listed in `memory/context/CURRENT_STATE.md:23` and `docs/PHOENIX_BEST_PLAN.md` §3.4 with the doc's own suspicion noted. **Treat as conjecture until F-13 (reconciliation) closes.** Do NOT cite this number in commits, in test assertions, or as justification for live-promotion changes.

## Backtest-related files
- `tools/phoenix_real_backtest.py` — canonical 5y backtester (strategy class execution)
- `tools/phoenix_new_strategy_lab.py` — lab for new strategies (g_inside_bar_breakout, e_multi_day_breakout, a_asian_continuation source)
- `tools/phoenix_trend_pullback_lab.py` — Raschke + trend-pullback variants
- `backtest_results/backtest_v3_sweep_results.csv` — Phase 12C 108-config sweep
- `backtest_results/exit_methodology_v3_results.csv` — Phase 12C 30 exit-method comparator
- `docs/audits/SYNTHESIS_2026-05-24.md` — F-06, F-13, F-16, P1-1 context
- `docs/audits/STRATEGY_SHIP_AUDIT.md` — Phase 13 ship rationale

## DO NOT
- Do NOT promote a strategy to `validated=True` based on backtest P&L alone — Wilson-CI guardrail (n≥100 live trades) is the live-side check; backtest is necessary, not sufficient.
- Do NOT cite the tier_3000 $1.09M / 34% DD curve in any commit or doc without flagging it as F-16 / suspect.
- Do NOT change the CVD proxy in `tools/phoenix_real_backtest.py` without updating the docstring at L140 — downstream readers depend on the proxy semantics being documented.
- Do NOT add a "fully testable" strategy to the coverage matrix without verifying every enrichment field is reconstructed correctly — partial fields silently produce wrong backtest numbers.
- Do NOT delete a strategy because the backtester reported 0 trades — the backtester may structurally lack the data (L2/DOM/volumetric/MES) the strategy needs. Verify the strategy's data dependencies first.
