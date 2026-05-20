# Early Reversal Exit Signal Analysis

**Generated:** 2026-05-19  
**Branch:** weekly-evolution/2026-05-17  
**Tool:** `tools/phoenix_early_reversal_signals.py`  
**Tick data:** `data/historical/databento_tbbo/mnq_ticks_clean.parquet` (43.8M MNQ ticks, 2026-03-17 to 2026-05-15)

## TL;DR

**No strategy benefits from any early-reversal early-exit policy tested.** Across all six strategies and three combined-policy variants, every early-exit configuration UNDERPERFORMS the Section U.3 baseline policy by 30-90% in total P&L over the 2-month tick window. False-positive rates dominate (44-78% per signal). DO NOT ship early-reversal exits. Keep the Section U.3 baseline (fixed_rr / chandelier / time_exit).

## Question

Phoenix's Phase 13 Section U production policies exit AFTER price hits a fixed RR target, Chandelier stop, or time-out. This tool asks: are there tick-level CLUES that price is about to REVERSE that we could use as an EARLY EXIT trigger to lock in MFE profits BEFORE the reversal?

## Signals tested

All five fire only after the trade has reached at least +0.5R favorable (worth-locking-in threshold). A 5s per-signal cooldown prevents repeat fires.

1. `delta_divergence` - rolling 60s cumulative aggressor delta turns AGAINST the trade while price within 4 ticks of MFE peak.
2. `tape_speed_collapse` - last-10s tick rate drops below 50% of trailing-60s avg rate.
3. `volume_climax` - last-5s volume > 2.5x the avg 5s bucket of the trailing 60s, while near peak.
4. `aggressor_flip` - last 30 TICKS show counter-side aggressor volume > 1.5x with-trade aggressor volume.
5. `stacked_imbalance` - in the last 30 ticks, 3+ distinct price levels show counter-side aggressor dominance > 3:1.

## Per-signal performance

True-positive (TP) = early-exit P&L beats baseline P&L; false-positive (FP) = early exit gave up P&L the baseline would have captured.  `avg_ticks_locked_vs_baseline` is the average per-fired-trade tick delta (early - baseline); negative means the signal cost ticks.

### bias_momentum

| signal | n_fired/n_trades | fire_rate | TP% | FP% | avg_delta_ticks | sum_delta_$ |
|---|---|---:|---:|---:|---:|---:|
| delta_divergence | 384/509 | 75.4% | 44.0% | 56.0% | -26.92 | $-5,169 |
| tape_speed_collapse | 370/509 | 72.7% | 44.1% | 55.9% | -26.61 | $-4,924 |
| volume_climax | 365/509 | 71.7% | 42.2% | 57.8% | -19.90 | $-3,631 |
| aggressor_flip | 385/509 | 75.6% | 44.2% | 55.8% | -25.95 | $-4,996 |
| stacked_imbalance | 385/509 | 75.6% | 44.2% | 55.8% | -26.16 | $-5,036 |

### g_inside_bar_breakout

| signal | n_fired/n_trades | fire_rate | TP% | FP% | avg_delta_ticks | sum_delta_$ |
|---|---|---:|---:|---:|---:|---:|
| delta_divergence | 10/18 | 55.6% | 30.0% | 70.0% | -5.22 | $-26 |
| tape_speed_collapse | 1/18 | 5.6% | 0.0% | 100.0% | -18.44 | $-9 |
| volume_climax | 13/18 | 72.2% | 38.5% | 61.5% | -4.00 | $-26 |
| aggressor_flip | 13/18 | 72.2% | 30.8% | 69.2% | -5.54 | $-36 |
| stacked_imbalance | 13/18 | 72.2% | 30.8% | 69.2% | -5.77 | $-37 |

### opening_session

| signal | n_fired/n_trades | fire_rate | TP% | FP% | avg_delta_ticks | sum_delta_$ |
|---|---|---:|---:|---:|---:|---:|
| delta_divergence | 15/29 | 51.7% | 26.7% | 73.3% | -112.09 | $-841 |
| tape_speed_collapse | 13/29 | 44.8% | 23.1% | 76.9% | -129.71 | $-843 |
| volume_climax | 14/29 | 48.3% | 21.4% | 78.6% | -114.59 | $-802 |
| aggressor_flip | 15/29 | 51.7% | 26.7% | 73.3% | -116.15 | $-871 |
| stacked_imbalance | 15/29 | 51.7% | 26.7% | 73.3% | -115.29 | $-865 |

