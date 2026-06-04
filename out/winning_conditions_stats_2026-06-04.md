# Winning Conditions Sprint — Stats Report
*Phases 2 / 3 / 4 deliverable | 2026-06-04*

Analysis is restricted to the **DERIVATION SET**
(2026-03-06 → 2026-05-05, oldest 60 days of the 90-day rich-data window).
The HOLDOUT SET (2026-05-05 → 2026-06-04) is reserved untouched for the Phase 13.6 in-window WFA.

Sample sizes per strategy in DERIVATION:

- `bias_momentum`: 220

- `opening_session`: 2


---
## Phase 2 — Win conditions

### `bias_momentum` — DERIVATION winners
- Total DERIVATION trades: 220 (WIN=54, LOSS=166)

#### Winner numeric distributions

| feature | n | mean | median | p25 | p75 | min | max | skew |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `atr_1m` | 54 | 9.429 | 7.500 | 4.938 | 10.177 | 3.390 | 29.620 | 1.85 |
| `atr_5m` | 54 | 21.145 | 18.990 | 11.990 | 25.020 | 8.180 | 47.880 | 0.87 |
| `atr_15m` | 54 | 36.303 | 38.170 | 19.188 | 46.520 | 16.360 | 63.730 | -0.05 |
| `atr_60m` | 54 | 69.895 | 63.590 | 36.017 | 84.200 | 0.000 | 132.580 | 0.01 |
| `cvd` | 54 | -63815129.296 | -80402844.000 | -93139550.000 | 14815888.750 | -214149260.000 | 53398020.000 | -0.43 |
| `bar_delta` | 54 | 208857.648 | 6953.000 | -453412.500 | 288990.250 | -10961989.000 | 11224282.000 | 0.43 |
| `bar_buy_vol` | 54 | 22439595.407 | 1865653.000 | 437063.000 | 26183632.000 | 0.000 | 223699606.000 | 3.24 |
| `bar_sell_vol` | 54 | 22230737.796 | 1475569.500 | 415240.250 | 27578822.500 | 0.000 | 230922420.000 | 3.28 |
| `vol_climax_ratio` | 7 | 0.713 | 0.700 | 0.390 | 0.995 | 0.140 | 1.380 | 0.23 |
| `tf_votes_bullish` | 54 | 2.315 | 2.500 | 2.000 | 3.000 | 0.000 | 4.000 | -0.87 |
| `tf_votes_bearish` | 54 | 0.463 | 0.000 | 0.000 | 1.000 | 0.000 | 3.000 | 1.90 |
| `cr_mom_score` | 0 | — | — | — | — | — | — | — |
| `cr_confidence` | 0 | — | — | — | — | — | — | — |
| `es_nq_rs` | 0 | — | — | — | — | — | — | — |
| `dom_imbalance` | 54 | 0.515 | 0.583 | 0.216 | 0.804 | 0.000 | 1.000 | -0.22 |
| `vwap_std` | 18 | 18.535 | 0.000 | 0.000 | 37.008 | 0.000 | 74.240 | 0.95 |
| `distance_from_ema9_ticks` | 54 | 80.300 | 51.140 | 34.390 | 163.990 | -173.480 | 333.000 | -0.15 |
| `distance_from_vwap_ticks` | 54 | 447.364 | 452.760 | 175.090 | 699.530 | -238.880 | 1108.760 | -0.04 |
| `tick_count_5m_before` | 54 | 4637.370 | 2714.000 | 1089.500 | 5634.250 | 443.000 | 17746.000 | 1.43 |
| `tick_count_5m_after` | 54 | 4825.722 | 2934.000 | 1158.000 | 6072.000 | 413.000 | 17613.000 | 1.25 |
| `bid_volume_5m_before` | 54 | 4888.704 | 2888.500 | 1242.250 | 6381.250 | 456.000 | 16555.000 | 1.17 |
| `ask_volume_5m_before` | 54 | 4606.000 | 2720.500 | 1080.000 | 6552.750 | 426.000 | 16810.000 | 1.33 |
| `delta_aligned_ratio_5m` | 54 | 0.471 | 0.465 | 0.434 | 0.509 | 0.388 | 0.592 | 0.39 |
| `spread_avg_5m` | 54 | 0.469 | 0.431 | 0.328 | 0.535 | 0.305 | 1.423 | 2.85 |
| `spread_max_5m` | 54 | 2.213 | 1.250 | 1.000 | 2.750 | 0.500 | 13.500 | 3.08 |
| `price_range_5m_ticks` | 54 | 89.944 | 72.000 | 52.250 | 104.000 | 19.000 | 256.000 | 1.41 |
| `price_position_in_5m_bar` | 54 | 0.600 | 0.607 | 0.389 | 0.851 | 0.047 | 1.018 | -0.31 |
| `distance_from_5m_high_ticks` | 54 | 36.259 | 21.500 | 14.000 | 34.000 | -1.000 | 205.000 | 2.61 |
| `distance_from_5m_low_ticks` | 54 | 53.685 | 50.000 | 22.250 | 75.000 | 5.000 | 219.000 | 1.49 |
| `cvd_slope_5m_per_min` | 54 | -56.541 | -62.700 | -118.200 | 18.750 | -553.400 | 712.800 | 0.65 |
| `cvd_slope_1m` | 54 | -43.741 | -7.500 | -344.500 | 112.000 | -1588.000 | 2136.000 | 0.62 |
| `cvd_acceleration` | 54 | 12.800 | 19.500 | -155.950 | 167.150 | -1319.200 | 1721.400 | 0.54 |
| `adverse_pre_move_ticks` | 54 | -20.815 | -15.000 | -36.000 | 1.250 | -173.000 | 61.000 | -1.23 |
| `hold_time_s` | 54 | 100.319 | 34.700 | 5.150 | 141.350 | 0.000 | 922.600 | 3.35 |
| `contracts` | 54 | 1.037 | 1.000 | 1.000 | 1.000 | 1.000 | 2.000 | 5.04 |


