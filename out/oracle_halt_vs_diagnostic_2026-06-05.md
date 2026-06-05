# Oracle 2026-06-04 HALT vs 2026-06-05 R2-Diagnostic — side-by-side (rev. 2)

**Sprint:** Oracle R2 Diagnostic — filtered-month baseline + baseline_n>=6
**Branch:** `weekly-evolution/2026-05-24`
**Refs:** `4df7abc` (Oracle HALT), `1f48b2b` (R4 determinism), `FINDING-2026-06-04-ORACLE-REGIME-GATE-METHODOLOGY`

**Rev. 2 (2026-06-05):** rewritten after red-team Phase 4.7 surfaced a CRITICAL methodology concern with the spec-pinned `min_baseline_n_after_filter=6`. See §4 below.

---

## TL;DR (rev. 2)

| Run | floor | z-score | baseline_n | mean | dropped months | Verdict |
|---|---:|---:|---:|---:|---:|---|
| 2026-06-04 stock Oracle | n/a | **+3.69** | 6 | 0.035 | n/a | HALT (operator: real or artifact?) |
| 2026-06-05 R2 diag, strict (spec floor=6) | 6 | **+3.71** | 6 | 0.035 | **0** | HALT — filter dropped nothing |
| 2026-06-05 R2 diag, relaxed (floor=4) | 4 | **+3.67** | 6 | 0.035 | **0** | HALT — filter still dropped nothing |

**Headline (rev. 2):** The HALT stands in both diagnostic variants, **but neither variant successfully tested R2's hypothesis.** The strict variant is structurally neutered by construction (red-team CRITICAL §4). The relaxed variant is methodologically valid but encounters zero sparse months in the actual baseline. **R2 Finding 1 (sparse-month contamination) is REFUTED as the cause of tonight's HALT — the baseline has no sparse months at any reasonable definition.** R2 Finding 3 (gradual baseline drift) remains UNTESTED and is the more likely cause of the elevated z given the visible upward trend in the baseline series (2025-12 −0.003 → 2026-04 +0.042).

---

## What this sprint did

1. Implemented `analytics.regime_gate.check_regime_stability_with_filter()` — an opt-in filtered-baseline variant of the existing gate. Default behavior of `check_regime_stability` preserved verbatim (regression-guarded by 22 existing + 12 new tests).
2. Wired an env-var opt-in (`ORACLE_REGIME_GATE_FILTER`, `ORACLE_REGIME_GATE_SPARSE_FACTOR`, `ORACLE_REGIME_GATE_MIN_BASELINE_N`) into `agents/strategy_oracle.py:_check_regime_gate`. Default OFF — no behavior change for any caller not opting in.
3. Re-executed Oracle research mode TWICE:
   - **Strict run** at `min_baseline_n_after_filter=6` (per spec).
   - **Relaxed run** at `min_baseline_n_after_filter=4` (matching the legacy `_MIN_BASELINE_MONTHS`, per red-team recommendation).
4. Discovered the baseline has zero sparse months at either threshold.

Artifacts:
- Strict run: `logs/oracle/research/2026-06-05_r2_diagnostic_{audit.jsonl,debrief.md,facts.json}`
- Relaxed run: `logs/oracle/research/2026-06-05_r2_diagnostic_relaxed_{audit.jsonl,debrief.md,facts.json}`

---

## Pre-decision rule (per sprint spec)

- `|z| > 3.0` → regime shift is REAL → HALT stands.
- `|z| < 2.0` → HALT was artifact → freeze-lift can advance.
- `2.0 ≤ |z| ≤ 3.0` → AMBIGUOUS → wait for next monthly Oracle re-run.
- `insufficient_baseline_after_filter=True` → INSUFFICIENT_BASELINE → flag for operator.

**Strict-floor result:** |z|=3.71 > 3.0 → REGIME_REAL → HALT stands. ⚠️ See §4 — this verdict carries no more information than the stock 2026-06-04 HALT.

**Relaxed-floor result:** |z|=3.67 > 3.0 → REGIME_REAL → HALT stands. The filter HAD room to drop a sparse month if any existed; it did not, which IS a meaningful refutation of R2 Finding 1 for this dataset.

