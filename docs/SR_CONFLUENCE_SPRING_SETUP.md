# S/R Confluence Analysis — spring_setup

**Date:** 2026-05-19
**Source:** `tools/phoenix_sr_confluence_analyzer.py`
**Strategy:** `spring_setup` (20,778 clean trades, 5 years)
**S/R engine:** `core/sr_zones.py` (swing + round + PDH/PDL + clustering)

## Hypothesis

> A spring wick at a strong S/R zone reflects real absorption/stop-runs
> at a known level — high probability. A spring in noise is just a wick.
> Therefore springs at strong S/R should outperform springs with no S/R.

## Method

For each spring_setup trade, compute S/R zones from the 300 most recent
5m bars STRICTLY BEFORE entry_ts. Find nearest zone of the matching
direction (SUPPORT for LONG, RESISTANCE for SHORT) within 4 ticks of
entry_price. Bucket by zone strength:

| bucket | criterion |
|---|---|
| `no_sr` | no qualifying zone within 4t |
| `weak_sr` | zone within 4t, strength < 0.50 |
| `strong_sr` | zone within 4t, 0.50 <= strength < 0.70 |
| `very_strong_sr` | zone within 4t, strength >= 0.70 |

## Edge check #1: how often do springs land at a zone?

7.1% of spring_setup trades occurred AT a
detected S/R zone (within 4 ticks). If this number is near 100% or near
0% the signal isn't actionable — must be in the actionable middle.

## Per-bucket statistics

| bucket | n | WR | total $ | avg $ | PF |
|---|---:|---:|---:|---:|---:|
| `no_sr` | 19,303 | 41.4% | $17,968 | $0.93 | 1.03 |
| `weak_sr` | 35 | 45.7% | $174 | $4.99 | 1.16 |
| `strong_sr` | 162 | 43.2% | $1,000 | $6.18 | 1.26 |
| `very_strong_sr` | 1,278 | 40.0% | $-600 | $-0.47 | 0.98 |
| `ALL` | 20,778 | 41.3% | $18,544 | $0.89 | 1.03 |

## Per-zone-source breakdown (only trades AT a zone)

| source | n | WR | total $ | avg $ | PF |
|---|---:|---:|---:|---:|---:|
| `source:pdh` | 22 | 50.0% | $280 | $12.70 | 1.81 |
| `source:pdl` | 19 | 36.8% | $126 | $6.61 | 1.29 |
| `source:round` | 164 | 40.2% | $326 | $1.99 | 1.07 |
| `source:swing` | 1,270 | 40.4% | $-156 | $-0.12 | 1.00 |

## Per-year stability (bucket x year, total $)

| year | no_sr | weak_sr | strong_sr | very_strong_sr |
|---:|:-:|:-:|:-:|:-:|
| 2021 | n=1960 $3,486 | n=4 $-66 | n=26 $164 | n=182 $-320 |
| 2022 | n=4271 $2,833 | n=5 $152 | n=31 $1,061 | n=223 $705 |
| 2023 | n=3183 $-2,454 | n=9 $194 | n=31 $-350 | n=262 $-172 |
| 2024 | n=3629 $8,476 | n=5 $117 | n=35 $-125 | n=273 $-1,769 |
| 2025 | n=4384 $2,730 | n=8 $-162 | n=29 $-148 | n=258 $73 |
| 2026 | n=1876 $2,897 | n=4 $-60 | n=10 $398 | n=80 $884 |

## Size-boost simulation (1.3x on `strong_sr` + `very_strong_sr`)

- Baseline 5y P&L: **$18,544**
- Boosted 5y P&L: **$18,664**
- Net lift: **$120**
- Trades boosted: 1,440 / 20,778 (6.9%)

### Boost lift by year

| year | n_total | n_boost | baseline $ | boosted $ | lift $ |
|---:|---:|---:|---:|---:|---:|
| 2021 | 2,172 | 208 | $3,264 | $3,217 | $-47 |
| 2022 | 4,530 | 254 | $4,750 | $5,280 | $530 |
| 2023 | 3,485 | 293 | $-2,781 | $-2,938 | $-157 |
| 2024 | 3,942 | 308 | $6,700 | $6,131 | $-568 |
| 2025 | 4,679 | 287 | $2,492 | $2,470 | $-22 |
| 2026 | 1,970 | 90 | $4,119 | $4,503 | $384 |

