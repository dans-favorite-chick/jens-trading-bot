# Phoenix Strategy Oracle -- research Debrief (HALTED)

## Status
This run halted before producing a narrative.

## Regime
UNSTABLE -- analysis halted. Warning: Regime instability detected (detrended): latest-month residual z-score = +3.35 (threshold +/- 3.00). Baseline trend slope=+0.00357 per month, intercept=+0.00815, r^2=0.250. Latest sharpe 0.129 (2026-05) vs projection 0.051 -> residual +0.078. Analysis halted.

## Report Card
- 16 strategies analyzed
- 7 cleared all gates -> proposals
- 9 strategies failed gate psr_0_90
- 9 strategies failed gate dsr_0_95
- 9 strategies failed gate hlz_3_0
- 9 strategies failed gate min_trl_met
- 6 strategies failed gate wfa_pass
- 4 strategies failed gate bhy_0_05


---

## Sprint Addendum (R2 Finding 3 Detrended Diagnostic — appended 2026-06-05)

This is the canonical detrended-baseline run. The Oracle-generated section above is the verifier-eligible output. This addendum is operator context.

### Verdict
`|z_detrended|=3.35 > 3.0` → **REGIME_REAL** per the operator's pre-decision rule. HALT stands; gradual baseline drift is REFUTED as the cause.

### Key numbers
- `z_score_detrended = +3.354` (was stock +3.69, filtered +3.67)
- `baseline_slope = +0.00357` per month (small upward drift)
- `baseline_r_squared = 0.250` (linear model explains only 25% of baseline variance)
- `baseline_n_months = 12` (post `_PULL_MONTHS=7→13` bump)
- `latest_residual = +0.078`, residual_std ≈ 0.023

### What this establishes
- R2 Finding 3 (gradual baseline drift) REFUTED as cause of tonight's HALT.
- The structural `_PULL_MONTHS=7` neutering (red-team CRITICAL on 2026-06-05) RESOLVED by the bump.
- HALT verdict is now robust across three methodologies: stock, filtered, detrended.

### What this does not establish
- Non-linear trend models untested (low r² is consistent with EITHER "no trend" OR "non-linear trend").
- A step-shaped boundary at the latest month boundary cannot be distinguished from "outlier latest" by a linear fit.
- May's under-traded state (826 trades vs baseline median ~1372) is unchanged; if late ingestion is real, residual can drift.

### PHANTOM-NT8 cross-sprint note
Guard A + B + Round 3 H3 shipped (`0d7c9d4`, `e1737de`). Neither this HALT nor a hypothetical PASS would license a parameter flip while PHANTOM-NT8 validation is in progress. Freeze-lift sequence unchanged.

### Sprint discipline
This sprint shipped zero production code or config changes. `pending_changes.json` unchanged. `FREEZE_ACTIVE` unchanged. The detrended variant is permanently available behind `ORACLE_REGIME_GATE_DETREND=1` (default OFF, mutually exclusive with `ORACLE_REGIME_GATE_FILTER`).

### Cross-references
- Side-by-side comparison: `out/oracle_detrended_vs_stock_2026-06-05.md`
- Implementation: `analytics/regime_gate.py:check_regime_stability_detrended` (new)
- `_PULL_MONTHS=13`: `analytics/regime_gate.py` (bumped from 7 this sprint)
- Tests: `tests/test_regime_gate_detrended.py` (9 tests), `tests/test_regime_gate_pull_window.py` (4 tests)

---

## REV. 2 NOTE — Red-team Phase 5.7 CRITICAL (appended 2026-06-05)

This debrief reflects the UNWEIGHTED detrended diagnostic with `_PULL_MONTHS=13`. The Phase 5.7 red-team identified a CRITICAL methodological violation that downgrades the operator-facing verdict from `REGIME_REAL` to `MARGINAL`:

**Equal-variance violation.** The diagnostic treats every month as equal-variance, but May 2026 has 826 trades vs baseline median 1,372 (60%). Sharpe-proxy sampling variance scales as ~1/n. With the sample-size correction the latest std inflates by ~1.29×, dropping z from 3.35 to **≈ 2.91 — into the MARGINAL band** (operator pre-decision rule: "wait + monitor").

**Operationally:** the HALT itself still stands (PHANTOM-NT8 + reconciliation are independent), but R2 Finding 3 is NOT fully refuted; it is partially discharged in that the linear-drift hypothesis fits poorly, but the residual evidence is borderline rather than definitively REGIME_REAL.

**Filed follow-up:** `FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING` — implement weighted OLS with Welch-style latest-month SE, or refuse-to-compute when latest is under-traded.

**Detail in `out/oracle_detrended_vs_stock_2026-06-05.md` rev. 2 §1.**