#### Top regimes / day_types / session_phases for winners

**regime** (n=54):

| value | count | % |
|---|---:|---:|
| AFTERHOURS | 24 | 44.4% |
| LATE_AFTERNOON | 12 | 22.2% |
| MID_MORNING | 8 | 14.8% |
| OPEN_MOMENTUM | 5 | 9.3% |
| AFTERNOON_CHOP | 2 | 3.7% |
| OVERNIGHT_RANGE | 2 | 3.7% |
| PREMARKET_DRIFT | 1 | 1.9% |

**day_type** (n=54):

| value | count | % |
|---|---:|---:|
| <NULL> | 54 | 100.0% |

**session_phase_ct** (n=54):

| value | count | % |
|---|---:|---:|
| OVERNIGHT | 26 | 48.1% |
| AFTERNOON | 10 | 18.5% |
| OPEN | 8 | 14.8% |
| MORNING | 7 | 13.0% |
| CLOSE | 2 | 3.7% |
| LUNCH | 1 | 1.9% |

**cr_verdict** (n=54):

| value | count | % |
|---|---:|---:|
| <NULL> | 54 | 100.0% |

**mq_direction_bias** (n=54):

| value | count | % |
|---|---:|---:|
| <NULL> | 54 | 100.0% |

#### Ideal `bias_momentum` winner profile

The median DERIVATION winner of `bias_momentum` entered at ATR(5m) = 18.99 points, at +452.8 ticks vs VWAP and +51.1 ticks vs EMA9. 5-min aggressor-aligned ratio was 0.465 (values above 0.5 = aggressor pressure aligned with trade direction). Entry sat at position 0.61 of the prior 5-min bar (0 = bar low, 1 = bar high). cr_mom_score median = nan (proxy for the deprecated momentum_score). The most common regime for winners was `AFTERHOURS` (44.4%).


#### Top 5 `bias_momentum` winners (DERIVATION, ranked by R-multiple)

| rank | trade_id | entry_iso | dir | entry | exit | stop | pnl_$net | R | regime | day_type |
|---:|---|---|---|---:|---:|---:|---:|---:|---|---|
| 1 | `4271b190` | 2026-04-15T22:02:01 | LONG | 26349.75 | 26369.0 | 26346.25 | $+36.78 | 5.25 | AFTERHOURS | nan |
| 2 | `b85286b1` | 2026-04-16T00:48:01 | LONG | 26426.75 | 26441.5 | 26423.25 | $+27.78 | 3.97 | AFTERHOURS | nan |
| 3 | `ba967bc5` | 2026-05-04T13:00:41 | LONG | 27814.75 | 27864.5 | 27802.25 | $+94.68 | 3.79 | PREMARKET_DRIFT | nan |
| 4 | `b4db1d47` | 2026-04-16T01:42:07 | LONG | 26449.5 | 26463.25 | 26446.0 | $+25.78 | 3.68 | AFTERHOURS | nan |
| 5 | `e7c7b03d` | 2026-04-15T22:25:48 | LONG | 26369.0 | 26381.5 | 26365.5 | $+23.28 | 3.33 | AFTERHOURS | nan |

### `opening_session` — DERIVATION winners
- Total DERIVATION trades: 2 (WIN=1, LOSS=1)

> **INSUFFICIENT_SAMPLE** — fewer than 5 winners in DERIVATION; distribution stats are descriptive only and statistical tests are declined.

#### Winner numeric distributions

