---
name: strategy-ib_breakout
description: Phoenix strategy ib_breakout (Initial Balance breakout, regime-aware morning-only). Triggers when modifying or debugging ib_breakout, or analyzing signals from this strategy. Read this file before editing strategies/ib_breakout.py.
---

# Strategy: ib_breakout

## What it does
The most statistically validated NQ strategy on paper: 96.2% of NQ days break the IB, 74.56% WR on 15-min ORB. Regime-aware — only trades during OPEN_MOMENTUM and MID_MORNING.

## Trigger condition
Build IB during first `ib_minutes` (10 min — dropped from 30 to keep strategy alive across mid-day restarts) anchored to `session_open_et=09:30`. After IB completes, LONG on breakout above IB high (with CVD confirming +), SHORT below IB low (CVD -).

## Entry gates
- **Window**: only OPEN_MOMENTUM (min_confluence 2.0) and MID_MORNING (min_confluence 2.5); blocked in all other regimes
- **Session anchor**: `session_open_et=09:30` (mirrors ORB session-anchor fix 2026-05-15 — pre-fix the daily reset fired at ET-midnight, producing 3,472 `gate:ib_too_wide` rejections)
- **ib_minutes=10** (was 30 — 30-min IB couldn't build across mid-session restarts)
- **max_ib_width_atr_mult=4.0** (relaxed from 1.5 in 2026-05-15; 1.5 was tuned for SPY, MNQ 10-min IB at the open routinely runs 50-80pt = 2-3× the 5m ATR)
- **CVD confirmation required** (`require_cvd_confirm=True`) — CVD > 0 for LONG, < 0 for SHORT. Hard gate. Without it: pre-fix saw SHORT at IB low with CVD=+6.05M → -164t loss (buyers absorbing)
- **stop_at_ib_midpoint=False** (stops at full IB opposite for room)

## Stop / target
- Structural stop must fit within `max_stop_ticks=200` (V2 raised from 120). If (price - ib_low) or (ib_high - price) exceeds this, SKIP signal (Fix 8)
- `target_extension=1.5` × IB width
- `stop_fallback_mode="confirmation"` for over-clamp fallback
- Max hold: 60 min
- Strategy declares `computes_own_target=True` and `computes_own_stop=True` (target_rr / stop_ticks not from config)

## Known issues
- **Wilson-CI demotion 2026-05-13**: only 8 trades in live record, below TENTATIVE (n≥100). The 75% WR with 8 trades has 95% CI of 41-93% — noise, not evidence. `validated=True` is operator-override (Phase 6); Phase 10 restores n≥100 rule.

## Reference files
- `strategies/ib_breakout.py:1-50` — module docstring + `_REGIME_OVERRIDES`
- `strategies/ib_breakout.py:35-50` — `IBBreakout.__init__` (IB state)
- `config/strategies.py:277-314` — config block

## DO NOT
- Do NOT raise `ib_minutes` back to 30 without checking that bots can stay up across the entire IB window — 30-min IB couldn't build across mid-session restarts pre-2026-04-24.
- Do NOT tighten `max_ib_width_atr_mult` back to 1.5 — that's the SPY-tuned value and produced 100% rejection on MNQ.
- Do NOT remove `require_cvd_confirm=True` — without it, the strategy fires SHORTs into buying absorption (pre-fix evidence: SHORT at IB low + CVD +6.05M → -164t loss).
- Do NOT flip `validated=True` to True on live promotion without Wilson-CI check at n≥100 (operator override is Phase 6 only).
- Do NOT remove `session_open_et` config — pre-fix the daily reset fired at ET-midnight and the IB was built from overnight bars (`gate:ib_too_wide` dominant failure mode).
