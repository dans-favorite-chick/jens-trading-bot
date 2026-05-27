**Step 1: Recon**
I treated `phoenix_bot` as the live project; `trading_bot_project`, `archive_mnq_trading_bot`, and `phoenix_lsr_build` look legacy or research-side.

- Data ingestion: NT8 TickStreamer C# files, [bridge/bridge_server.py](<C:/Trading Project/phoenix_bot/bridge/bridge_server.py:1>), [core/tick_aggregator.py](<C:/Trading Project/phoenix_bot/core/tick_aggregator.py:1>), Databento historical caches, yfinance/FRED/Finnhub/COT/Quiver/CNN/Reddit feeds, volumetric JSON capture.
- Signal generation: strategy configs in [config/strategies.py](<C:/Trading Project/phoenix_bot/config/strategies.py:67>), concrete strategies under `strategies/`, confluence gates, SMC/pattern detectors, HMM regime, intermarket/correlation modules.
- Execution: [bots/base_bot.py](<C:/Trading Project/phoenix_bot/bots/base_bot.py:3997>), `prod_bot.py`, `sim_bot.py`, [bridge/oif_writer.py](<C:/Trading Project/phoenix_bot/bridge/oif_writer.py:1>), NT8 OIF incoming/outgoing folders.
- Risk: [core/risk_manager.py](<C:/Trading Project/phoenix_bot/core/risk_manager.py:1>), `strategy_risk_registry.py`, `circuit_breakers.py`, [core/risk/risk_gate.py](<C:/Trading Project/phoenix_bot/core/risk/risk_gate.py:1>), daily flatten, kill switch scripts.
- Backtesting/research: [tools/phoenix_real_backtest.py](<C:/Trading Project/phoenix_bot/tools/phoenix_real_backtest.py:1>), [tools/validate_backtest_quality.py](<C:/Trading Project/phoenix_bot/tools/validate_backtest_quality.py:1>), `backtest_results/`, `out/`, labs and replay tools.
- Monitoring/ops: dashboard, watchdog/watcher agents, Telegram commands, incident logs, stdout/stderr logs.
- Deployment: Windows batch launchers, NT8 local integration, local ports `8765/8766/8767/5000`.
- AI/ML: Gemini agent council/pretrade/debrief, Chroma/RAG memory, HMM, FinBERT, pattern detectors; [core/xgboost_retrainer.py](<C:/Trading Project/phoenix_bot/core/xgboost_retrainer.py:1>) is a stub, not real retraining.

Apparent strategy: MNQ futures, mostly intraday, multi-strategy portfolio: momentum, VWAP pullbacks/reversion, IB breakout, spring setup, opening session, ES/NQ confluence, Asian continuation, multi-day breakout, inside bar, Raschke baseline. Current config says `LIVE_TRADING=False`, `ACCOUNT="Sim101"` in [config/settings.py](<C:/Trading Project/phoenix_bot/config/settings.py:24>), but sim routing spans many NT8 sim accounts.

Missing for production: mandatory fail-closed global risk gate, reliable order lifecycle/event stream, robust order-ID capture for stop modification, aggregate portfolio risk across accounts, real purged OOS/CPCV/PBO/DSR validation, backtest/live feature parity for CVD/volumetrics/MES, reliable live volumetric feed, log rotation, secret redaction, and clean deployment docs.

Summary: this is a Windows/NT8/OIF MNQ bot with a serious amount of engineering, many strategies, and a lot of safety scaffolding. But the current operational state is not healthy: only `watcher_agent.py` was running, bridge/dashboard health endpoints refused connections, and [incident_2026-05-24_10-00-17.txt](<C:/Trading Project/phoenix_bot/logs/incidents/incident_2026-05-24_10-00-17.txt:1>) says `prod_bot not running`. Ambiguity: whether you intend this for real money soon or only extended sim. I can still audit it, but that answer changes the severity.

**Step 2: Seven-Lens Audit**
- Strategy edge: C-. Strong: real strategy classes, 5y backtest effort, confluence gates, per-strategy configs. Weak: [docs/PHOENIX_BEST_PLAN.md](<C:/Trading Project/phoenix_bot/docs/PHOENIX_BEST_PLAN.md:1>) contains suspicious 70-80% win-rate optimism, while [out/validation_status_2026-05-22.md](<C:/Trading Project/phoenix_bot/out/validation_status_2026-05-22.md:1>) shows many live/sim strategies are WATCH, under-sampled, or negative. Consequential issue: you do not yet have a defensible out-of-sample edge.
- Data integrity: D+. Strong: tick price sanity, single-stream checks, Databento cache. Weak: historical CVD approximation, broken/fragile volumetric capture, missing MES live feed for ES/NQ confluence, flaky external feeds. Consequential issue: several live features are not reproduced honestly in backtest.
- Execution/infrastructure: D. Strong: STOPMARKET OIF, split-submit protection, live account guard. Weak: current stack is down, WS watchdog closes connections after quiet periods in [bots/base_bot.py](<C:/Trading Project/phoenix_bot/bots/base_bot.py:5620>), stop order IDs often missing, fill handling is polling/timeout based. Consequential issue: Python state can diverge from NT8 state.
- Risk management: C-. Strong: daily caps, per-strategy registry, actual stop-dollar gate, daily flatten. Weak: [orchestrator RiskGateSink](<C:/Trading Project/phoenix_bot/phoenix_bot/orchestrator/oif_writer.py:186>) fails soft if the pipe is unavailable, `RiskGate` appears disabled/not running, and there is no hard aggregate account exposure gate. Consequential issue: the protection layer is optional when it should be mandatory.
- AI/ML soundness: D. Strong: HMM/RAG/advisory modules exist. Weak: Gemini quota is exhausted, AI failures default mostly advisory/clear, XGBoost retrainer is a stub. Consequential issue: AI is operational noise right now, not edge.
- Observability/operations: C-. Strong: watcher, incidents, dashboard, logs, Telegram. Weak: watcher failed to keep the stack alive, incident AI burns quota, logs are huge, root README/main are stale. Consequential issue: you can observe failure after the fact, but not reliably self-heal.
- Capital-at-risk discipline: D+. Strong: live trading is currently off, which is good. Weak: plans discuss compounding and high annualized expectations before robust OOS/live validation. Consequential issue: I would not put real money on this tomorrow.

