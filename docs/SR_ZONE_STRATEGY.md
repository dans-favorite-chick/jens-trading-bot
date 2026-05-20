# S/R Zone Strategy — Investigation Report

**Date:** 2026-05-19
**Branch:** weekly-evolution/2026-05-17
**Window tested:** 2021-05-17 → 2026-05-15 (5 years, MNQ continuous)
**Total trades evaluated:** 10,357 across 6 variants
**Headline finding:** **NEGATIVE.** No S/R-bounce variant tested showed
positive expectancy. Best variant (round-numbers only) lost $476 over 5y
on 610 trades. Worst (sr_bounce_loose) lost $2,613 on 2,657 trades.
**Recommendation:** DO NOT add as a standalone strategy. Reconsider as a
filter/confluence input only.

---

## 1. Methodology

### 1.1 S/R detection engine (`core/sr_zones.py`)

The detector returns a ranked list of `SRZone(price, type, strength,
age_bars, n_tests, source, width_ticks, last_touch_bars)` from a rolling
window of 5m bars + optional context (prior day H/L/POC, VWAP bands).

**Four candidate-level methodologies, then clustered + scored:**

| Source | How it's identified | Touches required |
|--------|--------------------|------------------|
| `swing` | Local max/min in 5m bars with N-bar lookback each side (N=10) | >= 2 to qualify |
| `round` | Round-100 levels (24500, 24600, ...) within ±600 pts of current price | 0 (psychological) |
| `pdh` / `pdl` / `poc` | Prior day high / low / POC carried via Phoenix pipeline | 0 (always emitted) |
| `vwap_band_upper` / `vwap_band_lower` | VWAP ± 2.1 sigma bands | 0 (always emitted) |

**Touch detection:** a "touch" = a bar that came within `proximity_ticks=6`
of the level AND price reversed by `reversal_ticks=6` within 5 bars after.
Side-aware (support touches come from above, resistance from below).
Duplicate touches within 5 bars are deduplicated.

**Strength scoring** (composite 0-1):
- 40% touch count (saturates at 6)
- 25% recency (last touch < 200 bars away = full credit)
- 15% tightness (cluster spread; tighter = stronger)
- 20% source weight (PDH/POC=0.35, swing=0.30, round/vwap=0.20-0.25)

Zones that overlap within `cluster_ticks=8` are deduplicated, keeping
the strongest.

### 1.2 Strategy logic (`tools/phoenix_sr_strategy_lab.py`)

At each 5m bar close during RTH window **08:45 - 14:30 CT**:

1. Detect S/R zones (cached, recomputed every 15 minutes).
2. Find nearest qualifying zone within 12 ticks of current price.
3. Require a **rejection candle**:
   - Support LONG: bar.low <= zone + 4t, bar.close >= zone + 1t,
     lower wick >= 30% of range
   - Resistance SHORT: bar.high >= zone - 4t, bar.close <= zone - 1t,
     upper wick >= 30% of range
4. Stop: zone width + 2t buffer beyond the zone.
5. Target: 2R fixed (per Phase 13 Section U exit guidance).
6. Dedup: once per zone per day. Max 4 trades per day per variant.

### 1.3 Variants tested

| Variant | min_strength | min_tests | source filter |
|---------|--------------|-----------|---------------|
| `sr_bounce_strict` | 0.70 | 3 | any |
| `sr_bounce_moderate` | 0.50 | 2 | any |
| `sr_bounce_loose` | 0.30 | 2 | any |
| `sr_bounce_round_only` | 0.00 | 1 | `round` only |
| `sr_bounce_vwap_dev` | 0.00 | 0 | `vwap_band_*` only |
| `sr_bounce_swing_only` | 0.30 | 2 | `swing` only |

---

## 2. 5-Year Results (raw, no overfitting)

### 2.1 Per-variant aggregates

