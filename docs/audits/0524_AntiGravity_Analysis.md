Listed directory Trading%20Project
Listed directory trading_bot_project
Listed directory phoenix_bot
Viewed CLAUDE.md:1-124
Listed directory core
Listed directory config
Listed directory strategies
Listed directory bots
Listed directory bridge
Listed directory agents
Viewed audit_report.md:1-255
Viewed PHOENIX_ROADMAP_v4.md:1-443
Searched for "audit"
Viewed settings.py:1-341
Viewed prod_bot.py:1-89
Viewed sim_bot.py:1-800
Viewed account_routing.py:1-191
Viewed risk_manager.py:1-388
Viewed strategy_risk_registry.py:1-391
Viewed PROJECT_MEMORY.md:1-165
Viewed REBUILD_PLAN.md:1-390
Viewed OPERATOR_TODO.md:1-200
Listed directory tools
Viewed requirements.txt:1-31
Listed directory tests
Viewed pytest.ini:1-17
Viewed main.py:1-142
Viewed noise_area.py:1-400
Listed directory out
Viewed validation_status_2026-05-22.md:1-54
Viewed position_manager.py:1-800
Listed directory backtest_results
Listed directory grades
Viewed 2026-05-22.md:1-62
Viewed warmup_sigma_open.py:1-117
Viewed load_sigma_open_warmup.py:1-58
Ran command: `python -c "import glob; [print(f, line.strip()) for f in glob.glob('**/*.py', recursive=True) for line in open(f, encoding='utf-8') if 'load_sigma_open_warmup' in line]"`
Ran command: `python -c "import glob; [print(f, line.strip()) for f in glob.glob('**/*.py', recursive=True) for line in open(f, encoding='utf-8') if 'seed_history' in line]"`
Listed directory logs

I am currently running a fast background script to locate references to `load_sigma_open_warmup` and `seed_history` across the codebase. This will confirm whether the `noise_area` strategy is actually being warmed up correctly at bot startup, or if it is silently resetting on every launch. 

I will stop calling tools for a brief moment to let those background searches finish and return their outputs.

Ran command: `python -c "lines = open('bots/base_bot.py', encoding=\'utf-8\').readlines(); [print(f'Line {i+1}:\n' + ''.join(lines[max(0, i-5):min(len(lines), i+10)])) for i, l in enumerate(lines) if 'seed_history' in l or 'load_sigma_open_warmup' in l]"`
Ran command: `python -c "lines = open('bots/base_bot.py', encoding='utf-8').readlines(); [print(f'Line {i+1}:\n' + ''.join(lines[max(0, i-5):min(len(lines), i+10)])) for i, l in enumerate(lines) if 'seed_history' in l or 'load_sigma_open_warmup' in l]"`
Listed directory data
Ran command: `python -c "lines = open('config/strategies.py', encoding='utf-8').readlines(); [print(f'Line {i+1}: {l.strip()}') for i, l in enumerate(lines) if 'noise_area' in l]"`
Ran command: `python -c "lines = open('config/strategies.py', encoding='utf-8').readlines(); print(''.join(lines[375:415]))"`
Ran command: `python -c "lines = open('config/strategies.py', encoding='utf-8').readlines(); print(''.join(lines[375:415]).encode('ascii', 'replace').decode('ascii'))"`
Ran command: `python -c "lines = open('config/strategies.py', encoding='utf-8').readlines(); print(''.join(l.strip() for i, l in enumerate(lines) if 'spring_setup' in l))"`
Ran command: `python -c "lines = open('config/strategies.py', encoding='utf-8').readlines(); idx = [i for i, l in enumerate(lines) if '\"spring_setup\":' in l][0]; print(''.join(lines[idx-5:idx+25]).encode('ascii', 'replace').decode('ascii'))"`
Ran command: `python -c "lines = open('config/strategies.py', encoding='utf-8').readlines(); idx = [i for i, l in enumerate(lines) if 'spring_setup' in l][0]; print(''.join(lines[idx-5:idx+25]).encode('ascii', 'replace').decode('ascii'))"`
Ran command: `python -c "lines = open('bots/base_bot.py', encoding='utf-8').readlines(); [print(f'Line {i+1}: {l.strip()}') for i, l in enumerate(lines) if 'can_trade' in l]"`
Ran command: `python -c "lines = open('bots/base_bot.py', encoding='utf-8').readlines(); print(''.join(lines[3495:3515]).encode('ascii', 'replace').decode('ascii'))"`

