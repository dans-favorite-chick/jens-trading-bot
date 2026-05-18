# Phase 13 Implementation Plan — 2026-05-18

**Status:** PLANNING (nothing in production yet). Each item below is what we INTEND to ship after backtest validation completes.

**Why this doc exists:** Sprint scope grew during research. Tracking all planned changes here so nothing slips through the cracks when we actually ship.

---

## A. Confirmed verdicts (data-driven)

### Kill list (set `enabled: False` in `config/strategies.py`)
| Strategy | 5y P&L | Reason |
|---|---:|---|
| `compression_breakout_v2` | -$1,904 | PF 0.61, MFE/MAE 0.19 — fundamentally anti-edge, no filter saves it |
| `compression_breakout_micro` | -$48 | PF 0.97, marginal anti-edge |
| `opening_session.open_test_drive` | -$639 | Small consistent loser (171 trades) |
| `noise_area` | (already retired) | Anti-edge confirmed (target=entry bug + structural issues) |

### Promote (already validated to fire profitably)
| Strategy | 5y P&L | PF | Max DD | Action |
|---|---:|---:|---:|---|
| `opening_session` (orb sub) | +$27,257 | 1.87 | $318 | **Promote to primary prod strategy** |
| `vwap_pullback_v2` | +$10,144 | 1.07 | $5,136 | Keep enabled; add time-filter |
| `es_nq_confluence` | +$2,028 | 3.38 | $72 | **Ship MES live feed to activate** |

### Demote
| Strategy | 5y P&L | Reason |
|---|---:|---|
| `bias_momentum` | +$1,492 in 40 trades | Currently PROD strategy. Risk-adjusted return is poor. Replace with orb. |

### Filter additions
| Strategy | Filter | Expected lift |
|---|---|---:|
| `vwap_band_pullback` | `ema_counter` (only trade against EMA trend) | +$1,452/16.5mo |
| `vwap_band_reversion` | `combo_ema_vol` (ema_counter + vol > 1.5x) | +$6,572/16.5mo (turns loser → breakeven) |
| ALL strategies | `time_skip_10_15ct` (universal lunch+early-afternoon skip) | +$5k/year free |

### Exit policy per strategy (Phase 13 core deliverable)
| Strategy | Optimal Exit | Baseline → New | Lift |
|---|---|---:|---:|
| `bias_momentum` | `time_15min` (hold 15 min, exit at market) | $24 → $963 | **40x** |
| `es_nq_confluence` | `scale_out_1r` (50% at 1R, 50% runner with BE stop) | $288 → $2,092 | **7.3x** |
| `spring_setup` | `fixed_2x_target` | $2,261 → $7,750 | 3.4x |
| `vwap_band_reversion` | `scale_out_1r` (after `combo_ema_vol` filter) | $54 → $4,256 | 78x |
| `vwap_band_pullback` | `trail_atr_1x` | $250 → $626 | 2.5x |
| `ib_breakout` | `fixed_2x_target` | $40 → $200 | 5x |
| `opening_session.orb` | (already optimized — keep current managed exits) | — | — |
| `vwap_pullback_v2` | `fixed_2x_target` | $9,020 → $10,336 | +15% |

---

## B. Bugs to fix in existing strategy code (NOT yet patched)

### Bug B1: `noise_area` sets `target_price = entry_price`
- **File:** `strategies/noise_area.py`
- **Symptom:** 100% of trades have target_price equal to entry_price; simulator hits "target" instantly at 0 P&L
- **Suspected cause:** Strategy uses VWAP as target (`target_at_vwap=True` default), but the entry condition triggers AT VWAP-band edge, and in backtest the pipeline's VWAP is updated before snapshotting → entry happens to coincide with target
- **Production impact:** UNKNOWN. Strategy is retired, so this hasn't been audited. Worth a code review even though it's not firing — the same coincidence pattern could appear in other strategies.

