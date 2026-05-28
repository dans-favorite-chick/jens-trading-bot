# Phoenix Bot — Master Context Package (System State Transfer)
_Generated 2026-05-28. Source of truth = the repo + git, NOT this snapshot. Verify before relying._

> **READ FIRST — three premises corrected vs the request that generated this:**
> 1. There is **no 5-month sub-second tick-by-tick dataset.** The order-flow data is ~3.5 weeks (2026-05-04 → present) of **1500-tick volume bars** with per-bar delta/buy/sell/POC/imbalances — not raw sub-second ticks.
> 2. There is **no Level-2 DOM dataset.** Only per-bar footprint imbalances exist; the backtester stubs `dom_imbalance`. DOM is not reconstructable from stored data.
> 3. **The 5-year backtest is NOT a faithful proxy for live entries** for indicator-sensitive strategies (proven this session — see §2/§3). Treat its PF/WR numbers as *gross/approximate*, not validated.

---

## 1. UNDERLYING STRATEGY BLUEPRINTS (MNQ primary; MES used only for relative-strength)

Strategy classes live in `strategies/*.py`; each exposes `evaluate(market, bars_5m, bars_1m, session_info) -> Signal | None`. **Exact entry/exit rules are in those files — the two below are verified; the rest are summarized and must be read from code for precision.**

**Trend / breakout systems:**
- `bias_momentum` (DOMINANT earner). Gates, in order: `regime_veto` (skips OVERNIGHT_RANGE etc.); `day_type` skip (trades trend/volatile, skips RANGE); `tf_bias` 1m & 5m alignment (the binding gate — 2-of-3 close vote); `tf60m_es_gate` (NQ-vs-ES relative strength); CVD veto (skips entries fighting net institutional flow). MACD-hist and DOM add *confluence* only. Default `base_rr_ratio=5.0`, `min_confluence=5.0`, `min_momentum_confidence=80`.
- `opening_session` (sub-evaluators: `orb`, `open_drive`, `premarket_breakout`) — ORB variant is a real earner. `orb_v2` is a broken redundant reimplementation (1 trade / 5y).
- `ib_breakout` (60-min initial-balance break + ATR), `compression_breakout_v2` / `_micro` (Bollinger/Keltner squeeze), `multi_day_breakout`, `inside_bar_breakout` (Phase-13C), `a_asian_continuation` (Phase-13C).
- `nq_lsr`, `es_nq_confluence` (needs MES bars; dormant when MES feed absent), `big_move_signal`.

**Mean-reversion / reversal systems:**
- `noise_area` (VERIFIED, currently `retired=true`): fires when price breaks a **noise cone** `UB/LB = max/min(today_open, prev_close)·(1 ± band_mult·sigma_open)`, `band_mult=0.7`; `sigma_open` = rolling 14-day mean of `|close/today_open−1|` bucketed by minute-of-day (seeded from `data/sigma_open_table.json` via `seed_history`, then accrues per bar). Confirmed by VWAP; 30-min cadence; needs ≥30 minute-buckets warmed.
- `vwap_band_pullback`, `vwap_band_reversion` (VWAP ± sigma bands), `spring_setup` (wick + delta + ATR; reads `cr_verdict`), `orb_fade`, `footprint_cvd_reversal` (needs volumetric stream).
- Retired/deleted: `dom_pullback` (deleted 2026-05-21, 0 trades/5y), `noise_area`/`spring_setup`/`vwap_pullback_v2` flagged net-negative after commissions.

**AI gate stack (5A–5D):** PreTradeFilter (CLEAR/CAUTION/SIT_OUT, fail-open), CouncilGate (7-voter, fail-open), SessionDebriefer, AIParamTuner (suggestions only — never auto-applies). Council/agents currently DISABLED (P0-4).

## 2. 5-YEAR BACKTEST FINDINGS & PARAMETERS — **treat as approximate, see caveat**

From `tools/phoenix_real_backtest.py` over 2021-05-17 → 2026-05-15 (databento MNQ+MES). Commission floor **$2.82/round-trip** ($0.50/tick MNQ).
- `bias_momentum`: **+$177,748 net (5y)** — the dominant real earner.
- `opening_session.orb`: **+$37,289 net.**
- `vwap_pullback_v2`: net **−$4,885** all-hours; **+$7,817** windowed (queued, NOT applied) — per-trade edge $1.92 < $2.82 floor → **net-negative after cost**. `spring_setup` per-trade edge $0.93 → also net-negative.
- Phase-13C (standalone lab, not promoted): `inside_bar_breakout` +$11.3k/PF 4.88, `multi_day_breakout` +$9.1k/PF 6.79, `asian_continuation` +$5.9k/PF 8.29.
- Targets/bar (success bar): PF ≥ 1.5, Sharpe ≥ 1.0 OOS, max intraday DD ≤ 15%, WFE ≥ 0.5, ≥200–300 trades for significance.
- **CAVEAT (critical):** the backtester reconstructs enrichment from 1-min databento bars; its `tf_bias` (2-of-3 close vote) diverges from the live NT8-tick-built bars on ~50% of bars, so **backtest entries do not match live entries** for tf_bias-gated strategies. These P&L numbers are gross/approximate and were the reason for the `FREEZE`.

## 3. CURRENT CODE ARCHITECTURE