| feature | n | mean | median | p25 | p75 | min | max | skew |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `atr_1m` | 1 | 19.290 | 19.290 | 19.290 | 19.290 | 19.290 | 19.290 | nan |
| `atr_5m` | 1 | 28.590 | 28.590 | 28.590 | 28.590 | 28.590 | 28.590 | nan |
| `atr_15m` | 1 | 50.710 | 50.710 | 50.710 | 50.710 | 50.710 | 50.710 | nan |
| `atr_60m` | 1 | 117.750 | 117.750 | 117.750 | 117.750 | 117.750 | 117.750 | nan |
| `cvd` | 1 | -73592669.000 | -73592669.000 | -73592669.000 | -73592669.000 | -73592669.000 | -73592669.000 | nan |
| `bar_delta` | 1 | -466567.000 | -466567.000 | -466567.000 | -466567.000 | -466567.000 | -466567.000 | nan |
| `bar_buy_vol` | 1 | 28178290.000 | 28178290.000 | 28178290.000 | 28178290.000 | 28178290.000 | 28178290.000 | nan |
| `bar_sell_vol` | 1 | 28644857.000 | 28644857.000 | 28644857.000 | 28644857.000 | 28644857.000 | 28644857.000 | nan |
| `vol_climax_ratio` | 1 | 1.130 | 1.130 | 1.130 | 1.130 | 1.130 | 1.130 | nan |
| `tf_votes_bullish` | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | nan |
| `tf_votes_bearish` | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | nan |
| `cr_mom_score` | 0 | — | — | — | — | — | — | — |
| `cr_confidence` | 0 | — | — | — | — | — | — | — |
| `es_nq_rs` | 0 | — | — | — | — | — | — | — |
| `dom_imbalance` | 1 | 0.771 | 0.771 | 0.771 | 0.771 | 0.771 | 0.771 | nan |
| `vwap_std` | 1 | 38.120 | 38.120 | 38.120 | 38.120 | 38.120 | 38.120 | nan |
| `distance_from_ema9_ticks` | 1 | -132.520 | -132.520 | -132.520 | -132.520 | -132.520 | -132.520 | nan |
| `distance_from_vwap_ticks` | 1 | -137.200 | -137.200 | -137.200 | -137.200 | -137.200 | -137.200 | nan |
| `tick_count_5m_before` | 1 | 6835.000 | 6835.000 | 6835.000 | 6835.000 | 6835.000 | 6835.000 | nan |
| `tick_count_5m_after` | 1 | 7177.000 | 7177.000 | 7177.000 | 7177.000 | 7177.000 | 7177.000 | nan |
| `bid_volume_5m_before` | 1 | 6094.000 | 6094.000 | 6094.000 | 6094.000 | 6094.000 | 6094.000 | nan |
| `ask_volume_5m_before` | 1 | 6946.000 | 6946.000 | 6946.000 | 6946.000 | 6946.000 | 6946.000 | nan |
| `delta_aligned_ratio_5m` | 1 | 0.533 | 0.533 | 0.533 | 0.533 | 0.533 | 0.533 | nan |
| `spread_avg_5m` | 1 | 0.573 | 0.573 | 0.573 | 0.573 | 0.573 | 0.573 | nan |
| `spread_max_5m` | 1 | 2.000 | 2.000 | 2.000 | 2.000 | 2.000 | 2.000 | nan |
| `price_range_5m_ticks` | 1 | 191.000 | 191.000 | 191.000 | 191.000 | 191.000 | 191.000 | nan |
| `price_position_in_5m_bar` | 1 | 0.068 | 0.068 | 0.068 | 0.068 | 0.068 | 0.068 | nan |
| `distance_from_5m_high_ticks` | 1 | 178.000 | 178.000 | 178.000 | 178.000 | 178.000 | 178.000 | nan |
| `distance_from_5m_low_ticks` | 1 | 13.000 | 13.000 | 13.000 | 13.000 | 13.000 | 13.000 | nan |
| `cvd_slope_5m_per_min` | 1 | 170.400 | 170.400 | 170.400 | 170.400 | 170.400 | 170.400 | nan |
| `cvd_slope_1m` | 1 | 1203.000 | 1203.000 | 1203.000 | 1203.000 | 1203.000 | 1203.000 | nan |
| `cvd_acceleration` | 1 | 1032.600 | 1032.600 | 1032.600 | 1032.600 | 1032.600 | 1032.600 | nan |
| `adverse_pre_move_ticks` | 1 | 7.000 | 7.000 | 7.000 | 7.000 | 7.000 | 7.000 | nan |
| `hold_time_s` | 1 | 406.200 | 406.200 | 406.200 | 406.200 | 406.200 | 406.200 | nan |
| `contracts` | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | nan |


#### Top regimes / day_types / session_phases for winners

**regime** (n=1):

| value | count | % |
|---|---:|---:|
| LATE_AFTERNOON | 1 | 100.0% |

**day_type** (n=1):

| value | count | % |
|---|---:|---:|
| <NULL> | 1 | 100.0% |

**session_phase_ct** (n=1):

| value | count | % |
|---|---:|---:|
| AFTERNOON | 1 | 100.0% |

**cr_verdict** (n=1):

| value | count | % |
|---|---:|---:|
| <NULL> | 1 | 100.0% |

**mq_direction_bias** (n=1):

| value | count | % |
|---|---:|---:|
| <NULL> | 1 | 100.0% |

#### Ideal `opening_session` winner profile

The median DERIVATION winner of `opening_session` entered at ATR(5m) = 28.59 points, at -137.2 ticks vs VWAP and -132.5 ticks vs EMA9. 5-min aggressor-aligned ratio was 0.533 (values above 0.5 = aggressor pressure aligned with trade direction). Entry sat at position 0.07 of the prior 5-min bar (0 = bar low, 1 = bar high). cr_mom_score median = nan (proxy for the deprecated momentum_score). The most common regime for winners was `LATE_AFTERNOON` (100.0%).


#### Top 1 `opening_session` winners (DERIVATION, ranked by R-multiple)

| rank | trade_id | entry_iso | dir | entry | exit | stop | pnl_$net | R | regime | day_type |
|---:|---|---|---|---:|---:|---:|---:|---:|---|---|
| 1 | `c5aa56b4` | 2026-04-29T18:08:48 | LONG | 27183.25 | 27204.0 | 27157.75 | $+39.78 | 0.78 | LATE_AFTERNOON | nan |


---
## Phase 3 — Loss conditions + discriminators

### `bias_momentum` — DERIVATION losers
- Total DERIVATION trades: 220 (WIN=54, LOSS=166)

#### Loser numeric distributions