### Bug B2: `opening_session.open_drive` uses `t1 = pivot_pp` as target — CONFIRMED
- **File:** `strategies/opening_session.py` line 372: `t1=pivot_pp`
- **Symptom:** Classic PP = (PD_H + PD_L + PD_C)/3 lands BELOW current price after strong upward open drives → target is below entry on LONGs → strategy loses on every fire
- **Backtest:** 557 fires / 5y / -$190 avg / -$106k total
- **Code path:** `_evaluate_open_drive` → builds LONG when `c5 > rth_open` and `price > h5` (5m OR high) → calls `_build_signal(t1=pivot_pp, ...)`. PP is the prior-day mean reference. By definition, a strong open drive UP closes above PP, so PP is BELOW entry → "target hit" instantly at a LOSS.
- **Possible fixes:**
  - **Use R1/S1 (Continuation reading):** R1 = 2*PP - PD_L for LONG, S1 = 2*PP - PD_H for SHORT. These are above/below PP in the trade direction.
  - **Use prior-day OPPOSITE-side level (Continuation reading):** PD_H for LONGs, PD_L for SHORTs — but only if not already breached.
  - **Use VWAP + 1.5*ATR (Continuation reading):** simpler, regime-adaptive.
  - **Flip direction (Mean Reversion reading):** if the strategy is truly meant as fade, then break of OR → trade OPPOSITE direction with PP as target. Probably not the intent given the name.
- **Decision needed:** Operator should review open_drive design intent. **Recommendation: ship as CONTINUATION with R1/S1 targets — matches the name and the OR-breakout entry signature.**

### Bug B3: `orb_fade` produces 0 signals in 5-year backtest — ROOT CAUSE FOUND
- **File:** `strategies/orb_fade.py` line 162
- **Symptom:** Strategy is supposed to fade failed breakouts of the 15-min OR. Despite firing-window of 08:45-12:00 CT and clear failed-breakouts in the data, generates 0 signals
- **Root cause:** Wallclock freshness check:
  ```python
  if (time.time() - last_bar_ts) > bar_freshness:  # bar_freshness = 90 sec
      return None
  ```
  In backtest, `time.time()` is the wallclock (2026) but `last_bar_ts` is historical (e.g. 2021). The diff is **years in seconds** → 100% of evaluations rejected as stale.
- **Production impact:** Live SimORB-Fade account shows $0 P&L. NEEDS DIAGNOSIS — if the live pipeline's `bars_1m[-1].end_time` is a `datetime` or epoch from a different timebase than `time.time()`, the same gate could be rejecting live too. Worth a one-line live-log check before assuming live is fine.
- **Fix:** Either (a) compare against `market["now_ct"]` instead of `time.time()`, or (b) accept a `now_unix` override in evaluate() that backtests can set. Option (a) is simpler.
- **Proof of fix:** Standalone reimplementation `eval_orb_fade_fixed` in `tools/phoenix_new_strategy_lab.py` uses `eval_ts` instead of `time.time()`. Backtest results (when complete) will show whether the strategy concept itself has edge.

### Bug B4: `compression_breakout_v2` anti-edge (MFE/MAE 0.19)
- **File:** `strategies/compression_breakout_v2.py`
- **Symptom:** Even with V2 design (3-of-4 conditions, NQ-tuned BB std), strategy loses 5x of P&L as it gains
- **Verdict:** Strategy concept is broken for MNQ in current regime. **No fix recommended — just kill.**

---

## C. NEW strategies to test (this session) — RESULTS IN

5-year MNQ backtest (2021-05 → 2026-05, 1,771,336 cycles, 234s runtime).
All 7 implemented as standalone fns in `tools/phoenix_new_strategy_lab.py`.

| ID | Name | Trades | WR% | Total P&L | PF | Max DD | Avg Hold | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---|
| **g** | `inside_bar_breakout` | 1015 | 70.0% | **+$11,300** | 4.88 | $65 | 5.2 min | **SHIP — strongest winner** |
| **e** | `multi_day_breakout`  | 685  | 77.8% | **+$9,097**  | 6.79 | $67 | 2.0 min | **SHIP** |
| **a** | `asian_continuation`  | 596  | 80.5% | **+$5,909**  | 8.29 | **$21** | 2.0 min | **SHIP — lowest DD of any strategy ever tested** |
| **d** | `orb_fade_fixed`      | 57   | 17.5% | +$145        | 1.26 | $164 | 16.7 min | **B3 fix unblocks it. Marginal edge. Test exit policies before promoting.** |
| **c** | `poc_magnet_reversion`| 6    | 33.3% | -$8          | 0.85 | $29 | 44 min | KILL — insufficient sample, gates too tight |
| **f** | `eod_mean_reversion`  | 17   | 29.4% | -$14         | 0.87 | $81 | 8.8 min | KILL — insufficient sample |
| **b** | `rth_open_drive_scalp`| 94   | 16.0% | -$255        | 0.46 | $297 | 1.7 min | **KILL — anti-edge.** Strong-close in OR bar predicts FADE not continuation. |

