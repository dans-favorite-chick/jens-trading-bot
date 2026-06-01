# Phoenix Bot — Architecture

Topical reference. For ongoing changes consult [`memory/context/CURRENT_STATE.md`](../memory/context/CURRENT_STATE.md).

---

## 1. Data flow (one figure to remember)

```
NinjaTrader 8 ── (TickStreamer.cs Indicator) ─→ WebSocket :8765
                                                   │
                                                   ▼
                                          bridge/bridge_server.py
                                          (fans out to :8766)
                                                   │
                              ┌────────────────────┼────────────────────┐
                              ▼                    ▼                    ▼
                       bots/prod_bot.py    bots/sim_bot.py     other listeners
                       (BaseBot subclass)  (16-account sim)    (dashboards, tools)
                              │                    │
                              └─────── OIF JSON ───┴───┐
                                                       ▼
                              C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\
                                                       │
                                                       ▼
                                       NT8 ATI executes orders
                                                       │
                                                       ▼
                                                NT8 outgoing\ ─→ reconciled by core/startup_reconciliation.py
```

Health endpoint: `http://127.0.0.1:8767/health`. Dashboard: `http://127.0.0.1:5000/`.

Ports — single source of truth in [`config/settings.py:48-51`](../config/settings.py):
- `:8765` — bridge WS server, NT8 connects out to here
- `:8766` — bridge WS server, bots connect to here
- `:8767` — bridge health HTTP
- `:5000` — Flask dashboard

---

## 2. Component inventory

### Data ingestion / aggregation
- [`ninjatrader/TickStreamer.cs`](../ninjatrader/TickStreamer.cs) — NT8 Indicator
  (not Strategy). Emits raw ticks, heartbeats, volumetric snapshots on a
  1500-tick chart, MarketDepth summaries.
- [`bridge/bridge_server.py`](../bridge/bridge_server.py) — single-stream enforced
  (`PHOENIX_BRIDGE_SINGLE_STREAM=1`), peer-MAD validator
  (`PHOENIX_STREAM_VALIDATOR=1`), price-sanity guard.
- [`core/tick_aggregator.py`](../core/tick_aggregator.py) — builds 1m / 5m / 15m
  / 60m bars + 300-tick bars; computes ATR, anchored VWAP, EMAs, CVD.
- [`bridge/footprint_builder.py`](../bridge/footprint_builder.py) — volumetric
  buy/sell footprint bars.
- External feeds: yfinance VIX, FRED macros, Finnhub news, MenthorQ gamma
  (retired in Sprint J), Databento historical OHLCV + 2 months of TBBO ticks.

### Strategies
- Base class: [`strategies/base_strategy.py`](../strategies/base_strategy.py).
- Active (Phase 13 ship list): `bias_momentum`, `opening_session.orb`,
  `spring_setup`, `raschke_baseline`, `g_inside_bar_breakout`, `vwap_pullback_v2`,
  `e_multi_day_breakout`, `a_asian_continuation`, `vwap_band_pullback`,
  `ib_breakout`.
- Dormant: `es_nq_confluence` (waiting on MES feed wiring),
  `footprint_cvd_reversal` (volumetric stream OK; data quality observation).
- Retired/killed (do not re-enable without re-validation):
  `compression_breakout_v2`, `compression_breakout_micro`, `noise_area`,
  `high_precision_only`, `orb_fade`, `orb_v2`, `big_move_signal`, `nq_lsr`.
- Deleted: `dom_pullback` (2026-05-21) — file removed because canonical 5y
  backtest produced 0 trades; **note**: the backtester lacks L2/DOM data, so
  this deletion is being revisited (see [roadmap.md](roadmap.md) P2-5).

Config: [`config/strategies.py`](../config/strategies.py).

### Execution path
- [`bridge/oif_writer.py`](../bridge/oif_writer.py) — atomic `.tmp` → `.txt`
  staged write. Every filename prefixed `phoenix_<pid>_`. PhoenixOIFGuard
  NT8-side AddOn quarantines anything else in `incoming/`.
- [`phoenix_bot/orchestrator/oif_writer.py`](../phoenix_bot/orchestrator/oif_writer.py) —
  optional `RiskGateSink` over Windows named pipe (`\\.\pipe\phoenix_risk_gate`).
  Currently fail-soft to `DirectFileSink` on pipe failure; see
  [`audits/SYNTHESIS_2026-05-24.md`](audits/SYNTHESIS_2026-05-24.md) F-05.
