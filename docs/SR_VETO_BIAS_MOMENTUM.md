# S/R Zone VETO Filter on bias_momentum — Investigation Report

**Date:** 2026-05-19
**Branch:** `weekly-evolution/2026-05-17`
**Tool:** `tools/phoenix_sr_veto_analyzer.py`
**Inputs:** `backtest_results/phoenix_real_5year.csv` (13,790 bias_momentum trades), `data/historical/mnq_5min_databento.csv` (354,270 5m bars over 5 years)
**Output CSV:** `backtest_results/phoenix_sr_veto_summary.csv`
**Status:** **NEGATIVE — DO NOT SHIP**

---

## Hypothesis (from Phase 13 V.4)

`bias_momentum` is a trend-following / continuation strategy. When it fires LONG, it
expects price to keep moving up; SHORT expects continued drop. If a strong
**resistance** zone sits a few ticks above a LONG entry (or strong **support** below
a SHORT entry), the trade should structurally fail at that level. **Skipping
those trades entirely** ought to:

- shed the losers that stall and get stopped at the wall,
- keep the winners that have open road ahead,
- net: higher PF and per-trade $, same or modestly lower trade count.

This was the third deferred follow-up from Section V.3 (`717d23f`) — the direct
S/R-bounce strategy was negative but the detection engine (`core/sr_zones.py`) was
flagged as potentially valuable for VETO use cases like this one.

---

## Methodology

For each of the 13,790 `bias_momentum` trades:

1. Slice the last 300 5m bars whose `end_time <= entry_ts`.
2. Call `core.sr_zones.detect_sr_zones(...)` on that window.
   - Zones are cached per (date, 30-min bucket) so consecutive trades in the same
     half-hour share zone detection (745 cache hits / 13,045 misses).
3. For each (strength threshold X, proximity Y ticks) cell of the grid:
   - **LONG veto fires** if any zone has `type == "resistance"`,
     `strength >= X`, and price-distance `<= Y` ticks ABOVE entry.
   - **SHORT veto fires** if any zone has `type == "support"`,
     `strength >= X`, and price-distance `<= Y` ticks BELOW entry.
4. Compare kept-trade aggregate ($ total, win rate, profit factor) to baseline.
5. Per-direction and per-year breakdowns for verification.

**Tick conventions:** MNQ = 0.25 / tick, $0.50 per tick, $2 per point.

**Grid swept:** strength X in {0.5, 0.6, 0.7} × proximity Y in {2, 3, 4, 8, 12}
ticks = 15 cells (extended beyond the brief's 3×3 because the brief's tightest Y
was 4 ticks — given MNQ noise of 1-3 pts per 5m bar, the 2t and 3t cells are
where the cleanest "right at the wall" signal would live, if any).

**Calibration:** zones don't see prior-day H/L/POC or VWAP bands (none stored
in the trades CSV). They use only swing pivots + round numbers from the bar
window — the same primary sources used in the `phoenix_sr_strategy_lab` baseline.
This is a conservative test: with more inputs (VWAP, PDH/POC) the veto would
fire more often. Since the veto already destroys edge with this minimal input
set, adding more would only make it worse.

**Tool runtime:** 18-20 seconds on full 13,790 trades, Python 3.14 / pandas 3.0.

---

## Results — full 15-cell grid

Baseline: **13,790 trades / +$178,379 / WR 40.5% / PF 1.331**

