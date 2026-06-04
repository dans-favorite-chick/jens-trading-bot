# Reconciliation Report — bias_momentum

- **Date generated:** 2026-06-04T22:03:52.274609+00:00
- **Strategy:** `bias_momentum`
- **Window:** 2026-05-25 → 2026-05-27 (UTC)
- **Total sim trades evaluated:** 5

## Executive Summary

- **Verdict:** FAIL — backtester emitted zero direction-matched signals across the sim window.
- **Replayed (direction-matched):** 0 / 5 (0.0%)
- **Within tolerance:** 0 / 0 (0.0% of replays)
- **Outside tolerance:** 0
- **Blocked (missing fields):** 0
- **Sim-only (backtest emitted no signal):** 5
- **Backtest-only (opposite-direction signal in window):** 0

## Tolerance Configuration

```yaml
entry_price_ticks: 2
entry_time_seconds: 60
exit_reason_must_match: false
net_pnl_pct: 25.0
stop_price_ticks: 2
```

## Divergence Stats (REPLAYED trades only)

- **Entry time delta:** n/a
- **Entry price delta:** n/a
- **Stop price delta:** n/a
- **Net P&L delta (% of sim):** n/a
- **Net P&L delta (abs $):** n/a

## Per-Trade Detail

| trade_id | sim_dt (UTC) | dir | class | Δt(s) | Δentry(t) | Δstop(t) | sim_pnl$ | bt_pnl$ | ΔPnL% | in_tol | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| d4567afb | 2026-05-25T00:10:10 | SHORT | SIM_ONLY | - | - | - | -8.32 | - | - | - | no signal in ±5m window around 2026-05-25 00:10:10.562245+00:00 |
| d462224c | 2026-05-25T01:15:14 | LONG | SIM_ONLY | - | - | - | -3.32 | - | - | - | no signal in ±5m window around 2026-05-25 01:15:14.049300+00:00 |
| 422693a9 | 2026-05-25T01:34:39 | LONG | SIM_ONLY | - | - | - | -1.82 | - | - | - | no signal in ±5m window around 2026-05-25 01:34:39.256723+00:00 |
| a3a32107 | 2026-05-25T02:30:48 | LONG | SIM_ONLY | - | - | - | -5.32 | - | - | - | no signal in ±5m window around 2026-05-25 02:30:48.697963+00:00 |
| 6f37faff | 2026-05-25T02:50:20 | LONG | SIM_ONLY | - | - | - | -5.32 | - | - | - | no signal in ±5m window around 2026-05-25 02:50:20.026145+00:00 |

