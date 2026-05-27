---
name: strategy-spring_setup
description: Phoenix strategy spring_setup (Wyckoff Rule-of-Three liquidity-grab reversal). Triggers when modifying or debugging spring_setup, or analyzing signals from this strategy. Read this file before editing strategies/spring_setup.py.
---

# Strategy: spring_setup

## What it does
Port of MNQ v5 "Elite Spring" pattern. The Wyckoff "Rule of Three" liquidity-grab reversal: spring wick at S/R + VWAP reclaim + delta flip. All three must confirm. Validated, but the wick-and-reject pattern is structurally rare on MNQ — see Known Issues.

## Trigger condition
Recent 1m bars show a long lower wick (bullish) or upper wick (bearish) ≥ 6 ticks that swept S/R and reclaimed it. CVD must flip in the reversal direction. With V2 patches, must also have TF alignment (2/N votes minimum).

## Entry gates
- **min_wick_ticks** = 6 (long lower wick for LONG, long upper wick for SHORT)
- **require_vwap_reclaim** = False (V2 loosened from True 2026-05-17 — too gating)
- **require_delta_flip** = True (CVD direction confirms reversal)
- **require_tf_alignment** = True (must fire WITH dominant trend direction)
- **min_tf_votes** = 2 (V2 loosened from 3)
- **stop_at_structure** = True (stop at bar low/high ± buffer, NOT wick × multiplier)

## Stop / target
- Stop: structure-based — `min/max(last_bar, prev_bar) low/high ± 2 ticks` (FIX 2 from 2026-04-14)
- ATR-anchored mode: stop = wick_extreme ± (1.1 × ATR_5m). Anchored to wick low/high, NOT entry price (sits below defended level)
- Clamped to [40, 200] ticks (max raised from 120 in V2)
- `stop_fallback_mode="confirmation"` for over-clamp fallback
- Target: `target_rr=1.5` (1.5:1 minimum)
- Max hold: 15 min (short — this is a reversal scalp)

## Known issues
- **Pattern structurally rare on MNQ**: 48h log analysis on 2026-04-24 showed 1,250 NO_SIGNAL events (46% of evals reporting `no_spring_wick`). MNQ's intraday tape is mostly directional, not wick-and-reject. Was RETIRED then UN-RETIRED 2026-05-17 under operator override "all strategies firing" with V2 patches loosening gates (min_tf_votes 3→2, require_vwap_reclaim True→False).
- Re-evaluate after 30+ sim trades — if signal rate still < 1/week, consider widening `min_wick_ticks` or combining with VWAP mean reversion as confluence.

## Reference files
- `strategies/spring_setup.py:1-22` — module docstring (Rule of Three)
- `strategies/spring_setup.py:25-60+` — `evaluate()` body
- `config/strategies.py:170-205` — config block
- `config/settings.py` — `TICK_SIZE`

## DO NOT
- Do NOT revert `stop_at_structure=True` — pre-fix the stop was set at `wick × 1.5` which got stopped out at exact session lows. FIX 2 from 2026-04-14.
- Do NOT tighten `min_tf_votes` back to 3 without first confirming signal rate at 2 is acceptable — V2 loosening exists specifically because the strategy was producing < 1 signal/week.
- Do NOT lower `min_wick_ticks` below 6 without backtest evidence — sub-6t wicks are noise on MNQ.
- Do NOT remove the TF-alignment gate — pre-FIX-1, the strategy fired counter-trend springs that consistently failed (4 losses in pre-fix sim).