| cell | kept n | kept $ | kept WR | kept PF | blk n | blk $ | $/blk | fire % | $ lift |
|------|-------:|-------:|--------:|--------:|------:|------:|------:|-------:|-------:|
| X0.5_Y2 | 13,323 | $172,128 | 40.4% | 1.328 | 467 | +$6,251 | **-$13.39** | 3.4% | -$6,251 |
| X0.5_Y3 | 13,183 | $169,166 | 40.4% | 1.325 | 607 | +$9,214 | -$15.18 | 4.4% | -$9,214 |
| X0.5_Y4 | 13,010 | $168,974 | 40.4% | 1.329 | 780 | +$9,406 | -$12.06 | 5.7% | -$9,406 |
| X0.5_Y8 | 12,330 | $165,558 | 40.6% | 1.338 | 1,460 | +$12,822 | -$8.78 | 10.6% | -$12,822 |
| X0.5_Y12 | 11,699 | $156,843 | 40.6% | 1.333 | 2,091 | +$21,536 | -$10.30 | 15.2% | **-$21,536** |
| X0.6_Y2 | 13,343 | $172,520 | 40.4% | 1.329 | 447 | +$5,859 | -$13.11 | 3.2% | -$5,859 |
| X0.6_Y3 | 13,210 | $169,488 | 40.4% | 1.325 | 580 | +$8,891 | -$15.33 | 4.2% | -$8,891 |
| X0.6_Y4 | 13,042 | $169,096 | 40.4% | 1.328 | 748 | +$9,283 | -$12.41 | 5.4% | -$9,283 |
| X0.6_Y8 | 12,396 | $166,026 | 40.6% | 1.338 | 1,394 | +$12,354 | -$8.86 | 10.1% | -$12,354 |
| X0.6_Y12 | 11,802 | $158,672 | 40.6% | 1.335 | 1,988 | +$19,706 | -$9.91 | 14.4% | -$19,706 |
| X0.7_Y2 | 13,375 | $172,782 | 40.4% | 1.328 | 415 | +$5,598 | -$13.49 | 3.0% | **-$5,598** |
| X0.7_Y3 | 13,249 | $169,548 | 40.4% | 1.325 | 541 | +$8,832 | -$16.32 | 3.9% | -$8,832 |
| X0.7_Y4 | 13,099 | $168,924 | 40.4% | 1.327 | 691 | +$9,454 | -$13.68 | 5.0% | -$9,454 |
| X0.7_Y8 | 12,515 | $165,735 | 40.6% | 1.334 | 1,275 | +$12,644 | -$9.92 | 9.2% | -$12,644 |
| X0.7_Y12 | 11,982 | $160,607 | 40.6% | 1.334 | 1,808 | +$17,772 | -$9.83 | 13.1% | -$17,772 |

`$/blk` = average P&L of blocked trades (i.e. how much the veto **lost** us per
trade vetoed). A NEGATIVE value means we vetoed profitable trades.

**Every single cell produces a NEGATIVE `$/blk`.** Every cell sheds $5K-$22K of
real P&L. The "least-bad" cell (X=0.7, Y=2) still gives back $5,598.

PF improvements are also illusory: the best cell improves PF by **+0.007** (1.338
vs 1.331 baseline), a fourth-decimal noise improvement that costs **$12,822**
in absolute P&L.

---

## Why the hypothesis fails

`bias_momentum` is NOT a "buy support, sell resistance" mean-reversion strategy.
It fires on TREND continuation — momentum + bias alignment. The whole point of
the strategy is to **catch the break THROUGH the wall**, not to bounce off it.

Looking at the direction split (least-bad cell, X=0.7, Y=2):

| Side | blocked n | blocked $ | avg/trade |
|------|----------:|----------:|----------:|
| LONG (resistance veto) | 241 | +$3,282 | +$13.62 |
| SHORT (support veto) | 174 | +$2,316 | +$13.31 |