**Live chain:** NinjaTrader 8 (MNQ front-month indicator) → `bridge/bridge_server.py` (WS server :8765 in / :8766 out) → bots → OIF files (`oif*.txt`) → NT8 ATI executes. Bots are dashboard-managed subprocesses (`dashboard/server.py` :5000) with `tools/watchdog.py` (5s auto-restart) + `tools/watcher_agent.py`.
- `bots/base_bot.py` (BaseBot, ~enrichment + loops), `bots/prod_bot.py`, `bots/sim_bot.py` (Sim101 paper; own `_evaluate_strategies` override). Trade path: `bots/_strategy_dispatch.py` (enrichment + signal pick) → `bots/_trade_entry.py` (`open_position`, writes `market_snapshot`) → `_trade_exit.py`/`_trade_closer.py`. Enrichment computed in `_strategy_dispatch` / `core/tick_aggregator.py` (`snapshot()`).
- **Backtester:** `tools/phoenix_real_backtest.py::CSVEnrichmentPipeline` — loads CSVs, yields per-1m enriched `market` dict, runs the SAME strategy classes. Has an opt-in **de-stub** (`enable_real_enrichment()`): real `cvd_health` (recorded delta), `es_nq_rs` (MES bars), `day_type`/`cr_verdict` (live core modules), live-parity `tf_bias` (2-of-3 vote), live `regime` (SessionManager). `--bar-source recorded` replays the bot's own bars.
- **Reconcile/validation (built this session, `tools/replay_enrichment/`):** `recorded_cvd.py`, `recorded_es_nq_rs.py`, `recorded_day_cr.py`, `recorded_bars.py`, `enrichment_audit.py` (field-by-field BT-vs-live), `fidelity_vs_eval_logs.py` (decision fidelity). `tools/reconcile_sim_vs_backtest.py --real-enrichment --bar-source recorded` is the freeze-lift gate.
- **State:** `config/strategies.py::FREEZE_ACTIVE = True` (blocks backtest-justified prod changes). `LIVE_TRADING=False` (Sim101 paper). Trade memory: `logs/trade_memory_{sim,prod}.json` (records `market_snapshot` incl. the 4 strategy-blocking fields since the 2026-05-28 fix). Contract roll 2026-06-19 / MNQM6.
- **Validated fidelity (recorded bars, this session):** price/vwap 100% (corr 1.000), tf_bias 1m 99.88% / 5m 97.40%, regime 100%. `bias_momentum` decision agreement 98.2%. Residual = `cr_verdict`/`day_type` (live recorded UNKNOWN frequently) + `cvd`/`dom` (not in bar data).

## 4. DATA ENVIRONMENT & SCHEMAS (verified 2026-05-28)

**A. 5-year OHLCV (databento) — `data/historical/`:**
- `mnq_1min_databento.csv` (1,771,336 rows), `mnq_5min_databento.csv` (354,270), `mes_1min_databento.csv` (1,770,663), `mes_5min_databento.csv`.
- Columns: `ts_utc` (e.g. `2021-05-17 00:00:00+00:00`), `ts_ct` (`-05:00`/`-06:00`), `symbol` (e.g. `MNQM6`), `open, high, low, close, volume`.
- Coverage: **2021-05-17 → 2026-05-15**. (Loader `_load_bars_from_csv` maps `ts_utc`→`ts` UTC tz-aware.)

**B. Order-flow — `logs/volumetric_history.jsonl` (10.3 MB):**
- **~3.5 weeks only: 2026-05-04 → present.** **1500-tick volume bars** (NOT sub-second ticks).
- Per-record keys: `type, ts` (naive machine-local CT, 7-digit frac sec), `instrument, bar_size_ticks=1500, open/high/low/close, delta, total_volume, buy_volume, sell_volume, poc, imbalances[] (price/bid_vol/ask_vol/ratio/side), stacked_buy, stacked_sell, max_imbalance_ratio, cvd_session`.
- This is **footprint/CVD**, NOT a continuous Level-2 DOM book. `cvd` = cumulative sum of `delta`.

**C. Per-bar enrichment snapshots — `logs/history/<date>_<bot>.jsonl` (89 files, 2026-04-11 → 2026-05-28; bots: prod/sim/lab):**
- `event:"bar"` (1m & 5m): OHLCV + `vwap, ema9, ema21, atr_1m, atr_5m, cvd, bar_delta, dom_imbalance, tf_bias{1m,5m,15m,60m}, tf_votes_*, regime`. `event:"eval"`: per-strategy result+reason + the above market fields + `cr_verdict`. `event:"entry"/"exit"`: trade context.
- **This is the live ground truth used to validate the backtester** (and the source for `--bar-source recorded`).

**D. Trade records — `logs/trade_memory_{sim,prod}.json`:** per trade: `bot_id, strategy, entry_time (epoch), direction, entry_price, stop_price, target_price, exit_time, exit_reason, pnl_dollars*, contracts, market_snapshot{...}`. Reconcile reads `market_snapshot` and requires `day_type, cr_verdict, cvd_health, es_nq_rs` (present only on trades recorded after the 2026-05-28 fix).

**KNOWN DATA GAPS for the new backtester:** no sub-second ticks; no L2 DOM; volumetric only from 2026-05-04; live `cr_verdict` was broken (UNKNOWN) ~2026-05-06→05-25 (B2-3 bug); databento ends 2026-05-15 (no overlap with the post-fix working-cr window → forward reconcile should use `--bar-source recorded`).
