# Freeze-Lift Plan — Monday 2026-06-08 Execution Target

_Phase H deliverable from the 2026-06-01 → 2026-06-02 overnight master run._
_No code changes tonight; this is the design document the operator
reviews before Monday execution._

## 1. Why the freeze exists

`config/strategies.py:11-52` records the rationale verbatim. Summary:

The 2026-05-24 synthesis audit (`docs/audits/SYNTHESIS_2026-05-24.md`)
asked whether anyone had ever reconciled `sim_bot` live-paper output
against the Phase 13 5-year backtest, with a defensible per-strategy
divergence number. The answer was **no**. Without that
reconciliation, every backtest-derived verdict (kill list, validated
promotions, parameter retunes, sizing prep) is conjecture: the
backtest and the live bot might diverge in ways we haven't measured,
and the divergence could erase or invert the backtest's apparent edge.

`FREEZE_ACTIVE: bool = True` is therefore a project-wide invariant
that blocks any change whose justification reduces to "the backtest
says so" until a reconciliation harness produces a defensible
divergence number for at least one strategy (the canary,
`bias_momentum`).

## 2. Lift criteria

Per the freeze block (`config/strategies.py:40-46`) and `CLAUDE.md`:

1. **Reconciliation harness PASSES** for `bias_momentum` over a fresh
   window (at least 10 sim-trading days).
2. **`out/reconciliation_2026-06-08_bias_momentum.md` is committed** to
   the branch with full pre- and post-comparison numbers vs the
   tolerances defined below.
3. **Operator sign-off on the divergence numbers.** Specifically:
   a verbal/written confirmation that the measured divergence is
   acceptable for proceeding from sim to live, given the canary blast
   radius (`LIVE_STRATEGY_ALLOWLIST=("bias_momentum",)`).
4. **`FREEZE_ACTIVE` flips to False** in `config/strategies.py`. This is
   a PROTECTED-FILE edit per `.claude/PROTECTED_FILES.md`; the commit
   message MUST carry `OPERATOR-APPROVED: 2026-06-08` on its own line.

## 3. Reconciliation harness — `tools/reconciliation_harness.py`

New file. CLI signature:

```
python tools/reconciliation_harness.py \
    --strategy bias_momentum \
    --start 2026-06-02 --end 2026-06-08 \
    --out out/reconciliation_2026-06-08_bias_momentum.md
```

### 3.1 Inputs
- `--strategy` — strategy name (must exist in `config.strategies.STRATEGIES`).
- `--start`, `--end` — ISO dates bounding the comparison window.
- `--out` — output report path. Defaults to
  `out/reconciliation_<end_date>_<strategy>.md`.
- `--bot-tier` (optional) — `sim` (default) or `prod`. Picks which
  `trade_memory_<tier>.json` to read.

### 3.2 Steps

**Step 1: pull sim trades.**
Use `core.trade_memory.load_all_trades()` per `CLAUDE.md`. Filter by
strategy and the entry_ts window. NEVER raw-open `trade_memory.json`.

