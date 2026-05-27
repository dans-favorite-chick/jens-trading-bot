---
name: strategy-raschke_baseline
description: Phoenix strategy raschke_baseline (Phase 13, Linda Raschke 20-EMA trend pullback). Triggers when modifying or debugging raschke_baseline, or analyzing signals from this strategy. Read this file before editing strategies/raschke_baseline.py.
---

# Strategy: raschke_baseline

## What it does
Linda Raschke's canonical 20-EMA trend-pullback. Phase 13 ship — backtested 5 years on MNQ Databento data (2021-05-17 → 2026-05-17): **+$12,779 baseline / PF 4.10 / 6/6 years positive**. Largest-P&L Phase 13 strategy.

## Trigger condition
RTH window 08:30-15:00 CT, evaluated **only on 5m bar-close boundaries** (`now_ct.minute % 5 == 0`). Trend filter requires EMA21-EMA50 spread > 0.3 × ATR_5m. Pullback to EMA21 must touch in trend direction with bar close back across EMA21, then next 5m bar breaks the pullback bar's high (LONG) or low (SHORT) by ≥ 1 tick.

## Entry gates
- **5m bar-close boundary** (not on every tick)
- **Window**: 08:30-15:00 CT
- **Trend filter (ADX proxy)**: `e21 - e50 > 0.3 × atr_5m` for LONG, mirror for SHORT
- **Pullback bar**: in last 3 closed 5m bars, find one that touched EMA21 (low ≤ e21 + 2t buffer AND close > e21 for LONG; mirror for SHORT)
- **Entry trigger**: current 5m close breaks pullback bar's high (LONG) or low (SHORT) by ≥ 1 tick

## Stop / target
- Stop: opposite extreme of pullback bar ± 1 tick
- Clamped to [6, 40] ticks — outside that range, **signal SKIPPED**
- Target (legacy lab): entry ± 2 × stop_distance
- **Phase 13 override**: `exit_policy = time_exit(30m)`, `order_type = market`, `entry_mode = retest` (Section V.1 pilot: +$119 / 60d in entry-retest analyzer)
- Belt-and-suspenders: `max_hold_min=30` behind the TimeExitPolicy

## Known issues
None open. 5y backtest validated; Phase 13 ship plan promoted to `validated=True`.

## Reference files
- `strategies/raschke_baseline.py:1-46` — full docstring (entry logic spec)
- `config/strategies.py:942-962` — config block
- `core/exit_policies.py` — `PHASE_13_EXIT_ASSIGNMENTS` (time_exit binding)
- `core/confluence_gates.py` — `regime_veto`, `tf60m_es_gate`
- `tools/phoenix_trend_pullback_lab.py` — backtest source

## DO NOT
- Do NOT evaluate on every tick — the strategy is specced to fire **only on 5m bar-close boundaries**. Removing the `minute % 5 == 0` gate would 5× the signal rate with non-bar-close noise.
- Do NOT widen the [6, 40] stop clamp without re-running the 5y backtest — these bounds anchor the PF 4.10 result.
- Do NOT change `entry_mode` away from `retest` without checking Section V.1 evidence (the +$119 / 60d advantage is small but real).
- Do NOT remove the Phase 13 time_exit override — the legacy lab target (2 × stop) is a wide bracket placeholder, not the intended exit.
- Do NOT touch `max_hold_min=30` — it is intentional belt-and-suspenders behind `TimeExitPolicy` for the case where the policy mis-fires.
