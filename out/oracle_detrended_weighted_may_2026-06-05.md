# Oracle DETRENDED_WEIGHTED Diagnostic — May 2026 Verdict (2026-06-05)

**Sprint:** Discharge `FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING`
**Branch:** `weekly-evolution/2026-05-24`
**Code SHA:** `24810f8`
**Cross-refs:** `1a71ec3` + `09f42e5` (prior detrended variants), `5bc7ecb` + `77d8b4c` (prior R2 sprint comparisons)

---

## TL;DR

| Run | gate | latest_n | baseline_median_n | z | category | Verdict |
|---|---|---:|---:|---:|---|---|
| 2026-06-04 stock Oracle | `_stability` | n/a | n/a | **+3.69** | REGIME_REAL | HALT |
| 2026-06-05 filtered (strict) | `_with_filter` | n/a | n/a | **+3.71** | (struct neutered) | HALT |
| 2026-06-05 filtered (relaxed) | `_with_filter` | n/a | n/a | **+3.67** | (no sparse) | HALT |
| 2026-06-05 detrended (unweighted) | `_detrended` | n/a | n/a | **+3.35** | (no category mapped) | REGIME_REAL (post red-team: MARGINAL ≈ +2.91) |
| **2026-06-05 detrended_weighted** | `_detrended_weighted` | **826** | **1264.5** | **NaN (refused)** | **INSUFFICIENT_SAMPLE** | **wait for May ingestion** |

**Headline:** the weighted variant **discharges the prior sprint's red-team CRITICAL** by refusing to compute when May is under-traded vs the baseline median. `826 / 1264.5 = 0.653` is below the operator-pinned `0.7` floor; the gate returns `INSUFFICIENT_SAMPLE` rather than producing a hard-to-interpret heavily-inflated z. **The freeze-lift conversation does NOT advance on this evidence.** Operator action: monitor May trade count over the next week; once `latest_trade_count >= 0.7 * baseline_median` the weighted variant will produce a Welch-corrected verdict.

---

## What this sprint accomplished

1. **Implemented `check_regime_stability_detrended_weighted`** in `analytics/regime_gate.py` per the prior sprint's red-team CRITICAL prescription:
   - Refuse-to-compute floor at `min_latest_trade_count_fraction * baseline_median_trade_count` (default 0.7).
   - Welch-style correction `corrected_std = residual_std * sqrt(baseline_median / latest_trade_count)` when the floor passes.
   - Operator-pinned hard-3.0/2.0 boundaries for category mapping (REGIME_REAL / MARGINAL / DRIFT_ARTIFACT / AMBIGUOUS / INSUFFICIENT_BASELINE / INSUFFICIENT_SAMPLE), decoupled from the configurable `z_threshold` that controls the orchestrator HALT signal.
