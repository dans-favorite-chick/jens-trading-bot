# Phoenix Strategy Oracle -- research Debrief (HALTED)

## Status
This run halted before producing a narrative.

## Regime
UNSTABLE -- analysis halted. Warning: Regime instability detected (filtered baseline): latest-month sharpe-proxy z-score = +3.67 (threshold +/- 3.00). Filtered baseline mean 0.035 over 6 months (dropped 0 from 6); latest 0.129 (2026-05). Analysis halted.

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

## Sprint Addendum (R2 Diagnostic RELAXED-floor run — appended 2026-06-05 by sprint)

This is the relaxed-floor companion to the strict-floor run at `2026-06-05_r2_diagnostic_*`.

The Phase 4.7 red-team identified the spec-pinned `min_baseline_n_after_filter=6` as structurally neutered against the SQL pull `_PULL_MONTHS=7`. The relaxed floor (4) matches the legacy `_MIN_BASELINE_MONTHS=4` constant and gives the filter statistical headroom to drop sparse months without immediately triggering INSUFFICIENT_BASELINE.

### Verdict
- `z = +3.67` > threshold 3.0 → HALT stands by pre-decision rule.
- `dropped_months = []` — filter had room to drop and dropped zero.
- **R2 Finding 1 (sparse-month contamination) is REFUTED as the cause of tonight's HALT.** Even with floor relaxed, the baseline has no sparse months.
- R2 Finding 3 (baseline drift) remains UNTESTED — sparse-month filter doesn't detrend.

### Side-by-side comparison
See `out/oracle_halt_vs_diagnostic_2026-06-05.md` (rev. 2).
