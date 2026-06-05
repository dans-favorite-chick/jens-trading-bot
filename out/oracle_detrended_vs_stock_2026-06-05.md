# Oracle Detrended Diagnostic vs Stock/Filtered Gate — 2026-06-05 (rev. 2 with red-team CRITICAL)

**Sprint:** Oracle R2 Finding 3 — detrended-baseline + `_PULL_MONTHS` structural fix
**Branch:** `weekly-evolution/2026-05-24`
**Refs:** `1a71ec3` + `77d8b4c` (R2 Diagnostic sprint), `FINDING-2026-06-05-ORACLE-FILTERED-GATE-FLOOR-METHODOLOGY` (resolved), `FINDING-2026-06-04-ORACLE-REGIME-GATE-METHODOLOGY` (partially discharged)

**Rev. 2 (2026-06-05):** rewritten after Phase 5.7 red-team CRITICAL falsification. **The original "REGIME_REAL" headline overstated what the diagnostic established.** See §1 below.

---

## §1 — 🚨 RED-TEAM CRITICAL FINDING (rev. 2 lead)

The Phase 5.7 red-team identified a CRITICAL methodological violation that **moves the operator-pre-committed verdict from `REGIME_REAL` to `MARGINAL`**:

**The diagnostic treats every monthly sharpe-proxy as an equal-variance observation. May 2026 has 826 trades vs baseline median 1,372 — only ~60 % of typical n.** Sharpe-proxy sampling variance scales as ~1/n, so May's standard error is inflated by `sqrt(1372/826) ≈ 1.29×`. Under the conservative assumption that half the baseline residual std (0.0234) comes from per-month sampling noise:

- Corrected May standard error: `sqrt(between² + sampling² × 1.66) ≈ 0.0270`
- **Corrected z: `0.0784 / 0.0270 ≈ 2.91`**
- 2.91 is **below** the 3.0 threshold → MARGINAL band per operator's pre-decision rule
- Pre-decision rule for MARGINAL: "wait + monitor 2-3 monthly runs"

**Adjusted headline:** `|z_detrended_corrected| ≈ 2.91 ∈ [2.0, 3.0]` → **MARGINAL** → operator should wait for May's trade count to settle (R2 Finding 1 — late ingestion — is the live concern here) before re-running.

### Other red-team observations (don't flip the headline)

- **The "linear trend" with r² = 0.250 is a leverage artifact.** Dropping the 3 earliest baseline months (2025-05 → 2025-07, pre-NT8-migration regime), r² collapses to 0.0001 — the slope was held up by 3-point leverage at the window start. The bumped `_PULL_MONTHS=13` may have introduced data from an operational regime that doesn't belong in the baseline.
- **Quadratic fit gives z=3.86; recent-6 baseline gives z=3.96.** All alternative models YIELD HIGHER z, so model misspecification alone doesn't help the operator — only the sample-size correction does.
- **Shapiro-Wilk p=0.97** — normality NOT falsified (kills the "non-normal residuals" concern).
- **DW=1.60, autocorr=0.20** — modest positive AR, not damaging alone.
- **Residual sign pattern shows weak run structure but no clean step.**

### What the headline should say (corrected)

The original draft of this report claimed `REGIME_REAL` and "R2 Finding 3 FORMALLY REFUTED." Both overstate. The corrected position:

- **The HALT itself still stands operationally** — PHANTOM-NT8 + reconciliation are independent gates that don't depend on this diagnostic.
- **R2 Finding 3 is partially refuted:** the linear-drift hypothesis fits poorly (r²=0.25, and 0.0001 after dropping leverage points). There is no clear gradual drift to attribute the HALT to. So "drift IS the cause" is not supported.
- **R2 Finding 3 is NOT fully refuted:** with the sample-size correction, the residual evidence places us in MARGINAL territory, not REGIME_REAL. The detrended diagnostic does not, by itself, REFUTE the drift hypothesis — it can only say the data doesn't fit a *linear* drift model AND that the latest residual is borderline-anomalous against the (under-traded) recent month.