# PHOENIX SYSTEM AUDIT REPORT
**Auditor:** Quantitative Trading Systems Architect & Principal Auditor  
**Date:** May 24, 2026  
**Scope:** Architectural, Strategic, and Operational Review of the Phoenix Trading Bot  

---

## Step 1 — Reconnaissance Inventory

### 1. Functional Component Inventory

*   **Data Ingestion & Aggregation**
    *   [TickStreamer.cs](file:///c:/Trading%20Project/phoenix_bot/ninjatrader/TickStreamer.cs): Lean C# NinjaTrader 8 (NT8) indicator acting as a WebSocket client, broadcasting raw ticks to Python on port `8765`.
    *   [bridge_server.py](file:///c:/Trading%20Project/phoenix_bot/bridge/bridge_server.py): Python WS server that coordinates incoming ticks on `:8765`, buffers them, and fans them out to bots on `:8766`.
    *   [tick_aggregator.py](file:///c:/Trading%20Project/phoenix_bot/core/tick_aggregator.py): Builds time-based bars (1m, 5m, 15m, 60m), calculates ATR, Anchored VWAP, EMAs, and Cumulative Volume Delta (CVD) from raw ticks.
    *   [footprint_builder.py](file:///c:/Trading%20Project/phoenix_bot/bridge/footprint_builder.py): Aggregates ticks into volumetric buy/sell footprint bars.
*   **Signal Generation (Strategies)**
    *   [base_strategy.py](file:///c:/Trading%20Project/phoenix_bot/strategies/base_strategy.py): Abstract base class for all signal generators.
    *   Active Strategies: [bias_momentum.py](file:///c:/Trading%20Project/phoenix_bot/strategies/bias_momentum.py), [vwap_pullback_v2.py](file:///c:/Trading%20Project/phoenix_bot/strategies/vwap_pullback_v2.py), [orb_v2.py](file:///c:/Trading%20Project/phoenix_bot/strategies/orb_v2.py), [compression_breakout_v2.py](file:///c:/Trading%20Project/phoenix_bot/strategies/compression_breakout_v2.py), and [noise_area.py](file:///c:/Trading%20Project/phoenix_bot/strategies/noise_area.py) (currently marked `retired` in config).
    *   [session_manager.py](file:///c:/Trading%20Project/phoenix_bot/core/session_manager.py): Implements 8 time-of-day market regimes (e.g., `OPEN_MOMENTUM`, `AFTERNOON_CHOP`).
    *   [hmm_regime.py](file:///c:/Trading%20Project/phoenix_bot/core/hmm_regime.py): Hidden Markov Model regime classifier.
*   **Execution & Account Routing**
    *   [oif_writer.py](file:///c:/Trading%20Project/phoenix_bot/bridge/oif_writer.py): Writes Order Instruction Files (`oif*.txt`) to the local NT8 `incoming/` directory. Reads fills from the `outgoing/` directory.
    *   [account_routing.py](file:///c:/Trading%20Project/phoenix_bot/config/account_routing.py): Maps individual strategies to dedicated NT8 sub-accounts (e.g., `Sim_LSR`, `SimNoise Area`, `SimORB_v2`) for performance isolation.
    *   [position_manager.py](file:///c:/Trading%20Project/phoenix_bot/core/position_manager.py): Tracks active positions, updates high-water marks, calculates Maximum Adverse/Favorable Excursions (MAE/MFE), and handles exits.
*   **Risk Management**
    *   [risk_manager.py](file:///c:/Trading%20Project/phoenix_bot/core/risk_manager.py): Implements trade spacing, consecutive loss limits, recovery mode, and VIX filters.
    *   [strategy_risk_registry.py](file:///c:/Trading%20Project/phoenix_bot/core/strategy_risk_registry.py): Maps one isolated `RiskManager` per strategy. Tracks the $1,500 capital floor halt condition.
    *   [tier_sizer.py](file:///c:/Trading%20Project/phoenix_bot/core/tier_sizer.py): Governs the `tier_3000` compounding sizing logic ($3k equity per contract, max leverage cap).
*   **Monitoring & Observability**
    *   [server.py](file:///c:/Trading%20Project/phoenix_bot/dashboard/server.py): Flask server hosting the REST API and control panel UI.
    *   [telegram_notifier.py](file:///c:/Trading%20Project/phoenix_bot/core/telegram_notifier.py): Integrates Telegram alert feeds.
    *   [watchdog.py](file:///c:/Trading%20Project/phoenix_bot/tools/watchdog.py) & [watcher_agent.py](file:///c:/Trading%20Project/phoenix_bot/tools/watcher_agent.py): Logs monitoring, process crash handling, and heartbeat checking.
    *   [grade_open_predictions.py](file:///c:/Trading%20Project/phoenix_bot/tools/grade_open_predictions.py): Daily open-prediction grader checking session logs against invariant rules.
*   **AI Advisory Layer**
    *   [council_gate.py](file:///c:/Trading%20Project/phoenix_bot/agents/council_gate.py): LangGraph council of 7 LLM voters running at session open or regime shifts.
    *   [pretrade_filter.py](file:///c:/Trading%20Project/phoenix_bot/agents/pretrade_filter.py): LLM veto gate evaluating signals before execution.

### 2. Tech Stack, Hosting, and Infrastructure

*   **Languages & Frameworks:** Python 3.10+ (using `.venv-ml` for ML modules), C# (NinjaScript Indicator), Flask (Dashboard), asyncio/websockets.
*   **Hosting:** Local developer PC (Windows 11) in Frisco, TX. No VPS hosting (VPS plan was explicitly deprecated).
*   **Broker/Exchange Integration:** NinjaTrader 8 Automated Trading Interface (ATI) using text-based file polling via OIF.
*   **Data Sources:** CME Group feed via NinjaTrader (MNQ front month, currently `MNQM6 06-26`). Optional external REST feeds (yfinance, FRED, Finnhub, Databento).
*   **AI/ML Stack:** Gemini API (`gemini-2.5-flash` primary, `gemini-1.5-pro` for council), FinBERT ONNX INT8 local model for news sentiment, HMM regime classification (`hmmlearn`), and XGBoost retraining stubs.

### 3. Apparent Trading Strategy & Parameters

*   **Asset Class:** Micro E-mini Nasdaq-100 Futures (MNQ).
*   **Timeframes:** Multi-timeframe trend indicators (1m, 5m, 15m, 60m) combined with volumetric tick bars (300-tick or 1500-tick).
*   **Sizing Rules:** Currently set to `flat_1` (always 1 contract). A dormant `tier_3000` compounding mode exists.
*   **Risk Parameters:** Max $20 stop per trade (hard cap), $200 daily bot limit, $200 daily per-strategy cap, $1,500 capital floor per strategy account, VIX filter (VIX > 40 halts trading).

### 4. Critical Missing Production Components

1.  **Portfolio-Level Correlation Guard:** There is no centralized calculation of portfolio-wide correlation or directional exposure limit. If 4 strategies trigger simultaneous long signals on MNQ, the bot will execute all 4, creating an unhedged 4-contract exposure ($8/point risk) that can easily blow the daily limit on a single Nasdaq retrace.
2.  **Point-in-Time Order Book Database:** The system lacks historical Level 2/DOM archiving. Because order book data is not saved, order-flow strategies (such as `dom_pullback`) cannot be backtested historically.
3.  **Low-Latency Broker API Integration:** Operating execution via file-polling OIF is highly fragile, subject to disk I/O latency, and introduces 100ms–500ms of execution delay. A true production-grade futures bot should bypass NinjaTrader's file directory and write directly to a TCP socket or use a Rithmic/IB API bridge.

***

### Reconnaissance Summary
The Phoenix Trading Bot is a local Windows-hosted Python system designed to trade MNQ futures by using NinjaTrader 8 as an execution wrapper and data source. It utilizes a custom C# WebSocket client to pipe raw ticks to Python, where a monolithic codebase aggregates the data, runs indicators, runs a LangGraph AI council, evaluates a portfolio of 10+ active trend-following and mean-reversion strategies, and sends trade commands back via OIF file writes. The system is in the "live sim" validation phase, attempting to graduate strategies from paper-trading to live trading using a statistical hierarchy of tiers.

**Ambiguity Callout:** The actual operational link between the Hidden Markov Model (HMM) regime transitions, the LangGraph "Council of Seven" verdicts, and the strategy parameters is highly ambiguous. They exist as a loose advisory/logging layer, but there is no code that programmatically overrides strategy parameters based on their outputs at runtime. Furthermore, the timezone handling between local time (CT) and the strategy execution timezone (ET) shows potential drift risks in strategies like `noise_area` that rely on minute-of-day math relative to the 9:30 ET open.

---

## Step 2 — The Seven-Lens Audit

### 1. Strategy Edge
*   **Grade: C-**
*   **Strongest Evidence:** The `bias_momentum` strategy has a robust 5-year backtest performance (+308k, all positive years) and is the only strategy displaying a positive profit factor (1.46) over a significant sample (109 trades) in the live sim.
*   **Weakest Evidence:** The `noise_area` strategy (Zarattini 2024) failed live validation with a disastrous **0.24 profit factor** over 10 trades, while `vwap_pullback` (PF 0.75) and `spring_setup` (PF 0.52) are consistently bleeding capital.
*   **Most Consequential Issue:** **Academic-to-Asset Volatility Mismatch.** The system attempts to apply strategies optimized for the low-volatility S&P 500 ETF (SPY) directly to the highly volatile, trending Micro Nasdaq (MNQ). Hitting a $50 maximum actual stop-loss budget (`MAX_ACTUAL_STOP_DOLLARS_PER_TRADE`) on MNQ forces stop-loss placements that are far too tight for the natural volatility envelope of the asset, resulting in a 10% win rate on Noise Area before it hit the floor and was retired.

### 2. Data Integrity
*   **Grade: C**
*   **Strongest Evidence:** The WebSocket raw tick stream is successfully decoupled from the indicators, ensuring that Python handles all calculations deterministically.
*   **Weakest Evidence:** The backtester's inability to test L2 order flow data led the operator to delete `dom_pullback` (which was actually profitable in sim, PF 2.13) because it backtested to 0 trades.
*   **Most Consequential Issue:** **Dual-Path Validation Split.** Because L2 and volume footprint data are not archived or replayable in the backtester, the system is split. Half the strategies are validated via a 5-year backtest; the other half (`dom_pullback`, `footprint_cvd_reversal`) must be tested live in the sim bot. This breaks the scientific method and forces the operator to make strategy termination decisions without historical context.

### 3. Execution and Infrastructure
*   **Grade: D**
*   **Strongest Evidence:** The implementation of atomic bracket orders and stop-modify via cancel+replace order IDs.
*   **Weakest Evidence:** The file-polling OIF writer interface (`bridge/oif_writer.py`) which depends on Windows disk I/O and NinjaTrader's internal folder polling loop (up to 250ms latency).
*   **Most Consequential Issue:** **High-Latency Execution Loop.** In futures trading, a 100ms-500ms delay between signal and fill due to file writing is the difference between a profitable breakout and getting filled at the top of a wick (slippage). The transaction costs are already massive ($4.82/trade, or 9.6 ticks on a 1-contract trade), making high-latency execution a death sentence.

### 4. Risk Management
*   **Grade: C-**
*   **Strongest Evidence:** The `StrategyRiskRegistry` successfully isolates risk per strategy, using a $1,500 account floor to permanently halt failing strategies.
*   **Weakest Evidence:** The weekly loss limit ($150) is configured to be *smaller* than the daily loss limit ($200), meaning the daily limit is completely useless.
*   **Most Consequential Issue:** **Uncapped Portfolio Capital-at-Risk.** Because strategies run concurrently across separate Sim accounts without a centralized portfolio-level exposure cap, the system's total daily loss limit is the sum of the per-strategy limits. If 10 strategies have a highly correlated losing day (e.g. during a trend reversal), the total loss could be $2,000, wiping out the entire starter capital in one day.

### 5. AI/ML Soundness
*   **Grade: F**
*   **Strongest Evidence:** FinBERT ONNX model quantized for fast local CPU inference (<10ms).
*   **Weakest Evidence:** The "Council of Seven" (7 LLM agents) running at session start or regime shifts. It is pure decoration.
*   **Most Consequential Issue:** **Alpha-less Execution Latency.** The `PreTradeFilter` blocks execution to query the Gemini API via a web request. In active futures trading, adding a 1 to 3-second network latency barrier to check "retail exhaustion" or "macro traps" before submitting an order completely destroys entry execution. It is unvalidated "AI theater" that adds structural drag and api cost with zero proven alpha.

### 6. Observability and Operations
*   **Grade: B**
*   **Strongest Evidence:** The automated post-session grading framework (`tools/grade_open_predictions.py`) checks strategy and log invariants (such as off-cadence skips) and reports them in clean markdown formats.
*   **Weakest Evidence:** The grader code enforces strict expectations that do not match operational reality (e.g., expecting `spring_setup` to emit zero logs because it was retired, while the operator un-retired it in `config/strategies.py`).
*   **Most Consequential Issue:** **Grader-Config Divergence.** Because the grading scripts are hardcoded with static strategy assumptions, manual interventions in the strategy configs (like un-retiring `spring_setup` for V2 visibility) break the test suite, creating noise and false alarms that train the operator to ignore test failures.

### 7. Capital-at-Risk Discipline
*   **Grade: C**
*   **Strongest Evidence:** The bot runs with `LIVE_TRADING = False` by default and is strictly bound to Sim accounts.
*   **Weakest Evidence:** The system uses a 50-trade "graduation gate" inside the `PRELIMINARY` tier, which is statistically insufficient to prove edge before deploying real capital.
*   **Most Consequential Issue:** **Sim-to-Live Expectancy Deficit.** The portfolio's total simulated P&L is net negative post-B13. Moving from Sim to live trading with a system that fails to show positive expectancy *after* commissions and slippage on paper is a violation of capital discipline.

---

## Step 3 — The Four Brutal Questions

### 1. What's working?
*   **The WebSocket Aggregator Bridge:** The connection between NT8 `TickStreamer` and Python is highly stable and recovers from connection drops.
*   **`bias_momentum` Strategy:** This is the only strategy showing structural edge (PF 1.46, +$467.62 net). Its trend-following logic successfully captures directional runs on MNQ.
*   **Strategy Risk Registry:** The persistent halt state (`logs/strategy_halts.json`) is highly effective at shutting down failing strategies like `noise_area` before they drain the simulation account.

### 2. What's not working?
*   **Unconfirmed Mean-Reversion Strategies:** `vwap_pullback` and `spring_setup` are bleeding capital because they attempt to buy/sell wicks or band touches without volume/order flow confirmation. They are catching falling wicks in strongly trending markets.
*   **SPY Strategy Ports:** The `noise_area` strategy is completely non-viable on MNQ because it was designed for a low-beta, low-volatility equity index tracker.
*   **The Risk Limits Hierarchy:** The risk configuration has a fundamental hierarchy error: the weekly loss limit is lower than the daily loss limit.

### 3. What should I stop doing?
*   **STOP the Gemini API Council and PreTradeFilter:** Turn them off immediately in the settings. They add 1,000+ milliseconds of network latency to a tick-sensitive execution pipeline and contribute no statistical edge.
*   **STOP un-retiring strategies without structural changes:** Un-retiring `spring_setup` without changing its entry criteria or stop multipliers simply resulted in 1,268 useless log lines and continued simulated losses.
*   **STOP deleting strategies solely based on backtest failures when the backtest data is incomplete:** Deleting `dom_pullback` because it had "0 trades in 5y backtest" was a mistake; the backtester lacked the Level 2/DOM data required to evaluate it. The strategy was your best-performing simulated asset (PF 2.13).

### 4. What am I not looking at?
*   **The Micro Futures Friction Trap:** You are trading MNQ. At $4.82 per trade round-turn, a single contract requires a **9.6-tick move** just to clear transaction costs. If your average profit target is 20-30 ticks, your transaction costs are eating **16% to 24% of your gross P&L**. This is a massive statistical hurdle.
*   **Cross-Strategy Exposure Correlation:** You have 10+ strategies running concurrently. When Nasdaq drops hard, multiple strategies will generate SHORT signals simultaneously. Since there is no portfolio-level gross position cap, you will enter highly correlated positions, effectively leveraging up at the worst possible moment.

---

## Step 4 — The 80/20 Analysis

### The 20% producing 80% of the value:
*   The raw tick ingestion pipeline and the C# WebSocket bridge.
*   The `bias_momentum` strategy's trend-following logic.
*   The automated post-session grading and validation tools.

### The 20% producing 80% of the risk, drag, or wasted effort:
*   The Gemini AI Voter Council and the PreTrade veto filter.
*   Trading low-edge mean-reversion strategies (`spring_setup`, `vwap_pullback`) without volume or CVD confirmation.
*   Deleting the only profitable order-flow strategy (`dom_pullback`) due to backtest data limitations.

### The single highest-leverage change you could make in the next 7 days:
**Rebuild the backtester to ingest historical tick-level L2/footprint data, reinstate `dom_pullback`, and disable all AI latency-inducing filters.** This restores your only high-performing strategy (PF 2.13) to a validated, backtestable framework.

*   *Why this beats optimizing `vwap_pullback`:* Optimizing pullbacks without volume confirmation is a math exercise in curve-fitting.
*   *Why this beats activating compounding (`tier_3000`):* Compounding position sizing on a portfolio that is net-negative on paper will only accelerate the destruction of your simulated capital.

### The single thing you should kill even though it feels productive:
**Kill the Gemini AI integration.** Disabling the Voter Council and PreTrade filter removes execution latency, simplifies the codebase, and stops wasting API costs on non-alpha-generating LLM queries.

---

## Step 5 — The Fix Plan

### Now (This Week): Critical Fixes
1.  **Correct the Risk Limit Mismatch**
    *   *What:* Change `WEEKLY_LOSS_LIMIT` to `600.0` in [settings.py](file:///c:/Trading%20Project/phoenix_bot/config/settings.py).
    *   *Why:* Prevents a $150 loss on Monday from halting the bot for the entire week while keeping a logical 3x daily limit buffer.
    *   *Effort:* 1 minute.
    *   *DoD:* `WEEKLY_LOSS_LIMIT` is strictly greater than `DAILY_LOSS_LIMIT` and verified via tests.
2.  **Deactivate the AI PreTradeFilter and Council Gate**
    *   *What:* Set `AGENT_PRETRADE_FILTER_ENABLED = False` and `AGENT_COUNCIL_ENABLED = False` in [settings.py](file:///c:/Trading%20Project/phoenix_bot/config/settings.py).
    *   *Why:* Eliminates network latency (1–3s) before order entry.
    *   *Effort:* 2 minutes.
    *   *DoD:* Ticks process and generate signals without calling the Gemini API.
3.  **Resolve Grader/Strategy Configuration Mismatch**
    *   *What:* Disable `spring_setup` in [strategies.py](file:///c:/Trading%20Project/phoenix_bot/config/strategies.py) (`enabled = False`) and align [grade_open_predictions.py](file:///c:/Trading%20Project/phoenix_bot/tools/grade_open_predictions.py) to expect it to be retired.
    *   *Why:* Restores unit test integrity and stops spamming logs with useless wick evaluations.
    *   *Effort:* 15 minutes.
    *   *DoD:* `pytest` and `grade_open_predictions.py` run and pass with clean status.
4.  **Implement a Portfolio-Level Gross Contract Cap**
    *   *What:* Modify `_evaluate_strategies` in [sim_bot.py](file:///c:/Trading%20Project/phoenix_bot/bots/sim_bot.py) to enforce a hard cap on the total active contracts across all strategies (e.g., max 3 active contracts).
    *   *Why:* Protects the overall account from highly correlated drawdowns during volatile market turns.
    *   *Effort:* 2 hours.
    *   *DoD:* A unit test verifies that when 4 strategies emit simultaneous buy signals, only the first 3 are routed and the 4th is skipped.

### Next (This Month): Foundational Improvements
1.  **Reinstate and Re-Backtest `dom_pullback`**
    *   *What:* Re-introduce `dom_pullback` to `config/strategies.py` and map it to a dedicated sub-account. Write a script to convert the Databento footprint data into a format the backtester can read.
    *   *Why:* Leverages the only strategy that showed real edge (PF 2.13, net P&L +$509.26) in the live sim.
    *   *Effort:* 2-3 days.
    *   *DoD:* `dom_pullback` is active in `sim_bot` and has a working historical backtest using L2 data.
2.  **Add Volume/CVD Filters to Mean-Reversion Entries**
    *   *What:* Modify [vwap_pullback.py](file:///c:/Trading%20Project/phoenix_bot/strategies/vwap_pullback.py) to require a CVD divergence or absorption block before entry.
    *   *Why:* Prevents entering mean-reversion trades against strong momentum.
    *   *Effort:* 1 day.
    *   *DoD:* The strategy stops buying wicks that do not show buyer absorption.

### Later (Next Quarter): Strategic Bets
1.  **Direct TCP Socket/API Execution**
    *   *What:* Replace the OIF file-polling execution path with a direct TCP socket API to your broker (e.g. Rithmic API or IB API).
    *   *Why:* Reduces execution latency from 100-500ms down to <10ms, significantly decreasing slippage on breakouts.
    *   *Effort:* 2 weeks.
    *   *DoD:* Orders are routed and filled directly via API.

### Never
1.  **Never Use LLMs for Live Pre-Trade Filtering:** They are non-deterministic, too slow, and introduce massive execution risks in fast-moving futures markets.
2.  **Never Trade Scalping Strategies on MNQ:** The transaction costs are too high relative to the contract value.

---

## Step 6 — The Pre-Mortem

**Date:** November 24, 2026 (Six Months from Now)  
**Status:** The Phoenix Bot has completely blown up the $2,000 live account.

### What Went Wrong
A major market regime shift occurred during a CPI release. The Nasdaq went from clean trends to highly volatile, overlapping daily ranges. Because the AI Council and PreTrade filter were active, the bot spent 3 seconds waiting for Gemini to evaluate "macro traps" before entering a breakout trade. By the time the OIF order file was written, polled by NT8, and executed, the market had moved 40 points. The bot was filled at the very top of a wick (extreme slippage). 

Worse, because there was no portfolio-level correlation cap, 4 different strategies (all highly correlated) went long concurrently at the cash open. A sudden 100-point Nasdaq retrace occurred in seconds. The file-polling queue bottlenecked under heavy I/O, delaying the execution of the exit orders by 500ms. The stop orders filled with massive slippage, blowing past the daily stop-loss limit. By the time the bot finally shut down, the account had lost $950 in a single morning.

### Warning Signs Ignored
*   `NT8 SILENT_STALL` events in the logs showing 62-second tick delays while heartbeats remained fresh.
*   Transaction costs eating up to 40% of the simulated gains on mean-reversion pullbacks.
*   Running multiple instances of `sim_bot` simultaneously due to racy PID locks.

### What to Instrument Now to Prevent This
1.  **Slippage Alert:** Log the difference between the signal price and the actual fill price. Immediately pause the bot if slippage exceeds 4 ticks.
2.  **Hard OS-Level Single-Instance Watchdog:** Ensure the bot immediately kills itself if more than one PID is detected running the code.
3.  **Heartbeat/Tick Staleness Circuit Breaker:** Kill all active positions if the tick stream freezes for more than 5 seconds while heartbeats are active.

---

## Step 7 — The Final Question

> [!IMPORTANT]
> **Given that the round-turn transaction cost of $4.82 per contract on MNQ eats up 9.6 ticks of profit, why are you trading the micro contract (MNQ) instead of saving up capital to trade the mini contract (NQ), where the transaction cost is functionally identical (~$4.82) but represents only 0.96 ticks of profit due to the 10x larger point value?**

*If you are constrained to MNQ due to capital limits ($2,000), we must immediately disable all high-frequency or tight-target strategies (like scalping or Noise Area) and focus exclusively on high-expectancy trend-following strategies (like `bias_momentum` or `compression_breakout_30m`) where the target size is large enough to absorb the 9.6-tick friction fee.*

*If you are open to NQ, we will revise the plan to focus on getting your account size to a level where NQ is tradeable with a 1-contract sizing mode, which immediately unlocks the profitability of your pullback and mean-reversion strategies by removing 90% of the transaction friction.*