### raschke_baseline

| signal | n_fired/n_trades | fire_rate | TP% | FP% | avg_delta_ticks | sum_delta_$ |
|---|---|---:|---:|---:|---:|---:|
| delta_divergence | 5/8 | 62.5% | 60.0% | 40.0% | -9.00 | $-22 |
| tape_speed_collapse | 5/8 | 62.5% | 40.0% | 60.0% | -8.00 | $-20 |
| volume_climax | 5/8 | 62.5% | 60.0% | 40.0% | -9.40 | $-24 |
| aggressor_flip | 5/8 | 62.5% | 60.0% | 40.0% | -9.40 | $-24 |
| stacked_imbalance | 5/8 | 62.5% | 60.0% | 40.0% | -9.40 | $-24 |

### spring_setup

| signal | n_fired/n_trades | fire_rate | TP% | FP% | avg_delta_ticks | sum_delta_$ |
|---|---|---:|---:|---:|---:|---:|
| delta_divergence | 593/882 | 67.2% | 56.5% | 43.5% | -14.10 | $-4,180 |
| tape_speed_collapse | 591/882 | 67.0% | 56.5% | 43.5% | -14.55 | $-4,300 |
| volume_climax | 569/882 | 64.5% | 55.2% | 44.8% | -9.36 | $-2,664 |
| aggressor_flip | 594/882 | 67.3% | 56.6% | 43.4% | -14.17 | $-4,208 |
| stacked_imbalance | 594/882 | 67.3% | 56.6% | 43.4% | -13.96 | $-4,146 |

### vwap_pullback_v2

| signal | n_fired/n_trades | fire_rate | TP% | FP% | avg_delta_ticks | sum_delta_$ |
|---|---|---:|---:|---:|---:|---:|
| delta_divergence | 150/194 | 77.3% | 55.3% | 44.7% | -35.49 | $-2,662 |
| tape_speed_collapse | 146/194 | 75.3% | 54.1% | 45.9% | -39.16 | $-2,859 |
| volume_climax | 145/194 | 74.7% | 55.2% | 44.8% | -26.25 | $-1,903 |
| aggressor_flip | 150/194 | 77.3% | 55.3% | 44.7% | -35.09 | $-2,632 |
| stacked_imbalance | 150/194 | 77.3% | 55.3% | 44.7% | -35.60 | $-2,670 |

## Combined policy P&L per strategy

Per-strategy total P&L over the 2-month tick window (2026-03-17 to 2026-05-15).  `baseline` is the Section U.3 production policy (fixed_2r / fixed_3r / chandelier / time_exit).  Early policies fall back to baseline if no qualifying signal fires.

| strategy | policy | n | wr% | total_$ | avg_$ | pf | delta_vs_baseline_$ |
|---|---|---:|---:|---:|---:|---:|---:|
| bias_momentum | baseline | 509 | 45.0% | $+12,326 | $+24.22 | 1.50 | $+0 |
| bias_momentum | early_aggressive | 509 | 76.2% | $+7,280 | $+14.30 | 1.69 | $-5,046 |
| bias_momentum | early_conservative | 509 | 76.2% | $+7,288 | $+14.32 | 1.69 | $-5,038 |
| bias_momentum | early_high_conf | 509 | 76.2% | $+7,286 | $+14.31 | 1.69 | $-5,040 |

| g_inside_bar_breakout | baseline | 18 | 61.1% | $+92 | $+5.14 | 2.53 | $+0 |
| g_inside_bar_breakout | early_aggressive | 18 | 72.2% | $+59 | $+3.28 | 2.31 | $-33 |
| g_inside_bar_breakout | early_conservative | 18 | 72.2% | $+56 | $+3.08 | 2.23 | $-36 |
| g_inside_bar_breakout | early_high_conf | 18 | 72.2% | $+59 | $+3.28 | 2.31 | $-33 |