A defensible variant of the detrended gate needs either:
1. **Sample-size-weighted OLS** with Welch-style standard error on the latest month, OR
2. **Refusal-to-compute when `latest_trade_count < 0.7 × baseline_median_trade_count`** (matching the spirit of the filtered variant's sparse-month rule applied to the LATEST month).

Filed as `FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING` for follow-up.

---

## §2 — TL;DR table (rev. 2)

| Run | gate variant | _PULL_MONTHS | baseline_n | z | extras | Verdict |
|---|---|---:|---:|---:|---|---|
| 2026-06-04 stock | `check_regime_stability` | 7 | 6 | **+3.69** | baseline_mean=0.035, std=0.025 | HALT |
| 2026-06-05 filtered (strict, floor=6) | `_with_filter` | 7 | 6 | **+3.71** | dropped=0 | HALT (structurally neutered) |
| 2026-06-05 filtered (relaxed, floor=4) | `_with_filter` | 7 | 6 | **+3.67** | dropped=0 | HALT (no sparse months) |
| 2026-06-05 detrended (raw) | `_detrended` | **13** | **12** | **+3.35** | slope=+0.00357/m, r²=0.250 | REGIME_REAL *(unweighted)* |
| **2026-06-05 detrended (red-team-corrected)** | sample-size-adjusted | 13 | 12 | **≈ +2.91** | latest_n_share = 826/1372 = 0.60 | **MARGINAL — operator wait** |

The raw detrended z (3.35) is mathematically correct under equal-variance assumption. Under the more honest unequal-variance accounting that the red-team applied, z lands in the MARGINAL band.

---

## §3 — What this sprint accomplished (kept from rev. 1)

1. **Bumped `_PULL_MONTHS` from 7 to 13** — resolves the structural-neutering red-team CRITICAL from the prior sprint (`FINDING-2026-06-05-ORACLE-FILTERED-GATE-FLOOR-METHODOLOGY`). Filtered variant now has statistical headroom; detrended variant has 12 baseline data points for OLS.
2. **Implemented `check_regime_stability_detrended`** in `analytics/regime_gate.py` (mathematically sound — Bug Hunter R2 verdict: SHIP). Linear OLS + residual z + r².
3. **Wired env-var opt-in + mutual exclusion** in `agents/strategy_oracle.py:_check_regime_gate`. `ORACLE_REGIME_GATE_DETREND=1` routes through the new variant; both `_FILTER` and `_DETREND` set → `ValueError`.
4. **Locked the contract with 13 new unit tests** (4 pull-window + 9 detrended; both `drift_artifact_scenario` and `sign` tests added per R3 Test Quality MEDIUM #1+#2 fixes).
5. **Fixed stale R1-flagged warning text** at `regime_gate.py:207` ("trailing 6 months" → "trailing 12 months").

Default `check_regime_stability` behavior preserved byte-identical (158/158 in targeted suite green per R1; 3471/0/15 in full suite).

---

## §4 — Per-month baseline panel + residuals (unweighted detrended run)

12 baseline months (oldest first), linear fit `y = 0.00815 + 0.00357 × month_index`:

| month_index | residual |
|---:|---:|
| 0 | +0.002 |
| 1 | −0.030 |
| 2 | −0.009 |
| 3 | +0.021 |
| 4 | +0.006 |
| 5 | +0.035 |
| 6 | +0.027 |
| 7 | −0.037 |
| 8 | −0.022 |
| 9 | −0.004 |
| 10 | +0.014 |
| 11 | −0.005 |

- residual_std (ddof=2): **0.0234**
- Largest absolute baseline residual: 0.037 (~ 1.58 σ)
- Latest residual: **+0.0784 (~ 3.35 σ unweighted; ≈ 2.91 σ with red-team's sample-size correction)**

**Red-team falsification math:** if half of the 0.0234 residual std comes from per-month sampling noise (i.e., between-month variance ≈ sampling variance), the latest month's variance has two components: between (same as baseline) + sampling (inflated by 1.661× because of fewer trades). Combined std ≈ 0.0270. z = 0.0784 / 0.0270 = 2.91.

---

## §5 — Operator recommendation (rev. 2)

1. **HALT stands.** R2 Finding 3 was partially tested but not fully refuted. The freeze-lift conversation does NOT advance on this diagnostic.
2. **Monitor May's trade count over the next week.** If late ingestion is real (R2 Finding 1), May's sharpe-proxy and trade-count both drift, and a re-run produces a clearer verdict. This is the literal operator action per the MARGINAL pre-decision rule.
3. **`_PULL_MONTHS=13` is the new default** (the structural fix shipped this sprint). Future Oracle runs use the full-year baseline. Both filtered + detrended variants gain statistical headroom.
4. **PHANTOM-NT8 remains the dominant production-safety blocker.** Guard A+B + Round 3 H3 shipped; operator can validate.
5. **Do NOT advance any `pending_changes.json` entry** through the Phase 4E approval flow while either PHANTOM-NT8 validation is open OR regime_gate is HALTING (currently both).
6. **Follow-up sprint candidate:** implement the sample-size-weighted detrended variant per `FINDING-2026-06-05-ORACLE-DETRENDED-SAMPLE-SIZE-WEIGHTING`. Either weighted OLS with Welch-style latest-month SE, or refusal-to-compute when latest is under-traded. Below the bar for blocking PHANTOM-NT8.

---

## §6 — What this diagnostic DOES and DOES NOT establish (rev. 2)

### Establishes
- **`_PULL_MONTHS=13` is a clean structural fix** for the prior sprint's floor-methodology finding. 22 original regime_gate tests still pass byte-identical default behavior.
- **The detrended gate is mathematically sound** (Bug Hunter R2 verdict: SHIP; 0 CRITICAL/HIGH; all edge-case paths covered).
- **The linear-drift hypothesis fits poorly** (r²=0.25; or 0.0001 after dropping pre-NT8-migration leverage months per red-team). There is NO clear gradual drift to attribute the HALT to.
- **The mutual exclusion** between FILTER and DETREND env vars works end-to-end; opt-in env vars default to OFF; existing default behavior preserved.

### Does NOT establish (corrected)
- **`REGIME_REAL` is overstated.** The unweighted z=3.35 ignores May's under-traded state; the red-team-corrected z ≈ 2.91 places the verdict in the **MARGINAL** band, not REGIME_REAL.
- **Step-function regime boundary is untested.** A step at the latest-month boundary would look exactly like the elevated residual we see.
- **Non-linear trends are untested.** Quadratic fit yields z=3.86; doesn't help.
- **May's under-traded state is the operative concern.** R2 Finding 1 applied to LATEST may be the actual signal.

---

## §7 — Cross-references

- 2026-06-04 stock Oracle HALT debrief: `logs/oracle/research/2026-06-04_debrief.md`
- 2026-06-05 filtered (strict): `logs/oracle/research/2026-06-05_r2_diagnostic_debrief.md`
- 2026-06-05 filtered (relaxed): `logs/oracle/research/2026-06-05_r2_diagnostic_relaxed_debrief.md`
- 2026-06-05 filtered side-by-side: `out/oracle_halt_vs_diagnostic_2026-06-05.md`
- **2026-06-05 detrended debrief: `logs/oracle/research/2026-06-05_r2_detrended_debrief.md`**
- **2026-06-05 detrended facts: `logs/oracle/research/2026-06-05_r2_detrended_facts.json`**
- Implementation: `analytics/regime_gate.py:check_regime_stability_detrended`
- `_PULL_MONTHS=13` constant: `analytics/regime_gate.py:_PULL_MONTHS`
- Oracle wiring + mutual exclusion: `agents/strategy_oracle.py:_check_regime_gate`
- Tests: `tests/test_regime_gate_detrended.py` (11 tests), `tests/test_regime_gate_pull_window.py` (4 tests)
- Subagent transcripts: R1 PASS `a0c2209bee595bd74`, R2 SHIP `abbdf1d1d08ee1e12`, R3 2 MEDIUM + missing-test (all addressed) `ae0e7b39d64de96bb`, R4 NO-CONFLICT `a7e1576a3db989902`, **P5.7 red-team CRITICAL `ac199c12f670e8999`**.
