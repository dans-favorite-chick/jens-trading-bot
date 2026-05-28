# Backtester De-Stub + Replay Fidelity — bias_momentum

**Date:** 2026-05-28 · **Author:** Claude Code session · **Freeze status:** `FREEZE_ACTIVE = True` (UNCHANGED)

## TL;DR

We de-stubbed the backtester's strategy-branching enrichment (`cvd_health`,
`es_nq_rs`, `day_type`, `cr_verdict`) using recorded data, and built a fidelity
harness to compare it against the live prod eval-log ground truth — to get a
defensible `bias_momentum` divergence number **without waiting for 30 new live
trades**. The tooling works. The honest verdict: **the backtest is NOT yet a
trustworthy live proxy for `bias_momentum`, and a clean validation is not
possible on the data we have.** This argues for keeping the freeze.

This measures STRATEGY-LOGIC + ENRICHMENT fidelity (does the backtest see/decide
like live). It does NOT measure execution fidelity (fills, latency, slippage) —
that still needs real live trades.

## What was built (research/backtest tooling only — no live-path changes)

- `tools/replay_enrichment/recorded_cvd.py` — real `cvd_health` from recorded
  order-flow delta (`logs/volumetric_history.jsonl`), via the live `CVDTrendHealth`.
- `tools/replay_enrichment/recorded_es_nq_rs.py` — `es_nq_rs` from MNQ(=NQ)/MES(=ES)
  5m bars, replicating `core/market_intel.get_nq_es_relative_strength`.
- `tools/replay_enrichment/recorded_day_cr.py` — real `day_type` + `cr_verdict`
  via the live core modules, with an `isolated_momentum_file()` mechanism so the
  cross-session momentum trajectory is fed chronologically (not read from stale
  shared state) — never touches production `data/momentum_scores.json`.
- `tools/phoenix_real_backtest.py` — `CSVEnrichmentPipeline.enable_real_enrichment()`
  + `_apply_real_enrichment()`: opt-in de-stub, safe fallback to the old stub
  where recorded coverage is absent. Default path unchanged.
- `tools/reconcile_sim_vs_backtest.py` — `--real-enrichment` flag (builds the
  CVD provider once, shares it across per-trade pipelines).
- `tools/replay_enrichment/fidelity_vs_eval_logs.py` — the comparison harness.
- Unit tests: 25 across the three modules, all green.

## De-stub verification (window 2026-05-12 → 05-14)

| Field | Result | Note |
|---|---|---|
| `es_nq_rs` | **98.9%** populated | misses only first ~30 min (need 7 bars) |
| `cvd_health` | **52.8%** real `recorded_delta` | rest correctly falls back to `bar_approx` where volumetric coverage has gaps |
| `day_type` | de-stubbed | now VOLATILE/RANGE, no longer hard `BALANCED` |
| `cr_verdict` | root-caused + fixed | replay was reading one frozen EOD momentum value; fixed via isolated trajectory feeding |

## Fidelity vs live prod eval log (the divergence number)

| Date | matched min | bias_mom decision agreement | both-signal | live-only | **bt-only (over-fire)** | live cr_verdict |
|---|---|---|---|---|---|---|
| 2026-05-13 | 464 | 86.6% | 0 | 0 | **62** | UNKNOWN (100%) |
| 2026-05-14 | 308 | 85.1% | 0 | 0 | **46** | UNKNOWN (100%) |

The "agreement" is entirely driven by both sides agreeing on *no signal*. The
backtest produced **46–62 entry signals per day that live produced zero of** —
a one-sided over-fire. Caveats: the harness calls `evaluate()` ungated, while
live applies additional prod gates/allowlist/risk that suppress entries; and
`rsi`/`macd`/`dom` remain stubbed in the backtester. So this is indicative of
over-firing, not yet a within-tolerance reconciliation.

## Why a clean bias_momentum validation is impossible on current data

1. **The data windows don't overlap.** Live `cr_verdict` was silently broken
   (`UNKNOWN`) for the *entire* CSV-covered period — the B2-3 bug
   (`_strategy_dispatch.py:353-358`), introduced by the 2026-05-06 Sprint-J
   MenthorQ cleanup and not fixed until **2026-05-25**. The databento CSVs end
   **~2026-05-15**. So there is **no date** with both working-live-CR and
   backtest data. Reconstructed `cr_verdict` (CONTESTED/etc.) vs live (`UNKNOWN`)
   is 0% by construction — the live value was a bug, not ground truth.
2. **Reconcile is still BLOCKED on existing sim trades.** Re-running
   `reconcile_sim_vs_backtest.py --real-enrichment` over the 61 sim
   `bias_momentum` trades in 2026-05-04→05-15 returns **12/12 (and 220/220)
   BLOCKED** — because BLOCKED depends on the *sim record* missing the 4 fields,
   which de-stubbing the *backtester replay* cannot fix. Only NEW sim trades
   (recorded after the 2026-05-28 sim field-persistence fix) will carry them.

## Forward path to a defensible PASS

1. Run the now-fixed sim bot (post-2026-05-28) so `bias_momentum` sim trades
   record with all 4 fields.
2. Obtain databento MNQ/MES coverage for the **post-2026-05-25** period (working
   live CR) so the backtest window overlaps good live ground truth.
3. Re-run `reconcile_sim_vs_backtest.py --real-enrichment` → real-vs-real. Only
   if REPLAYED trades land within tolerance (entry 60s / 2 ticks) AND blocked=0
   does the freeze-lift criterion hold — then operator sign-off.

Until then: **keep the freeze.** The de-stub is now ready so that, when the data
exists, the reconciliation is meaningful rather than real-vs-stub.