| opening_session | baseline | 29 | 37.9% | $+903 | $+31.14 | 2.55 | $+0 |
| opening_session | early_aggressive | 29 | 51.7% | $+38 | $+1.33 | 1.09 | $-865 |
| opening_session | early_conservative | 29 | 51.7% | $+40 | $+1.38 | 1.10 | $-863 |
| opening_session | early_high_conf | 29 | 51.7% | $+38 | $+1.33 | 1.09 | $-865 |

| raschke_baseline | baseline | 8 | 37.5% | $+46 | $+5.69 | 1.76 | $+0 |
| raschke_baseline | early_aggressive | 8 | 62.5% | $+22 | $+2.69 | 1.69 | $-24 |
| raschke_baseline | early_conservative | 8 | 62.5% | $+22 | $+2.69 | 1.69 | $-24 |
| raschke_baseline | early_high_conf | 8 | 62.5% | $+22 | $+2.69 | 1.69 | $-24 |

| spring_setup | baseline | 882 | 30.3% | $+5,206 | $+5.90 | 1.12 | $+0 |
| spring_setup | early_aggressive | 882 | 67.3% | $+1,056 | $+1.20 | 1.05 | $-4,150 |
| spring_setup | early_conservative | 882 | 67.3% | $+1,063 | $+1.21 | 1.05 | $-4,143 |
| spring_setup | early_high_conf | 882 | 67.3% | $+1,060 | $+1.20 | 1.05 | $-4,146 |

| vwap_pullback_v2 | baseline | 194 | 36.1% | $+4,654 | $+23.99 | 1.65 | $+0 |
| vwap_pullback_v2 | early_aggressive | 194 | 77.3% | $+1,974 | $+10.17 | 1.77 | $-2,680 |
| vwap_pullback_v2 | early_conservative | 194 | 77.3% | $+1,986 | $+10.24 | 1.78 | $-2,668 |
| vwap_pullback_v2 | early_high_conf | 194 | 77.3% | $+1,982 | $+10.21 | 1.78 | $-2,672 |

## Verdict per strategy

- **bias_momentum** (n=509): baseline $+12,326, best early-exit variant `early_conservative` $+7,288 ($-5,038 delta). Verdict: **NO**.
- **g_inside_bar_breakout** (n=18): baseline $+92, best early-exit variant `early_aggressive` $+59 ($-33 delta). Verdict: **NO**.
- **opening_session** (n=29): baseline $+903, best early-exit variant `early_conservative` $+40 ($-863 delta). Verdict: **NO**.
- **raschke_baseline** (n=8): baseline $+46, best early-exit variant `early_aggressive` $+22 ($-24 delta). Verdict: **NO**.
- **spring_setup** (n=882): baseline $+5,206, best early-exit variant `early_conservative` $+1,063 ($-4,143 delta). Verdict: **NO**.
- **vwap_pullback_v2** (n=194): baseline $+4,654, best early-exit variant `early_conservative` $+1,986 ($-2,668 delta). Verdict: **NO**.

## Caveats and limitations

1. **TBBO has no level-2 depth.** stacked_imbalance uses recent aggressor traffic at each price level as a PROXY for resting depth.  A true MBO order-book replay would give cleaner stacked-defense signals.
2. **Aggressor flip + stacked imbalance use a 30-TICK window**, not 30 seconds.  In thin tape that is a longer effective window; in fast tape, shorter.  Trade-off is intentional: signal sensitivity scales with activity.
3. **No slippage modeling on early exits.**  An early-exit fill is simulated at the signal-tick price exactly.  Real fills would be a tick or two worse, which would WORSEN every early-exit P&L result by ~$0.50-$1.00 per trade.
4. **Activation threshold of +0.5R is a knob.** Higher thresholds would fire fewer false-positives but miss more true-positive captures.
5. **2-month sample window** (~tick-data limit).  Small-sample strategies (opening_session, g_inside_bar_breakout, raschke_baseline) verdicts are NOISY -- treat any ADOPT verdict at n<30 as INSUFFICIENT.
6. **No interaction with strategy filters.** The trade list is the historical entry set; we are only swapping exits.