Both directions: vetoing trades that fire INTO a nearby wall sheds positive
expectancy. The "structural failure at the level" intuition is real for
mean-reversion setups (and Section V.3 confirmed bounce strategies do work
mechanically — they just don't have positive R/R on MNQ). For a momentum
strategy entering on confirmed bias + acceleration, those nearby walls are the
**target**, not the **obstacle**.

This is internally consistent with Section V.5's broader conclusion: "Phoenix's
edges live in entry selection (good triggers) and patient exits (fixed RR).
Trying to micro-manage [entry timing] reactive to existing structures hurts."

---

## Per-year breakdown (best cell, X=0.7, Y=2)

| Year | Baseline n / $ | Veto kept n / $ | Blocked n / $ | $ lift |
|------|-------------:|---------------:|--------------:|-------:|
| 2021 | 1,624 / +$15,246 | 1,551 / +$14,904 | 73 / +$342 | **-$342** |
| 2022 | 2,787 / +$41,795 | 2,725 / +$41,019 | 62 / +$776 | -$776 |
| 2023 | 2,524 / +$20,632 | 2,427 / +$18,588 | 97 / +$2,044 | -$2,044 |
| 2024 | 2,701 / +$28,302 | 2,622 / +$28,946 | 79 / **-$644** | **+$644** |
| 2025 | 2,992 / +$49,802 | 2,916 / +$47,650 | 76 / +$2,152 | -$2,152 |
| 2026 | 1,162 / +$22,601 | 1,134 / +$21,676 | 28 / +$926 | -$926 |
| **5y** | **13,790 / +$178,379** | **13,375 / +$172,782** | **415 / +$5,598** | **-$5,598** |

**Only 1 of 6 years** (2024) showed blocked trades net negative — by a marginal
$644 over 79 trades ($-8.15/trade). Every other year, blocked trades were
positive. The single positive year is fully within Wilson-CI noise on 79
trades.

For the heavy-handed cell (X=0.5, Y=12, 15% fire rate), the per-year picture
is even more lopsided — every year shows positive blocked-trade P&L, ranging
from +$1,546 to +$5,232.

---

## Comparison vs baseline

| Variant | n | Total $ | WR | PF | 6/6 years positive? |
|---------|--:|--------:|---:|---:|:-------------------:|
| **bias_momentum (baseline)** | 13,790 | **+$178,379** | 40.5% | **1.331** | ✅ 6/6 |
| Best VETO (X=0.7, Y=2) | 13,375 (-3.0%) | $172,782 (-$5,598) | 40.4% | 1.328 (-0.003) | ✅ 6/6 (kept) |
| Heaviest VETO (X=0.5, Y=12) | 11,699 (-15.2%) | $156,843 (-$21,536) | 40.6% | 1.333 (+0.002) | ✅ 6/6 (kept) |

No cell improves both axes (total $ AND PF) over baseline.
A few cells tickle PF up by +0.002 to +0.007 — purely noise at this scale —
while bleeding $5K-$22K of real money.

---

## Verdict

**SKIP. Do not ship S/R as a VETO filter on `bias_momentum`.**

The hypothesis — that nearby S/R structurally fails momentum continuation
trades — is **wrong** on this dataset. The strategy actually MAKES MONEY on
trades that fire into nearby walls, because that's the trade thesis: breaking
through structure. Vetoing those entries strips $5K-$22K/5y of clean signal
to gain at most +0.007 PF (statistical noise).

This is consistent with Section V's broader theme: reactive filters on
already-validated entry triggers tend to subtract value, not add it. The
S/R detection engine remains useful for OTHER roles (e.g. dynamic target
extension, confluence sizing on mean-rev strategies like `spring_setup`)
but **not** as a binary entry veto on momentum strategies.

The Phase 13 V.4 list of "deferred uses for `core/sr_zones.py`" should be
updated:

- ~~S/R as VETO filter for bias_momentum~~ → **NEGATIVE, killed 2026-05-19**
- S/R as CONFLUENCE boost for spring_setup → still untested
- Failed-hold continuation strategy → still untested

---

## Production code

**Not shipping.** No `core/entry_filters.py` written. The analyzer
(`tools/phoenix_sr_veto_analyzer.py`) is committed for re-runnability so future
strategy variants can be tested with the same harness, but no production wiring
is appropriate given the negative outcome.

If a future strategy DOES benefit from an S/R veto (e.g. a tested mean-rev
variant where the "wall in front" thesis actually holds), the wiring pattern
would look like:

```python
# Hypothetical pattern — NOT BEING SHIPPED FOR bias_momentum
from core.sr_zones import detect_sr_zones, nearest_zone

def sr_veto_should_block(direction, entry_price, bars_5m,
                          min_strength=0.7, max_prox_ticks=2,
                          prior_day_high=None, prior_day_low=None,
                          prior_day_poc=None, vwap=None, vwap_std=None):
    zones = detect_sr_zones(
        bars_5m=bars_5m, current_price=entry_price,
        prior_day_high=prior_day_high, prior_day_low=prior_day_low,
        prior_day_poc=prior_day_poc, vwap=vwap, vwap_std=vwap_std,
    )
    veto_type = "resistance" if direction == "LONG" else "support"
    z = nearest_zone(zones, entry_price, veto_type,
                     max_distance_ticks=max_prox_ticks)
    return (z is not None and z.strength >= min_strength)
```

---

## Files

- `tools/phoenix_sr_veto_analyzer.py` — the analyzer (re-runnable)
- `backtest_results/phoenix_sr_veto_summary.csv` — full 15-cell grid + per-direction split
- `docs/SR_VETO_BIAS_MOMENTUM.md` — this report

## Reproduction

```bash
cd "C:\Trading Project\phoenix_bot"
python tools/phoenix_sr_veto_analyzer.py
# add --quick to limit to first 500 trades (~1s smoke test)
```

Runtime: ~18-20s for the full 13,790 trades.
