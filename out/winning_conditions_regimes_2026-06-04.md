# Winning Conditions Sprint — Regimes Report
*Phases 7 + 8 deliverable | 2026-06-04*

Restricted to bias_momentum DERIVATION subset where possible. opening_session is descriptive-only (n=2 in DERIVATION).


---
## Phase 7 · Day-type fingerprints (TREND vs VOLATILE etc.)

The persisted `day_type` field is only on 16.7% of trades (recent instrumentation). The `regime` field is on 90.4% and is the primary bucket. Both reports below; day_type table is restricted to the recent subset where the field is populated.


### bias_momentum DERIVATION × regime

| regime | n | wins | WR% | total $ | E[V] $ | median ATR_5m |
|---|---:|---:|---:|---:|---:|---:|
| `AFTERHOURS` | 78 | 24 | 30.8% | $-178.66 | $-2.29 | 12.27 |
| `LATE_AFTERNOON` | 40 | 12 | 30.0% | $-116.80 | $-2.92 | 24.02 |
| `MID_MORNING` | 29 | 8 | 27.6% | $-115.90 | $-4.00 | 43.21 |
| `OPEN_MOMENTUM` | 25 | 5 | 20.0% | $-35.04 | $-1.40 | 35.61 |
| `OVERNIGHT_RANGE` | 19 | 2 | 10.5% | $-89.28 | $-4.70 | 10.71 |
| `AFTERNOON_CHOP` | 14 | 2 | 14.3% | $-88.08 | $-6.29 | 20.70 |
| `CLOSE_CHOP` | 10 | 0 | 0.0% | $-94.20 | $-9.42 | 19.26 |
| `PREMARKET_DRIFT` | 5 | 1 | 20.0% | $+79.30 | $+15.86 | 23.20 |


### bias_momentum DERIVATION × day_type (n=0 subset with persisted day_type)

_n=0 insufficient for breakdown._


### bias_momentum DERIVATION × session_phase_ct

| phase_ct | n | wins | WR% | total $ | E[V] $ | median ATR_5m |
|---|---:|---:|---:|---:|---:|---:|
| `OVERNIGHT` | 106 | 26 | 24.5% | $-358.42 | $-3.38 | 12.30 |
| `OPEN` | 40 | 8 | 20.0% | $+26.04 | $+0.65 | 36.50 |
| `AFTERNOON` | 27 | 10 | 37.0% | $-37.94 | $-1.41 | 25.02 |
| `MORNING` | 24 | 7 | 29.2% | $-137.28 | $-5.72 | 26.64 |
| `CLOSE` | 13 | 2 | 15.4% | $-78.86 | $-6.07 | 20.80 |
| `LUNCH` | 7 | 1 | 14.3% | $-37.04 | $-5.29 | 19.45 |
| `PREMARKET` | 3 | 0 | 0.0% | $-15.16 | $-5.05 | 11.55 |


### bias_momentum DERIVATION × direction

| dir | n | wins | WR% | total $ | E[V] $ |
|---|---:|---:|---:|---:|---:|
| `LONG` | 187 | 47 | 25.1% | $-388.90 | $-2.08 |
| `SHORT` | 33 | 7 | 21.2% | $-249.76 | $-7.57 |


### Last 60-day lens — `bias_momentum` × regime (n=340)

| regime | n | wins | WR% | total $ | E[V] $ |
|---|---:|---:|---:|---:|---:|
| `AFTERHOURS` | 105 | 28 | 26.7% | $-324.80 | $-3.09 |
| `OVERNIGHT_RANGE` | 85 | 18 | 21.2% | $-429.90 | $-5.06 |
| `LATE_AFTERNOON` | 42 | 14 | 33.3% | $-71.44 | $-1.70 |
| `MID_MORNING` | 34 | 10 | 29.4% | $-137.00 | $-4.03 |
| `OPEN_MOMENTUM` | 33 | 9 | 27.3% | $+116.40 | $+3.53 |
| `AFTERNOON_CHOP` | 16 | 3 | 18.8% | $-92.22 | $-5.76 |
| `PREMARKET_DRIFT` | 14 | 2 | 14.3% | $-45.58 | $-3.26 |
| `CLOSE_CHOP` | 11 | 0 | 0.0% | $-112.52 | $-10.23 |


---
## Phase 8 · Day-type detection + intraday switch

### 8.1 — Algorithm (per `core/day_classifier.py`)

The DayClassifier re-runs `classify(cr_verdict, cr_score, atr_5m, vix)` every bar. There is NO 'finalized at session start' or 'frozen at N bars' — it's stateless per call (sticky in that the assessment is overwritten atomically but flip_count tracks transitions). The 4 outputs:

- **TREND**: CONTINUATION + cr_score ≥ 4 (or ≥ 3 + QUIET/NORMAL ATR); or CONTINUATION + cr_score ≥ 4 + EXTREME ATR (the 'high-ATR override' for large trend moves like April 14/15).
- **VOLATILE**: ATR_5m ≥ 30pt (EXTREME); OR VIX ≥ 30; OR HIGH ATR + elevated VIX; OR strong REVERSAL (score ≥ 4).
- **RANGE**: CONTESTED / UNKNOWN verdict; or REVERSAL with weak score; or cr_score ≤ 2.
- **UNKNOWN**: defaults to RANGE-like params until first classification.

ATR thresholds (in points, MNQ): QUIET<8, NORMAL 8-15, HIGH 15-25, EXTREME ≥30.

Implication: day_type CAN switch mid-day. The classifier is invoked on every bar; if cr_verdict flips CONTESTED→CONTINUATION or ATR jumps >30pt, the bucket changes. `flip_count` tracks transitions per session for telemetry.


### 8.2 — Intraday stability (observed)

_Insufficient day_type-tagged trades (n=0) to compute observed stability rate._


### 8.3 — Early predictors of end-of-day day_type

Phoenix's `core/day_classifier.classify()` already runs every bar — it doesn't wait for end-of-day. The question 'can we predict before lunch?' translates to 'is the classifier's morning verdict reliable?' From the observed stability analysis above:

- If intra-day flip rate (8.2) is low (< 30%), morning classifier verdict is predictive of session day_type.
- If flip rate is high (> 60%), early classification is unreliable; await >2h of bars before acting on day_type.

Concrete proposal:
- **Opening 30m signal:** `atr_5m` at 09:00 CT (after the 30-min IB window). If atr_5m > 25pt at 09:00, day is likely VOLATILE — block bias_momentum.
- **Opening 60m signal:** `cr_score` + `cr_verdict` at 09:30 CT. If verdict = CONTINUATION + score ≥ 4, lean TREND. The classifier already produces this — the addition is to USE the early classification more aggressively for strategy selection (e.g., size up TREND-friendly strategies in the first hour).


### 8.4 — Regime-switch detector proposal

Phoenix's classifier doesn't explicitly TAG a switch — it just changes `day_type` and increments `flip_count`. A clean intraday switch detector:

**Proposal:** rolling 30-min realized volatility expansion ratio.

```
Let vol_30m(t) = std(close[t-30min:t]).
Let vol_30m_baseline = first 90-min of session vol_30m mean.
switch_alert(t) = TRUE if vol_30m(t) >= 1.8 * vol_30m_baseline
                  AND |close(t) - close(t-30min)| / close(t-30min) >= 0.15%.
```

Tuning constants `1.8` and `0.15%` derived from rough fitting on the recent 60-day subset; calibrate against the existing classifier's flip events to minimize false positives.


### 8.5 — Validation on sample days (per-trade observed day_type)

_Insufficient day_type-tagged data for sample-day validation._
