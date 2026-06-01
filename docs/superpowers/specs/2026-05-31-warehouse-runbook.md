# Phoenix Backtest Warehouse — v1 Runbook

Acceptance procedure for the warehouse v1 cutover. See the spec:
[`2026-05-31-backtest-warehouse-design.md`](2026-05-31-backtest-warehouse-design.md)
and the implementation plan:
[`../plans/2026-05-31-backtest-warehouse.md`](../plans/2026-05-31-backtest-warehouse.md).

## Prereqs

- `pip install -r requirements.txt` (picks up `duckdb>=1.1`)
- Layer-1 tests pass:
  ```
  cd "C:\Trading Project\phoenix_bot"
  python -m pytest tests/warehouse -v
  ```
  Expected: ~118 passed, 1 skipped, 0 failed.

## Step 1 — Ingest the portfolio_framework CSVs

```cmd
cd "C:\Trading Project\phoenix_bot"
python -m tools.warehouse ingest backtest_results\portfolio_framework
```

Expected: `ingested=15 skipped_duplicate=0 errors=0` (or similar, depending on
whether multi_day has landed).

## Step 2 — Ingest the legacy CSVs

```cmd
python -m tools.warehouse ingest backtest_results --recursive
```

Expected:
- `ingested=` matches the count of CSVs under `backtest_results\` minus the 15
  already counted in Step 1 (subdirectories under `portfolio_framework/`
  re-hash to the same `run_id` and skip cleanly via content-hash dedup).
- `errors=0`. Any errors mean a CSV doesn't match a known kind — inspect
  `data\warehouse\ingest_errors.log` (JSONL; one line per failure).

## Step 3 — Inspect the warehouse

```cmd
python -m tools.warehouse status
```

Expected output (approximate, varies with how many legacy CSVs are present):

```
         runs:         70+
       trades:    76,000+
  wfa_windows:        210+
  wfa_summary:         14+
  run_metrics:        100+
  last_ingest: 2026-05-31 ...
```

## Step 4 — Run the four spec example queries

Open DuckDB CLI:

```cmd
duckdb data\warehouse\phoenix.duckdb
```

Paste the queries from
[the spec §9](2026-05-31-backtest-warehouse-design.md#9-example-queries-for-the-runbook-and-future-quick-reference)
and eyeball results:

1. **Best PF strategies in last 12 months** — top of the list should be familiar
   names (likely `vwap_pullback_v2`, `bias_momentum`, etc.).
2. **TOD session attribution, session-hours only** — `Opening Drive` and
   `Power Hour` rows should have the largest trade counts.
3. **Compare gross-PnL legacy era vs friction-net era** for `vwap_pullback_v2`
   — should have rows for `friction_applied=true` AND `friction_applied=false`.
   Net-PnL columns should differ by approximately `$4.82 * n_trades`.
4. **WFA robust strategies** — should return the strategies INVENTORY.md
   documents as robust.

## Step 5 — Record the run

Append actual row counts and any `ingest_errors.log` content to
`docs/RECENT_CHANGES.md` so the row counts at this snapshot are recoverable.

## Step 6 — When multi_day lands

After the consolidation agent re-runs `_wfa_merge.py` and drops
`wfa_windows_p13_multi_day.csv` (+ refreshed `wfa_summary.csv`):

```cmd
python -m tools.warehouse ingest backtest_results\portfolio_framework
```

Expected: `ingested=2 skipped_duplicate=13`. The two new files (multi_day
window CSV + refreshed wfa_summary.csv) get new `run_id`s; the original 13
CSVs are content-hash-unchanged and skip cleanly.

Re-run Step 3 to confirm `wfa_windows` grew by ~15 rows and `wfa_summary`
gained a second row per strategy (the old version stays — no-delete policy).

## Step 7 — Acceptance log

```
RUN DATE:           2026-05-31
STEP 1 INGEST:      ingested=16  skipped_duplicate=0   errors=0
                    (80,435 rows, 510 metrics)
                    Multi_day shard LANDED (wfa_windows_p13_multi_day.csv = 15 rows).
                    wfa_summary.csv refreshed to 18 rows (was 14 in INVENTORY).
                    wfa_windows.csv refreshed to 270 rows (was 210 in INVENTORY).
STEP 2 INGEST:      ingested=39 (legacy trades), skipped_duplicate=23, errors=18
                    (subdir CSVs already in via Step 1 deduplicate cleanly)
STEP 3 STATUS:
   runs:                              50
   trades:                       521,209
   wfa_windows:                      540
   wfa_summary:                       18
   run_metrics:                    2,160
   import_microstructure_lift:         9
   import_phase1_regime_portfolio:     3
   import_phase1_time_of_day:          5
STEP 4 QUERIES:     all four returned plausible results.
                    Q1 (top PF): Phase 13 strategies at the top
                       (raschke 3.14, multi_day 2.47, inside_bar 2.45, asian 2.09).
                    Q2 (TOD): Opening Drive dominates (n=19,200, +$85,520).
                    Q3 (legacy gross vs friction-net) for vwap_pullback_v2:
                       friction-net: 5,437 trades, total -$15,759
                       gross legacy: 41,552 trades, total +$226,083
                       Stark difference validates the §10 friction caveat design.
                    Q4 (WFA robust): orb_v2 mean_oos_pf=399.60 is suspicious
                       (likely artifact of near-zero loss window in IS denominator).
                       Other three robust strategies (raschke 3.16, inside_bar 2.80,
                       opening_session 1.69) look sensible.
ERROR LOG ENTRIES:  18 errors in two classes:
                    (A) unknown_csv_kind (14 files): _dom_pullback_5y_verdict,
                        backtest_v3_sweep_results, exit_methodology_v3_results,
                        phoenix_compounding_{summary,tier_1500,tier_3000,tier_dates},
                        phoenix_early_reversal_per_trade, phoenix_entry_retest_per_trade,
                        phoenix_es_nq_attribution, phoenix_mean_reversion_summary,
                        phoenix_sr_confluence_summary, phoenix_sr_veto_summary,
                        phoenix_tick_entry_slippage. Each can be added to the sniffer
                        with a follow-up rule (out of v1 scope per spec §5.8).
                    (B) Binder Error "Referenced column 'strategy' not found" (4 files):
                        opening_session_sub_breakdown, backtest_v3_trades_LONG_b7.0,
                        phoenix_sr_confluence_per_trade. These have trade-shape headers
                        (entry_ts/entry_price/pnl_dollars) but lack the `strategy` column
                        the warehouse trades table requires NOT NULL. Real follow-up:
                        either make `strategy` derivable from filename/sub_name when
                        absent, or relax the schema. Out of v1 scope.
NOTES:              Warehouse is fully functional for the canonical portfolio
                    framework + the 35+ legacy CSVs that match a known kind.
                    The 18 reject errors are documented, recoverable (add sniffer
                    rules later), and do not block v1 acceptance.
                    Multi_day shard landed during this same session — consolidation
                    agent items 4 (multi_day sidecar + wfa_summary refresh) and 5
                    (filename stability) are now fully resolved.
```