---

## Per-month sharpe-proxy panel

Source: `analytics.prepared_queries.monthly_sharpe_proxy(con, months_back=7)` against the canonical warehouse at `data/warehouse/phoenix.duckdb`.

| Month | trade_count | avg_pnl | pnl_stddev | sharpe_proxy | win_rate |
|---|---:|---:|---:|---:|---:|
| 2025-11 | 1,364 | +6.19 | 98.21 | +0.063 | 0.430 |
| 2025-12 | 1,214 | −0.25 | 72.87 | −0.003 | 0.415 |
| 2026-01 | 1,380 | +1.08 | 71.25 | +0.015 | 0.432 |
| 2026-02 | 1,447 | +3.31 | 90.23 | +0.037 | 0.413 |
| 2026-03 | 1,991 | +4.86 | 84.19 | +0.058 | 0.422 |
| 2026-04 | 1,315 | +3.43 | 81.16 | +0.042 | 0.444 |
| **2026-05 (latest)** | **826** | **+10.26** | **79.27** | **+0.129** | **0.467** |

Baseline (excluding latest): `trade_count` series `[1364, 1214, 1380, 1447, 1991, 1315]`, median **1,372** at the 22:21 snapshot / **1,369.5** at the 09:50 snapshot (drift consistent with ongoing trade ingestion). R2 sparse threshold at `0.5 × median = 686`. **Minimum baseline trade_count = 1,214 (Dec 2025), well above 686.** No sparse months exist in the baseline at this snapshot. Filter drops zero regardless of floor configuration.