- [`config/account_routing.py`](../config/account_routing.py) — maps strategy →
  NT8 sub-account (16 sim accounts).
- [`core/position_manager.py`](../core/position_manager.py) — position state,
  HWM/MAE/MFE, exits.
- [`core/startup_reconciliation.py`](../core/startup_reconciliation.py) — replays
  NT8 outgoing files at bot start to recover position state.

### Risk
- [`core/risk_manager.py`](../core/risk_manager.py) — per-bot caps (daily,
  weekly, per-trade), recovery mode, VIX gate, cooloff, spacing. The
  `[CAP:...]` log signatures are once-per-state-transition — watcher greps them.
- [`core/strategy_risk_registry.py`](../core/strategy_risk_registry.py) — one
  isolated `RiskManager` per strategy, per-strategy daily cap, $1,500 floor halt.
- [`core/circuit_breakers.py`](../core/circuit_breakers.py) — coarse halt
  conditions.
- [`core/tier_sizer.py`](../core/tier_sizer.py) — `tier_3000` compounding policy
  (1 contract per $3K equity, 30-contract cap). **Dormant** —
  `SIZING_MODE="flat_1"` in [`config/settings.py:289`](../config/settings.py).
- [`core/risk/risk_gate.py`](../core/risk/risk_gate.py),
  [`tools/risk_gate_runner.py`](../tools/risk_gate_runner.py),
  [`tools/watchdog_runner.py`](../tools/watchdog_runner.py) — out-of-process
  risk gate (Windows named pipe). Off by default (`PHOENIX_RISK_GATE=0`).

### Master bot loop
- [`bots/base_bot.py`](../bots/base_bot.py) — 5,951 LOC. Strategy dispatch,
  signal handling, AI filter call site, OIF write trigger, daily flatten,
  hydration, market enrichment, sub-strategy overrides. This is the god-class;
  decomposition is queued as P4-1.
- [`bots/prod_bot.py`](../bots/prod_bot.py) — production bot (validated
  strategies). 88 LOC, thin wrapper.
- [`bots/sim_bot.py`](../bots/sim_bot.py) — 16-account simulated execution. 827
  LOC.

### Monitoring / ops
- [`dashboard/server.py`](../dashboard/server.py) — Flask, `:5000`. Slider
  control, REST API, TODAY card, Daily Stats, trade table, Grades tab, Logs tab.
- [`tools/watcher_agent.py`](../tools/watcher_agent.py) — escalation daemon:
  Telegram alerts at 60s threshold, Twilio SMS at 5-min threshold, NT8
  SILENT_STALL detection.
- [`tools/watchdog.py`](../tools/watchdog.py) — process-level watchdog with
  bulletproof restart (`creationflags=0`; see lessons_learned for the Windows
  zombie pattern).
- 11 scheduled tasks under `Trading PC` user: PhoenixBoot, PhoenixWatcher,
  PhoenixFinnhubNews, PhoenixFredMacros, PhoenixGrading, PhoenixMorningRitual,
  PhoenixPostSessionDebrief, PhoenixWeeklyEvolution, PhoenixRiskGate,
  PhoenixRiskWatchdog, PhoenixLearner.
- [`tools/daily_session_summary.py`](../tools/daily_session_summary.py) — 7-day-baseline anomaly detection.
- [`tools/validation_tracker.py`](../tools/validation_tracker.py) — Wilson 95%
  CI per strategy; tier classification.
- [`tools/oif_killswitch.py`](../tools/oif_killswitch.py) — writes
  `outgoing/halt_all.json` to halt new entries.

### AI advisory
- [`agents/council_gate.py`](../agents/council_gate.py) — 7-voter LLM consensus
  at session open / regime shift.
- [`agents/pretrade_filter.py`](../agents/pretrade_filter.py) — 3-second
  hard-timeout LLM sanity check before entry. Default `advisory` (log-only) —
  AI cannot block trades.
