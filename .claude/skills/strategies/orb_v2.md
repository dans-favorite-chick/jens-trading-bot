---
name: strategy-orb_v2
description: Phoenix strategy orb_v2 (NQ-tuned drop-in for orb with CVD-aligned filter + confirmation-bar stop fallback). Triggers when modifying or debugging orb_v2, or analyzing signals from this strategy. Read this file before editing strategies/orb_v2.py.
---

# Strategy: orb_v2

## What it does
Drop-in alternative to `strategies/orb.py`. Same Zarattini ORB methodology (15-min OR + 5m close confirmation + STOPMARKET trigger) with three NQ-2026 fixes that address why V1 fired almost never.

## Trigger condition
15-min OR built from RTH open (`session_open_et=09:30`). 5m close beyond OR high (LONG) or low (SHORT). V2 adds CVD-alignment requirement (recent 5-bar `bar_delta` sum must align with breakout direction).

## V1 → V2 fixes
**FIX A — Confirmation-bar stop fallback** instead of stop_distance reject. V1 rejected when (OR opposite + buffer) > 25pt = 100t. On NQ 2026 this is the typical case. V2 detects this and switches to confirmation-bar stop: just beyond recent 5-bar swing low/high. Typical NQ result: 16-40t stops that fit a $50/trade budget.

**FIX B — CVD-aligned filter.** V1 fires LONG on ANY 5m close above OR_high. V2 requires recent 5-bar `bar_delta` sum to align with breakout direction. Filters out ~35-45% of failed breakouts (which the complementary `orb_fade` then catches as REVERSAL signals). Result: ORB v2 fires fewer but higher-conviction signals (~65% WR vs ~50% blind on NQ research).

**FIX C — Tick-grid snapping.** V1 uses `round(price, 2)` producing off-grid prices like `21998.13` that NT8 may reject. V2 uses `snap_to_tick(price, 0.25)` everywhere.

## Entry gates
- **session_open_et**: "09:30" (RTH anchor)
- **or_duration_minutes**: 15
- **OR-range floor/cap**: min 11pt, max 80pt (or 4× ATR adaptive, hard cap 150pt)
- **max_entry_delay_minutes**: 60
- **require_cvd_aligned=True**, cvd_lookback=5
- **stop_buffer_ticks**: 2

## Stop / target
- Stop clamped to [12, 60] ticks
- Target: `target_rr=2.0`
- Confirmation-bar fallback when natural stop would exceed cap

## Known issues / status
- **DISABLED (enabled=False, validated=False) since 2026-05-20 Phase 13 ship audit pt2 (F-004).** B-002 notes orb_v2 produced only 1 trade in 5y backtest. Phase 13 plan ships `opening_session.orb` (with managed-exit chandelier), NOT orb_v2. Strategy is kept on disk for git-history and the V2-fix-pattern reference.

## Relationship to other strategies
- `orb` (V1, DISABLED): superseded
- `orb_v2` (this, DISABLED): superseded by `opening_session.orb` sub-evaluator
- `opening_session.orb` (ACTIVE): canonical ORB; uses range_pts cap 110 (raised 2026-05-22)
- `orb_fade` (DISABLED): counter-strategy for failed breakouts

## Reference files
- `strategies/orb_v2.py:1-50` — full docstring (V1→V2 fix journey)
- `config/strategies.py:762-782` — config block
- `strategies/orb.py` — V1
- `strategies/opening_session.py` — `_evaluate_orb` sub (canonical active ORB)

## DO NOT
- Do NOT re-enable without (a) a 5y backtest justifying re-promotion AND (b) reconciling against `opening_session.orb` (the canonical active ORB).
- Do NOT remove `snap_to_tick` — NT8 may reject off-grid prices.
- Do NOT remove `require_cvd_aligned=True` — that's the V2 quality filter; without it the strategy reverts to V1's blind-breakout 50% WR.
- Do NOT change `session_open_et` away from "09:30" — V1's ET-midnight anchor was the primary failure mode.