**Step 2: re-run the canonical backtest over the same window.**
Use `tools/phoenix_real_backtest.py` (the post-7de51ee harness with
Fix B's check_exit wiring shipped tonight). Pass the same start/end
dates and `--strategies <strategy>`. Capture the resulting
`phoenix_real_trades.csv` for that strategy.

**Step 3: match trades.**
Pair sim trades to backtest trades by `entry_ts` with a ±60-second
tolerance. Outputs a list of MATCHED pairs and UNMATCHED rows on
either side.

**Step 4: compute divergence metrics.**
For the MATCHED pairs:
- `trade_count_delta_pct = abs(n_sim - n_bt) / max(n_sim, n_bt)`
- `matched_pnl_delta_pct` — for each pair, `abs(sim_pnl - bt_pnl) / max(abs(sim_pnl), abs(bt_pnl))`, then aggregate (mean + p95)
- `entry_price_delta_ticks` — `abs(sim_entry - bt_entry) / TICK_SIZE`; report mean + p95
- `exit_price_delta_ticks` — same shape for exit
- `hold_time_delta_seconds` — `abs(sim_hold - bt_hold)`; mean + p95

For the UNMATCHED rows:
- `unmatched_trade_pct = unmatched_count / total`

**Step 5: tolerance comparison.**

| Metric | Proposed tolerance |
|---|---|
| `trade_count_delta_pct` | ≤ 5% |
| `matched_pnl_delta_pct` (mean) | ≤ 10% |
| `unmatched_trade_pct` | ≤ 5% |
| `entry_price_delta_ticks` (mean) | ≤ 1 tick |
| `exit_price_delta_ticks` (mean) | ≤ 2 ticks |
| `hold_time_delta_seconds` (mean) | ≤ 60s (1 bar) |

These are starting proposals. The operator should treat them as a
strawman and tune per their tolerance for live risk.

**Step 6: emit the markdown report.**
`out/reconciliation_<date>_<strategy>.md` contains:
- Window dates + strategy + n_sim + n_bt
- Per-metric measured vs tolerance table with PASS/FAIL flags
- Overall verdict: PASS (all metrics within tolerance) / FAIL
- Top-3 worst-divergence trade pairs (sim_entry_ts / bt_entry_ts /
  sim_pnl / bt_pnl / diff)
- Top-3 unmatched-on-each-side trades (with hypothesis: timing skew?
  ATR latency? entry_price oracle? out-of-session?)

## 4. Tests required

New file: `tests/test_reconciliation_harness.py`. Cases:

1. **Matching by entry_ts within tolerance.** Two synthetic trade lists
   with timestamps ±30s apart; assert all pairs match. Then ±90s
   apart; assert none match.
2. **Divergence math correctness.** Hand-crafted matched pair with
   known PnL/entry/exit deltas; assert the computed metrics match the
   hand math to 3 decimals.
3. **Tolerance comparison.** Build a metric dict at the boundaries
   (exact tolerance, +epsilon, -epsilon); assert PASS/FAIL flips at
   the right threshold.
4. **Unmatched handling.** Sim has 10 trades, backtest has 6 of them;
   assert `unmatched_trade_pct = 4/16 = 25%` (counting across both
   sides) and that the report flags the 4 sim-only trades.
5. **End-to-end smoke.** Fixture sim + backtest CSVs; full pipeline
   producing the markdown report file; assert the file exists, the
   table header is present, and the verdict line matches expectation.

## 5. Estimated effort

| Phase | Hours |
|---|---|
| Harness implementation | 4-6 |
| Tests (5 cases) | 2-3 |
| Validation on bias_momentum over a fresh sim window | 2-4 |
| Operator review of divergence numbers | 1-2 |
| **Total** | **~1.5 days** |

## 6. Open questions for operator

1. **Are the proposed tolerances right?** Especially `matched_pnl_delta_pct
   ≤ 10%` — that's the metric most likely to fail given that sim
   uses live ticks vs backtest uses 1m OHLC. A wider tolerance (15-20%)
   may be necessary for the first pass.
2. **Pass criterion**: should ALL metrics be within tolerance, or N-of-M?
   E.g., is it acceptable if entry_price_delta is 1.5 ticks but
   everything else is clean?
3. **Cadence**: should the harness run automatically (e.g., nightly via
   `tools/loop.py`) once it's working, or stay a manual tool the operator
   invokes before promotion decisions?
4. **What if the window is sparse?** If sim only produced 3 bias_momentum
   trades in the window, is that enough to PASS the reconciliation? Or
   do we require a minimum N (e.g., 20 trades)?

## 7. Acceptance criteria

A successful reconciliation pass produces:
1. `out/reconciliation_2026-06-08_bias_momentum.md` with verdict PASS.
2. A commit that updates `config/strategies.py:FREEZE_ACTIVE = False`
   with a comment naming this report path and an
   `OPERATOR-APPROVED: 2026-06-08` line.
3. Optional: `tests/test_freeze_interlock.py` updated to reflect the
   lifted state (or kept as-is to assert the constant exists at all).

## 8. Execution gate

**Monday 2026-06-08 morning** if operator approves this design.
Until approval, no code lands. The harness module + tests + the freeze
flip are queued together as one workstream.

## 9. Out-of-scope (for this design)

- Multi-strategy reconciliation. Bias_momentum first; widen later.
- Live (real-money) reconciliation. The current LIVE_STRATEGY_ALLOWLIST
  has only bias_momentum, and `LIVE_TRADING=False`. Once freeze lifts +
  bias_momentum goes live, a separate live-vs-sim reconciliation
  becomes its own workstream (probably the 5-day canary monitoring loop).
- Tooling for tolerance auto-tuning. The first pass uses operator-set
  tolerances; learning them from observed sim variance is a Phase II item.
