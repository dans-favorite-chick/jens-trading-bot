# Phoenix Strategy Oracle -- research Debrief (HALTED)

## Status
This run halted before producing a narrative.

## Regime
UNSTABLE -- analysis halted. Warning: Regime instability detected (filtered baseline): latest-month sharpe-proxy z-score = +3.71 (threshold +/- 3.00). Filtered baseline mean 0.035 over 6 months (dropped 0 sparse month(s) from 6); latest 0.129 (2026-05). Analysis halted.

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

## Sprint Addendum (R2 Diagnostic — appended post-run by 2026-06-05 sprint)

**Source:** 2026-06-05 R2 Diagnostic sprint. The Oracle-generated section above is the canonical, verifier-eligible output. This addendum is operator context that does not flow through the verifier.

### Diagnostic verdict

`|z|=3.71 > 3.0` → **REGIME_REAL** → HALT stands.

R2's sparse-month filter dropped **zero** baseline months (all 6 baseline months have trade_count ≥ 686; minimum was Dec 2025 at 1,214). The filter cannot help R2's CRITICAL Finding 1 (sparse-month contamination) when no sparse months exist.

### What this diagnostic establishes vs leaves open

- **Refuted:** R2 Finding 1 is NOT the cause of tonight's HALT — no sparse months in the baseline.
- **Refuted (partially):** R2 Finding 2 (z=+3.69 is "only 0.69σ above threshold and within noise") — z actually went UP slightly to +3.71 under the filtered path, not collapsed. Both gate variants agree on HALT.
- **OPEN:** R2 Finding 3 (baseline contamination from gradual regime drift) is NOT tested by a sparse-month filter. Visual inspection of the per-month panel shows a clear upward trend through 2025-12 → 2026-04, so Finding 3 has qualitative merit but cannot be discharged by this sprint.
- **OPEN, but actionable:** May has only 826 trades vs baseline median 1,372 (60 %). If late May trades continue to land, May's sharpe-proxy can drift further. Operator can validate by snapshotting May's trade_count over the next week.

### Cross-references

- Side-by-side comparison: `out/oracle_halt_vs_diagnostic_2026-06-05.md`
- Stock Oracle HALT debrief: `logs/oracle/research/2026-06-04_debrief.md`
- Filter implementation: `analytics/regime_gate.py:check_regime_stability_with_filter`
- Wiring: `agents/strategy_oracle.py:_check_regime_gate` (env-var opt-in `ORACLE_REGIME_GATE_FILTER`)

### PHANTOM-NT8 cross-sprint note

The Guard A + Guard B fixes shipped at `0d7c9d4` ("PHANTOM-NT8 live-replay loop containment"). This sprint does NOT change that picture — neither tonight's HALT nor a hypothetical PASS would license a parameter flip while PHANTOM-NT8 validation is still in progress. The freeze-lift sequence is unchanged.

### Sprint discipline

This sprint shipped **zero production code or config changes**. `pending_changes.json` is unchanged (Oracle did not stage any new proposals because the LLM loop never ran past the HALT). Only file additions: this debrief addendum, the new filtered-baseline variant + tests, the env-var wiring in Oracle's regime-gate wrapper (default OFF — unchanged behavior for any caller not setting the env var), and the comparison report.

---

## REV. 2 NOTE — Red-team Phase 4.7 CRITICAL (appended 2026-06-05 by sprint)

This debrief is the STRICT-FLOOR run (`min_baseline_n_after_filter=6`). The Phase 4.7 red-team identified this configuration as **structurally incapable of overturning the HALT** because:
- SQL pull is 7 months (6 baseline + 1 latest typical)
- Floor=6 means any drop triggers INSUFFICIENT_BASELINE
- Zero drops → z computed against same baseline as stock gate (matches by construction)

A **relaxed-floor companion run** (`min_baseline_n_after_filter=4`, matching the legacy `_MIN_BASELINE_MONTHS`) was executed and is at `logs/oracle/research/2026-06-05_r2_diagnostic_relaxed_*`. The relaxed run is the operationally-valid run. It also produced `dropped_months=[]` — the baseline has zero sparse months at either floor.

See the side-by-side comparison at `out/oracle_halt_vs_diagnostic_2026-06-05.md` (rev. 2) for the full reasoning.