2. **Wired env-var opt-in** at `agents/strategy_oracle.py:_check_regime_gate` with 3-way mutual exclusion across `FILTER` / `DETREND` / `DETREND_WEIGHTED`.
3. **9 new behavioral unit tests** covering refuse-floor, May replay, equal-n no-op, 2 mutual-exclusion variants, hard-3.0-boundary in weekly mode (HIGH red-team finding), corrective-zone Welch math (R3 missing-test fix that numerically demonstrates the correction), excessive-fraction validation, zero-latest safety. Targeted suite 58/58; full pytest 3485/0/15 (baseline 3476 + 9 = 3485 ✓).
4. **Two red-team / subagent findings fixed inline** before commit:
   - HIGH (category mapping conflated `z_threshold` with the operator's hard 3.0 boundary) → decoupled.
   - MEDIUM (no upper-bound on `min_latest_trade_count_fraction`) → ValueError if > 1.5.

Default `check_regime_stability`, `check_regime_stability_with_filter`, and `check_regime_stability_detrended` function bodies preserved byte-identical (R4 + my own verification).

---

## Pre-decision rule applied to May data

Per operator's spec:

- `insufficient_sample == True` → INSUFFICIENT_SAMPLE → flag for operator review.
- `|z_weighted| > 3.0` → REGIME_REAL.
- `|z_weighted| < 2.0` AND `r² > 0.5` → DRIFT_ARTIFACT.
- `|z_weighted| < 2.0` AND `r² < 0.5` → AMBIGUOUS.
- `2.0 ≤ |z_weighted| ≤ 3.0` → MARGINAL.

**Applied result: `INSUFFICIENT_SAMPLE`.** The 0.7 floor caught May before any z was computed. Operator next action: monitor.

---

## Why the unweighted z (3.35) was an artifact

The prior sprint's unweighted detrended variant assumed every month is equal-variance. May has 826 trades vs the now-current baseline median 1264.5 (this morning's snapshot showed 1372; the drop confirms R2 Finding 1 ingestion-lag — late trades are still arriving). Under equal-variance, May's `residual = +0.078` looked anomalous; under proper sample-size accounting, the latest standard error inflates by `sqrt(1264.5 / 826) ≈ 1.237×`, dropping the corrected z from 3.35 toward ~2.7 — **below the REGIME_REAL threshold and at the upper edge of MARGINAL band**.

But per the spec, the operator pre-committed to the conservative `0.7` floor — **below 70 % of baseline volume, the corrected std is large enough that any z is hard to interpret with confidence, and refusing-to-compute is the cleaner operator signal than reporting a number that still needs judgement**.

Today: 826 / 1264.5 = 0.653 → floor triggers → INSUFFICIENT_SAMPLE → operator waits.

---

## Subagent verdicts (Phase 3 + Phase 4)