| Strategy | n | WR% | Wilson 95% low | Total $ | Avg $ | PF | Max DD | Avg hold (min) |
|----------|---:|------:|---:|---:|---:|---:|---:|---:|
| sr_bounce_vwap_dev | 390 | 26.7 | 22.5 | -464 | -1.19 | 0.74 | 546 | 1.7 |
| sr_bounce_round_only | 610 | 29.8 | 26.3 | -476 | -0.78 | 0.83 | 658 | 1.4 |
| sr_bounce_swing_only | 1,975 | 27.1 | 25.2 | -1,832 | -0.93 | 0.73 | 1,839 | 1.4 |
| sr_bounce_strict | 2,078 | 27.0 | 25.1 | -2,059 | -0.99 | 0.73 | 2,088 | 1.4 |
| sr_bounce_moderate | 2,647 | 27.4 | 25.7 | -2,613 | -0.99 | 0.74 | 2,635 | 1.4 |
| sr_bounce_loose | 2,657 | 27.3 | 25.7 | -2,613 | -0.98 | 0.74 | 2,633 | 1.4 |

**Aggregate: -$10,056 P&L on 10,357 trades over 5 years.** Every variant
has Wilson 95% CI lower bound BELOW the 33% breakeven WR required for a
2:1 RR system → **statistical confirmation of negative edge**, not noise.

### 2.2 Per-variant per-year P&L ($)

| Strategy | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|----------|-----:|-----:|-----:|-----:|-----:|-----:|
| sr_bounce_loose | -578 | -179 | -592 | -624 | -426 | -214 |
| sr_bounce_moderate | -574 | -172 | -598 | -624 | -432 | -212 |
| sr_bounce_round_only | -92 | **+198** | -90 | -210 | -108 | -172 |
| sr_bounce_strict | -512 | -240 | -392 | -504 | -231 | -180 |
| sr_bounce_swing_only | -406 | -363 | -440 | -263 | -266 | -93 |
| sr_bounce_vwap_dev | -131 | -54 | -166 | **+12** | -160 | **+36** |

Only 2 of 36 strategy-years are profitable. No variant shows year-over-year
consistency. The 2022 bump for round_only is the only standout — almost
certainly a regime artifact (high vol back-and-forth around obvious round
levels in a bear-market year), not generalizable.

### 2.3 Trade counts per year (proving 5-year coverage)

| Strategy | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|----------|---:|---:|---:|---:|---:|---:|
| sr_bounce_loose | 364 | 478 | 588 | 570 | 507 | 150 |
| sr_bounce_moderate | 363 | 474 | 586 | 569 | 506 | 149 |
| sr_bounce_round_only | 52 | 127 | 117 | 122 | 135 | 57 |
| sr_bounce_strict | 297 | 359 | 457 | 459 | 393 | 113 |
| sr_bounce_swing_only | 293 | 329 | 448 | 446 | 365 | 94 |
| sr_bounce_vwap_dev | 56 | 78 | 107 | 79 | 54 | 16 |

Clean coverage across all 5 years + 2026 YTD (window ends 2026-05-15).
2021 is partial (May-Dec). 2026 is partial (Jan-May).

### 2.4 By zone source (which type of S/R held best)

| Source | n | Total $ | Avg $/trade |
|--------|---:|--------:|---:|
| pdh | 247 | -164 | **-0.66** |
| round | 1,791 | -1,488 | -0.83 |
| swing | 7,255 | -6,669 | -0.92 |
| poc | 288 | -381 | -1.32 |
| vwap_band_lower | 216 | -360 | -1.66 |
| vwap_band_upper | 367 | -640 | -1.74 |
| pdl | 193 | -355 | -1.84 |

**Prior-day-high source bleeds least** (-$0.66/trade). All sources are
unprofitable. Round numbers are second-cheapest. VWAP std-dev bands and
prior-day low are the worst — counter-intuitive given industry "wisdom"
that VWAP 2-sigma extremes are reliable reversal points.

### 2.5 Why the strategy fails (mechanical autopsy)

