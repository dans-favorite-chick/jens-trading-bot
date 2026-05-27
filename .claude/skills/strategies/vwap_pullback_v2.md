---
name: strategy-vwap_pullback_v2
description: Phoenix strategy vwap_pullback_v2 (V2 NQ-tuned drop-in replacement for vwap_pullback). Triggers when modifying or debugging vwap_pullback_v2, or analyzing signals from this strategy. Read this file before editing strategies/vwap_pullback_v2.py.
---

# Strategy: vwap_pullback_v2

## What it does
Drop-in alternative to `vwap_pullback` with confirmation-bar stop fallback replacing the V1 `skip_on_stop_clamp` pattern. VWAP-proximity pullback continuation entry on the bounce candle. Demonstrates the "stop-clamp → confirmation fallback" pattern that applies to 9 strategies bot-wide.

## Trigger condition
Price within 60 ticks of VWAP after a pullback excursion ≥ 8t. Bounce candle (confirmation bar) must form with TF agreement.

## Entry gates
- **VWAP proximity**: price within 60t of VWAP
- **Pullback excursion**: ≥ 8t from VWAP at recent extreme
- **Bounce candle confirmation** (REQUIRED)
- **EMA structure intact** (trend gate)
- **CVD direction check**
- **TF votes**: `min_tf_votes=2` (2 non-trend, 1 trend)
- **Trend-day MQ bias override**
- **Max trades/day**: 4

## Stop / target
- Stop: `stop_atr_mult=2.0`, clamped to [16, 200] ticks (V2 widens from V1's 40 / 120)
- **stop_fallback_mode = "confirmation"** — when natural ATR > max, use confirmation-bar stop (next bar's close-side stop) instead of skipping
- Target: `target_rr=1.8`
- All prices snapped to tick grid via `snap_to_tick`

## What V2 changes vs V1
1. `stop_fallback_mode="confirmation"` (was `skip_on_stop_clamp=True` — rejected everything wider than 120t)
2. `max_stop_ticks` raised 120 → 200 (NQ 2026 vol regime)
3. `min_stop_ticks` lowered 40 → 16 (NQ-appropriate floor)
4. Tick-grid snapping everywhere via `snap_to_tick`

## Known issues
None open.

## Reference files
- `strategies/vwap_pullback_v2.py:1-50` — full docstring
- `strategies/vwap_pullback.py` — V1 (DISABLED 2026-05-17; superseded by V2)
- `config/strategies.py:837-847` — config block
- `core/snap_to_tick` helper

## DO NOT
- Do NOT re-enable V1 (`vwap_pullback`) alongside V2 without a co-fire correlation analysis (`tools/strategy_correlation_audit.py`) — they target the same setup.
- Do NOT remove `snap_to_tick` — NT8 may reject off-grid prices like `21998.13`.
- Do NOT revert `stop_fallback_mode` to a skip-on-clamp pattern without restoring V1's tighter clamp range.
- Do NOT tighten `max_stop_ticks` below 200 without re-running on NQ 2026 vol data.
