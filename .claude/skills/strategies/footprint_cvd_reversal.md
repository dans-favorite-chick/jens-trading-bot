---
name: strategy-footprint_cvd_reversal
description: Phoenix strategy footprint_cvd_reversal (Sprint H v3, institutional 4-confluence reversal at HTF levels). Triggers when modifying or debugging footprint_cvd_reversal, or analyzing signals from this strategy. Read this file before editing strategies/footprint_cvd_reversal.py.
---

# Strategy: footprint_cvd_reversal

## What it does
Sprint H v3 — institutional 4-confluence reversal at MenthorQ HTF levels. Operates on a 1,500-tick volumetric stream from NT8 (Order Flow+ data emitted by `TickStreamer.cs` and persisted by `bridge/bridge_server.py:_handle_volumetric_bar`). Uses an Institutional Quality Score (IQS, 0-100) aggregating 4 confluence buckets.

## Trigger condition
IQS ≥ `entry_threshold_iqs` (default 70). IQS = sum of 4 confluence-bucket scores capped at 100:

1. **HTF level confluence** (25 pts): within buffer of MenthorQ Put Support / 0DTE / Call Resistance / 0DTE / HVL / HVL 0DTE. Fallback 15 pts for VP POC. **0 pts → no signal possible**.
2. **CVD divergence** (up to 30 pts): 15 base on multi-bar regular divergence over lookback + up to 10 magnitude-weighted + 5 single-bar delta-div bonus
3. **Footprint** (up to 25 pts): 15 stacked imbalance + 15 absorption + 5 oversized imbalance bonus, capped at 25
4. **CVD compression** (5 sub-dimensions × 5 pts each): delta magnitude shrinking, bar range shrinking, volume holding, effort/result spike, single-bar delta div

Tier metadata: A++ (≥90) / A (≥80) / B (≥70) / C (≥60, logged-only).

## Entry gates
- **MenthorQ level required** (`require_menthorq_level=True`)
- **Level buffer**: 8 ticks
- **Session windows**: 08:30-15:00 CT with 5-min skip at open and close
- **Lunch block**: 10:00-13:29 CT
- **min_history_bars**: 25
- **data_freshness_sec**: 90
- Block opposite-direction strong CVD entries (`block_negative_strong_long`, `block_positive_strong_short`)

## Stop / target
- Stop: bar low/high ± `stop_buffer_ticks` (4), clamped to [8, 60] ticks
- T1: 50% scale-out at +1R (`target_t1_rr=1.0`)
- T2: +2R (`target_t2_rr=2.0`)
- Time stop: 20 volumetric bars

## Known issues / status
- **DISABLED (enabled=False, validated=False) since 2026-05-21 Phase 13 ship audit pt3.** Strategy is dormant pending volumetric NT8 feed — logs `DATA_NOT_AVAILABLE` 100% of the time. Was `validated=False` but still loaded by sim_bot every tick. Killed entirely to stop log noise. Re-enable when (a) the volumetric feed lands AND (b) a 5y backtest justifies promotion.
- MenthorQ was retired in Sprint J — level-source dependency may need re-validation.

## Reference files
- `strategies/footprint_cvd_reversal.py:1-50+` — full docstring (4-confluence scoring + IQS spec)
- `config/strategies.py:658-705` — config block
- `bridge/bridge_server.py:_handle_volumetric_bar` — volumetric stream landing
- `ninjatrader/TickStreamer.cs` — volumetric emitter (NT8 side)

## DO NOT
- Do NOT enable without (a) volumetric feed live AND (b) 5y backtest evidence — the strategy is `enabled=False` precisely to stop log noise from dormant evals.
- Do NOT remove `require_menthorq_level=True` without a replacement level source — the strategy's edge is HTF-level confluence; without it, CVD/footprint alone is not enough.
- Do NOT lower `entry_threshold_iqs` below 70 — Tier C (60-70) is logged-only for tuning visibility, NOT a fire threshold.
- Do NOT relax the lunch block — chop zone, by design.
- Do NOT touch volumetric plumbing in `bridge_server.py:_handle_volumetric_bar` without operator sign-off (touches the bridge data path).