- Median stop distance: **2.5 points** (10 ticks)
- Median hold time: **1 minute** (one bar!)
- 73% of trades hit STOP (7,525 / 10,357)
- 27% hit TARGET, 0% time-exit

The "rejection candle" pattern is too aggressive. Once entered at the wick
low/high, MNQ noise (typical 1-3 pt fluctuation per minute) takes out the
2-3 pt stop before the 2:1 target can develop. We tested an alt with
ATR-based wider stops (max(0.6 × ATR, structural stop), capped at 8 pts,
target 1.5R): **EVEN WORSE — -$10,454 PF 0.65** on 2,692 moderate-variant
trades. Wider stops just bleed more per trade without enough WR uplift.

The fundamental problem: at S/R zones, the EDGE is captured by smart-money
ABSORBING the rejection over many minutes/hours, not by a single rejection
candle on a 5m timeframe. By the time a "clean" rejection candle prints,
the move is often already 50-70% over.

### 2.6 By direction

| Direction | n | Total $ | Avg $/trade |
|-----------|---:|--------:|---:|
| LONG | 5,512 | -5,119 | -0.93 |
| SHORT | 4,845 | -4,937 | -1.02 |

Symmetric losses; not a directional bias issue.

---

## 3. Comparison vs. existing Phoenix strategies (5y)

| Strategy | n | Total $ | $/trade |
|----------|---:|--------:|---:|
| bias_momentum | 13,790 | +178,379 | +12.94 |
| spring_setup | 20,778 | +18,544 | +0.89 |
| vwap_pullback_v2 | 5,879 | +10,144 | +1.73 |
| es_nq_confluence | 131 | +2,028 | +15.48 |
| vwap_band_pullback | 324 | +794 | +2.45 |
| ib_breakout | 152 | +342 | +2.25 |
| compression_breakout_micro | 254 | -49 | -0.19 |
| **sr_bounce_round_only (best of new)** | **610** | **-476** | **-0.78** |
| **sr_bounce_loose (worst of new)** | **2,657** | **-2,613** | **-0.98** |

Even the **least-bad** S/R variant is worse per-trade than `compression_breakout_micro`,
which is already a borderline strategy. None of our S/R variants belongs in
the live portfolio.

---

## 4. Overlap with existing strategies (estimated)

S/R concepts ARE already partially priced into the Phoenix portfolio:

- **`vwap_band_pullback` / `vwap_band_reversion`** — already exploits
  VWAP std-dev bands (1-sigma typically; our `sr_bounce_vwap_dev` used 2.1
  sigma). The fact that band_pullback is profitable (+$794, $2.45/trade)
  while our `vwap_dev` variant loses suggests the EDGE is in the 1-sigma
  pullback-to-fair-value direction, NOT the 2-sigma extreme reversal.
- **`opening_session.orb`** — uses the 15-min RTH OR as S/R (`rth_15min_high`
  / `rth_15min_low`). That's a special case of "today's swing pivot" and
  is profitable (in the orb sub of opening_session).
- **`compression_breakout_v2`** — implicitly trades the break of a tight
  range, which is breaking through a micro-S/R.
- **`spring_setup`** — sweep + reversal at a level, then bounce. This IS
  essentially S/R-bounce-with-better-confirmation (wick + delta + ATR
  gating), and it's profitable.

**Quantifying overlap is hard, but the takeaway is clear:** Phoenix already
extracts S/R edge through smarter entry filters (delta divergence, volume
climax, wick-AND-CVD confirmation). Adding a generic "wait for rejection
candle at any qualifying zone" strategy adds noise, not signal.

---

## 5. Data quality validation

Ran `python tools/validate_backtest_quality.py` after the backtest.
All 6 S/R variants pass cleanly:

