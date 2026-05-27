---
name: strategy-a_asian_continuation
description: Phoenix strategy a_asian_continuation (Phase 13, overnight range breakout in Asian/London session). Triggers when modifying or debugging a_asian_continuation, or analyzing signals from this strategy. Read this file before editing strategies/a_asian_continuation.py.
---

# Strategy: a_asian_continuation

## What it does
Phase 13 ship — Asian session continuation breakout. A 5m close beyond the 17:00-08:30 CT overnight range, padded by 0.5 × ATR_5m. Backtested 5 years on MNQ Databento: **+$5,909 baseline / PF 8.29 / 6/6 years positive**. Highest PF of the Phase 13 cohort.

## Trigger condition
CT window 03:00-08:00 (after Asian range is built). A 5m close beyond the overnight 17:00-08:30 CT range padded by `range_break_atr_mult × ATR_5m`:
- LONG: `close_5m > overnight_high + 0.5 × ATR_5m`
- SHORT: `close_5m < overnight_low - 0.5 × ATR_5m`

Fires at most **once per calendar day**.

## Entry gates
- **Window**: 03:00-08:00 CT
- **ATR-padded range break** (`range_break_atr_mult=0.5`)
- **Min overnight range**: 8 ticks (skip flat-range nights)
- **Daily fire-once cap**
- **Bar warmup**: needs ~5 hours of bars covering 17:00-08:30 CT to build the range

## Stop / target
- Stop: `distance = min(distance_to_opposite_range_edge, 14 ticks)`, clamped to ≥ 6 ticks
- `min_stop_ticks=6`, `max_stop_ticks=14`
- Target (legacy lab): entry ± 2 × stop_distance
- **Phase 13 override**: `exit_policy = time_exit(30m)`, `order_type = market`, `entry_mode = first_touch`
- Belt-and-suspenders: `max_hold_min=30` behind TimeExitPolicy

## Data dependency
Requires `market["atr_5m"]` and a self-maintained overnight high/low window rebuilt every `evaluate()` call from `bars_1m` covering the 17:00-08:30 CT span. Falls back to NO_SIGNAL if < ~5 hours of bars available.

## Known issues
None open.

## Reference files
- `strategies/a_asian_continuation.py:1-39` — full docstring
- `config/strategies.py:886-905` — config block
- `core/exit_policies.py` — `PHASE_13_EXIT_ASSIGNMENTS` (time_exit binding)
- `tools/phoenix_new_strategy_lab.py` — backtest source

## DO NOT
- Do NOT remove the daily fire-once cap — overnight extremes can be re-broken in 03:00-08:00, and re-firing inflates the trade count without proportional edge.
- Do NOT widen the [6, 14] stop clamp without re-running the 5y backtest — the tight stop is part of the PF 8.29 edge.
- Do NOT change `range_break_atr_mult=0.5` without backtest evidence — shallow probes get filtered by the ATR padding.
- Do NOT remove `max_hold_min=30` — it is intentional belt-and-suspenders behind TimeExitPolicy.
- Do NOT shift window start earlier than 03:00 CT — the strategy depends on the Asian range being substantially built.