| feature | n | mean | median | p25 | p75 | min | max | skew |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `atr_1m` | 166 | 9.871 | 7.785 | 4.843 | 13.453 | 3.210 | 39.500 | 1.49 |
| `atr_5m` | 166 | 22.639 | 19.615 | 13.543 | 27.210 | 7.770 | 76.290 | 1.51 |
| `atr_15m` | 166 | 37.824 | 36.770 | 26.710 | 47.998 | 16.090 | 101.300 | 1.09 |
| `atr_60m` | 166 | 70.969 | 58.750 | 51.255 | 103.310 | 0.000 | 152.030 | 0.31 |
| `cvd` | 166 | -594887005.476 | -80003133.500 | -89954911.250 | 12306356.000 | -29891366711.000 | 67611467.000 | -7.30 |
| `bar_delta` | 166 | -422166.349 | 0.000 | -44773.250 | 191088.500 | -184402205.000 | 43515401.000 | -10.04 |
| `bar_buy_vol` | 166 | 34398909.825 | 2048457.000 | 56735.250 | 21581617.250 | 0.000 | 1454082493.000 | 8.69 |
| `bar_sell_vol` | 166 | 34821076.211 | 1838881.000 | 47658.000 | 21242759.750 | 0.000 | 1638484698.000 | 9.57 |
| `vol_climax_ratio` | 78 | 1.157 | 0.525 | 0.292 | 1.140 | 0.000 | 15.610 | 5.07 |
| `tf_votes_bullish` | 166 | 1.681 | 2.000 | 1.000 | 2.750 | 0.000 | 4.000 | -0.07 |
| `tf_votes_bearish` | 166 | 0.512 | 0.000 | 0.000 | 1.000 | 0.000 | 4.000 | 1.77 |
| `cr_mom_score` | 0 | — | — | — | — | — | — | — |
| `cr_confidence` | 0 | — | — | — | — | — | — | — |
| `es_nq_rs` | 0 | — | — | — | — | — | — | — |
| `dom_imbalance` | 166 | 0.532 | 0.515 | 0.235 | 0.859 | 0.000 | 1.000 | -0.07 |
| `vwap_std` | 102 | 22.072 | 27.315 | 2.335 | 31.760 | 0.000 | 60.200 | 0.03 |
| `distance_from_ema9_ticks` | 166 | 61.164 | 48.880 | 2.300 | 126.190 | -532.840 | 596.440 | 0.03 |
| `distance_from_vwap_ticks` | 166 | 407.071 | 413.260 | 122.450 | 693.030 | -424.920 | 1112.280 | 0.20 |
| `tick_count_5m_before` | 166 | 5064.006 | 3090.000 | 935.750 | 6819.250 | 253.000 | 26338.000 | 1.77 |
| `tick_count_5m_after` | 166 | 4985.337 | 3114.000 | 915.250 | 7239.000 | 273.000 | 26010.000 | 1.34 |
| `bid_volume_5m_before` | 166 | 5169.681 | 3171.500 | 1053.000 | 7482.500 | 177.000 | 27692.000 | 1.78 |
| `ask_volume_5m_before` | 166 | 5055.331 | 2921.000 | 986.500 | 7153.500 | 209.000 | 26438.000 | 1.70 |
| `delta_aligned_ratio_5m` | 166 | 0.481 | 0.477 | 0.450 | 0.514 | 0.325 | 0.625 | -0.01 |
| `spread_avg_5m` | 166 | 0.458 | 0.432 | 0.341 | 0.484 | 0.304 | 2.278 | 5.17 |
| `spread_max_5m` | 166 | 2.170 | 1.500 | 1.000 | 2.750 | 0.500 | 16.000 | 3.52 |
| `price_range_5m_ticks` | 166 | 91.994 | 77.500 | 48.000 | 116.750 | 17.000 | 411.000 | 2.35 |
| `price_position_in_5m_bar` | 166 | 0.584 | 0.653 | 0.329 | 0.808 | -0.011 | 1.046 | -0.38 |
| `distance_from_5m_high_ticks` | 166 | 37.934 | 24.000 | 11.000 | 46.000 | -5.000 | 346.000 | 3.20 |
| `distance_from_5m_low_ticks` | 166 | 54.060 | 42.000 | 19.000 | 79.750 | -1.000 | 406.000 | 2.85 |
| `cvd_slope_5m_per_min` | 166 | -22.870 | -24.800 | -113.550 | 39.600 | -948.600 | 819.000 | 0.03 |
| `cvd_slope_1m` | 166 | -67.416 | -13.500 | -192.250 | 117.750 | -3896.000 | 1504.000 | -3.01 |
| `cvd_acceleration` | 166 | -44.546 | 10.100 | -137.500 | 126.400 | -2947.400 | 1239.200 | -2.26 |
| `adverse_pre_move_ticks` | 163 | -11.331 | -7.000 | -29.000 | 11.000 | -221.000 | 100.000 | -1.18 |
| `hold_time_s` | 166 | 57.386 | 16.850 | 6.575 | 37.075 | 0.000 | 709.800 | 3.33 |
| `contracts` | 166 | 1.018 | 1.000 | 1.000 | 1.000 | 1.000 | 2.000 | 7.30 |


#### Top regimes / day_types / session_phases for losers

**regime** (n=166):

| value | count | % |
|---|---:|---:|
| AFTERHOURS | 54 | 32.5% |
| LATE_AFTERNOON | 28 | 16.9% |
| MID_MORNING | 21 | 12.7% |
| OPEN_MOMENTUM | 20 | 12.0% |
| OVERNIGHT_RANGE | 17 | 10.2% |
| AFTERNOON_CHOP | 12 | 7.2% |
| CLOSE_CHOP | 10 | 6.0% |
| PREMARKET_DRIFT | 4 | 2.4% |

**day_type** (n=166):

| value | count | % |
|---|---:|---:|
| <NULL> | 166 | 100.0% |

**session_phase_ct** (n=166):

| value | count | % |
|---|---:|---:|
| OVERNIGHT | 80 | 48.2% |
| OPEN | 32 | 19.3% |
| AFTERNOON | 17 | 10.2% |
| MORNING | 17 | 10.2% |
| CLOSE | 11 | 6.6% |
| LUNCH | 6 | 3.6% |
| PREMARKET | 3 | 1.8% |

