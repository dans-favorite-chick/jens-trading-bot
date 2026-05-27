---
name: strategy-orb_fade
description: Phoenix strategy orb_fade (counter-strategy to ORB — fades failed breakouts with CVD divergence + wick rejection). Triggers when modifying or debugging orb_fade, or analyzing signals from this strategy. Read this file before editing strategies/orb_fade.py.
---

# Strategy: orb_fade

## What it does
Counter-strategy to ORB. When 5m close goes beyond OR boundary but CVD diverges + wick rejection + volume present → take the REVERSAL direction. Research: ~35-45% of NQ OR breakouts FAIL within first 15 min; WR of fading these failed breakouts is 65-75% on NQ (FuturesHive 2025, OrderFlow Labs 2024).

## Trigger condition
Multi-bar failed-breakout pattern within 08:45-12:00 CT:
- A bar in the recent past closed BEYOND OR (the breakout)
- Current bar has retraced BACK INSIDE OR (the reversal)
- Wick rejection ≥ 50%, CVD divergence, volume ≥ 1.3× baseline

Distinct from LSR's single-bar sweep-and-reject pattern. Runs INSIDE the opening_session dispatch window, complementing LSR (LSR tracks ALL liquidity levels; ORB FADE is OR-specific).

## Entry gates
- **Session window**: 08:45-12:00 CT
- **max_trades_per_day**: 2
- **min_wick_pct**: 0.50
- **min_volume_ratio**: 1.3
- **lookback_for_breakout**: 20 bars
- **cvd_lookback**: 5
- **volume_lookback**: 20
- **bar_freshness_sec**: 90
- **Bar dedup**: doesn't fire twice on the same bar epoch (`_last_signal_bar_ts`)

## Stop / target
- Stop: clamped to [8, 30] ticks
- Time exit: 30 min
- Coordinates with LSR by marking ORH/ORL as consumed in the LSR tracker on signal (60-min cooloff prevents re-trading same level)

## Known issues / status
- **DISABLED (enabled=False, validated=False) since 2026-05-20 Phase 13 ship audit pt2 (F-004).** PHASE_13_IMPLEMENTATION_PLAN §O explicitly KILLED the strategy (PF 0.34, -$255/5y, "anti-edge" verdict). Just flipping `validated=False` wasn't enough — sim_bot still loaded + fired signals through it (`only_validated=False`). Disabled entirely to stop compute burn and log noise.
- **2026-05-18 B3 bug fix**: `time.time()` freshness gate broke backtests because wallclock is 2026 while `last_bar_ts` is historical bar epoch. Fix at `strategies/orb_fade.py:159-166` uses `now_ct.timestamp()` instead. **THIS PATTERN IS CRITICAL — apply same fix to any new strategy with bar freshness gates.**

## Reference files
- `strategies/orb_fade.py:1-50` — module docstring (research basis)
- `strategies/orb_fade.py:154-168` — B3 bug fix (use `now_ct.timestamp()`, not `time.time()`)
- `config/strategies.py:739-760` — config block
- `core/confirmation_stop.py` — stop calculation
- `core/liquidity_levels.py` (optional) — coordinates with LSR tracker

## DO NOT
- Do NOT re-enable without (a) a fresh 5y backtest justifying re-promotion AND (b) operator sign-off — Phase 13 plan KILLED this strategy.
- Do NOT revert the B3 fix at lines 159-166 to `time.time()` — that breaks backtests. The B3 pattern is canonical for any strategy with bar freshness gates.
- Do NOT remove bar dedup (`_last_signal_bar_ts`) — the failed-breakout pattern is detected over multiple bars; without dedup the strategy fires every evaluate() while the condition holds.
- Do NOT remove LSR coordination (consume-on-signal) — double-fires with LSR on the same OR-boundary level were a real bug.
- Do NOT widen the time window past 12:00 CT — the failed-breakout edge is concentrated in the first 3.25 hours of RTH.
