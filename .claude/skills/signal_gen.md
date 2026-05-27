---
name: signal_gen
description: Phoenix signal-generation layer (anything inside strategies/, bots/base_bot.py signal-emit path, entries, exits, stops, targets, confluence gates). Read this before editing any strategy file or anything that emits a Signal.
---

# Layer: Signal Generation

## What this layer does
Strategies in `strategies/*.py` evaluate the `market` dict + `bars_5m` + `bars_1m` + `session_info` every tick and emit `Signal | None`. `bots/base_bot._handle_signal` routes accepted signals to `_enter_trade` which builds the OIF bracket (see the `risk_compliance` layer for OIF rules).

## Strategy interface
- `strategies/base_strategy.py` — `BaseStrategy` ABC + `Signal` dataclass
- `evaluate(market, bars_5m, bars_1m, session_info) -> Signal | None`
- Class attributes: `name` (required), optionally `computes_own_target=True` / `computes_own_stop=True` to opt out of config-driven stop/target math

## Per-strategy detail
Strategy-specific gates, stops, targets, and historical regression context live in `.claude/skills/strategies/<name>.md`. Read the relevant strategy skill BEFORE editing its `.py` file. The strategy skills also document the V2 deployment pattern and Phase 13 ship overrides.

## ⚠️ MANDATORY VERIFICATION BLOCK — before delivering signal-logic changes
Before delivering ANY signal-logic change, explicitly confirm in your response:

1. **No look-ahead bias** — every pandas indicator / rolling computation derived from bars is shifted ≥ 1 period so the current bar's close/high/low is NOT used in the gate that decides whether to enter on the current bar's signal.

2. **`time.time()` is NOT used as a freshness gate** — use `market["now_ct"].timestamp()` instead (or whatever the strategy's `now_ct` source is). The canonical pattern is the B3 fix at `strategies/orb_fade.py:159-166`. Rationale: backtester wallclock is 2026, historical `last_bar_ts` is years old, so `time.time() - last_bar_ts` is always huge → backtest never fires. The strategy's own "now" works in both live AND backtest.

3. **Any new strategy ships disabled** (`enabled=False`, `validated=False`) until live validation. Standard Wilson-CI guardrail: n ≥ 100 live trades before flipping `validated=True`. Operator override (Phase 6 / V2 deployment) is documented exception — do NOT apply it to NEW strategies.

If your change cannot satisfy one of those three, call it out explicitly and explain why.

## Layered design rules
- Strategies must NOT call OIF writers directly — they emit `Signal`; `bots/base_bot._enter_trade` is the single OIF gateway. (See `risk_compliance` skill.)
- Strategies must NOT mutate `market`, `bars_5m`, `bars_1m`, or `session_info`. Treat as immutable inputs.
- Strategy `__init__` may hold per-instance state (e.g. IB warmup, `_last_signal_bar_ts` dedup) — this is the only allowed strategy-level state.
- Tick-grid: every price emitted must be on the 0.25 grid. Use `snap_to_tick` (see `vwap_pullback_v2`, `orb_v2` for canonical pattern). Off-grid prices like `21998.13` may be rejected by NT8.

## Phase 13 exit-policy overrides
Strategies promoted in Phase 13 (`a_asian_continuation`, `e_multi_day_breakout`, `g_inside_bar_breakout`, `raschke_baseline`) have legacy lab `target_rr` values that serve as wide-bracket placeholders. The REAL exit is bound in `core/exit_policies.PHASE_13_EXIT_ASSIGNMENTS` and applied by `bots/base_bot._apply_phase13_overrides()` at signal emit:
- `a_asian_continuation` → `time_exit(30m)` + `market` + `first_touch`
- `e_multi_day_breakout` → `chandelier(50, 3x, 1R)` + `limit_5s` + `first_touch`
- `g_inside_bar_breakout` → `chandelier(50, 3x, 1R)` + `limit_5s` + `first_touch`
- `raschke_baseline` → `time_exit(30m)` + `market` + `retest`

Do NOT change the legacy lab `target_rr` for these strategies as a way to change live exit — the override takes effect at emit time.

## Confluence helpers
- `core/confluence_gates.py` — `regime_veto`, `tf60m_es_gate`, `tf5m_es_gate`
- `core/candlestick_patterns.py` — `CandlestickAnalyzer`, `get_pattern_confluence`
- `core/session_levels.py` — `classify_opening_type`, `is_in_window`, `is_news_blackout`
- `core/session_manager.py` — 8 market regimes
- `core/exit_policies.py` — `PHASE_13_EXIT_ASSIGNMENTS`, `TimeExitPolicy`, `ChandelierPolicy`

## DO NOT
- Do NOT add a new strategy without a corresponding `.claude/skills/strategies/<name>.md` skill file documenting entry gates, stops, and DO NOT rules.
- Do NOT change the `BaseStrategy` / `Signal` interface without updating EVERY strategy class — base_strategy is a load-bearing ABC.
- Do NOT promote behavior changes to live without operator sign-off if the strategy is named in `docs/PHOENIX_BEST_PLAN.md` §1.1 (the 11 winners).
- Do NOT delete a "silent" (zero-signal) strategy without first checking the eval log for the dominant gate — silent on MNQ may be a data-gap issue (volumetric/MES/L2), not a real anti-edge.
- Do NOT change the per-bar evaluation cadence (`now_ct.minute % 5 == 0` boundary) for Phase 13 5m strategies — backtest math depends on bar-close evaluation.