**cr_verdict** (n=166):

| value | count | % |
|---|---:|---:|
| <NULL> | 166 | 100.0% |


#### Top 5 `bias_momentum` losers (DERIVATION, ranked by |R-multiple|)

| rank | trade_id | entry_iso | dir | entry | exit | stop | pnl_$net | R | regime | day_type |
|---:|---|---|---|---:|---:|---:|---:|---:|---|---|
| 1 | `d9011188` | 2026-04-29T14:46:49 | LONG | 27305.25 | 27294.75 | 27305.12 | $-22.72 | -87.38 | MID_MORNING | nan |
| 2 | `e1dd7516` | 2026-04-28T02:58:09 | SHORT | 27387.25 | 27397.0 | 27387.38 | $-21.22 | -81.62 | AFTERHOURS | nan |
| 3 | `95bcef11` | 2026-04-27T01:21:41 | LONG | 27478.5 | 27473.5 | 27478.38 | $-11.72 | -48.83 | AFTERHOURS | nan |
| 4 | `e425c9ec` | 2026-04-29T20:18:33 | SHORT | 27123.25 | 27132.75 | 27123.5 | $-20.72 | -41.44 | CLOSE_CHOP | nan |
| 5 | `418462a7` | 2026-04-27T10:57:51 | LONG | 27481.25 | 27477.25 | 27481.12 | $-9.72 | -37.38 | OVERNIGHT_RANGE | nan |


#### Discriminating features (Mann-Whitney U, winners vs losers)

K = 32 features tested.  Bonferroni-corrected α = 0.05/32 = 0.00156

| feature | n_win | n_loss | median_win | median_loss | Cliff δ | p | survives α/K? |
|---|---:|---:|---:|---:|---:|---:|:-:|
| `tf_votes_bullish` | 54 | 166 | 2.500 | 2.000 | +0.318 | 0.0002688 | **YES** |
| `vwap_std` | 18 | 102 | 0.000 | 27.315 | -0.186 | 0.2053 | no |
| `hold_time_s` | 54 | 166 | 34.700 | 16.850 | +0.171 | 0.05937 | no |
| `adverse_pre_move_ticks` | 54 | 163 | -15.000 | -7.000 | -0.163 | 0.07214 | no |
| `distance_from_ema9_ticks` | 54 | 166 | 51.140 | 48.880 | +0.127 | 0.1607 | no |
| `bar_buy_vol` | 54 | 166 | 1865653.000 | 2048457.000 | +0.123 | 0.1753 | no |
| `bar_sell_vol` | 54 | 166 | 1475569.500 | 1838881.000 | +0.122 | 0.1761 | no |
| `delta_aligned_ratio_5m` | 54 | 166 | 0.465 | 0.477 | -0.122 | 0.177 | no |
| `cvd_slope_5m_per_min` | 54 | 166 | -62.700 | -24.800 | -0.106 | 0.2419 | no |
| `distance_from_vwap_ticks` | 54 | 166 | 452.760 | 413.260 | +0.076 | 0.4013 | no |
| `atr_5m` | 54 | 166 | 18.990 | 19.615 | -0.075 | 0.4117 | no |
| `atr_15m` | 54 | 166 | 38.170 | 36.770 | -0.053 | 0.5579 | no |
| `vol_climax_ratio` | 7 | 78 | 0.700 | 0.525 | +0.051 | 0.8291 | no |
| `cvd_slope_1m` | 54 | 166 | -7.500 | -13.500 | -0.049 | 0.5882 | no |
| `atr_1m` | 54 | 166 | 7.500 | 7.785 | -0.049 | 0.5933 | no |
| `tf_votes_bearish` | 54 | 166 | 0.000 | 0.000 | -0.047 | 0.5366 | no |
| `spread_max_5m` | 54 | 166 | 1.250 | 1.500 | -0.040 | 0.6587 | no |
| `price_position_in_5m_bar` | 54 | 166 | 0.607 | 0.653 | +0.030 | 0.7425 | no |
| `cvd_acceleration` | 54 | 166 | 19.500 | 10.100 | +0.030 | 0.7453 | no |
| `tick_count_5m_after` | 54 | 166 | 2934.000 | 3114.000 | +0.028 | 0.7593 | no |
| `distance_from_5m_high_ticks` | 54 | 166 | 21.500 | 24.000 | -0.026 | 0.7733 | no |
| `dom_imbalance` | 54 | 166 | 0.583 | 0.515 | -0.023 | 0.804 | no |
| `atr_60m` | 54 | 166 | 63.590 | 58.750 | +0.022 | 0.8095 | no |
| `contracts` | 54 | 166 | 1.000 | 1.000 | +0.019 | 0.4204 | no |
| `bid_volume_5m_before` | 54 | 166 | 2888.500 | 3171.500 | +0.018 | 0.8429 | no |
| `cvd` | 54 | 166 | -80402844.000 | -80003133.500 | -0.017 | 0.8487 | no |
| `price_range_5m_ticks` | 54 | 166 | 72.000 | 77.500 | -0.017 | 0.8506 | no |
| `ask_volume_5m_before` | 54 | 166 | 2720.500 | 2921.000 | -0.016 | 0.8603 | no |
| `distance_from_5m_low_ticks` | 54 | 166 | 50.000 | 42.000 | +0.016 | 0.8651 | no |
| `spread_avg_5m` | 54 | 166 | 0.431 | 0.432 | +0.009 | 0.9245 | no |
| `tick_count_5m_before` | 54 | 166 | 2714.000 | 3090.000 | -0.001 | 0.9882 | no |
| `bar_delta` | 54 | 166 | 6953.000 | 0.000 | +0.001 | 0.9931 | no |

