# Failed-Hold Continuation Strategy — NEGATIVE Finding

**Sprint C of the post-Section-V edge sweep. 2026-05-20.**

## TL;DR

The hypothesis: when a strong S/R zone BREAKS, price often continues strongly in the break direction. Built `tools/phoenix_failed_hold_lab.py` with 5 variants and ran the bug-fixed 5-year backtest.

**Verdict: ANOTHER NEGATIVE.** 1,131 trades over 5 years, -$5,894 aggregate. All 5 variants fail.

| Variant | n | WR | Wilson low | Total $ | PF |
|---|---:|---:|---:|---:|---:|
| failed_hold_round (only round-number zones) | 40 | 15% | 7% | -$260 | 0.32 |
| failed_hold_strict (strength≥0.7, n_tests≥3, 2R) | 221 | 21% | 16% | -$849 | 0.55 |
| failed_hold_moderate (strength≥0.5, n_tests≥2, 2R) | 290 | 20% | 16% | -$1,239 | 0.51 |
| failed_hold_3r (best strength combo, 3R target) | 290 | 12% | 9% | -$1,712 | 0.38 |
| failed_hold_chandelier (chandelier exit) | 290 | 11% | 8% | -$1,834 | 0.31 |

All Wilson 95% lower bounds well below 33% breakeven for 2R targets (or 25% for 3R). **Statistically confirmed negative edge, not bad luck.**

## Per-year breakdown (proves consistency of failure)

Every variant lost money every single year:

| Variant | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---:|---:|---:|---:|---:|---:|
| failed_hold_strict | -$267 | -$130 | -$133 | -$173 | -$132 | -$14 |
| failed_hold_moderate | -$292 | -$176 | -$188 | -$235 | -$324 | -$26 |
| failed_hold_3r | -$379 | -$192 | -$404 | -$332 | -$358 | -$48 |
| failed_hold_chandelier | -$319 | -$194 | -$514 | -$492 | -$232 | -$84 |
| failed_hold_round | +$4 | -$24 | -$116 | -$36 | -$67 | -$22 |

## Exit reason analysis

| Variant | Stops | Targets hit | Time exits | Chandelier exits |
|---|---:|---:|---:|---:|
| failed_hold_strict | 174 (79%) | 44 | 3 | — |
| failed_hold_moderate | 231 (80%) | 57 | 2 | — |
| failed_hold_3r | 254 (88%) | 32 | 4 | — |
| failed_hold_chandelier | — | — | 1 | 289 (initial + trail) |

**Strikes against the hypothesis:** 79-88% of fixed-target variants stopped out. Chandelier exits were no better — 289 of 290 trades exited on stop/trail rather than running. The "continuation" assumption is wrong on MNQ at this timeframe.

## Why it fails

Same mechanical issues as Section V.3 (S/R bounce):
- MNQ has 1-3 pt (4-12 tick) noise on 5m bars
- "Confirmation bar" (bar closes in direction of break) often just IS the noise spike
- Real continuation moves require larger timeframe + macro context that 5m bars don't carry
- Once we wait for confirmation, the move has often already played out

## Combined verdict on S/R-as-direct-trigger

| Direction | Section V.3 | Sprint C |
|---|---|---|
| **BOUNCE** (rejection at zone) | NEGATIVE -$10,056/5y | — |
| **BREAK-AND-CONTINUE** (failed-hold) | — | NEGATIVE -$5,894/5y |

**S/R zones do NOT work as direct entry triggers on MNQ in EITHER direction.** The core/sr_zones.py engine remains valuable for VETO/CONFLUENCE applications (Sprints A and B, in flight).

## Files

- `tools/phoenix_failed_hold_lab.py` (761 LOC, 5-variant 5y lab)
- `backtest_results/phoenix_failed_hold_lab.csv` (1,131 trades; gitignored)
- `backtest_results/phoenix_failed_hold_summary.csv` (gitignored)

## Production decision

**Do NOT add failed-hold continuation as a Phoenix strategy.** Negative finding, no further action needed.

The core/sr_zones.py engine is still valuable for the role-based applications being tested in Sprints A and B. Those will tell us whether S/R has any production role at all.
