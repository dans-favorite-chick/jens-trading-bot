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

Record this section once the runbook has been executed end-to-end (operator
fills in actual numbers — leave as-is when committing the runbook for the
first time):

```
RUN DATE:           2026-MM-DD
STEP 1 INGEST:      ingested=__ skipped_duplicate=__ errors=__
STEP 2 INGEST:      ingested=__ skipped_duplicate=__ errors=__
STEP 3 STATUS:
   runs:        ____
   trades:      ____
   wfa_windows: ____
   wfa_summary: ____
   run_metrics: ____
STEP 4 QUERIES:     all four returned plausible results? [yes/no, notes]
ERROR LOG ENTRIES:  ____ (paste any unique error_class values)
NOTES:              ____
```