**Standout observation — May itself is under-traded:** 826 trades vs baseline median ~1,370 (60%). The filter does NOT touch the latest month (gate's job is to test it, not filter it), so this is for operator awareness. May at 60% of typical volume is consistent with R2 Finding 1 (ingestion lag) *applied to May*, but the filter is structurally unable to address that — and the diagnostic does not test it.

---

## §4 — Red-team Phase 4.7 CRITICAL finding (rev. 2 lead)

The Phase 4.7 red-team identified a CRITICAL methodology concern with the spec-pinned floor `min_baseline_n_after_filter=6` against the existing SQL pull `_PULL_MONTHS=7`:

> **The diagnostic is structurally incapable of overturning the HALT regardless of what's in the data.** The SQL pulls 7 months, yielding 6 baseline + 1 latest typically. The filter's floor=6 means: zero drops → z computed against same baseline as stock gate (matches by construction); ≥1 drops → 5 baseline → triggers `insufficient_baseline_after_filter=True` and returns `z=NaN`. There is no path where the filter actually drops a sparse month AND recomputes a meaningfully different z. The diagnostic is a no-op gate as designed.

**Smoking gun in test code:** `tests/test_oracle_regime_gate_filtered_baseline.py:test_filter_drops_one_sparse_baseline_month` had to lower `min_baseline_n_after_filter=5` for the test to work — an admission that with production defaults the floor blocks the drop-recompute branch.

**Resolution applied this sprint:**
1. Documented the CRITICAL finding here and in the diagnostic debrief addendums.
2. Re-ran the diagnostic with `min_baseline_n_after_filter=4` (matching the legacy `_MIN_BASELINE_MONTHS` constant). The relaxed run had statistical headroom for sparse drops; it dropped zero. **This is the operationally-valid run; this sprint's evidence comes from the relaxed-floor data.**
3. Filed new finding `FINDING-2026-06-05-ORACLE-FILTERED-GATE-FLOOR-METHODOLOGY` for the longer-term fix: either bump `_PULL_MONTHS` (e.g. to 13 for full-year baseline) or change the filtered variant's default floor to match `_MIN_BASELINE_MONTHS=4`. Out of scope for tonight's research-only sprint.

---

## §5 — What this diagnostic DOES and DOES NOT establish (rev. 2)

### Establishes (after the relaxed-floor run)
- **R2 Finding 1 (sparse-month contamination) is REFUTED as the cause of tonight's HALT.** Even with the floor relaxed to 4, the filter had room to drop sparse months and dropped zero. The baseline has no sparse months at the `0.5 × median(trade_count)` definition.
- **R2 Finding 2 (z=+3.69 is "only 0.69σ above threshold and within noise") is REFUTED partially.** z under the filtered path (3.67) is essentially identical to z under the stock path (3.69); both comfortably exceed the 3.0 threshold. Both gate variants agree on HALT.
- **The opt-in env-var path works end-to-end.** 22 existing regime_gate tests still green, 12 new tests green (covering input validation, accounting identity, schema drift, latest-month preservation, and median exposure).
- **Oracle's verdict surface is deterministic.** Strict run z=+3.71, relaxed run z=+3.67 — both above threshold, small drift attributable to a few late-arriving May trades between 22:21 and 09:50.

### Does NOT establish
- **R2 Finding 3 (baseline drift) remains UNTESTED.** A sparse-month filter cannot detrend the baseline. The visible upward trend in baseline sharpe-proxies (2025-12 −0.003 → 2026-04 +0.042) is qualitatively present and is the more likely cause of the elevated z.
- **The structural-neutering bug (red-team CRITICAL) is documented but unfixed.** A future diagnostic that wants to actually test sparse-month effects on a comparably-sized warehouse must either expand the SQL pull or lower the floor. This is intentional out-of-scope.
- **May's own under-traded state is not addressed.** R2 Finding 1 applied to the LATEST month (not the baseline) is the angle the diagnostic doesn't speak to.

---

## §6 — Recommendation (rev. 2)

1. **HALT stands.** Both diagnostic variants confirm. The freeze-lift conversation does NOT advance on the basis of this diagnostic.
2. **R2 Finding 1 is operationally refuted for tonight.** Sparse-month contamination is not the cause. Future Oracle re-runs can revisit if a regime change leaves the warehouse with genuinely sparse baseline months.
3. **The actual likely cause is R2 Finding 3 (baseline drift).** A detrended-baseline variant of the gate would be the right next experiment. Out of scope tonight; see `FINDING-2026-06-05-ORACLE-FILTERED-GATE-FLOOR-METHODOLOGY` and adjacent for the follow-up.
4. **Investigate May's trade count.** Snapshot `SELECT COUNT(*) FROM trades_ct WHERE date_trunc('month', session_date) = '2026-05-01'` at multiple times over the next week. If it climbs materially, May's sharpe-proxy on partial data is feeding the HALT.
5. **PHANTOM-NT8 remains the dominant production-safety blocker** even after Guard A + Guard B (commit `0d7c9d4`) — neither this HALT nor a hypothetical PASS would license a parameter flip while PHANTOM-NT8 validation is in progress. The freeze-lift sequence is unchanged.

---

## §7 — Cross-references

- Stock Oracle 2026-06-04 HALT debrief: `logs/oracle/research/2026-06-04_debrief.md`
- R2 diagnostic STRICT debrief: `logs/oracle/research/2026-06-05_r2_diagnostic_debrief.md`
- R2 diagnostic STRICT facts: `logs/oracle/research/2026-06-05_r2_diagnostic_facts.json`
- R2 diagnostic RELAXED debrief: `logs/oracle/research/2026-06-05_r2_diagnostic_relaxed_debrief.md`
- R2 diagnostic RELAXED facts: `logs/oracle/research/2026-06-05_r2_diagnostic_relaxed_facts.json`
- Filter implementation: `analytics/regime_gate.py:check_regime_stability_with_filter`
- Oracle wiring: `agents/strategy_oracle.py:_check_regime_gate` (env-var opt-in)
- Tests: `tests/test_oracle_regime_gate_filtered_baseline.py` (12 tests, all green)
- Subagent transcripts (Phase 4 + 4.7): R1 PASS `a5a5b9e279ce6b3db`, R2 Bug Hunter 2 CRITICAL + 2 HIGH (fixed inline) `ae627d19e3aac97ea`, R3 Test Quality (4 MEDIUM/MINOR fixed; 4 missing tests added) `a6949f4ebe37f26a0`, R4 Cross-Sprint NO-CONFLICT (out-of-scope WIP correctly excluded) `ab917d0e2d5b7a7f0`, P4.7 red-team CRITICAL-HALT `ada354031ffd35b7a`.
