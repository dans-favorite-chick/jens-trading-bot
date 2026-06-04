# Outline — Strategy Validation Buildout for `footprint_cvd_reversal`

_Follow-on outline from 2026-06-04 Footprint/CVD/DOM feasibility sprint._
_NOT a master prompt. Operator + Cowork will turn this into a proper sprint prompt._

## Hypothesis

`strategies/footprint_cvd_reversal.py` is 1,679 lines of fully-implemented 4-confluence (HTF level + CVD divergence + footprint confirmation + CVD compression → IQS scoring) reversal strategy. It was disabled 2026-05-21 (commit `b9a3b2e`) for PROCEDURAL reasons — "logs DATA_NOT_AVAILABLE 100% of the time" — not because backtests showed no edge. It has never been backtested because the 5y backtester can't replay volumetric data older than 2026-05-04.

**The new precondition that makes this testable:** operator already downloaded Databento TBBO footprint data for 2026-03-17→2026-05-17 (per commit `0b773aa`), and the live `logs/volumetric_history.jsonl` covers 2026-05-04→present. Total true-footprint coverage: ~80 unique days with ~14 days of overlap. That's enough to validate a strategy that fires ≥1/day at the Wilson n≥100 gate (Phoenix's standard promotion bar).

**Buildout hypothesis:** plumb `footprint_cvd_reversal` into a Databento-aware backtest harness, run the 80-day backtest, and either (a) validate to Wilson n≥100 + PF ≥ 1.5 and graduate to sim, or (b) confirm it has no edge in the available data — closing the question definitively.

## Files touched

- `tools/phoenix_real_backtest.py` — add `--databento-footprint` mode that loads `data/historical/databento_tbbo/mnq_footprint_5m.csv` into a backtester-friendly per-bar format
- `tools/replay_enrichment/recorded_cvd.py` — possibly extend to accept Databento data source as an alternate path (operator-friendly: same `RecordedCVDProvider` API)
- New: `tools/databento_to_volumetric.py` — convert Databento sparse-parquet → `logs/volumetric_history.jsonl`-compatible records, so existing `footprint_cvd_reversal._load_volumetric_history()` works as-is
- `config/strategies.py` — flip `footprint_cvd_reversal.enabled` to True ONLY IF the backtest passes Wilson n≥100 + PF ≥ 1.5 AND WFA-robust
- `tests/test_plan_winners_parity.py` — add `footprint_cvd_reversal` to `WINNERS_BEYOND_PLAN` per the b9a3b2e kill-commit's re-enable protocol

## Protection status

- `config/strategies.py` — `enabled`/`validated`/`walk_forward_gate` flips are PROTECTED. Flipping `enabled=True` requires operator sign-off + the audit-trail commit message format.
- `tools/*.py` — unprotected; safe to edit
- `tests/test_plan_winners_parity.py` — unprotected; safe to edit
- New files in `tools/` — unprotected

## Operator approval needed

YES, AT TWO POINTS:
1. Before plumbing Databento footprint into the backtester (because the harness change affects every future backtest run — must not silently change historical numbers for OTHER strategies' 5y results).
2. Before flipping `footprint_cvd_reversal.enabled = True` in `config/strategies.py` (per b9a3b2e protocol and CLAUDE.md protected-zone rule).

## Effort estimate

M (~1–1.5 weeks):
- 2 days: write `tools/databento_to_volumetric.py` + schema conversion + golden tests
- 2 days: extend `phoenix_real_backtest.py` for Databento footprint mode; validate on the 14-day overlap window vs live `logs/volumetric_history.jsonl` (sanity check)
- 1 day: run footprint_cvd_reversal backtest over 80-day Databento + live window
- 1 day: per-year breakout, WFA window run, Wilson-CI compute
- 1 day: write up verdict; if positive, operator sign-off and sim-deploy
- 0.5 day: dashboard updates, halt-signature verify, kill-switch test

If verdict is "no edge on this data": ~0.5 day to file as `FOOTPRINT-STRATEGY-DEAD-END` and close.

## Dependencies

- Conditional on the `filter_integration.md` outline being deferred or running on a separate branch — both outlines plumb Databento footprint into the backtester and would collide.
- Conditional on operator confirming the Databento data file is intact (`data/historical/databento_tbbo/mnq_footprint_5m.csv` was created 2026-05-18, has not been touched since per `git log`).
- NO dependency on the parallel remediation, confluence, or forensics sprints — this sprint reads stable on-disk data and writes only to tools/ + tests/ + (with operator sign-off) config.

## Out of scope for this outline

- Adding footprint analysis to OTHER existing strategies — that's `filter_integration.md`.
- Building new footprint-derived strategies from scratch — premature optimization. First test what's already built.
- DOM-depth history persistence — orthogonal task; not required for this strategy (it consumes volumetric_history only).

## Negative case fallback

If the 80-day backtest of `footprint_cvd_reversal` produces:
- n_trades < 100 (insufficient signal volume) → strategy is data-impractical at current parameters. Either tune `entry_threshold_iqs` down (more signals) and re-test, OR accept and close.
- PF < 1.3 over n ≥ 100 → strategy has no edge in the available 80-day window. Close as FOOTPRINT-PRIMARY-DEAD-END and pivot exclusively to `filter_integration.md`.
- WR < 40% on a reversal strategy → suspicious; may indicate the IQS scoring is mis-calibrated for current market regime (high VIX changes the optimal IQS threshold).

Either way, the experiment IS the answer — currently we have neither validation nor disproof.