```
=== S/R zone strategy lab: backtest_results/phoenix_sr_strategy_lab.csv ===
  [ OK ] sr_bounce_loose                 n= 2657  last=2026-05-15  clean
  [ OK ] sr_bounce_moderate              n= 2647  last=2026-05-15  clean
  [ OK ] sr_bounce_round_only            n=  610  last=2026-05-15  clean
  [ OK ] sr_bounce_strict                n= 2078  last=2026-05-15  clean
  [ OK ] sr_bounce_swing_only            n= 1975  last=2026-05-15  clean
  [ OK ] sr_bounce_vwap_dev              n=  390  last=2026-05-06  clean
```

(The tool's overall RESULT = ERRORS is from pre-existing lab files
unrelated to this work; the silent-stop bug fix from commit a9a5ef9 is
inherited via `simulate_trade()`.)

**Issue found and fixed during development:**
First-pass run produced trades only through 2023-05-15 (silent stop after
2 years). Root cause: zone-cache freshness used `len(bars_5m)` as the
key, but `bars_5m` is a `deque(maxlen=200)` — it saturates after ~16 hours
and `len()` never grows after that. Cache became permanently stale, no
new zones were detected.
Fix: use `bars_5m[-1].end_time` (epoch seconds, monotonic) instead.
Recomputed → 10,357 trades spanning full 2021-2026 window. This is the
"Phoenix failures are silent" pattern (see `memory/feedback_silent_failures.md`).

---

## 6. Verdict

### 6.1 Is S/R bouncing profitable on MNQ?

**No, not as a standalone 5m strategy with the rejection-candle entry
trigger we tested.** Across 6 variants, 10,357 trades, 5 years of clean
data, every single variant has:
- Win rate Wilson-95%-low BELOW the 33% breakeven for 2:1 RR
- PF < 1.0
- Negative every year for 4-of-6 variants

The 27-30% WR is **STATISTICALLY** lower than 33% (Wilson lower bound
22-26%), not just unlucky. There IS no edge in this exact setup.

### 6.2 Should it join the Phoenix portfolio?

**No.** Best variant loses $0.78/trade. Worst loses $1.74/trade (vwap_band).
Compare to current min-bar (`spring_setup`, $0.89/trade winning). No
parameter tweak we can imagine recovers a positive expectancy.

### 6.3 Could the S/R detection engine be useful elsewhere?

**Yes — as a filter, not a primary trigger.** The `core/sr_zones.py`
module produces a clean, ranked list of zones with strength scores. Two
high-value follow-ups:

1. **Veto strategies that fire INTO strong S/R.** Example: `bias_momentum`
   shouldn't open a long if a strong resistance zone is < 4 ticks above.
   This could improve PF by avoiding obvious mean-reversion targets.

2. **Confluence boost for existing winners.** `spring_setup` could gain a
   +25% size if the wick rejection is AT a strength >= 0.7 zone. (Test
   first; don't ship blind.)

3. **Test FAILED-HOLD continuations.** If price BREAKS a strong zone,
   that's often the start of a real move. We didn't test this direction
   here — would be a separate spawn task.

### 6.4 Expected baseline if added anyway (warning)

If for some reason the operator wanted to ship `sr_bounce_round_only`
(the least-bad variant):
- **Expected: -$476 / 5y = -$95 / year on ~122 trades/yr**
- Sharpe-equivalent: negative
- Worst year: -$210 (2024)
- This would degrade portfolio PF and trip the daily -$45 drawdown gate
  on bad days

**Bottom line: leave it out.**

---

## 7. Deliverables shipped

| File | Purpose | Status |
|------|---------|--------|
| `core/sr_zones.py` | Pure S/R detection engine (importable) | New, 280 LOC |
| `tools/phoenix_sr_strategy_lab.py` | 6-variant lab + 5y runner | New, 380 LOC |
| `backtest_results/phoenix_sr_strategy_lab.csv` | 10,357 raw trades | Generated |
| `backtest_results/phoenix_sr_strategy_summary.csv` | Per-variant aggregates | Generated |
| `tools/validate_backtest_quality.py` | Updated LAB_FILES to include S/R lab | Edited |
| `docs/SR_ZONE_STRATEGY.md` | This report | New |

No production code touched. No existing strategy modified.
