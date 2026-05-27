---
name: strategy-opening_session
description: Phoenix strategy opening_session (family of 6 sub-evaluators: open_drive, open_test_drive, open_auction_in, open_auction_out, premarket_breakout, orb, plus an orb_fade sub-eval). Triggers when modifying or debugging any of these sub-strategies. Read this file before editing strategies/opening_session.py.
---

# Strategy: opening_session

## What it does
Family of six opening-window sub-strategies dispatched by time-of-day and opening-type classification (`classify_opening_type` in `core/session_levels.py`). Each sub-evaluator targets a different opening pattern; only the one matching today's classification fires.

## Trigger condition
Each sub has its own time window and opening-type requirement:

| Sub | Window CT | Opening Type | Notes |
|---|---|---|---|
| premarket_breakout | 08:30-08:45 | any | Pre-RTH range breakout |
| open_drive | 08:35-09:00 | OPEN_DRIVE | Classifier rarely returns this on MNQ — loosened in V2 |
| open_test_drive | 08:30-09:00 | OPEN_TEST_DRIVE | Also rarely dispatched |
| open_auction_in | 09:30-12:30 | OPEN_AUCTION_IN | Most common |
| open_auction_out | 08:45-11:00 | OPEN_AUCTION_OUT | V2 PATCH 2: requires CVD divergence |
| orb | 08:45-14:30 | any | 15-min OR + 5m confirmation; CVD-aligned (V2) |
| orb_fade (sub) | within orb window | any | Fades failed breakouts (wick rejection + CVD div) |

## Entry gates (universal)
- News blackout (±5 min around high-impact releases)
- Max trades/day = 4 (V2 raised from 2)
- Day flat by 14:30 CT
- Volume confirmation on entry bar (each sub)
- Stops: `min_stop_ticks=32`, `max_stop_ticks=200`, confirmation-bar fallback above 80t (V2 PATCH 2)
- `stop_fallback_mode="confirmation"`

## Per-sub gate highlights (post-V2)
- **open_drive**: `min_displacement_pts=8` (was 15), `max_pullback_pct=0.40`, vol ratio 1.3
- **open_test_drive**: test buffer 4t (was 8), reversal vol ratio 1.3, time exit 75 min
- **open_auction_in**: wick_pct_min=0.50, vol ratio 1.2, time exit 12:30 CT
- **open_auction_out**: 15-min wait, vol ratio implicit, time exit 11:00 CT, **require CVD div = True**
- **premarket_breakout**: min_range_pts=15 (was 10), vol ratio 1.4, time exit 10:30 CT
- **orb**: 15-min window, range floor 11pt / cap **110pt** (raised 2026-05-22 from 80 after silent-strategy diagnosis), `orb_max_range_pct=0.008` adaptive, `require_cvd_aligned=True`, lookback 5 bars, target = 50% of OR, BE at 25% of OR, time exit 14:30 CT
- **orb_fade**: `min_wick_pct=0.50`, requires CVD divergence

## Stop / target
Per sub-evaluator stop with universal clamps. Targets are sub-specific (ORB uses % of OR; others use buffer-based or fade-target patterns). 1-contract exits with BE-on-milestone and time exit.

## Known issues
- **classifier rarely returns OPEN_DRIVE on MNQ vol profile** — open_drive and open_test_drive subs nearly never dispatch. V2 PATCH 2 loosened the gates; standing follow-up is to relax `_DRIVE_DISPLACEMENT_POINTS` in `core/session_levels.py` after 4+ weeks of observed displacement distribution.
- **2026-05-22 silent-strategy bug**: ORB cap of 80pt rejected ALL 414 in-window evals when day's OR was 96.2pt. Fixed by raising `orb_max_range_pts` to 110.
- ORB sub interacts with standalone `orb_v2` (currently disabled) and `orb_fade` (disabled) — make sure those don't double-fire.

## Reference files
- `strategies/opening_session.py:1-30` — module docstring (sub list)
- `strategies/opening_session.py:_evaluate_open_drive` and siblings — per-sub evaluators
- `config/strategies.py:482-584` — config block
- `core/session_levels.py` — `classify_opening_type`, `is_in_window`, `is_news_blackout`
- `core/confluence_gates.py` — `regime_veto`, `tf60m_es_gate`

## DO NOT
- Do NOT remove the universal max_trades_per_day cap — the per-sub time windows overlap and without the cap a single morning can fire 4+ separate signals.
- Do NOT change `orb_max_range_pts` back down to 80 without re-checking that day's OR width distribution (see silent-strategy diagnosis 2026-05-22 a481d78c).
- Do NOT remove `orb_require_cvd_aligned` without re-running the 5y backtest — blind ORB on MNQ runs ~50% WR; CVD-aligned runs ~65%.
- Do NOT promote `validated=True` for sub-evaluators that have only fired a handful of times — operator override is already on, but Wilson-CI guardrail still applies in Phase 10.
- Do NOT raise `stop_fallback_mode` back to a clamp-skip pattern without restoring the V1 80t threshold reasoning.
