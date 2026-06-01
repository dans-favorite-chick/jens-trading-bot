# Portfolio Backtest Result CSVs — Inventory + DuckDB Loading Guide

Generated 2026-05-31 from the in-flight portfolio backtest framework.

All paths below are relative to this directory:
`C:\Trading Project\phoenix_bot\backtest_results\portfolio_framework\`

## Quick start in DuckDB

```sql
-- DuckDB infers types from CSVs automatically.
INSTALL httpfs; -- optional

-- The master trade log
CREATE TABLE trades AS
  SELECT * FROM read_csv_auto('macro_trades.csv', header=true, timestampformat='%Y-%m-%d %H:%M:%S%z');

-- WFA windows (original 14-strategy run; merged shardA + shardB)
CREATE TABLE wfa_windows AS
  SELECT * FROM read_csv_auto('wfa_windows.csv', header=true);

-- WFA windows for the 4 Phase 13 strategies (3 done, multi_day in flight)
CREATE TABLE wfa_windows_phase13 AS
  SELECT * FROM read_csv_auto('wfa_windows_p13_*.csv', header=true, union_by_name=true);

-- WFA per-strategy robustness summary
CREATE TABLE wfa_summary AS
  SELECT * FROM read_csv_auto('wfa_summary.csv', header=true);

-- Headline metrics tables (small, derivable from `trades` but materialized for speed)
CREATE TABLE strategy_summary AS SELECT * FROM read_csv_auto('phase1_strategy_summary.csv', header=true);
CREATE TABLE stop_target_suggestion AS SELECT * FROM read_csv_auto('phase1_stop_target.csv', header=true);
CREATE TABLE perf_by_regime AS SELECT * FROM read_csv_auto('phase1_regime_portfolio.csv', header=true);
CREATE TABLE perf_by_time_of_day AS SELECT * FROM read_csv_auto('phase1_time_of_day.csv', header=true);

-- Phase 2 microstructure lift
CREATE TABLE microstructure_lift AS SELECT * FROM read_csv_auto('microstructure_lift.csv', header=true);

