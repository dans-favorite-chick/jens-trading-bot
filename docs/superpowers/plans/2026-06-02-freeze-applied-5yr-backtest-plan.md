# Freeze-Friction Applied 5-Year Backtest — Operational Plan

**Date:** 2026-06-02
**Operator:** Jennifer
**Goal:** Produce a fresh 5-year backtest of Phoenix using the CURRENT frozen config (all post-master-fix disables, tunings, and gates intact) as independent evidence supporting the eventual freeze lift.
**Executor:** Claude Code
**Adjacent docs:**
- `docs/superpowers/plans/2026-06-08-freeze-lift-plan.md` — the reconciliation-harness workstream that is the *actual* lift criterion
- `docs/FRESH_WFA_PLAN_2026-06-01.md` — the prior 2-yr WFA tactical refresh
- `logs/oracle/research/2026-06-01_master_fix_report.md` — the post-fix codebase state

---

## 1. Why this plan, in one paragraph

The freeze (`config/strategies.py::FREEZE_ACTIVE = True`) blocks any decision whose only justification is "the backtest says so" until the reconciliation harness produces a defensible sim-vs-backtest divergence number for `bias_momentum`. That gate is Monday 2026-06-08's workstream. **This plan does NOT bypass the freeze** — it runs alongside it, producing the *other* leg of the evidence package: a clean 5-year backtest of the frozen state. When Monday's reconciliation PASSES and the operator signs off, the freeze flip happens with two pieces of evidence in hand: (a) sim-vs-backtest fidelity on the canary strategy, and (b) a 5-yr history that confirms the frozen config produces sensible numbers across regimes. Either alone is suggestive; both together is decisive.

---

## 2. "Freeze friction applied" — what that means here

- **No grid sweeps.** No parameter tuning. No re-enabling disabled strategies. We run exactly the config Phoenix is in TODAY, period.
- **No kill/promote decisions emerge from this run.** Every output is *measurement only*. The freeze block in `config/strategies.py:11-52` still owns those decisions.
- **The Oracle is not invoked.** This is pure backtest math; no LLM narration, no proposal generation.
- **Disabled strategies are still measured** (so we know what the frozen state would look like at year 1, 2, 3, 4, 5), but their disabled status is preserved in the output ("DISABLED — for reference only").

This shape is deliberately conservative. The point is that when the operator decides to lift the freeze, they can point to this report as evidence the frozen config is sane over 5 years — without anyone arguing that the act of running the backtest itself re-opened decisions the freeze was protecting.

---

## 3. Pre-flight checklist (operator, before kickoff)

- [ ] `prod_bot` is OFF (or paused) for the run duration
- [ ] `sim_bot` is OFF
- [ ] NT8 stale OIFs from 2026-06-02 incident cleared (per the bug-sweep checklist)
- [ ] Disk headroom in `backtest_results/` ≥ 500 MB
- [ ] No active warehouse ingest job running
- [ ] Confirmed today's date is post-`dc02808` (the most recent shipped commit on `weekly-evolution/2026-05-24`)

---

## 4. Data availability — already confirmed

Verified 2026-06-02:

| Source | Path | Span | Status |
|---|---|---|---|
| MNQ 1-minute bars | `data/historical/mnq_1min_databento.csv` | 2021 → 2026 | ✅ Present |
| MNQ 5-minute bars | `data/historical/mnq_5min_databento.csv` | 2021 → 2026 | ✅ Present |
| MES 1-minute bars | `data/historical/mes_1min_databento.csv` | 2021 → 2026 | ✅ Present |
| MES 5-minute bars | `data/historical/mes_5min_databento.csv` | 2021 → 2026 | ✅ Present |
| Market-state per day | `data/warehouse/phoenix.duckdb::market_state_per_day` | 5-yr backfill (354,270 rows per master-fix report Phase 8) | ✅ Present |

**Not available** (and not needed for this plan):
- Tick-level data older than 60 days. The `databento_tbbo` archive spans 2026-03-17 → 2026-05-15 only. Tick replay is out of scope; bar-level is sufficient for the freeze evidence package.

---

## 5. Strategy scope

All 25 strategies from `config/strategies.py::STRATEGIES`. The 12 currently `enabled: True` are the load-bearing measurements; the 13 `enabled: False` are run for reference (so future operators can see what disabling cost or saved).

The harness already supports this — `tools/phoenix_real_backtest.py --all` ignores the `enabled` flag and runs every strategy with a testable enrichment path. Strategies in the "Cannot test" category from the harness docstring (`footprint_cvd_reversal`, `nq_lsr`) are skipped automatically.

Output rows carry a `was_enabled_at_run_time` boolean (NEW column, see Phase 3) so the verdict slicing distinguishes the two groups.

---

## 6. Phases

### Phase 0 — Pre-run audit (10 min)