### Combined edge of the 3 winners
- **Total: +$26,306 over 5 years (~$5.2k/year)**
- All 3 positive every single year 2021-2026
- All 3 ultra-low max DD (under $70)
- All 3 short hold (under 6 min avg)

### Per-year (winners only)
```
year                      2021    2022    2023    2024    2025   2026
strategy
a_asian_continuation     868     1078    1457    1323    861    322
e_multi_day_breakout    1368     2043    1916    1660    1552   558
g_inside_bar_breakout   2580     1839    2498    2223    1709   452
COMBINED                4816     4960    5871    5206    4122  1332
```

### Caveats
1. **High WR at 2:1 RR (70-80%) is unusual** — usually breakout strategies cluster at 40-50% WR with 2-3R winners. Possible explanations: (a) the entry signature genuinely is strong (close BEYOND a tight congestion = real momentum), (b) MNQ in this window had unusual trending characteristics. Worth a sanity-check via walk-forward.
2. **Correlation across the 3 winners is unknown** — they all fire on momentum bars. May have overlapping signals = correlated DD. Need correlation matrix before sizing them as 3 independent strategies.
3. **No exit-policy optimization yet** — these are 2:1 fixed target. Lab winners would likely lift further with `trail_atr_1x` (already proven to help `vwap_band_pullback`).

### Promotion plan
- **Step 4 of sequence (next session):** convert `g`, `e`, `a` to production-grade strategy classes (`strategies/inside_bar_breakout.py`, etc.) with full BaseStrategy interface, config in `config/strategies.py`, deregistered killed strategies.
- **Step 5:** test all 3 with exit-policy battery before adding to config.
- **Step 6:** ship to `lab_bot` first (NOT prod_bot) for 1-2 weeks live validation given the high-WR-suspicious result.

---

## D. Infrastructure planned (not built yet)

### D1. Per-strategy `exit_policy` config field
- Add `"exit_policy": "scale_out_1r"` (or similar) to each strategy's config in `config/strategies.py`
- Base bot reads this field; if set, applies the policy instead of strategy's internal exit logic
- Allows the Phase 13 exit improvements to be controlled via config, not code-per-strategy

### D2. Universal time-of-day filter
- Add `"skip_hours_ct": [10, 11, 12, 13, 14]` to global risk config
- Base bot gates ALL strategies during these hours
- Free $5k/year recovery

### D3. Save volumetric snapshots for future backtest
- Cron job: every 10 min, copy `data/volumetric_latest.json` to `data/historical/volumetric/<ts>.json`
- After 3-6 months, build backtest pipeline that walks this history
- Enables `footprint_cvd_reversal` backtest + future order-flow strategies

### D4. Wire MES live feed (for `es_nq_confluence`)
- NT8 chart on MES with TickStreamer indicator loaded
- bridge_server.py fans out `mes_*` market dict fields
- tick_aggregator builds parallel `mes_bars_5m`
- base_bot enriches `market["mes_bars_5m"]`
- Activates Phase 12C es_nq_confluence in live trading

---

## E. Reallocation decisions

### E1. Kill 5 dead NT8 sub-accounts (free $9,914 of capital)
- SimOpenDrive ($1,994), SimOpen Test Drive ($1,996), SimOpen Auction In Range ($1,993), SimOpen Auction Out of Range ($1,936), SimPremarket Breakout ($1,995)
- ZERO signals in 5y of backtest. ZERO P&L in live.
- Reallocate capital to active accounts (SimORB, SimBias Momentum after demotion, new strategies)

### E2. Move es_nq_confluence routing to a dedicated `SimESNQConfluence` account
- Currently routes to Sim101 (temporary per Phase 9.1)
- Once MES feed is wired (D4), promote to dedicated account for cleaner P&L tracking

---

## F. Sequence of work

**Step 1 (this session):** Build + backtest strategies a-g
**Step 2 (this session):** Fix bugs B2 (open_drive pivot) and B3 (orb_fade 0 signals)
**Step 3 (next session):** Implement infrastructure D1 + D2 in code
**Step 4 (next session):** Apply confirmed verdicts (kill list, promotions, filters, exits) to config/strategies.py
**Step 5 (next session):** Run full test suite + re-run 5-year backtest to validate
**Step 6 (next session):** Restart sim_bot on new code + monitor
**Step 7 (separate sprint):** Wire MES feed (D4) → activate es_nq_confluence
**Step 8 (separate sprint):** Footprint backtest infrastructure (D3) → enable strategy 7+