- [`agents/session_debriefer.py`](../agents/session_debriefer.py) — end-of-session coaching.
- [`agents/strategy_oracle.py`](../agents/strategy_oracle.py) — Phase 4D successor (replaced `agents/historical_learner.py` on 2026-06-01). Runs via `python -m tools.run_oracle <mode>` (research / weekly / daily). Outputs land under `logs/oracle/` with the proposal queue at `logs/oracle/pending_changes.json` (consumed by `agents/adaptive_params.py`).
- [`core/sentiment_finbert.py`](../core/sentiment_finbert.py) — FinBERT INT8 ONNX. `SENTIMENT_FLOW_ACTIVE=false` — installed, not active.
- [`core/hmm_regime.py`](../core/hmm_regime.py) — Hidden Markov regime classifier.

### Backtesting
- [`tools/phoenix_real_backtest.py`](../tools/phoenix_real_backtest.py) — 1314
  LOC. Uses bar-level CVD/delta proxy (`bar.delta = ±volume` based on close
  vs open) — see `audits/SYNTHESIS_2026-05-24.md` F-06.
- [`tools/phoenix_compounding_backtest.py`](../tools/phoenix_compounding_backtest.py) — `tier_3000` curve simulator.
- [`tools/phoenix_new_strategy_lab.py`](../tools/phoenix_new_strategy_lab.py) — research lab for Phase 13C winners.

---

## 3. Immutable technical rules (DO NOT CHANGE)

These rules are not preferences; each was learned from a specific incident.
Changing one without reading the linked incident risks repeating the failure.

1. **NT8 Indicator, not Strategy.** NinjaScript Strategies with
   `ErrorHandling=Stop` crash on any unhandled exception. Indicators are
   resilient. See [`ninjatrader/TickStreamer.cs`](../ninjatrader/TickStreamer.cs)
   and CLAUDE.md.
2. **Python is the WS server, NT8 connects out to it.** The reverse direction
   was tried and failed.
3. **OIF files are the execution path.** Atomic `.tmp` → `.txt` staged write,
   all filenames prefixed `phoenix_<pid>_`. PhoenixOIFGuard AddOn on the NT8
   side quarantines anything else. See [`bridge/oif_writer.py:1-41`](../bridge/oif_writer.py).
4. **NT8 data folder path is config-driven via `NT8_DATA_ROOT`.** The folder
   was migrated out of OneDrive on 2026-04-18 — see [incidents.md](incidents.md).
5. **No `Newtonsoft.Json` in C# NinjaScript.** NT8 does not bundle it; use
   `StringBuilder`.
6. **VWAP is computed in Python.** Order Flow+ license required for NT8-side
   VWAP; Phoenix derives its own from ticks.
7. **No raw-open of `logs/trade_memory.json`.** The canonical reader is
   `core.trade_memory.load_all_trades()` (it merges the legacy file with every
   per-bot `trade_memory_<bot>.json` and dedupes by `trade_id`). Twelve readers
   were silently drifting before the 2026-05-13 audit (commit `c9099d7`).
8. **Subprocess on Windows: `creationflags=0`, not `CREATE_NEW_PROCESS_GROUP`.**
   The latter kills child bots in 2-3 minutes. Fixed in `8b471af` 2026-05-12.
9. **Phoenix failures are SILENT by design of the dependency chain.** Process
   alive, dashboard "running" — but bot deaf. Every new feature must be designed
   to fail LOUDLY. See `feedback_silent_failures.md` in the operator's user-memory (`~/.claude/projects/C--Trading-Project/memory/`). <!-- LINK BROKEN 2026-05-25: was ../memory/feedback_silent_failures.md (external user-memory, not in repo) -->
10. **Code changes do NOT auto-deploy.** A `git commit` does not update a
    running bot; the process keeps its in-memory code snapshot from launch.
    Always flag "prod needs restart" after behavior-affecting commits.

---

## 4. Risk layers (defense in depth)

In order of fire:

1. **Per-trade $-budget** — `MAX_ACTUAL_STOP_DOLLARS_PER_TRADE = 50.0`
   ([`config/settings.py:45`](../config/settings.py)). Hard skip if the placed
   stop's dollar exposure exceeds this.
2. **Per-strategy daily cap** — `PER_STRATEGY_DAILY_LOSS_CAP = 200.0`
   ([`config/settings.py:271`](../config/settings.py)). One halted strategy
   doesn't take down siblings.
3. **Per-strategy $1,500 floor** — strategy halts permanently on its
   sub-account; manual re-enable required.