Confirm working tree is clean (no uncommitted edits to `strategies/`, `core/`, `config/`):

```bash
git diff --name-only HEAD -- strategies/ core/ config/
```

If any files come back: STOP and surface to operator. The freeze-friction-applied semantics require the run to use HEAD, not the working tree.

Confirm `FREEZE_ACTIVE = True` is still in place:

```bash
python -c "from config.strategies import FREEZE_ACTIVE; assert FREEZE_ACTIVE is True; print('FREEZE_ACTIVE = True — confirmed')"
```

If `False`: STOP. This plan only runs against a frozen config.

### Phase 1 — Snapshot existing 5yr CSV (5 min)

Archive the most recent `phoenix_real_5year.csv` to dated subdir so we can do a pre/post delta:

```bash
mkdir -p backtest_results/_pre_freeze_friction_2026-06-02/
cp backtest_results/phoenix_real_5year.csv \
   backtest_results/_pre_freeze_friction_2026-06-02/
```

This preserves the 2026-05-19 snapshot for comparison.

### Phase 2 — Smoke test (20 min)

Single-strategy single-year smoke to validate the harness still works post all the master-fix commits:

```bash
python tools/phoenix_real_backtest.py \
    --strategies bias_momentum \
    --start 2025-06-01 --end 2026-06-01 \
    --out backtest_results/_smoke_2026-06-02.csv \
    2>&1 | tee logs/wfa/smoke_bias_momentum_5yr_2026-06-02.log
```

PASS criteria:
- Run completes without error
- `bias_momentum` produces trades (n > 50 expected for a full year)
- Mean PnL per trade is non-zero (rules out harness contamination regression)
- Run time < 30 minutes on the current machine

If smoke fails: STOP, surface the traceback, do not proceed.

### Phase 3 — Full 5yr run (4-8 hours)

```bash
python tools/phoenix_real_backtest.py \
    --all \
    --start 2021-06-01 --end 2026-06-01 \
    --out backtest_results/phoenix_real_5year_2026-06-02.csv \
    --include-disabled \
    2>&1 | tee logs/wfa/phoenix_real_5year_2026-06-02.log
```