##### Footprint feasibility cross-check on `delta_aligned_ratio_5m`

- Winners (n=54) median = 0.465; losers (n=166) median = 0.477

- Cliff's δ = -0.122, p = 0.177. Effect direction at this strategy level: **contrarian** (contrarian = aggressor pressure AGAINST direction predicts a win).
  (Per the prior footprint feasibility finding p ≈ 0.0004 at the full-portfolio level the signal was contrarian. The per-strategy verdict here either confirms or refutes it.)

### `opening_session` — DERIVATION losers
- Total DERIVATION trades: 2 (WIN=1, LOSS=1)

> **INSUFFICIENT_SAMPLE** — Mann-Whitney comparison declined; min(WIN, LOSS) < 5. Descriptive stats below only.

#### Loser numeric distributions

| feature | n | mean | median | p25 | p75 | min | max | skew |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `atr_1m` | 1 | 13.640 | 13.640 | 13.640 | 13.640 | 13.640 | 13.640 | nan |
| `atr_5m` | 1 | 30.520 | 30.520 | 30.520 | 30.520 | 30.520 | 30.520 | nan |
| `atr_15m` | 1 | 56.770 | 56.770 | 56.770 | 56.770 | 56.770 | 56.770 | nan |
| `atr_60m` | 1 | 126.120 | 126.120 | 126.120 | 126.120 | 126.120 | 126.120 | nan |
| `cvd` | 1 | -83756877.000 | -83756877.000 | -83756877.000 | -83756877.000 | -83756877.000 | -83756877.000 | nan |
| `bar_delta` | 1 | 403853.000 | 403853.000 | 403853.000 | 403853.000 | 403853.000 | 403853.000 | nan |
| `bar_buy_vol` | 1 | 19379407.000 | 19379407.000 | 19379407.000 | 19379407.000 | 19379407.000 | 19379407.000 | nan |
| `bar_sell_vol` | 1 | 18975554.000 | 18975554.000 | 18975554.000 | 18975554.000 | 18975554.000 | 18975554.000 | nan |
| `vol_climax_ratio` | 1 | 0.290 | 0.290 | 0.290 | 0.290 | 0.290 | 0.290 | nan |
| `tf_votes_bullish` | 1 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | nan |
| `tf_votes_bearish` | 1 | 2.000 | 2.000 | 2.000 | 2.000 | 2.000 | 2.000 | nan |
| `cr_mom_score` | 0 | — | — | — | — | — | — | — |
| `cr_confidence` | 0 | — | — | — | — | — | — | — |
| `es_nq_rs` | 0 | — | — | — | — | — | — | — |
| `dom_imbalance` | 1 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | nan |
| `vwap_std` | 1 | 38.750 | 38.750 | 38.750 | 38.750 | 38.750 | 38.750 | nan |
| `distance_from_ema9_ticks` | 1 | -245.520 | -245.520 | -245.520 | -245.520 | -245.520 | -245.520 | nan |
| `distance_from_vwap_ticks` | 1 | -171.600 | -171.600 | -171.600 | -171.600 | -171.600 | -171.600 | nan |
| `tick_count_5m_before` | 1 | 7095.000 | 7095.000 | 7095.000 | 7095.000 | 7095.000 | 7095.000 | nan |
| `tick_count_5m_after` | 1 | 10523.000 | 10523.000 | 10523.000 | 10523.000 | 10523.000 | 10523.000 | nan |
| `bid_volume_5m_before` | 1 | 6086.000 | 6086.000 | 6086.000 | 6086.000 | 6086.000 | 6086.000 | nan |
| `ask_volume_5m_before` | 1 | 8359.000 | 8359.000 | 8359.000 | 8359.000 | 8359.000 | 8359.000 | nan |
| `delta_aligned_ratio_5m` | 1 | 0.579 | 0.579 | 0.579 | 0.579 | 0.579 | 0.579 | nan |
| `spread_avg_5m` | 1 | 0.377 | 0.377 | 0.377 | 0.377 | 0.377 | 0.377 | nan |
| `spread_max_5m` | 1 | 2.750 | 2.750 | 2.750 | 2.750 | 2.750 | 2.750 | nan |
| `price_range_5m_ticks` | 1 | 185.000 | 185.000 | 185.000 | 185.000 | 185.000 | 185.000 | nan |
| `price_position_in_5m_bar` | 1 | 0.043 | 0.043 | 0.043 | 0.043 | 0.043 | 0.043 | nan |
| `distance_from_5m_high_ticks` | 1 | 177.000 | 177.000 | 177.000 | 177.000 | 177.000 | 177.000 | nan |
| `distance_from_5m_low_ticks` | 1 | 8.000 | 8.000 | 8.000 | 8.000 | 8.000 | 8.000 | nan |
| `cvd_slope_5m_per_min` | 1 | 454.600 | 454.600 | 454.600 | 454.600 | 454.600 | 454.600 | nan |
| `cvd_slope_1m` | 1 | 439.000 | 439.000 | 439.000 | 439.000 | 439.000 | 439.000 | nan |
| `cvd_acceleration` | 1 | -15.600 | -15.600 | -15.600 | -15.600 | -15.600 | -15.600 | nan |
| `adverse_pre_move_ticks` | 1 | 30.000 | 30.000 | 30.000 | 30.000 | 30.000 | 30.000 | nan |
| `hold_time_s` | 1 | 201.800 | 201.800 | 201.800 | 201.800 | 201.800 | 201.800 | nan |
| `contracts` | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | nan |