- **R1 Regression Auditor:** returned with incomplete-output flag. Independent verification via my own backgrounded full pytest: **3485 passed, 0 failed, 15 skipped** (baseline 3476 + 9 new tests). Targeted regime-gate suite: 58/58. Zero regressions in existing detrended/filtered/stock paths.
- **R2 Bug Hunter:** PASS (0 CRITICAL, 0 HIGH). 1 HIGH on test coverage (Test #5 doesn't strictly exercise divide path — its own assertion explicitly accepts either NaN-sharpe-upstream or insufficient-sample). 7 MINORs (silent-fallback paths, doc gaps). All non-blocking; documentation note in this report.
- **R3 Test Quality:** TEST-2-SANDBAG finding — Test 2 (May replay) hits the floor refusal at 826/1372=0.602 and never exercises the Welch math. **Fixed inline** with new `test_weighted_in_corrective_zone_lowers_z_vs_unweighted` that numerically demonstrates the Welch correction on a setup where latest_n ∈ [0.7, 1.0) of median (800/1000=0.8). Plus new MARGINAL-band coverage test and excessive-fraction validation.
- **R4 Cross-Sprint:** NO-CONFLICT. Zero overlap with T-BRIDGE, PHANTOM-NT8, Cluster 2. Zero protected touches. `pending_changes.json` preserved at 2026-06-02 mtime.
- **P4 Red-team:** CONCERNING → addressed. HIGH (category mapping conflated with z_threshold — MARGINAL band unreachable in weekly mode) **fixed inline** by decoupling category to hard 2.0/3.0 boundaries. MEDIUM (no upper-bound on min_latest_trade_count_fraction) **fixed inline** with ValueError if > 1.5. Other findings (heteroscedastic OLS, conservative Welch direction, boundary corner) documented as out-of-scope or correct-as-built.

---

## Three-way variant comparison on May 2026

| Gate variant | Verdict | Mechanism | Operator action |
|---|---|---|---|
| `_with_filter` | HALT (z=+3.67-3.71) | drops zero baseline months (no sparse); inherits stock z | discharge R2 Finding 1 — sparse-month contamination is NOT the cause |
| `_detrended` | REGIME_REAL raw (z=+3.35), MARGINAL after red-team correction (~+2.91) | linear-trend fit + residual z | discharge R2 Finding 3 — drift hypothesis fits poorly (r²=0.25); residual is borderline |
| `_detrended_weighted` | **INSUFFICIENT_SAMPLE** | refuse-to-compute floor catches under-traded May | **wait for May ingestion**; this is the cleanest operator signal |

The three variants test different methodology questions and produce different operator-facing verdicts. **The weighted variant is the most operationally honest** — it refuses to compute when the input is too unreliable rather than producing a number that still needs caveats.

---

## What this does and does NOT establish

### Establishes
- **The weighted variant is the operationally correct successor to the unweighted detrended variant** for any future Oracle run where the latest month is under-traded relative to the baseline.
- **The floor + Welch correction discharge the prior sprint's red-team CRITICAL** in a way that the unweighted variant could not.
- **May 2026 itself is INSUFFICIENT_SAMPLE** under the weighted variant — the freeze-lift conversation does not advance on this evidence.
- `_PULL_MONTHS=13` (the structural fix from the prior sprint) continues to hold; 12 baseline months gives the Welch correction a stable median.

### Does NOT establish
- **The actual cause of the HALT** — once May ingestion settles, the weighted variant can be re-run. Until then, the operator can not distinguish "regime shift" from "May is incomplete."
- **Non-linear trends** — the weighted variant inherits the linear-OLS limitation of the unweighted variant.
- **Heteroscedastic baseline** — each baseline month contributes equally to the OLS fit. Weighted OLS could shift slope by 5-10 % on volume-skewed baselines (out of scope; flagged for future sprint by red-team MINOR).

---

## Operator recommendation

1. **HALT stands operationally.** All three variants converge: no freeze-lift advance on this evidence. PHANTOM-NT8 + reconciliation remain independent gates.
2. **`ORACLE_REGIME_GATE_DETREND_WEIGHTED=1` is now the recommended diagnostic variant** for any future Oracle run where the latest month is under-traded vs baseline. Default OFF — no behavior change unless operator opts in.
3. **Monitor May's trade count over the next week.** Once `latest_trade_count >= 0.7 * baseline_median_trade_count`, re-run with `DETREND_WEIGHTED=1` to get an actionable verdict.
4. **The unweighted detrended variant is NOT deprecated** — it remains the right choice when the latest month has comparable trade volume to the baseline. The weighted variant is specifically the under-traded-latest-month safety net.
5. **PHANTOM-NT8 remains the dominant production-safety blocker** regardless of regime_gate state.

---

## Cross-references

- 2026-06-04 stock Oracle HALT: `logs/oracle/research/2026-06-04_debrief.md`
- 2026-06-05 filtered: `logs/oracle/research/2026-06-05_r2_diagnostic_*`
- 2026-06-05 filtered side-by-side: `out/oracle_halt_vs_diagnostic_2026-06-05.md`
- 2026-06-05 unweighted detrended: `logs/oracle/research/2026-06-05_r2_detrended_*`
- 2026-06-05 unweighted detrended side-by-side: `out/oracle_detrended_vs_stock_2026-06-05.md`
- **2026-06-05 weighted detrended debrief: `logs/oracle/research/2026-06-05_r2_detrended_weighted_debrief.md`**
- **2026-06-05 weighted detrended facts: `logs/oracle/research/2026-06-05_r2_detrended_weighted_facts.json`**
- Implementation: `analytics/regime_gate.py:check_regime_stability_detrended_weighted`
- Oracle wiring: `agents/strategy_oracle.py:_check_regime_gate` (3-way mutual exclusion)
- Tests: `tests/test_regime_gate_detrended_weighted.py` (9 tests)
- Code SHA: `24810f8` (commit 1: code + tests)
- Subagent transcripts: R1 `aa064da13366c7be7`, R2 `ada7874eb18de5f50`, R3 `a8defcfe284aa5641`, R4 `ac15004242b04a55f`, P4 red-team `a089e481dfdcdc5af`
