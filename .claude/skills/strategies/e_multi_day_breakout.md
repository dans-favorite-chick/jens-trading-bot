---
name: strategy-e_multi_day_breakout
description: Phoenix strategy e_multi_day_breakout (Phase 13, 3-day RTH range breakout). Triggers when modifying or debugging e_multi_day_breakout, or analyzing signals from this strategy. Read this file before editing strategies/e_multi_day_breakout.py.
---

# Strategy: e_multi_day_breakout

## What it does
Phase 13 ship — multi-day breakout. A 5m close that breaks the highest high (LONG) or lowest low (SHORT) of the prior 3 RTH sessions. Backtested 5 years on MNQ Databento: **+$9,097 baseline / PF 6.79 / 6/6 years positive**.

## Trigger condition
RTH window 08:45-13:00 CT. A 5m close that BREAKS the highest high (LONG) or lowest low (SHORT) of the prior 3 RTH (08:30-15:00 CT) sessions by ≥ 1 tick. Fires at most **once per calendar day**.

## Entry gates
- **Window**: 08:45-13:00 CT
- **Lookback**: 3 RTH sessions
- **Break buffer**: 1 tick beyond prior 3-day extreme
- **Daily fire-once cap**

## Stop / target
- Stop: opposite extreme of the breakout 5m bar + 2 ticks buffer
- Clamped to [6, 30] ticks — outside, SKIPPED
- Target (legacy lab): entry ± 2 × stop_distance
- **Phase 13 override**: `exit_policy = chandelier(50, 3x, 1R)`, `order_type = limit_5s` (Section U.2), `entry_mode = first_touch`. The legacy 2R target is effectively replaced by a 10R-wide bracket placeholder which the ChandelierPolicy trails dynamically.

## Data dependency
Maintains a rolling list of `(date_str, rth_high)` / `(date_str, rth_low)` from previous RTH sessions, computed in-place from `bars_1m`. "Warm" once ≥ 3 prior RTH days are seen. Warmup is NOT the constraining factor in the 5y backtest.

## Known issues
None open.

## Reference files
- `strategies/e_multi_day_breakout.py:1-38` — full docstring
- `config/strategies.py:907-922` — config block
- `core/exit_policies.py` — `PHASE_13_EXIT_ASSIGNMENTS` (chandelier binding)
- `tools/phoenix_new_strategy_lab.py` — backtest source

## DO NOT
- Do NOT remove the daily fire-once cap — prior-3-day extremes can be re-tested multiple times in a session; the cap is essential.
- Do NOT change `order_type` to `market` — `limit_5s` per Section U.2 evidence.
- Do NOT widen the [6, 30] stop clamp without re-running the 5y backtest.
- Do NOT lower `lookback_days` below 3 without re-validating — the 3-session window is the published spec and what the backtest evaluated.