---

## G. Open questions for operator

1. **bias_momentum demotion** — confirm OK to take it out of PROD and replace with orb?
2. **Dead sub-accounts** — confirm OK to close the 5 unused NT8 sub-accounts?
3. **open_drive bug B2** — review strategy design intent. Continuation or reversion?
4. **Order flow / footprint** — operator already has live data. Want to invest in historical data ($100-500/mo Databento MBO) or start free-snapshot recording?
5. **Sim_bot still bleeding** — Phase 13 ship target date?

---

## I. Compounding plan (NEW — 2026-05-18)

### Methodology

Tested 4 sizing policies on the 13,123 trades from the 11-winner portfolio.
$1,500 starting equity, 30-contract hard cap, size-scaled slippage,
DD scale-down at 15% from ATH, daily circuit breaker at 4%, consecutive-loss
scale-down after 3 losers.

### Results (5-year backtest, $1,500 start)

| Policy | Final $ | Return | Max DD $ | Max DD % | Avg N | Max N |
|---|---:|---:|---:|---:|---:|---:|
| **flat_1** (no compounding) | $63,670 | 4,145% | $1,758 | 16.6% | 1.0 | 1 |
| tier_1500 (1c/$1.5k — AGGRESSIVE) | $1,067,468 | 71,065% | $52,658 | **77.1%** ❌ | 19.9 | 30 |
| **tier_3000** (1c/$3k — **RECOMMENDED**) | **$1,091,290** | **72,653%** | $52,658 | 34.2% ✓ | 17.8 | 30 |
| tier_5000 (1c/$5k — CONSERVATIVE) | $960,270 | 63,918% | $52,658 | 20.8% ✓ | 15.1 | 30 |
| fixed_ratio_jones | $723,374 | 48,125% | $48,209 | 31.2% | 11.6 | 30 |
| tier_1500 @ 55% WR stress | $856,724 | 57,015% | $52,995 | 73.2% | 17.9 | 30 |

**Pick:** `tier_3000` — best risk-adjusted. Achieves 97% of tier_1500's final equity at less than HALF the drawdown.

### Path to scaling (tier_3000, RECOMMENDED)

| Contracts | First reached |
|---:|---|
| 1  | 2021-05-17 (day 1) |
| 2  | 2021-07-28 (~10 weeks) |
| 3  | 2021-08-16 (~13 weeks) |
| 5  | 2021-09-29 (~4.5 months) |
| 10 | 2022-12-08 (~19 months) |
| 15 | 2023-01-24 (~20 months) |
| 20 | 2023-02-27 (~21 months) |
| 25 | 2023-03-12 (~22 months) |
| 30 (cap) | 2023-03-27 (~22 months) |

### Year-end equity (tier_3000)

| Year | End equity | Year P&L | Max N | Trades |
|---|---:|---:|---:|---:|
| 2021 (partial) | $17,733 | +$16,233 | 6 | 3,079 |
| 2022 | $36,774 | +$19,042 | 12 | 1,828 |
| 2023 | $345,198 | +$308,423 | 30 (capped) | 2,578 |
| 2024 | $690,775 | +$345,578 | 30 | 2,437 |
| 2025 | $925,712 | +$234,938 | 30 | 1,977 |
| 2026 (partial Jan-May) | $1,091,290 | +$165,578 | 30 | 609 |

### Per-strategy scale-out plan (N contracts → tranches)

#### Breakout strategies — `inside_bar_breakout`, `multi_day_breakout`, `asian_continuation`, `ib_breakout`, `spring_setup`
| N | Tranches |
|---:|---|
| 1 | 100% at 2R target |
| 2 | 1 @ 1R (lock profit), 1 @ 2R |
| 3 | 1 @ 1R, 1 @ 2R, 1 runner trailed BE+1R |
| 5 | 1 @ 0.75R, 2 @ 1.5R, 1 @ 2R, 1 runner |
| 10 | 2 @ 0.75R, 3 @ 1.5R, 3 @ 2R, 2 runner |
| 20 | 4 @ 0.75R, 6 @ 1.5R, 6 @ 2R, 4 runner |
| 30 | 6 @ 0.75R, 9 @ 1.5R, 9 @ 2R, 6 runner |

