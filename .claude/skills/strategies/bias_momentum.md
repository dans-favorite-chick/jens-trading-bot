---
name: strategy-bias_momentum
description: Phoenix strategy bias_momentum. Triggers when modifying or debugging bias_momentum, or analyzing signals from this strategy. Read this file before editing strategies/bias_momentum.py.
---

# Strategy: bias_momentum

## What it does
Port of V3 BiasMomentumFollow. Trades in the direction of multi-timeframe (15m + 5m + 1m) bias when momentum confirms. The baseline validated strategy and the largest claimed P&L line in the live record. Regime-aware: loosens gates during OPEN_MOMENTUM and MID_MORNING golden windows.

## Trigger condition
Multi-TF EMA stack alignment (15m + 5m + 1m must ALL agree on direction), momentum score above threshold, and VWAP-side gate. Active in all regimes except OVERNIGHT_RANGE (vetoed 2026-05-22 — 5y data showed WR drops to 36.0% and -$2.55/trade drag in that regime).

## Entry gates
- **Direction**: 15m + 5m + 1m EMA stack all aligned (hardcoded in `evaluate()`, not config)
- **Momentum confidence** ≥ `min_momentum` (per-regime; 80 in golden windows, 60 in low-vol)
- **Confluence** ≥ `min_confluence` (5.5 in golden windows, 5.0 in low-vol)
- **TF votes**: `min_tf_votes=2` (V2 loosened from 3)
- **VWAP gate** with VCR explosive-bypass (threshold 1.2, close-pos long 0.65 / short 0.35)
- **EMA9 extension gate**: reject if price > 60t from EMA9 outside golden windows
- **SHORT extra gates**: `short_extra_gate_enabled=False` in V2 (was True — too aggressive); when enabled, SHORTs require BOTH 1m and 5m bias = BEARISH
- **CVD health veto**: only veto on STRONG opposing CVD (threshold -0.4)
- **RSI bearish divergence**: hard gate (`rsi_div_hard_gate=True`)
- **Stop clamp skip**: if natural ATR stop > `max_stop_ticks` (200), SKIP signal — confirmation-bar fallback wired in via `stop_fallback_mode="confirmation"`
- **Trend-stall grace**: 60s after entry before trend_stall exit can fire
- **Early-session EMA fallback**: use 1m EMAs before 09:00 CT when 5m EMAs are still warming up
- **OVERNIGHT_RANGE regime**: hard veto (2026-05-22 ship)

## Stop / target
- Stop: ATR-anchored, `stop_atr_mult=2.0` from last 5m wick, clamped to [24, 200] ticks
- Fallback: confirmation-bar stop (V2) if natural stop > max
- Target: `target_rr=2.5` (recalibrated 2026-05-03 from 5.0 — most exits are managed `ema_dom_exit`, not target_hit)
- Max hold: 60 min
- BE arming: requires CLOSED 1m bar past trigger when `be_on_bar_close=True` (default)

## Known issues
- **Pre-Sprint-H session-block windows are EMPTY** — operator wants all-hours trading for visibility. Forensic data shows 10:00-13:29 CT = 0W/7L; restoration of those windows is a pre-live TODO documented in config/strategies.py:96-111.
- **F-16 / P1-1**: this is the strategy targeted by the live-vs-backtest reconciliation harness — the suspect compounding curve and overall P&L claims rest most heavily on bias_momentum.
- **Backtest enrichment is approximate**: `cvd_health` is stubbed, RSI div is stubbed in tools/phoenix_real_backtest.py — live behavior may differ.

## Reference files
- `strategies/bias_momentum.py:34-58` — `_REGIME_OVERRIDES` table
- `strategies/bias_momentum.py:60+` — `evaluate()` body
- `strategies/bias_momentum.py:78-82` — OVERNIGHT_RANGE veto
- `config/strategies.py:67-169` — config block
- `core/confluence_gates.py` — `regime_veto`, `tf60m_es_gate`
- `core/candlestick_patterns.py` — pattern confluence helpers

## DO NOT
- Do NOT raise `skip_on_stop_clamp` back to False without restoring the `stop_fallback_mode="confirmation"` pairing — the 2026-05-17 V2 deployment left it False under a "SIM TESTING — RESTORE before live" comment that didn't get restored for 3 days. F-012 fixed 2026-05-20.
- Do NOT remove the OVERNIGHT_RANGE veto without 5y backtest re-evaluation (the veto is documented in `CONFLUENCE_VOTER_RESEARCH_2026-05-21.md`).
- Do NOT collapse the 3-TF direction gate to 2-TF without reverting `min_tf_votes` first; the gate is hardcoded in evaluate().
- Do NOT restore the inline session_block_windows from comments without operator sign-off — they were intentionally emptied.
- Do NOT promote behavior changes to live without running the P1-1 reconciliation harness first (this is the most-watched strategy).
