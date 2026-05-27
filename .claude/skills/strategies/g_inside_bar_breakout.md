---
name: strategy-g_inside_bar_breakout
description: Phoenix strategy g_inside_bar_breakout (Phase 13, 5m inside-bar breakout). Triggers when modifying or debugging g_inside_bar_breakout, or analyzing signals from this strategy. Read this file before editing strategies/g_inside_bar_breakout.py.
---

# Strategy: g_inside_bar_breakout

## What it does
Phase 13 ship — 5-min inside-bar breakout. Backtested 5 years on MNQ Databento (2021-05-17 → 2026-05-17): **+$11,300 baseline / PF 4.88 / 6/6 years positive**.

## Trigger condition
RTH window 08:45-14:00 CT, **only on 5m bar-close boundaries**. Three-bar pattern:
- `PARENT  = bars_5m[-3]` (outer reference)
- `INSIDE  = bars_5m[-2]` (must be inside parent: high ≤ parent.high AND low ≥ parent.low)
- `CURRENT = bars_5m[-1]` (must break inside bar by ≥ 1 tick)

Direction = LONG if `current.close > inside.high + 1t`, SHORT if `current.close < inside.low - 1t`.

## Entry gates
- **5m bar-close boundary** (`now_ct.minute % 5 == 0`)
- **Window**: 08:45-14:00 CT
- **Inside-bar quality**:
  - `inside_range >= 4 ticks` (avoid micro-bar noise)
  - `inside_range <= 0.85 * parent_range` (must actually be tighter)
- **Bar dedup**: doesn't fire twice on the same 5m bar boundary

## Stop / target
- Stop: opposite extreme of the INSIDE bar ± 1 tick
- Clamped to [6, 30] ticks — outside, signal SKIPPED
- Target (legacy lab): entry ± 2 × stop_distance
- **Phase 13 override**: `exit_policy = chandelier(lookback_bars=50, atr_mult=3.0, activate_r=1.0)`, `order_type = limit_5s` (Section U.2 — market orders chase RTH-open breakouts), `entry_mode = first_touch`

## Known issues
None open. Phase 13 validated.

## Reference files
- `strategies/g_inside_bar_breakout.py:1-39` — full docstring
- `config/strategies.py:924-940` — config block
- `core/exit_policies.py` — `PHASE_13_EXIT_ASSIGNMENTS` (chandelier binding)
- `tools/phoenix_new_strategy_lab.py` — backtest source

## DO NOT
- Do NOT evaluate on every tick — strategy is specced for 5m bar-close boundaries only.
- Do NOT remove the inside-bar quality gates (4t floor, 0.85 parent ratio) — they filter out micro-bars that would noise-fire.
- Do NOT change `order_type` to `market` — Section U.2 evidence shows market orders chase RTH-open breakouts; `limit_5s` was chosen specifically for this.
- Do NOT widen the [6, 30] stop clamp without re-running the 5y backtest.
- Do NOT remove bar dedup — pre-dedup the strategy could fire on each evaluate() within a single 5m bar.
