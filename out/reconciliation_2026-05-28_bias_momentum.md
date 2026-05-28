# Reconciliation Report — bias_momentum

- **Date generated:** 2026-05-28T05:33:09.767018+00:00
- **Strategy:** `bias_momentum`
- **Window:** 2026-04-13 → 2026-05-28 (UTC)
- **Total sim trades evaluated:** 220

## Executive Summary

- **Verdict:** BLOCKED — no trades could be replayed because all are missing strategy-blocking fields.
- **Replayed (direction-matched):** 0 / 220 (0.0%)
- **Within tolerance:** 0 / 0 (0.0% of replays)
- **Outside tolerance:** 0
- **Blocked (missing fields):** 220
- **Sim-only (backtest emitted no signal):** 0
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

## Strategy-Blocking Field Impact

Counts of trades blocked because the listed market_snapshot field was missing:

- `day_type`: 220
- `cr_verdict`: 220
- `cvd_health`: 220
- `es_nq_rs`: 220

## Informational Divergence (BLOCKED trades, backtester ran anyway)

- **Blocked trades where backtest fired a signal:** 98 / 220
- **Blocked trades where backtest emitted no signal:** 122
- **Direction-matched (informational):** 92
- **Direction-mismatched (informational):** 6

Divergence stats across direction-matched BLOCKED trades (treats the backtester output as a comparison anchor — NOT a tolerance pass; many of these likely take a different code branch in live sim because of the missing strategy-blocking fields):

- **Entry time delta:** n=92 mean=200.01s p50=246.39s p90=286.58s max=298.53s
- **Entry price delta:** n=92 mean=39.65t p50=17.00t p90=55.00t max=1265.00t
- **Stop price delta:** n=92 mean=116.71t p50=107.52t p90=179.48t max=1305.00t
- **Net P&L delta (% of sim):** n=92 mean=574.39% p50=251.56% p90=1421.99% max=3636.04%
- **Net P&L delta (abs $):** n=92 mean=28.79$ p50=17.72$ p90=49.72$ max=615.78$

## Per-Trade Detail