-- Phase 3 multi-tier comparison
CREATE TABLE multitier_comparison AS SELECT * FROM read_csv_auto('phase3_multitier_comparison.csv', header=true);
```

## File inventory

### Primary trade log

| File | Rows | Purpose |
|---|---:|---|
| **`macro_trades.csv`** | **76,342** | One row per trade across all 18 strategies (the 14 original + 4 Phase 13). Friction-net P&L (commission + slippage already deducted). MAE / MFE, volatility regime, and time-of-day bucket attached per trade. **The canonical trade log.** |
| `phase13_trades.csv` | 3,518 | Subset of `macro_trades.csv` for just the 4 Phase 13 strategies. Same schema. Redundant with `macro_trades` (already merged in). Useful only if you want to inspect Phase 13 separately. |

**Columns (both files identical):**
- `strategy` (TEXT) — name; one of the 18 strategy names.
- `direction` (TEXT) — `LONG` or `SHORT`.
- `entry_ts`, `exit_ts` (TIMESTAMPTZ UTC) — bar boundaries.
- `entry_price`, `stop_price`, `target_price`, `exit_price` (DOUBLE).
- `exit_reason` (TEXT) — `target`, `stop`, `time_exit`, `no_data_after_entry`, `no_data_in_window`.
- `pnl_dollars` (DOUBLE) — net P&L after $4.82 round-turn friction (commission + 2-tick slippage).
- `pnl_ticks` (INTEGER) — gross tick count, friction NOT deducted from this.
- `hold_min` (DOUBLE) — minutes between entry and exit.
- `year` (INTEGER) — calendar year of entry, for convenience.
- `mae_ticks` (DOUBLE) — Maximum Adverse Excursion in ticks (how far the trade went against you before exiting).
- `mfe_ticks` (DOUBLE) — Maximum Favorable Excursion (best unrealized point).
- `regime` (TEXT) — `LOW_VOL_TREND` / `MEAN_REVERT_CHOP` / `HIGH_VOLATILITY` / `UNKNOWN` (label at entry; no-look-ahead via .shift(1)).
- `tod_bucket` (TEXT) — `Opening Drive` / `Mid-Day Lull` / `Power Hour` / `Globex Overnight` / `Other RTH`.

### Walk-forward analysis

| File | Rows | Purpose |
|---|---:|---|
| **`wfa_windows.csv`** | **210** | Per-window WFA for the original 14 strategies. **Merged** version of shardA + shardB. 14 strategies × 15 windows = 210. |
| `wfa_windows_shardA.csv` | 135 | Pre-merge shard. 9 strategies × 15 windows. Subset of `wfa_windows.csv`. |
| `wfa_windows_shardB.csv` | 75 | Pre-merge shard. 5 strategies × 15 windows. Subset of `wfa_windows.csv`. |
| `wfa_windows_p13_raschke.csv` | 15 | Phase 13 WFA for raschke_baseline. |
| `wfa_windows_p13_inside_bar.csv` | 15 | Phase 13 WFA for g_inside_bar_breakout. |
| `wfa_windows_p13_asian.csv` | 15 | Phase 13 WFA for a_asian_continuation. |
| `wfa_windows_p13_multi_day.csv` | (in flight) | **Will appear when `e_multi_day_breakout` shard finishes.** |

**Columns (all WFA window files identical):**
- `strategy` (TEXT).
- `window_idx` (INTEGER) — 0-based index, 15 windows total per strategy.
- `is_start`, `is_end` (DATE) — 12-month in-sample window.
- `oos_start`, `oos_end` (DATE) — 3-month out-of-sample window.
- `best_params` (TEXT, JSON-encoded) — winning parameter combo from IS grid optimization. Parse with `json_extract()`.
- `is_pf` (DOUBLE) — profit factor in-sample.
- `is_trades` (INTEGER).
- `oos_pf` (DOUBLE) — profit factor out-of-sample. **This is the validation metric.**
- `oos_trades` (INTEGER).
- `oos_net` (DOUBLE) — out-of-sample net dollars.
- `wfe` (DOUBLE) — walk-forward efficiency = `oos_pf / is_pf`. 1.0 = matches IS; <0.8 = degraded.
- `degraded` (BOOLEAN) — true if `oos_pf < 0.80 * is_pf` (>20% degradation).

| File | Rows | Purpose |
|---|---:|---|
| **`wfa_summary.csv`** | **14** | Per-strategy WFA robustness summary. (Phase 13 strategies not yet included — those will be added when multi_day finishes and I re-run the merge.) |

**Columns:**
- `strategy` (TEXT).
- `n_windows` (INTEGER) — always 15 for full 5y span.
- `mean_is_pf`, `mean_oos_pf`, `median_oos_pf` (DOUBLE).
- `pct_windows_degraded` (DOUBLE) — fraction of windows that degraded.
- `robust` (BOOLEAN) — `pct_degraded <= 0.34 AND mean_oos_pf >= 1.30`.

### Headline / summary tables

| File | Rows | Purpose | Columns |
|---|---:|---|---|
| `phase1_strategy_summary.csv` | 17 | Per-strategy headline metrics for the full 5y window. | strategy, n, net_pnl, win_rate, profit_factor, expectancy, sharpe, sortino, max_dd, max_dd_dur_trades, max_tuw_days, max_consec_losses |
| `phase1_stop_target.csv` | 17 | MAE/MFE-derived empirical optimal stop/target per strategy. | strategy, n, winners, stop_ticks, target_ticks, winner_mae_p50, winner_mae_p90, mfe_p50, mfe_p75 |
| `phase1_regime_portfolio.csv` | 3 | Portfolio-level performance by volatility regime. | regime, + same 11 metrics as strategy_summary |
| `phase1_time_of_day.csv` | 5 | Portfolio-level performance by ET time-of-day bucket. | tod_bucket, + same 11 metrics |
| `microstructure_lift.csv` | 9 | Phase 2 — does each tick-level filter (absorption / delta-trail / intermarket) lift baseline win rate / PF? | filter, subset (`baseline`/`passed`/`failed`/`trail_applied`), n, net_pnl, win_rate, profit_factor |
| `phase3_multitier_comparison.csv` | 17 | Per-strategy: 5y baseline vs tick-period baseline vs each microstructure filter. | strategy, 5y_n, 5y_pf, 5y_net, sub_n, sub_pf, sub_net, absorp_n, absorp_pf, trail_net, inter_n, inter_pf |

## Suggested DuckDB table model

For the cleanest setup, three "fact" tables and the rest as small materialized derived views:

| Table | Source | Why |
|---|---|---|
| `trades` | `macro_trades.csv` | The grain. Every other metric is derivable from here. |
| `wfa_windows` | `wfa_windows.csv` + all `wfa_windows_p13_*.csv` UNION'd | Per-window walk-forward results across all 18 strategies once multi_day lands. |
| `wfa_summary` | `wfa_summary.csv` | Pre-computed robustness summary. Refresh from `wfa_windows` if you want. |

Then everything else (regime breakdowns, time-of-day, microstructure lift, multi-tier) can either be loaded from the CSVs as-is OR computed on demand from `trades` and `wfa_windows` via SQL. The pre-computed CSVs are convenience.

## Notes / caveats for DuckDB analysis

- **All P&L is friction-net.** `pnl_dollars` already has `$4.82` round-turn (commission + 2-tick slippage) deducted. To recover gross, add `4.82` to `pnl_dollars` per trade.
- **One contract assumed** throughout. Multiply by your real position size for portfolio P&L.
- **Timestamps are UTC** in CSV; convert to America/New_York for the ToD bucket logic that produced `tod_bucket`, or to America/Chicago to match the strategy window definitions in `config/strategies.py`.
- **`tod_bucket` and `regime` were assigned at TRADE TIME with no look-ahead.** Don't recompute them from the entry_ts naively without `.shift(1)`.
- **Sub-period tick microstructure data covers 2026-03-17 → 2026-05-15** (the TBBO clean-tick cache window). Trades outside that range are silently absent from `microstructure_lift.csv`, `phase3_multitier_comparison.csv` `absorp_*`/`inter_*` columns.
- **4 of the 18 names had no class wired into the harness's instantiate_strategies originally**: `raschke_baseline`, `g_inside_bar_breakout`, `e_multi_day_breakout`, `a_asian_continuation`. Their trades were produced by a parallel driver (`_run_phase13_4.py`) that imports the classes directly. Same trade-record schema.

## Framework code lives at

`C:\Trading Project\phoenix_bot\tools\portfolio_backtest\` — drivers (`run_portfolio_backtest.py`, `_wfa_shard.py`, `_wfa_merge.py`, `_run_phase13_4.py`, `_phase13_breakdown.py`) plus the library modules (`analytics.py`, `wfa.py`, `microstructure.py`, `report.py`, `paths.py`). Run any of the scripts with the canonical Python 3.14 interpreter at `%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe`.

To regenerate everything from scratch:
```cmd
cd "C:\Trading Project\phoenix_bot\tools\portfolio_backtest"
python run_portfolio_backtest.py --strategies all --start 2021-05-17 --end 2026-05-15
```