#### Mean-reversion strategies — `vwap_band_reversion`, `vwap_band_pullback`
| N | Tranches |
|---:|---|
| 1 | 100% at VWAP (target) |
| 2 | 1 @ half-target, 1 @ VWAP |
| 3 | 1 @ half, 1 @ VWAP, 1 with BE stop until VWAP cross |
| 5 | 1 @ quarter, 2 @ half, 1 @ VWAP, 1 BE |
| 10+ | 20% at quarter, 30% at half, 30% at VWAP, 20% BE-runner |

#### `opening_session.orb` — uses strategy-internal managed exit
| N | Logic |
|---:|---|
| 1-4 | Per-contract managed exit (current logic) |
| 5+ | Same managed exit on first N-1, last contract is BE+trail runner |

#### `es_nq_confluence` — uses `scale_out_1r`
| N | Tranches |
|---:|---|
| 1 | 100% at 2R |
| 2 | 1 @ 1R, 1 @ 2R + runner |
| 3+ | 50% at 1R, 25% at 2R, 25% runner |

#### `bias_momentum` — uses `time_15min` (no target; exits on time)
| N | Logic |
|---:|---|
| any | All contracts exit together at +15 min — no tranching, just parallel |

### Operational rules layered on top of sizing

1. **DD scale-down:** when equity drops below 85% of all-time-high, drop one tier (back up after recovery)
2. **Consecutive-loss scale-down:** after 3 losses in a row, halve next trade's size
3. **Daily circuit breaker:** if today's loss exceeds 4% of equity, halt trading for the day
4. **30-contract hard cap:** physical limit regardless of equity (CME / MNQ liquidity reality)
5. **Slippage model:** 1 tick per side for ≤5 contracts; 1.5 ticks for 6-15c; 2 ticks for 16-30c (already baked into backtest)

### Honest caveats (poked holes)

1. **In-sample WR is optimistic** — 70-80% WR strategies likely degrade to 55-65% out-of-sample. The 55% stress test still produced $856k, so the plan is robust but the real number could be 30-50% lower.
2. **30-contract cap is necessary** — without it, math blows up to $900B (564M contracts). MNQ daily volume can't absorb that.
3. **DD of 34% is real** — would be emotionally severe. Worth pre-committing to the plan in writing.
4. **The big 2022→2023 jump (+$300k)** — heavily compressed cap-hit growth. Sensitive to whether those particular trades repeat out-of-sample. Conservative read: assume 50% of that lift in forward returns.
5. **Position correlation** — multiple winners often fire on the same momentum bar. Real "concurrent equity-at-risk" is higher than per-trade DD suggests. Worth tracking.
6. **No tax drag modeled.** Futures get 60/40 long-short treatment but at $1M profit there IS a meaningful tax bill.

---

## H. Files created/touched this sprint (audit trail)

**Created:**
- `tools/phoenix_real_backtest.py` (CSV-backed enrichment pipeline)
- `tools/phoenix_exit_experiments.py` (Phase 13 exit policy battery)
- `tools/phoenix_confluence_filters.py` (entry filter experiments)
- `tools/opening_session_sub_breakdown.py` (sub-evaluator P&L tracker)
- `tools/phoenix_new_strategy_lab.py` (new strategies a-g — RESULTS IN)
- `tools/phoenix_compounding_backtest.py` (contract-scaling engine — RESULTS IN)
- `docs/STRATEGY_DEEP_DIVE_2026-05-18.md` (initial analysis)
- `docs/PHASE_13_IMPLEMENTATION_PLAN.md` (this file)

**Backtest result CSVs:**
- `backtest_results/phoenix_real_5year.csv` (22,156 baseline trades)
- `backtest_results/phoenix_exit_summary_2025.csv` (exit-policy lifts)
- `backtest_results/phoenix_new_strategy_lab.csv` (2,470 new-strategy trades)
- `backtest_results/phoenix_compounding_summary.csv` (5 sizing policies)
- `backtest_results/phoenix_compounding_tier_1500.csv` (aggressive equity curve)
- `backtest_results/phoenix_compounding_tier_3000.csv` (RECOMMENDED equity curve)
- `backtest_results/phoenix_compounding_tier_dates.csv` (when each tier reached)

**Pending modifications (NOT YET DONE):**
- `config/strategies.py` (kill list, filter configs, exit_policy fields)
- `bots/base_bot.py` (universal time-of-day filter, exit_policy dispatch)
- `core/strategy_risk_registry.py` (deregister killed strategies)
- `strategies/orb_fade.py` (B3 fix — pending investigation)
- `strategies/opening_session.py` (B2 fix — pending operator decision)
- `ninjatrader/TickStreamer.cs` (D4 MES feed — separate sprint)