| trade_id | sim_dt (UTC) | dir | class | Δt(s) | Δentry(t) | Δstop(t) | sim_pnl$ | bt_pnl$ | ΔPnL% | in_tol | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 92f7adad | 2026-04-22T13:49:39 | LONG | BLOCKED | - | - | - | -2.22 | -11.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| da072c26 | 2026-04-22T13:59:47 | LONG | BLOCKED | - | - | - | -1.72 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_1876588f | 2026-04-23T13:42:08 | SHORT | BLOCKED | - | - | - | -68.72 | +75.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_0d7a0254 | 2026-04-27T00:55:04 | LONG | BLOCKED | - | - | - | +29.28 | +114.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 95bcef11 | 2026-04-27T01:21:41 | LONG | BLOCKED | - | - | - | -11.72 | +6.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| b5c1e5f1 | 2026-04-27T01:37:44 | LONG | BLOCKED | - | - | - | -6.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| a48a9896 | 2026-04-27T10:25:25 | LONG | BLOCKED | - | - | - | -6.72 | -12.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 06ae6e1c | 2026-04-27T10:41:38 | LONG | BLOCKED | - | - | - | -3.72 | +8.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 418462a7 | 2026-04-27T10:57:51 | LONG | BLOCKED | - | - | - | -9.72 | +8.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_6468fe92 | 2026-04-27T11:18:05 | LONG | BLOCKED | - | - | - | +10.28 | +19.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| b8e2a3bf | 2026-04-27T11:44:48 | LONG | BLOCKED | - | - | - | -3.72 | +6.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 92499687 | 2026-04-27T12:06:46 | SHORT | BLOCKED | - | - | - | -2.22 | +9.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 7ef8ab67 | 2026-04-27T12:46:47 | LONG | BLOCKED | - | - | - | -9.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 62a3f4a9 | 2026-04-27T13:03:09 | SHORT | BLOCKED | - | - | - | -1.72 | +13.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| cb7e1ab9 | 2026-04-27T13:19:11 | SHORT | BLOCKED | - | - | - | -2.22 | +38.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 6bb23748 | 2026-04-27T13:35:17 | SHORT | BLOCKED | - | - | - | -2.22 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_6435e610 | 2026-04-27T13:51:49 | SHORT | BLOCKED | - | - | - | -51.72 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 738e2584 | 2026-04-27T14:09:14 | LONG | BLOCKED | - | - | - | -1.72 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d51e203a | 2026-04-27T14:26:50 | SHORT | BLOCKED | - | - | - | -2.22 | -15.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 39cdc490 | 2026-04-27T14:42:15 | SHORT | BLOCKED | - | - | - | -2.22 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 1e445a6a | 2026-04-27T14:59:17 | SHORT | BLOCKED | - | - | - | -3.22 | +27.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 4e2668b3 | 2026-04-27T15:15:17 | SHORT | BLOCKED | - | - | - | -2.22 | +71.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 1e99f54d | 2026-04-27T15:31:19 | SHORT | BLOCKED | - | - | - | -4.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 8e1d90b0 | 2026-04-27T15:46:52 | SHORT | BLOCKED | - | - | - | -2.22 | +13.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| a1bd3336 | 2026-04-27T16:16:52 | LONG | BLOCKED | - | - | - | -11.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9893eeee | 2026-04-27T16:33:24 | LONG | BLOCKED | - | - | - | -2.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| abfedd72 | 2026-04-27T16:49:25 | LONG | BLOCKED | - | - | - | -8.22 | -13.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| a935634b | 2026-04-27T17:06:27 | LONG | BLOCKED | - | - | - | -3.72 | +25.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| b5f77f11 | 2026-04-27T17:21:54 | LONG | BLOCKED | - | - | - | -1.72 | +44.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d2db9e8a | 2026-04-27T17:37:32 | LONG | BLOCKED | - | - | - | -11.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e9676605 | 2026-04-27T17:56:34 | LONG | BLOCKED | - | - | - | -3.72 | -18.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e91ca81b | 2026-04-27T18:16:37 | LONG | BLOCKED | - | - | - | +1.28 | +4.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e2082813 | 2026-04-27T18:32:41 | LONG | BLOCKED | - | - | - | -5.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 0a6c6c16 | 2026-04-27T18:48:43 | LONG | BLOCKED | - | - | - | -5.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_9777e8c9 | 2026-04-27T19:37:41 | LONG | BLOCKED | - | - | - | +36.28 | -20.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 3e8cb8ec | 2026-04-27T20:00:58 | LONG | BLOCKED | - | - | - | -8.22 | -28.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 5e5fe918 | 2026-04-27T20:17:00 | LONG | BLOCKED | - | - | - | -4.72 | -27.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 6095ba32 | 2026-04-27T20:33:13 | LONG | BLOCKED | - | - | - | -1.72 | +20.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 668769d2 | 2026-04-27T20:48:24 | LONG | BLOCKED | - | - | - | -2.72 | -11.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| f4857327 | 2026-04-27T22:00:08 | LONG | BLOCKED | - | - | - | -2.22 | +78.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d3cdb4e4 | 2026-04-27T22:16:14 | LONG | BLOCKED | - | - | - | +1.28 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 1dd879dc | 2026-04-27T22:24:05 | LONG | BLOCKED | - | - | - | -3.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 13304c61 | 2026-04-27T22:27:05 | LONG | BLOCKED | - | - | - | -1.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 790298b1 | 2026-04-27T22:38:06 | LONG | BLOCKED | - | - | - | -6.22 | +8.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2b589eae | 2026-04-27T22:40:06 | LONG | BLOCKED | - | - | - | -4.22 | -5.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 5952da43 | 2026-04-27T22:43:09 | LONG | BLOCKED | - | - | - | -1.72 | -14.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 368699cb | 2026-04-27T22:45:08 | LONG | BLOCKED | - | - | - | -2.72 | -21.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_b659c485 | 2026-04-27T23:23:40 | LONG | BLOCKED | - | - | - | +8.78 | +3.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9ac2f579 | 2026-04-27T23:31:11 | LONG | BLOCKED | - | - | - | -2.22 | +10.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| c0aff3b3 | 2026-04-27T23:33:00 | LONG | BLOCKED | - | - | - | -2.22 | +19.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ab97176a | 2026-04-27T23:36:05 | LONG | BLOCKED | - | - | - | -2.22 | +19.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2334b087 | 2026-04-27T23:38:08 | LONG | BLOCKED | - | - | - | -2.72 | +10.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 1d83ff63 | 2026-04-27T23:43:33 | LONG | BLOCKED | - | - | - | -3.22 | -17.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 909bf623 | 2026-04-27T23:45:06 | LONG | BLOCKED | - | - | - | -7.72 | -23.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 15672ac3 | 2026-04-27T23:50:09 | LONG | BLOCKED | - | - | - | -2.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 85efbc7f | 2026-04-27T23:53:07 | LONG | BLOCKED | - | - | - | -2.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 98599a1a | 2026-04-27T23:58:16 | LONG | BLOCKED | - | - | - | -2.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d6208091 | 2026-04-28T00:03:10 | LONG | BLOCKED | - | - | - | -2.72 | +32.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| b68502a2 | 2026-04-28T00:25:12 | LONG | BLOCKED | - | - | - | -6.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| c92f71c1 | 2026-04-28T00:28:03 | LONG | BLOCKED | - | - | - | -2.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| da3104e9 | 2026-04-28T01:08:08 | LONG | BLOCKED | - | - | - | -4.72 | +1.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 700e0d92 | 2026-04-28T02:42:14 | SHORT | BLOCKED | - | - | - | -1.72 | +31.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e1dd7516 | 2026-04-28T02:58:09 | SHORT | BLOCKED | - | - | - | -21.22 | +28.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 05ba86a0 | 2026-04-28T05:04:00 | SHORT | BLOCKED | - | - | - | -2.72 | +38.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 39407134 | 2026-04-28T05:08:12 | SHORT | BLOCKED | - | - | - | -1.72 | +48.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_72561132 | 2026-04-28T16:24:26 | SHORT | BLOCKED | - | - | - | +609.78 | -6.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_e1cab1f9 | 2026-04-29T14:28:14 | LONG | BLOCKED | - | - | - | +8.78 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d9011188 | 2026-04-29T14:46:49 | LONG | BLOCKED | - | - | - | -22.72 | +8.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 8a13ddbe | 2026-04-29T15:13:26 | LONG | BLOCKED | - | - | - | -1.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| f8ce30d6 | 2026-04-29T15:29:28 | LONG | BLOCKED | - | - | - | -3.22 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| f48f7c98 | 2026-04-29T15:45:28 | LONG | BLOCKED | - | - | - | -1.72 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_89861203 | 2026-04-29T16:23:53 | SHORT | BLOCKED | - | - | - | +14.78 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_e010e95c | 2026-04-29T16:42:26 | SHORT | BLOCKED | - | - | - | +9.78 | -11.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 115378b8 | 2026-04-29T17:12:37 | SHORT | BLOCKED | - | - | - | +7.78 | -4.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| c25838c2 | 2026-04-29T17:30:42 | SHORT | BLOCKED | - | - | - | -2.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_d3488508 | 2026-04-29T18:15:32 | SHORT | BLOCKED | - | - | - | -51.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 7df9e980 | 2026-04-29T18:33:32 | SHORT | BLOCKED | - | - | - | -2.22 | -30.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e425c9ec | 2026-04-29T20:18:33 | SHORT | BLOCKED | - | - | - | -20.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_b9302c06 | 2026-04-29T20:51:38 | SHORT | BLOCKED | - | - | - | +12.28 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9e8bc333 | 2026-04-29T22:00:08 | SHORT | BLOCKED | - | - | - | -76.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 5502f85c | 2026-04-29T22:16:10 | LONG | BLOCKED | - | - | - | -11.72 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ccd211a6 | 2026-04-29T22:32:17 | LONG | BLOCKED | - | - | - | -21.22 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9598dc1b | 2026-05-04T04:24:28 | LONG | BLOCKED | - | - | - | -4.82 | -8.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 1dcafb43 | 2026-05-04T04:42:07 | LONG | BLOCKED | - | - | - | -5.32 | -18.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d9c1b025 | 2026-05-04T04:57:46 | LONG | BLOCKED | - | - | - | -4.32 | -13.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 045b7ac9 | 2026-05-04T05:14:43 | LONG | BLOCKED | - | - | - | -5.32 | +21.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 410322e6 | 2026-05-04T05:47:56 | LONG | BLOCKED | - | - | - | -5.82 | -18.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_2113a6d1 | 2026-05-04T07:31:57 | LONG | BLOCKED | - | - | - | +9.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e04252e8 | 2026-05-04T09:43:32 | LONG | BLOCKED | - | - | - | -6.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ba967bc5 | 2026-05-04T13:00:41 | LONG | BLOCKED | - | - | - | +94.68 | -27.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_9ca6f3b8 | 2026-05-04T13:07:34 | LONG | BLOCKED | - | - | - | +70.18 | +6.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d17990f7 | 2026-05-06T22:00:10 | LONG | BLOCKED | - | - | - | -7.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 55048294 | 2026-05-07T01:58:12 | LONG | BLOCKED | - | - | - | -5.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e1cc4698 | 2026-05-07T02:14:21 | LONG | BLOCKED | - | - | - | -5.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ab03a860 | 2026-05-07T02:31:30 | LONG | BLOCKED | - | - | - | -4.32 | -3.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 4967bacb | 2026-05-07T03:23:08 | LONG | BLOCKED | - | - | - | -6.82 | -9.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 26e86657 | 2026-05-07T03:38:33 | LONG | BLOCKED | - | - | - | -6.32 | +7.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 6d2c1085 | 2026-05-07T03:53:56 | LONG | BLOCKED | - | - | - | -4.82 | -6.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| bb5f8cd1 | 2026-05-07T04:10:09 | LONG | BLOCKED | - | - | - | -5.32 | +32.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| f6154307 | 2026-05-07T04:25:58 | LONG | BLOCKED | - | - | - | -5.32 | +6.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 1af6825f | 2026-05-07T04:41:31 | LONG | BLOCKED | - | - | - | -3.82 | +21.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_fdc657ba | 2026-05-07T05:01:51 | LONG | BLOCKED | - | - | - | +16.18 | -9.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_f3c0fa2f | 2026-05-07T05:53:27 | LONG | BLOCKED | - | - | - | +5.18 | -7.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2fcfc4b9 | 2026-05-07T06:30:38 | LONG | BLOCKED | - | - | - | -6.82 | +6.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 323d4e40 | 2026-05-07T06:49:51 | LONG | BLOCKED | - | - | - | +32.18 | +11.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d4acff71 | 2026-05-07T07:20:00 | LONG | BLOCKED | - | - | - | +7.68 | +6.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 6544f849 | 2026-05-08T00:50:07 | LONG | BLOCKED | - | - | - | -5.32 | +28.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 0f0443a3 | 2026-05-08T02:00:32 | LONG | BLOCKED | - | - | - | -5.82 | -9.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| b407aa00 | 2026-05-08T02:16:59 | LONG | BLOCKED | - | - | - | -4.82 | -23.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 16e1cc86 | 2026-05-08T02:37:11 | LONG | BLOCKED | - | - | - | -5.32 | -14.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 308a64a3 | 2026-05-08T03:00:21 | LONG | BLOCKED | - | - | - | -3.82 | -6.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 43ec16a2 | 2026-05-08T04:00:53 | LONG | BLOCKED | - | - | - | -5.32 | +2.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 95eb85d4 | 2026-05-08T04:16:34 | LONG | BLOCKED | - | - | - | -5.32 | -4.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| e1f3c4c3 | 2026-05-08T04:36:00 | LONG | BLOCKED | - | - | - | -4.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 5310a6f1 | 2026-05-08T05:09:32 | LONG | BLOCKED | - | - | - | -5.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 114c7222 | 2026-05-08T05:26:06 | LONG | BLOCKED | - | - | - | -5.32 | +33.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 73b79403 | 2026-05-08T07:01:14 | LONG | BLOCKED | - | - | - | -5.32 | -17.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_33df4423 | 2026-05-08T08:40:19 | LONG | BLOCKED | - | - | - | +6.18 | +7.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 435bbbb4 | 2026-05-08T09:30:50 | LONG | BLOCKED | - | - | - | -5.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2082b762 | 2026-05-08T09:46:57 | LONG | BLOCKED | - | - | - | -22.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 762cc6f7 | 2026-05-08T10:13:11 | LONG | BLOCKED | - | - | - | -1.32 | +14.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 07fca4ea | 2026-05-08T10:33:28 | LONG | BLOCKED | - | - | - | +9.18 | +18.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 57f517b7 | 2026-05-08T10:50:36 | LONG | BLOCKED | - | - | - | -5.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9a496f93 | 2026-05-08T11:58:17 | LONG | BLOCKED | - | - | - | -13.82 | -13.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 44b36294 | 2026-05-08T12:15:26 | LONG | BLOCKED | - | - | - | -7.82 | +3.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 29d68a1f | 2026-05-08T12:50:38 | LONG | BLOCKED | - | - | - | -5.82 | +14.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 29d70814 | 2026-05-11T03:14:42 | LONG | BLOCKED | - | - | - | -13.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_fbca6aea | 2026-05-11T03:32:07 | LONG | BLOCKED | - | - | - | +20.18 | +38.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 96ee3819 | 2026-05-11T03:57:17 | LONG | BLOCKED | - | - | - | -4.82 | +18.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_85ddbd91 | 2026-05-11T04:45:11 | LONG | BLOCKED | - | - | - | +15.18 | -3.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_b3cd6de0 | 2026-05-11T05:39:46 | LONG | BLOCKED | - | - | - | +8.68 | -49.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_83464fb4 | 2026-05-11T06:32:49 | LONG | BLOCKED | - | - | - | -54.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| c1fc6cab | 2026-05-11T09:55:43 | LONG | BLOCKED | - | - | - | -2.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| a3d0d1d5 | 2026-05-11T16:39:42 | LONG | BLOCKED | - | - | - | -4.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_9790374c | 2026-05-11T18:41:38 | LONG | BLOCKED | - | - | - | -54.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 30b82ead | 2026-05-13T22:21:16 | LONG | BLOCKED | - | - | - | -5.82 | -13.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| f9781751 | 2026-05-13T22:39:29 | LONG | BLOCKED | - | - | - | -3.82 | +50.50 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 82cc9a10 | 2026-05-13T22:56:36 | LONG | BLOCKED | - | - | - | -6.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 398523b9 | 2026-05-13T22:59:15 | LONG | BLOCKED | - | - | - | +0.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_abc1a6ad | 2026-05-13T23:42:09 | LONG | BLOCKED | - | - | - | +10.68 | +18.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 4f59da1f | 2026-05-14T01:19:16 | LONG | BLOCKED | - | - | - | +5.18 | -14.00 | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_e9a683d6 | 2026-05-14T07:05:50 | LONG | BLOCKED | - | - | - | +50.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 4e505282 | 2026-05-14T16:29:24 | LONG | BLOCKED | - | - | - | +0.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_d4cb7716 | 2026-05-18T02:25:58 | SHORT | BLOCKED | - | - | - | -54.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 61bc08ba | 2026-05-18T02:45:41 | SHORT | BLOCKED | - | - | - | -13.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 78c1cbb7 | 2026-05-18T03:10:42 | LONG | BLOCKED | - | - | - | -42.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| fdf585ab | 2026-05-18T03:40:44 | LONG | BLOCKED | - | - | - | +3.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 516bc94c | 2026-05-18T03:58:24 | LONG | BLOCKED | - | - | - | -3.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 252f66b5 | 2026-05-18T04:15:34 | LONG | BLOCKED | - | - | - | +7.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9ce7d2fd | 2026-05-18T04:43:44 | LONG | BLOCKED | - | - | - | +3.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ef404bf6 | 2026-05-18T05:03:52 | LONG | BLOCKED | - | - | - | +21.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| af7d9f85 | 2026-05-18T05:28:01 | LONG | BLOCKED | - | - | - | -5.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d2ee3406 | 2026-05-18T06:05:55 | SHORT | BLOCKED | - | - | - | -31.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9a9922dd | 2026-05-18T06:24:25 | SHORT | BLOCKED | - | - | - | -18.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 923c55e5 | 2026-05-18T06:40:58 | LONG | BLOCKED | - | - | - | +3.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 49a74766 | 2026-05-18T06:59:33 | LONG | BLOCKED | - | - | - | +32.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2ccef865 | 2026-05-18T07:15:57 | LONG | BLOCKED | - | - | - | -7.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 7a4c8acb | 2026-05-18T07:32:42 | LONG | BLOCKED | - | - | - | -33.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2d5d7df8 | 2026-05-18T07:52:43 | LONG | BLOCKED | - | - | - | -15.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 0bc23ba5 | 2026-05-21T04:29:32 | LONG | BLOCKED | - | - | - | -17.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d1c391e7 | 2026-05-21T04:33:01 | LONG | BLOCKED | - | - | - | -16.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2ad7d2db | 2026-05-21T04:36:19 | LONG | BLOCKED | - | - | - | -13.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 42650a22 | 2026-05-21T04:59:18 | LONG | BLOCKED | - | - | - | -7.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 8bb290c2 | 2026-05-21T05:44:24 | LONG | BLOCKED | - | - | - | -35.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| a5a62a1c | 2026-05-21T06:14:10 | SHORT | BLOCKED | - | - | - | -16.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| f8e4175b | 2026-05-21T06:34:27 | LONG | BLOCKED | - | - | - | -35.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d932aacb | 2026-05-21T06:52:22 | SHORT | BLOCKED | - | - | - | -26.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 81680d5f | 2026-05-21T07:09:23 | SHORT | BLOCKED | - | - | - | -10.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 30787e00 | 2026-05-21T07:28:27 | LONG | BLOCKED | - | - | - | +40.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 14b41352 | 2026-05-21T07:57:39 | LONG | BLOCKED | - | - | - | +12.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 5f498a99 | 2026-05-21T08:14:40 | LONG | BLOCKED | - | - | - | +28.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 3e3eb277 | 2026-05-21T08:31:45 | LONG | BLOCKED | - | - | - | -33.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_b71be9a5 | 2026-05-21T08:50:15 | LONG | BLOCKED | - | - | - | +18.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2153189e | 2026-05-21T12:04:11 | LONG | BLOCKED | - | - | - | -24.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 777e1897 | 2026-05-21T12:32:19 | SHORT | BLOCKED | - | - | - | -31.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| cce52716 | 2026-05-21T12:49:15 | SHORT | BLOCKED | - | - | - | +19.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d8d0c9be | 2026-05-21T13:08:21 | LONG | BLOCKED | - | - | - | -35.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 8b3e017f | 2026-05-22T00:16:29 | LONG | BLOCKED | - | - | - | +10.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 0f07384f | 2026-05-22T00:45:31 | LONG | BLOCKED | - | - | - | -3.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ef6234c5 | 2026-05-22T01:10:47 | LONG | BLOCKED | - | - | - | -3.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 057d4823 | 2026-05-22T01:49:02 | LONG | BLOCKED | - | - | - | +7.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| b5a6d4c4 | 2026-05-22T07:42:37 | LONG | BLOCKED | - | - | - | +12.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 0603d9ae | 2026-05-22T08:14:16 | LONG | BLOCKED | - | - | - | +18.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| bbcea956 | 2026-05-22T08:52:33 | LONG | BLOCKED | - | - | - | +0.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_149aa7d6 | 2026-05-22T11:13:19 | SHORT | BLOCKED | - | - | - | -54.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_45f0dedf | 2026-05-22T13:34:20 | LONG | BLOCKED | - | - | - | +70.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 8a29f242 | 2026-05-22T14:23:02 | SHORT | BLOCKED | - | - | - | +56.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ca29097d | 2026-05-22T14:40:02 | LONG | BLOCKED | - | - | - | +38.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 68c82e60 | 2026-05-22T14:59:05 | LONG | BLOCKED | - | - | - | -8.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| faefc5fa | 2026-05-24T22:50:06 | LONG | BLOCKED | - | - | - | -2.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d4567afb | 2026-05-25T00:10:10 | SHORT | BLOCKED | - | - | - | -8.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d462224c | 2026-05-25T01:15:14 | LONG | BLOCKED | - | - | - | -3.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 422693a9 | 2026-05-25T01:34:39 | LONG | BLOCKED | - | - | - | -1.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| a3a32107 | 2026-05-25T02:30:48 | LONG | BLOCKED | - | - | - | -5.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 6f37faff | 2026-05-25T02:50:20 | LONG | BLOCKED | - | - | - | -5.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 4bcb270e | 2026-05-25T03:33:59 | LONG | BLOCKED | - | - | - | -4.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 7bfc7ff6 | 2026-05-25T05:00:14 | LONG | BLOCKED | - | - | - | -4.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 4c28ebe5 | 2026-05-25T06:36:32 | LONG | BLOCKED | - | - | - | -0.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 89a2d0a6 | 2026-05-25T08:45:45 | LONG | BLOCKED | - | - | - | -3.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 5da9262e | 2026-05-25T09:03:59 | LONG | BLOCKED | - | - | - | -7.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d60c90a5 | 2026-05-25T09:31:03 | LONG | BLOCKED | - | - | - | -2.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| d664cd48 | 2026-05-25T10:55:52 | LONG | BLOCKED | - | - | - | -14.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 1fc9df53 | 2026-05-25T11:16:22 | LONG | BLOCKED | - | - | - | -14.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 853ab515 | 2026-05-25T12:12:32 | SHORT | BLOCKED | - | - | - | -8.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 628f8aac | 2026-05-25T12:45:59 | LONG | BLOCKED | - | - | - | -6.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 73343a04 | 2026-05-25T13:36:03 | LONG | BLOCKED | - | - | - | -4.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| ff1e6bc3 | 2026-05-25T13:56:04 | LONG | BLOCKED | - | - | - | -6.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 2df24e50 | 2026-05-26T04:57:02 | SHORT | BLOCKED | - | - | - | -0.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 82c50066 | 2026-05-26T11:22:12 | LONG | BLOCKED | - | - | - | -6.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 0cfc1966 | 2026-05-26T11:44:23 | LONG | BLOCKED | - | - | - | +0.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 3f713337 | 2026-05-26T12:07:34 | LONG | BLOCKED | - | - | - | -24.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| c2dfeced | 2026-05-26T13:30:02 | LONG | BLOCKED | - | - | - | +32.68 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| RECONCILED_SimBias Momentum_328749db | 2026-05-26T13:46:00 | LONG | BLOCKED | - | - | - | -3.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 6069376d | 2026-05-26T14:11:20 | LONG | BLOCKED | - | - | - | -35.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 616303c3 | 2026-05-26T14:36:20 | LONG | BLOCKED | - | - | - | -23.82 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| a95d1bcb | 2026-05-26T19:06:25 | LONG | BLOCKED | - | - | - | +41.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 13c6a1e9 | 2026-05-26T19:31:28 | LONG | BLOCKED | - | - | - | +4.18 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 98016b37 | 2026-05-26T20:17:37 | LONG | BLOCKED | - | - | - | -18.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| f4918cbc | 2026-05-27T01:00:48 | LONG | BLOCKED | - | - | - | -19.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |
| 9bb2f51b | 2026-05-27T01:42:07 | LONG | BLOCKED | - | - | - | -15.32 | - | - | - | missing strategy-blocking fields: day_type,cr_verdict,cvd_health,es_nq_rs |