**Step 3: Brutal Questions**
What is working: the NT8/OIF bridge architecture is real; the strategy/risk configuration is explicit; the backtest tooling is non-trivial; the code has learned from past incidents, especially around STOPMARKET, daily flatten, and price sanity.

What is not working: the bot is not currently up; the watchdog/reconnect path is failing; AI quota is exhausted; order lifecycle truth is weak; risk gate behavior is fail-soft; strategy count exceeds validation quality.

What you should stop doing: stop expanding AI/pattern libraries from [STRATEGY_KNOWLEDGE_INJECTION_PROMPT.md](<C:/Trading Project/phoenix_bot/STRATEGY_KNOWLEDGE_INJECTION_PROMPT.md:1>) until execution, validation, and risk gates are boring. That work feels productive but mostly manufactures false confidence.

What you are not looking at: aggregate exposure across many sim accounts, pending GTC limit fills after Python gives up, NT8 working orders independent of Python state, backtest/live feature mismatch, secret leakage in logs, and whether the system behaves correctly on quiet/closed-market periods.

**Step 4: 80/20**
Value-producing 20%: `bridge_server.py` plus `oif_writer.py`, `BaseBot._enter_trade/_exit_trade`, strategy config discipline, Databento backtesting, per-strategy risk registry.

Risk/drag 20%: AI council/pretrade/debrief during live ops, dormant external feeds, stale legacy projects/docs, optional risk gate, dynamic stop moves without reliable order IDs, strategy sprawl.

Highest-leverage 7-day change: make the stack fail-closed and operationally stable: bridge/dashboard/bots/risk gate all supervised, WS heartbeat fixed, risk gate mandatory, pending order lifecycle explicit. Do this before adding strategies or optimizing AI exits; those alternatives improve a machine that currently cannot be trusted to stay synchronized with NT8.

Single thing to kill: the AI strategy-generation/pattern-ingestion push. It is not the bottleneck.

**Step 5: Fix Plan**
Now, this week:
1. Restore process health. DoD: bridge, dashboard, prod, sim, watcher, and risk gate run 24h without `process_down`.
2. Fix WS heartbeat semantics. DoD: no bot reconnect loop during quiet NT8 periods.
3. Make risk gate fail-closed if enabled. DoD: no fallback to direct OIF when gate is unreachable.
4. Implement pending limit timeout/adoption/cancel truth. DoD: every entry has final state: filled, canceled, adopted, or flattened.
5. Fix stop-order ID capture or disable strategies needing dynamic stop moves. DoD: no `[STOP_MOVE_NO_ID]` for active managed strategies.

Next, this month:
1. Add purged walk-forward/OOS/CPCV/PBO/DSR validation.
2. Build a backtest-vs-live feature parity report.
3. Cut active strategies to the few with enough live/sim evidence.
4. Add aggregate exposure/loss limits across all routed accounts.
5. Clean stale docs/root entrypoints/legacy launch confusion.

Later, next quarter:
1. Add MES live feed and validate ES/NQ confluence honestly.
2. Build real footprint/volumetric historical validation.
3. Consider tier sizing only after 30-60 stable live-sim sessions.
4. Move AI to offline review until it proves incremental edge.
5. Harden deployment beyond local Windows fragility.

Never:
- Do not auto-modify live params with AI.
- Do not add more strategies without predeclared kill criteria.
- Do not believe compounding projections from in-sample curves.
- Do not treat manual NT8 reload rituals as production operations.

**Step 6: Pre-Mortem**
Six months from now, this fails because a pending order fills while Python thinks it is flat, a stop move updates Python but not NT8, the risk gate silently bypasses, and an overfit strategy cluster bleeds across correlated MNQ regimes. The warning signs were already here: current process-down incident, quota-exhausted AI, WATCH-grade live validation, missing order IDs, broken volumetric/MES parity, and optimistic docs outrunning evidence.

Instrument now: NT8/Python position diff, working-order ledger, pending order age, stop ack/capture rate, aggregate exposure, data freshness by feature, live-vs-backtest drift by strategy, incident recurrence, and hard redaction for secrets in logs.

**Step 7: Final Question**
Are you trying to put real money behind this within the next 30 calendar days, or is the next 30 days strictly sim validation? I’ll revise the plan based on that answer.