If `--include-disabled` flag does not exist in the harness today, **STOP and ask operator** rather than synthesizing one. (It almost certainly doesn't — the harness was previously written assuming `--all` only ran enabled strategies. Adding it is a one-line patch in `tools/phoenix_real_backtest.py` to the strategy iteration block; non-protected file, safe edit. But ask first.)

Output: per-trade CSV at `backtest_results/phoenix_real_5year_2026-06-02.csv`.

Expected: ~75,000-100,000 trades total across 25 strategies over 5 years. File size ~10 MB.

### Phase 4 — Build the report (45 min)

Write `tools/freeze_friction_5yr_report.py` (NEW file, non-protected, safe to add). Inputs: the Phase 3 CSV. Outputs:

1. `logs/oracle/research/2026-06-02_5yr_freeze_friction_report.md` — narrative report.
2. `backtest_results/phoenix_real_5year_2026-06-02_summary.csv` — per-strategy aggregates.

Per-strategy aggregates include:
- `n_trades_5yr`, `n_trades_per_year` (rough avg)
- `win_rate`, `profit_factor`, `expectancy_per_trade`
- `max_drawdown_dollars`, `max_drawdown_duration_days`
- `wilson_95_ci_lower_wr`, `wilson_95_ci_upper_wr` (per CLAUDE.md tier reference)
- `total_pnl_per_year` (2021, 2022, 2023, 2024, 2025, 2026)
- `was_enabled_at_run_time`
- `tier` (INSUFFICIENT_SAMPLE / PRELIMINARY / TENTATIVE / VALIDATED / HIGH_CONFIDENCE per CLAUDE.md)

Per-state slice (uses `market_state_per_day` from the warehouse):
- For each strategy × market_state: n_trades, PF, total_pnl
- Flag strategies where any single state contains > 70% of total PnL (regime-fragile)

### Phase 5 — Delta vs the pre-master-fix baseline (30 min)

Add to the same report (Phase 4 script):

For each strategy, compare:
- 2026-05-19 snapshot (`backtest_results/_pre_freeze_friction_2026-06-02/phoenix_real_5year.csv`) — pre-harness-fix
- 2026-06-02 fresh run — post-master-fix

Delta columns: `wr_delta`, `pf_delta`, `n_trades_delta`, `mean_pnl_delta`.

Goal: confirm the master-fix run actually changed numbers in the direction the master-fix report predicted (e.g., previously-contaminated strategies now show non-zero PnL). If a strategy is unchanged, that's a flag — was the fix actually applied?

### Phase 6 — Freeze-lift readiness verdict (15 min)

Append to the report a section "Freeze-lift readiness — 5yr leg." Three-state verdict:

- **GREEN** — All enabled strategies clear PRELIMINARY tier (≥30 trades) AND profitable over 5yr AND no single-state PnL concentration > 70%. The frozen config holds up across regimes; safe to lift once the reconciliation harness PASSES.
- **YELLOW** — Most enabled strategies clear but 1-2 are regime-fragile or under-sampled. Lift is conditional on operator's review of the specific outliers.
- **RED** — Material problems (drawdowns exceeding loss caps, strategies losing money over 5yr, regime concentration > 90%). Do NOT lift on this evidence; investigate before Monday.

This verdict is NOT a freeze-lift action. The actual lift requires the reconciliation harness PASS per `2026-06-08-freeze-lift-plan.md`. This plan supplies the second leg of evidence.

---

## 7. Time budget

| Phase | Estimate |
|---|---|
| 0 — Pre-run audit | 10 min |
| 1 — Snapshot existing CSV | 5 min |
| 2 — Smoke test | 20 min |
| 3 — Full 5yr run | **4-8 hours** |
| 4 — Build report | 45 min |
| 5 — Delta vs baseline | 30 min |
| 6 — Readiness verdict | 15 min |
| **Total wall clock** | **~6-10 hours** |

Natural window: overnight Friday → Saturday, or any weekend window where both bots are off.

---

## 8. Files this plan touches

| File | Action | Protected? |
|---|---|---|
| `tools/phoenix_real_backtest.py` | POSSIBLY MODIFIED — `--include-disabled` flag (operator approval first) | No |
| `tools/freeze_friction_5yr_report.py` | NEW | No |
| `backtest_results/phoenix_real_5year_2026-06-02.csv` | NEW | No |
| `backtest_results/_pre_freeze_friction_2026-06-02/` | NEW (snapshot dir) | No |
| `backtest_results/phoenix_real_5year_2026-06-02_summary.csv` | NEW | No |
| `logs/oracle/research/2026-06-02_5yr_freeze_friction_report.md` | NEW | No |
| `logs/wfa/*` | NEW logs | No |
| `config/strategies.py` | UNCHANGED — freeze stays | YES (untouched) |
| `config/settings.py` | UNCHANGED | YES (untouched) |
| `strategies/*.py` | UNCHANGED | Mostly no (but untouched here anyway) |
| `core/*` | UNCHANGED | YES (untouched) |

**Zero protected-file edits.** This is pure measurement infrastructure.

---

## 9. Operator decisions (LOCKED 2026-06-02)

1. **Disabled strategies:** **EXCLUDED.** "Disabled need to be filed away and not touched." Only the 12 currently `enabled: True` strategies get measured. The `--include-disabled` flag is therefore NOT needed and the harness is run without it.
2. **Readiness verdict:** **AUTO-EMIT.** The GREEN / YELLOW / RED rubric from §6 fires automatically at the end of Phase 6. Operator reviews the narrative afterward; the label is mechanical.
3. **Volatility-regime slice (added 2026-06-02):** Every per-strategy table in the report is sliced by `market_state_per_day` (the 354,270-row backfill from master-fix Phase 8). A strategy with strong aggregate PnL but >70% concentration in a single state is flagged as regime-fragile.
4. **Run timing & Phase 5 delta-anomaly handling:** Operator decides at kickoff. Defaults: overnight window, surface anomalies as findings (no auto-investigate).

---

## 10. What this plan does NOT do

- Does NOT lift the freeze. (That's Monday 2026-06-08 + reconciliation PASS.)
- Does NOT touch protected files.
- Does NOT modify strategy logic or numerical parameters.
- Does NOT invoke the Oracle. No LLM, no proposals, no kill-list edits.
- Does NOT auto-promote or auto-disable strategies.
- Does NOT replace the reconciliation harness — it complements it.

---

## 11. Self-second-guess

- **Strongest risk:** Phase 3 takes longer than 8 hours on a sluggish machine, and prod_bot can't stay off that long. Mitigation: smoke test (Phase 2) gives a per-strategy-per-year time estimate; if that estimate × 25 strategies × 5 years exceeds the window, drop the disabled-13 to save ~50% of the time.
- **Weakest assumption:** That `tools/phoenix_real_backtest.py` still works correctly post-master-fix. The master-fix run touched it (commit `7de51ee` was the harness target synthesis bug fix). Phase 2 smoke is specifically designed to catch a regression there before committing to the full 8-hour run.
- **Best alternative:** If smoke fails or Phase 3 is too slow, fall back to using the existing 2026-05-27 reproduction CSV (`backtest_results/_reproduction_2026-05-27/phoenix_real_5year.csv`) and just regenerate the Phase 4-6 report from it. That report would be slightly stale (a week old, pre-master-fix Phases 4-9 from the 2026-06-01 run) but still defensible as evidence.

---

*End of plan.*