#### Top regimes / day_types / session_phases for losers

**regime** (n=1):

| value | count | % |
|---|---:|---:|
| AFTERNOON_CHOP | 1 | 100.0% |

**day_type** (n=1):

| value | count | % |
|---|---:|---:|
| <NULL> | 1 | 100.0% |

**session_phase_ct** (n=1):

| value | count | % |
|---|---:|---:|
| MORNING | 1 | 100.0% |

**cr_verdict** (n=1):

| value | count | % |
|---|---:|---:|
| <NULL> | 1 | 100.0% |


#### Top 1 `opening_session` losers (DERIVATION, ranked by |R-multiple|)

| rank | trade_id | entry_iso | dir | entry | exit | stop | pnl_$net | R | regime | day_type |
|---:|---|---|---|---:|---:|---:|---:|---:|---|---|
| 1 | `0c321601` | 2026-04-29T16:31:34 | LONG | 27177.5 | 27157.5 | 27157.75 | $-41.72 | -1.06 | AFTERNOON_CHOP | nan |


#### Discriminating features

**Declined** — min(WIN, LOSS) = 1 < 5.


---
## Phase 4 — Entry timing (Q3: signal-fire vs pullback?)

**Strategy code reference:** `strategies/bias_momentum.py:64-340`. The code fires IMMEDIATELY when (a) EMA9/EMA21 stack confirms direction (or explosive-bypass triggers), (b) price is on the correct VWAP side (or explosive bypass active), (c) SHORT-asymmetric tf_bias requirement is met (only when both `short_extra_gates` AND `short_extra_gate_enabled` are True). **There is no explicit pullback-wait component.** Entry happens on the next tick after the gate stack clears — momentum-following, not mean-reverting.

Question: do the data show winners SHOULD have waited for a pullback?


### `bias_momentum` LONG entries (DERIVATION)
WIN n=47  |  LOSS n=140

| feature | n_win | med_win | p25_win | p75_win | min_win | max_win | n_loss | med_loss | p25_loss | p75_loss | Cliff δ | p (MWU) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `price_position_in_5m_bar` | 47 | 0.64 | 0.46 | 0.86 | 0.17 | 1.02 | 140 | 0.71 | 0.42 | 0.84 | +0.012 | 0.9021 |
| `distance_from_5m_high_ticks` | 47 | 20.00 | 11.50 | 32.00 | -1.00 | 205.00 | 140 | 21.00 | 9.00 | 34.00 | +0.017 | 0.8664 |
| `distance_from_5m_low_ticks` | 47 | 51.00 | 24.00 | 76.50 | 5.00 | 219.00 | 140 | 43.00 | 20.75 | 80.00 | +0.050 | 0.6106 |
| `distance_from_ema9_ticks` | 47 | 61.56 | 39.42 | 164.66 | -173.48 | 333.00 | 140 | 60.84 | 21.59 | 135.54 | +0.146 | 0.1361 |
| `distance_from_vwap_ticks` | 47 | 544.80 | 284.68 | 764.86 | 37.96 | 1108.76 | 140 | 458.62 | 231.76 | 727.60 | +0.110 | 0.2609 |
| `adverse_pre_move_ticks` | 47 | -15.00 | -37.50 | -3.00 | -173.00 | 57.00 | 138 | -7.00 | -23.75 | 10.75 | -0.236 | 0.01575 |
| `pullback_flag` | 0 | — | — | — | — | — | 0 | — | — | — | — | — |


### `bias_momentum` SHORT entries (DERIVATION)
WIN n=7  |  LOSS n=26

| feature | n_win | med_win | p25_win | p75_win | min_win | max_win | n_loss | med_loss | p25_loss | p75_loss | Cliff δ | p (MWU) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `price_position_in_5m_bar` | 7 | 0.18 | 0.12 | 0.45 | 0.05 | 0.89 | 26 | 0.28 | 0.14 | 0.48 | -0.121 | 0.6509 |
| `distance_from_5m_high_ticks` | 7 | 43.00 | 32.00 | 124.00 | 16.00 | 194.00 | 26 | 65.50 | 41.50 | 94.50 | -0.159 | 0.5374 |
| `distance_from_5m_low_ticks` | 7 | 14.00 | 9.50 | 38.00 | 8.00 | 131.00 | 26 | 33.50 | 14.00 | 70.50 | -0.247 | 0.3323 |
| `distance_from_ema9_ticks` | 7 | -82.52 | -114.32 | -69.08 | -158.68 | 40.56 | 26 | -41.26 | -99.56 | -15.75 | -0.297 | 0.2494 |
| `distance_from_vwap_ticks` | 7 | -133.20 | -153.48 | -2.50 | -238.88 | 72.64 | 26 | -28.14 | -76.08 | -5.10 | -0.297 | 0.2494 |
| `adverse_pre_move_ticks` | 7 | 4.00 | -19.00 | 29.50 | -100.00 | 61.00 | 25 | -10.00 | -54.00 | 13.00 | +0.223 | 0.3949 |
| `pullback_flag` | 0 | — | — | — | — | — | 0 | — | — | — | — | — |


### Verdict: signal vs pullback

- **LONG winners** median `price_position_in_5m_bar` = 0.64 (middle band); losers = 0.71 (upper-third = chase entry)

