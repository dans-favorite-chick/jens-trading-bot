# Ad-hoc Winning Conditions sprint artifacts — 2026-06-04

**Status:** SUPERSEDED.

**Canonical replacement:** `logs/oracle/research/2026-06-04_*.{md,json,jsonl}` produced by `agents/strategy_oracle.py` research mode.

## Why this folder exists

The 2026-06-04 Winning Conditions sprint was originally executed via ad-hoc analysis scripts in `C:\tmp\winning_conditions\` rather than through the canonical Phoenix Strategy Oracle (`agents/strategy_oracle.py`, built 2026-06-02). After the operator flagged the miss, the sprint was re-run through Oracle. These artifacts are retained for audit-trail purposes.

## What's here

| Path | Contents |
|---|---|
| `*.md` (top-level) | Original ad-hoc sprint reports (data / stats / counterfactual / regimes / consolidated) |
| `scripts/` | The ad-hoc Python drivers, intermediate CSVs, dataset parquet, the Oracle invocation driver |
| `charts/` | Original 10 PNG charts (top-5 wins + top-5 losses for bias_momentum) |
| `oracle_run_summary.json` | Result dict from the 2026-06-04 Oracle research-mode invocation |

## What the ad-hoc sprint found vs Oracle

The ad-hoc analysis converged on the operationally-correct conclusion ("the post-Apr-18 tightening is doing its job; the counterfactual at relaxed thresholds loses money"), but it lacked:

- **Regime stability gate** — the ad-hoc sprint analyzed the 90-day window without checking whether that window's sharpe-proxy was an outlier vs the trailing baseline.
- **Verifier** — there was no automated cross-check that narrative claims matched the deterministic facts panel.
- **Standardized `pending_changes.json`** for the downstream Phase 4E human-approval flow.
- **Audit.jsonl** of every analytical decision in canonical form.

Oracle's regime_gate HALTED the run (`z_score = +3.69`, threshold ±3.00; latest-month sharpe-proxy 0.129 vs 6-month baseline mean 0.035). The HALT is itself the deliverable: parameter tuning on a +3.69σ outlier month is the classic overfitting trap. The 5-year warehouse facts confirm both scoped strategies (`bias_momentum`, `opening_session`) pass every statistical gate over the long window — they don't need rescue.

## Why these scripts may still inform future work

The ad-hoc scripts implemented:
- A clean trade-memory filter that excludes `NO_FILL` records, `RECONCILED` records (both status-tagged and trade-id-prefixed `RECONCILED_*`), and `source: 'manual_reconciled'` records. This filter logic could feed into Oracle's `prepared_queries` as a SQL view if helpful.
- Per-regime expectancy tabulation on live closed trades (separate from the warehouse backtest universe).
- Top-N R-multiple ranking with sub-tick-stop artifact filtering (a real bug surface that the visual charts exposed during red-team review).
- A footprint+CVD 3-panel chart generator with an int64 cast fix for the uint32 size column overflow.

If a future Oracle prepared_query needs any of these, the scripts are here.
