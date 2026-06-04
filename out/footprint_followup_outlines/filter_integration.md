# Outline — Filter Integration

_Follow-on outline from 2026-06-04 Footprint/CVD/DOM feasibility sprint._
_NOT a master prompt. Operator + Cowork will turn this into a proper sprint prompt._

## Hypothesis

Phase 3 of the feasibility sprint found `delta_aligned_ratio_5m` Bonferroni-significant (p_bonf=0.0004) as a WIN/LOSS discriminator across 397 trades, with the **inverted-from-intuition direction**: WINS averaged 31% delta-alignment, LOSSES averaged 45%. Two near-miss features (`price_vs_vwap_aligned`, `dom_heavy_aligned`) showed the same inverted pattern. The convergent pattern suggests Phoenix's strategy stack systematically wins when entering AGAINST recent order flow — a reversal-strategy fingerprint.

Phase 4 showed the strongest single-feature filter is within Monte Carlo p5–p95 of random rejection at the operating sample sizes (37–149 trades per strategy). The signal is real (Phase 3); the lift over random is not yet provable at current N (Phase 4).

**Filter integration hypothesis:** add a per-strategy, INVERTED-direction filter (reject when the candidate's local order flow is already "agreeing" with the entry too strongly) using a strategy-specific threshold, and validate it on the Databento 60-day backfill window for out-of-sample lift.

## Files touched

- `core/cvd_trend_health.py` — could host the new `assess_inverted_alignment()` method
- `strategies/bias_momentum.py` — first candidate (largest N, validated, in live canary)
- `strategies/base_strategy.py` — optional shared helper if the filter generalizes
- New: `core/footprint_filter.py` — pure-function module containing the filter logic, so it can be unit-tested independently and replayed against historical data without bot-state side effects
- New: `tests/test_footprint_filter.py` — golden tests against `out/footprint_pattern_analysis_2026-06-04.md` numbers

## Protection status

`strategies/bias_momentum.py` — NOT in protected zone, safe to edit
`core/cvd_trend_health.py` — NOT in protected zone, safe to edit
`config/strategies.py` — only protected for `validated`, `enabled`, `walk_forward_gate`, freeze flag. Adding a new `footprint_filter_enabled` key + threshold to the bias_momentum block is unprotected.

## Operator approval needed

YES — this is a live-canary-strategy edit. Must follow the protocol in CLAUDE.md "Protocol when a change is needed": propose diff in chat, wait for explicit go-ahead, ship + run full pytest suite, commit message with `OPERATOR-APPROVED: 2026-MM-DD` line.

## Effort estimate

S–M (~3–5 days):
- 1 day: write `core/footprint_filter.py` + golden tests against this sprint's numbers
- 1 day: wire into bias_momentum behind feature-flag gate (default OFF), reconciliation harness verify
- 1 day: Databento backfill replay — extend `tools/phoenix_real_backtest.py` to optionally consume per-bar footprint from `data/historical/databento_tbbo/mnq_footprint_5m.csv` for the 2026-03-17→2026-05-17 window
- 1 day: validate filter against backfill, generate the OOS Phase-3-style table
- 0.5 day: docs, kill-switch verification, operator sign-off prep

## Dependencies

- This outline is conditional on operator agreeing the Phase 3 inverted-direction finding is interesting enough to validate. If operator reads the report and says "interesting but not now," this outline does not graduate to a sprint.
- Conditional on the Databento backfill data being load-able by the existing 5y backtest harness (Phase 5 of the feasibility report flags this as ~1 day of plumbing).
- Should NOT run in parallel with `bias_momentum` parameter changes or live-canary status flips.

## Out of scope for this outline

- Re-enabling `footprint_cvd_reversal` as a primary-signal strategy — that's a separate question (`new_strategy_buildout.md` outline).
- DOM-history persistence — separate engineering task; covered briefly in `data_collection_uplift.md`.
- Multi-feature/ML model — if a single-feature filter doesn't show OOS lift, abandon, don't escalate to ensembles. YAGNI.