4. **Bot-wide daily cap** — `DAILY_LOSS_LIMIT = 200.0`.
5. **Bot-wide weekly cap** — `WEEKLY_LOSS_LIMIT = 150.0`. ⚠ **This is less
   than the daily cap.** A single $150 loss day closes the bot for the week.
   See `audits/SYNTHESIS_2026-05-24.md` F-02 and roadmap.md P0-3.
6. **Cooloff** — 10 min pause after 2 consecutive losses.
7. **VIX gate** — VIX ≥ 40: no trade.
8. **15-min trade spacing** between any two trades.
9. **Recovery mode** — at –$30 daily, cut size 50%.
10. **Daily flatten** — 15:53 CT (no new entries) → 15:54 CT (PRIMARY flatten)
    → 15:54:45 (WARN if still open) → 15:55 (NT8 Auto Close safety net) →
    16:00 CME maintenance break hard floor.
11. **B59 live-account hard guard** — `LIVE_ACCOUNT=1590711` is hardcoded as
    the *only* allowed real account for live trading. Never auto-routed in sim
    or test paths.
12. **Kill switch** — `tools/oif_kill_switch.py` writes
    `outgoing/halt_all.json`; bot detects on next cycle and refuses new entries.

**Layers that are missing or weak** (see synthesis F-07, F-20, F-21):

- No portfolio-level correlation cap. 11 strategies firing LONG in the same
  10-second window each see their own per-strategy cap; the global cap is the
  only stop.
- `RiskGateSink` is fail-soft. If `PHOENIX_RISK_GATE=1` is set and the pipe is
  unreachable, the bot falls back to direct OIF write with a one-shot WARN.
- No fill-latency / slippage telemetry. Backtest assumes `SLIPPAGE_TICKS_PER_SIDE = 2`.

---

## 5. Paths and constants (one-stop)

| Setting | Value | Source |
|---|---|---|
| Instrument | `MNQM6` (rolls `MNQU6 09-26` 2026-09-18, 8 days before) | [`config/settings.py:17-21`](../config/settings.py) |
| Account | `Sim101` | [`config/settings.py:23`](../config/settings.py) |
| Live trading | `False` (gated until acct ≥ $2,000; currently $300) | [`config/settings.py:24`](../config/settings.py) |
| Tick size / value | 0.25 / $0.50 per contract | [`config/settings.py:25-26`](../config/settings.py) |
| Commission round-turn | ≈ $4.82 / contract ≈ 9.6 ticks | [`config/settings.py:184-187`](../config/settings.py) |
| OIF incoming | `C:\Users\Trading PC\Documents\NinjaTrader 8\incoming` | [`config/settings.py:60`](../config/settings.py) |
| OIF outgoing | `…\outgoing` | [`config/settings.py:61`](../config/settings.py) |
| File fallback | `C:\temp\mnq_data.json` | [`config/settings.py:62`](../config/settings.py) |
| NT8 indicators | `…\bin\Custom\Indicators\` | CLAUDE.md |
| Sizing mode | `flat_1` (1 contract per entry) | [`config/settings.py:289`](../config/settings.py) |

Trading windows (CT):

- 08:30–11:00 — prod primary (open momentum + mid-morning)
- 13:00–14:30 — prod secondary (institutional repositioning)
- 13:00–15:00 — extended secondary on CONTINUATION score ≥ 4 (CR adaptive)
- 10:00–13:59 — universal `SKIP_HOURS_CT` lunch zone (F-010)
- 15:53 → 15:54 → 15:55 → 16:00 — daily flatten cascade

---

## 6. Memory system

`memory/` is a structured, write-back-on-`SessionEnd` knowledge base for the
operator's AI sessions. The top-level memory index `MEMORY.md` lives in the
operator's user-memory (`~/.claude/projects/C--Trading-Project/memory/`),
not the repo. <!-- LINK BROKEN 2026-05-25: was ../memory/MEMORY.md (external user-memory, not in repo) --> Two
categories:

- **`memory/context/`** — live state (auto-loaded each session). Files:
  `CURRENT_STATE.md`, `RECENT_CHANGES.md`, `KNOWN_ISSUES.md`,
  `OPEN_QUESTIONS.md`, `NIGHTLY_INTEGRITY.md`, `ROLLBACK_RUNBOOK.md`.
- **`memory/semantic/lessons_learned.md`** — curated long-term observations.

The `memory/` system is NOT documentation; it is operational state. Do not move
its files into `docs/`.