## Conservative boost variant — `strong_sr` ONLY (skip `very_strong_sr`)

Driven by the observation that `very_strong_sr` is the disaster bucket
(price already at a multi-tested level — next test more likely to break).
This variant boosts ONLY the cleaner `strong_sr` bucket.

- Baseline 5y P&L: **$18,544**
- Boosted 5y P&L: **$18,844**
- Net lift: **$300**
- Trades boosted: 162 / 20,778 (0.8%)

| year | n_boost | baseline $ | boosted $ | lift $ |
|---:|---:|---:|---:|---:|
| 2021 | 26 | $3,264 | $3,313 | $49 |
| 2022 | 31 | $4,750 | $5,069 | $318 |
| 2023 | 31 | $-2,781 | $-2,886 | $-105 |
| 2024 | 35 | $6,700 | $6,662 | $-38 |
| 2025 | 29 | $2,492 | $2,448 | $-44 |
| 2026 | 10 | $4,119 | $4,238 | $119 |

## Edge check #2: direction-vs-bucket interaction

Hypothesis sub-test: does a LONG into known support behave the same as
a SHORT into known resistance?

| direction x bucket | n | WR | total $ | avg $ | PF |
|---|---:|---:|---:|---:|---:|
| `LONG_no_sr` | 6,117 | 42.7% | $11,730 | $1.92 | 1.08 |
| `LONG_weak_sr` | 12 | 50.0% | $98 | $8.17 | 1.29 |
| `LONG_strong_sr` | 79 | 44.3% | $420 | $5.31 | 1.27 |
| `LONG_very_strong_sr` | 552 | 39.9% | $-680 | $-1.23 | 0.94 |
| `SHORT_no_sr` | 13,186 | 40.8% | $6,238 | $0.47 | 1.01 |
| `SHORT_weak_sr` | 23 | 43.5% | $76 | $3.33 | 1.10 |
| `SHORT_strong_sr` | 83 | 42.2% | $581 | $7.00 | 1.25 |
| `SHORT_very_strong_sr` | 726 | 40.1% | $80 | $0.11 | 1.00 |

## Verdict

- `no_sr` avg/trade: $0.93  (WR 41.4%)
- combined strong/very_strong avg/trade: $0.28  (WR 40.3%)
- delta avg/trade: $-0.65  (-70% vs no_sr)

- Years with positive boost lift (default boost): 2/6

- Years with positive lift (strong_sr-only variant): 3/6

**VERDICT: Weak partial support.** `strong_sr`-only variant is
positive ($300) but sample size is small 
(only a few hundred trades over 5y). Consider as future research,
not production wiring.

## Production wiring (draft)

Draft `SpringSrSizeBoostFilter` lives in `core/entry_filters_size.py`
(separate file from Spawn A's `entry_filters_sr.py` veto for
`bias_momentum`).

CRITICAL: the filter must use a NARROW strength band (0.50 <= s < 0.70).
Boosting >= 0.70 (`very_strong_sr`) is an actual anti-edge — that band
must be EXCLUDED, not included.

```python
from core.entry_filters_size import SpringSrSizeBoostFilter

# inside base_bot._evaluate_strategies, after a spring_setup signal:
if signal and signal.strategy == 'spring_setup':
    boost_filter = SpringSrSizeBoostFilter()
    multiplier = boost_filter.size_multiplier(
        signal, bars_5m=bars_5m, market=market,
    )
    # multiplier == 1.30 ONLY when nearest zone is in [0.50, 0.70).
    # Returns 1.00 for noise AND for very_strong_sr (skip the anti-edge).
    signal.size_multiplier = multiplier
```

**Do NOT ship until live-shadow paper-tracked for 30+ trades** —
the `strong_sr` bucket has only 162 historical trades, so a Wilson 95%
CI on its win rate is wide. Validation tier: PRELIMINARY.