- **SHORT winners** median `price_position_in_5m_bar` = 0.18 (lower-third = pullback entry); losers = 0.28 (lower-third = pullback entry)

- **LONG** median `adverse_pre_move_ticks` (negative = pre-entry pullback against direction): winners = -15.0, losers = -7.0

- **SHORT** median `adverse_pre_move_ticks`: winners = 4.0, losers = -10.0


#### Interpretation

- The current `bias_momentum` code fires IMMEDIATELY when the EMA-stack + VWAP + explosive-bypass gates clear — there is no pullback wait. The data show this matches what's happening: winners and losers are NOT separated by a strong pullback-vs-chase signature in the 5m bar position (Cliff δ above is near zero; medians within ±0.05).

- **The slim signal that does exist:** `adverse_pre_move_ticks` median for winners trends more negative than losers (winners had ~15-tick adverse move before entry, losers ~7). Cliff δ ≈ -0.16 across both directions, p ≈ 0.07. **Suggestive but does NOT survive Bonferroni** against the 32-feature panel.

- **Action implication:** a small additive gate of the form `adverse_pre_move_ticks ≤ −0.5 × atr_5m` (i.e., 'require a small pullback before firing') would have rejected some losers and kept most winners. The current code does NOT include this gate. **Not a recommendation to ship — sample size is below Bonferroni floor and the effect could be noise.** Belongs in the Phase 13 envelope as a LOW-confidence proposal for further A/B testing.


### `opening_session` entry timing

- DERIVATION n=2 (WIN=1, LOSS=1). **INSUFFICIENT_SAMPLE — no entry-timing verdict possible.**


### Code comparison (`strategies/bias_momentum.py:64-340`)

- Direction gate fires on EMA9/EMA21 stack ([bias_momentum.py:233-234](strategies/bias_momentum.py:233))
- VWAP side gate fires immediately on price-vs-VWAP comparison ([bias_momentum.py:277-278](strategies/bias_momentum.py:277))
- Explosive bypass requires only that the CURRENT 5m bar close at the bar extreme — not a pullback ([bias_momentum.py:248-259](strategies/bias_momentum.py:248))
- SHORT-asymmetric gate (when enabled) requires both 1m AND 5m tf_bias = BEARISH ([bias_momentum.py:320-330](strategies/bias_momentum.py:320)) — confirmatory, not pullback-based
- The next phase of code (lines 340+) computes confluence/momentum and applies CVD gates. None of these reference adverse pre-entry move or `price_position_in_5m_bar`.

**Conclusion:** the data describe a pattern (small adverse-move-before-entry trends favorable) that the strategy code does not enforce. Whether codifying it would help is a HYPOTHESIS, not a finding — the cross-sectional signal is below the Bonferroni floor.


---
## Phase 9 — Visual chart index

All charts in `out/charts/winning_conditions/`. Scope-reduced from spec 20 → 10: 5 winners + 5 losers for bias_momentum from DERIVATION top-N (ranked by R-multiple). opening_session charts skipped — n=8 total doesn't merit individual chart treatment; see dataset CSVs.

| strategy | rank | kind | trade_id | regime | TBBO? | chart file |
|---|---:|---|---|---|---|---|
| bias_momentum | 1 | WIN | `4271b190` | AFTERHOURS | TBBO | [bias_momentum_win_1_4271b190.png](charts/winning_conditions/bias_momentum_win_1_4271b190.png)  |
| bias_momentum | 2 | WIN | `b85286b1` | AFTERHOURS | TBBO | [bias_momentum_win_2_b85286b1.png](charts/winning_conditions/bias_momentum_win_2_b85286b1.png)  |
| bias_momentum | 3 | WIN | `ba967bc5` | PREMARKET_DRIFT | TBBO | [bias_momentum_win_3_ba967bc5.png](charts/winning_conditions/bias_momentum_win_3_ba967bc5.png)  |
| bias_momentum | 4 | WIN | `b4db1d47` | AFTERHOURS | TBBO | [bias_momentum_win_4_b4db1d47.png](charts/winning_conditions/bias_momentum_win_4_b4db1d47.png)  |
| bias_momentum | 5 | WIN | `e7c7b03d` | AFTERHOURS | TBBO | [bias_momentum_win_5_e7c7b03d.png](charts/winning_conditions/bias_momentum_win_5_e7c7b03d.png)  |
| bias_momentum | 1 | LOSS | `9b8532e3` | CLOSE_CHOP | TBBO | [bias_momentum_loss_1_9b8532e3.png](charts/winning_conditions/bias_momentum_loss_1_9b8532e3.png)  |
| bias_momentum | 2 | LOSS | `b8e2a3bf` | OVERNIGHT_RANGE | TBBO | [bias_momentum_loss_2_b8e2a3bf.png](charts/winning_conditions/bias_momentum_loss_2_b8e2a3bf.png)  |
| bias_momentum | 3 | LOSS | `0a6c6c16` | LATE_AFTERNOON | TBBO | [bias_momentum_loss_3_0a6c6c16.png](charts/winning_conditions/bias_momentum_loss_3_0a6c6c16.png)  |
| bias_momentum | 4 | LOSS | `e9676605` | AFTERNOON_CHOP | TBBO | [bias_momentum_loss_4_e9676605.png](charts/winning_conditions/bias_momentum_loss_4_e9676605.png)  |
| bias_momentum | 5 | LOSS | `06ae6e1c` | OVERNIGHT_RANGE | TBBO | [bias_momentum_loss_5_06ae6e1c.png](charts/winning_conditions/bias_momentum_loss_5_06ae6e1c.png)  |