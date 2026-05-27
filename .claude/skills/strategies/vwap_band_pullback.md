---
name: strategy-vwap_band_pullback
description: Phoenix strategy vwap_band_pullback (1σ/2σ VWAP-band pullback + RSI(2)). Triggers when modifying or debugging vwap_band_pullback, or analyzing signals from this strategy. Read this file before editing strategies/vwap_band_pullback.py.
---

# Strategy: vwap_band_pullback

## What it does
1σ/2σ VWAP-band pullback + RSI(2) — ported from b12 research. Runs alongside `vwap_pullback` (proximity) and `vwap_pullback_v2` for head-to-head lab data. Author prediction (b12 header): PF 1.5-1.8 at WR 45-55%, RR 1.5-2:1.

## Trigger condition
Strong-trend day; price pulls back into the 1σ VWAP band (or deeper to 2σ) instead of all the way to VWAP. Enter on bounce candle with RSI(2) oversold.

## Entry gates (LONG; SHORT is the mirror)
1. **HTF trend bullish** (≥ 3/4 TF votes bullish AND bullish > bearish)
2. **Value-zone touch**: bar low touched VWAP → lower_1σ, OR deeper pullback into lower_1σ → lower_2σ
3. **Bullish reversal bar**: bar close > bar midpoint
4. **Bounce completion**: bar close ≥ lower_1σ
5. **RSI(2) < 30** (oversold at dip)
6. **Volume ≥ 0.8× 20-bar average**
7. **min_bars=50** warmup
8. **min_tf_votes=2** (loosened 2026-05-13 from 3 — mean-reversion entries naturally fire when only the lowest TF has flipped)

## Stop / target
- Stop: outside 2σ band by 0.5 × ATR
- Clamped to [40, 200] ticks (max raised 120 → 200 in V2)
- If natural stop > `max_stop_ticks`, signal SKIPPED (Fix 8-style guard)
- **stop_fallback_mode="confirmation"** for over-clamp fallback
- Target: `target_rr=2.0`
- Max hold: 60 min

## Known issues
None open.

## Reference files
- `strategies/vwap_band_pullback.py:1-48` — full docstring (research basis + entry rules)
- `config/strategies.py:586-613` — config block

## DO NOT
- Do NOT raise `min_tf_votes` back to 3 without backtest evidence — pre-loosening, the gate rejected most band touches because reversal-bar formation only flips the lowest TF.
- Do NOT remove the trend-day filter — vwap_band_pullback is INTENDED to fire only on trending days (chop produces fake band-touch bounces).
- Do NOT lower `min_volume_ratio` below 0.8 — low-volume band touches are noise, not institutional defense.
- Do NOT lower `rsi_long_threshold` above 30 — the entire edge of the b12 research is RSI(2) extremes confirming overdone counter-moves.
