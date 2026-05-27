I'll start with reconnaissance before forming any opinions. Let me pull the full session state and inventory the codebase in parallel.

Let me get a structural view of the actual codebase in parallel — strategies, risk, execution, backtesting, and AI components.

Good — I have the structural picture. Now let me read the critical files: risk, sizing, the master bot loop, AI/council, config, and the most recent strategy doctrine.

I have a strong picture. Let me read the remaining critical pieces: known issues, the master bot loop entry path, the backtester, and the AI council.

Let me grab the backtester architecture, the ship plan, and the existing lessons-learned in parallel before forming my opinions.

Recon done. I have enough to write this honestly. What follows is long because you asked for end-to-end, but I've tried to keep every claim file-and-line anchored.

---

# PHOENIX BOT — FULL AUDIT

## STEP 1 — RECONNAISSANCE

### Inventory by function

**Data ingestion**
- NinjaTrader 8 with custom indicator [`ninjatrader/TickStreamer.cs`](ninjatrader/TickStreamer.cs)
- TCP/WebSocket bridge: [`bridge/bridge_server.py`](../../bridge/bridge_server.py) (943 LOC), [`bridge/footprint_builder.py`](../../bridge/footprint_builder.py), [`bridge/oif_writer.py`](../../bridge/oif_writer.py) (1,385 LOC)
- Aggregator: [`core/tick_aggregator.py`](../../core/tick_aggregator.py) (983 LOC) — builds bars, ATR, VWAP, EMA, CVD from ticks
- External feeds: FinBERT, FRED macros, Finnhub news, MenthorQ gamma (subscription retired)
- Databento historical: 5y MNQ + MES 1-min OHLCV, plus 44M ticks of TBBO

**Signal generation** — 27 strategy files / ~11,310 LOC:
- Active (Phase 13 ship list): `bias_momentum`, `opening_session.orb`, `spring_setup`, `raschke_baseline`, `g_inside_bar_breakout`, `vwap_pullback_v2`, `e_multi_day_breakout`, `a_asian_continuation`, `vwap_band_pullback`, `ib_breakout`
- Dormant: `es_nq_confluence` (waiting on MES feed), `footprint_cvd_reversal` (volumetric stream)
- Killed/retired/disabled: `compression_breakout_v2`, `compression_breakout_micro`, `noise_area`, `high_precision_only`, `orb_fade`, `orb_v2`, `dom_pullback` (deleted), `big_move_signal`, `nq_lsr`

**Execution** — order intent files (OIF) written to NT8's `incoming/`, ATI executes. No direct broker API.

**Risk management** — [`core/risk_manager.py`](../../core/risk_manager.py), [`core/simple_sizing.py`](../../core/simple_sizing.py), [`core/tier_sizer.py`](../../core/tier_sizer.py), [`core/strategy_risk_registry.py`](../../core/strategy_risk_registry.py), [`core/circuit_breakers.py`](../../core/circuit_breakers.py)

**Backtesting** — [`tools/phoenix_real_backtest.py`](../../tools/phoenix_real_backtest.py), [`tools/phoenix_compounding_backtest.py`](../../tools/phoenix_compounding_backtest.py), [`tools/phoenix_new_strategy_lab.py`](../../tools/phoenix_new_strategy_lab.py), ~30 other `phoenix_*_lab.py` and `phoenix_*_optimizer.py` scripts.

**Monitoring** — Flask dashboard on :5000, watcher_agent with Telegram + Twilio SMS escalation, watchdog auto-restart, 11 scheduled tasks (PhoenixBoot, PhoenixWatcher, PhoenixGrading, PhoenixMorningRitual, etc.)

**AI components** — [`agents/pretrade_filter.py`](../../agents/pretrade_filter.py), [`agents/council_gate.py`](../../agents/council_gate.py), [`agents/historical_learner.py`](../../agents/historical_learner.py), [`agents/session_debriefer.py`](../../agents/session_debriefer.py), [`core/sentiment_finbert.py`](../../core/sentiment_finbert.py). Default mode `advisory` (log-only) — currently AI cannot block trades.

**Master bot** — [`bots/base_bot.py`](../../bots/base_bot.py) at **5,951 lines** — the elephant.

### Tech stack
- Python 3.x on Windows 11, one Trading PC (no cloud, no VPS — operator explicitly chose local)
- NinjaTrader 8 + custom C# indicator (`TickStreamer.cs`)
- WebSockets/asyncio, Flask, pandas, FinBERT INT8 ONNX
- Gemini 2.5 Flash for AI agents (also Anthropic + others configured but unused for runtime)
- 99,763 lines of Python, 188 test files (~2,110 tests passing)

### Trading strategy
**Asset:** MNQ futures (Micro E-mini Nasdaq-100), one instrument.
**Timeframes:** 1m + 5m bars, plus 300-tick bars for entry precision.
**Signal logic:** A zoo of 10–16 simultaneously active strategies (breakout, momentum, mean-reversion, opening-range, fade) — each with its own gates, stops, targets.
**Position sizing:** Today flat 1 contract via [`SimpleSizer`](../../core/simple_sizing.py:119). Phase 13 plans `tier_3000` compounding (1 contract per $3K equity, 30-contract cap).
**Risk rules:** $20/trade hard cap, $200/day, $150/week, cooloff after 2 consecutive losses, 15-min spacing, VIX>40 = no trade, recovery mode at –$30 daily.

### Conspicuously missing or weak
1. **No walk-forward validation harness in production code.** The "CPCV / DSR / PBO" checkboxes in `weekly_evolution.py` have read "NOT YET RUN" for over a month. ([`docs/PHASE_13_IMPLEMENTATION_PLAN.md`](../../docs/PHASE_13_IMPLEMENTATION_PLAN.md) and [`KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md) confirm.)
2. **No slippage/fill model differentiated by liquidity regime.** Backtester assumes ~2t slippage RTH (`SLIPPAGE_TICKS_PER_SIDE = 2`); no overnight/news/thin-tape adjustment.
3. **No real broker integration for live trading.** Every order leaves Phoenix as a JSON file dropped in `Documents\NinjaTrader 8\incoming\` and is picked up by NT8 ATI. Bridge logic, but no API-level acknowledgments, no fill latency measurement, no broker-side reconciliation.
4. **No survivorship / point-in-time discipline check on Databento data.** Strategy lab uses raw historical OHLCV with bar-level CVD proxy (sign-of-close).
5. **No drift detection on FinBERT or Gemini.** Models are installed but no retraining cadence, no eval set.
6. **No "single source of truth" position state.** Position lives in NT8, in `position_manager.py`, in OIF outgoing files, in `trade_memory_<bot>.json` — and the 2026-05-13 audit fixed twelve different readers that had silently drifted. ([`KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md))
7. **No A/B harness for the AI council.** It runs in "advisory" by default, log-only — there's no proven uplift.
8. **No formal CI.** I see `pytest` running locally, scheduled tasks, but no GitHub Actions / blocking pre-merge tests for `weekly-evolution/*` branches.

### One-paragraph summary

Phoenix is a sprawling, locally-hosted, Python+NT8 algo trading rig built around one instrument (MNQ) with 10–16 concurrently active intraday strategies, a $300 real account currently paper-trading at Sim101 across 16 simulated $2K sub-accounts, an extensive backtesting toolkit driven from Databento 5y OHLCV, and a thick layer of observability (watcher, dashboard, Telegram, scheduled tasks). The operator is one person who's iterating extremely fast (multiple commits and "sprints" per day), has documented an honest awareness of the field's failure rate ("97% of retail algo traders lose money") and the system's own pathologies ("every Phoenix bug fails silently"), and is now post a "Phase 13" research round that claims an unflattened backtest of $1.5K → $1.09M over 5 years (tier_3000 compounding). The system is technically impressive in surface area but architecturally heavy for its actual scope, the AI layer is currently decorative, and the live-trading edge is still entirely unproven — no live money has been at risk since the real account hit $300, prod has always been paper.

**Ambiguous to me:** The actual relationship between sim_bot's 16-account validation results and the Phase 13 backtest results. Sim_bot has been live-paper for weeks; how do its measured numbers compare to the 5y backtest projections? You may already have this but I didn't see a single doc that maps "sim said X, backtest said Y, divergence Z." If you do, point me to it — that's the most important file in the project.

---

## STEP 2 — SEVEN-LENS AUDIT

### Lens 1 — Strategy edge: **C-**

**Strongest evidence:** A small set of strategies have a defensible structural story. `opening_session.orb` is Zarattini's published opening-range breakout — there's at least an academic prior that intraday breakouts in index futures have edge in the first hour. `a_asian_continuation`, `e_multi_day_breakout`, `g_inside_bar_breakout` all enter on momentum bar-close beyond a tight congestion — that's a classic continuation pattern with a real mechanism (volatility expansion after compression).

**Weakest evidence:** The three NEW Phase 13 winners post 70%, 78%, and 80.5% win rates with profit factors of 4.88, 6.79, and 8.29 over 5 years. Your own [`PHOENIX_BEST_PLAN.md`](../../docs/PHOENIX_BEST_PLAN.md:184) explicitly flags this: *"70-80% WR on the 3 new winners is suspicious vs literature (50-65% typical)"*. Your own [`lessons_learned.md`](../../memory/semantic/lessons_learned.md:31): *"Chasing 80%+ WR is a retail trap... One bad streak = blowup."* You've already written the contradiction; you just haven't acted on it.

The compounding backtest is even worse on this lens: $1,500 → $1.09M in 5 years with 34% max DD is a **727x** return. Renaissance Medallion, with hundreds of PhDs, ~40% gross over 30 years, was 51% WR. A 5-year 727x on a retail-built MNQ bot is, with respect, almost certainly an artifact of:
- Survivorship in the strategy selection (you tested 7 new strategies, kept the 3 winners — that's 4 looks per strategy worth of multiple-comparison inflation)
- Bar-level CVD/delta proxy that overstates clean breakouts ([`tools/phoenix_real_backtest.py`](../../tools/phoenix_real_backtest.py:160) literally sets `bar.delta = ±volume` based on close vs open)
- "Silent stop" bug that already added $177K to bias_momentum once and Phase 13 caught a **new variant** in Section V.3 (per your own ship plan)
- No walk-forward / CPCV / DSR (your own checkboxes admit it)
- A 5m timeframe where 300-tick bars are aliased into 5m candles in backtest but tick-streamed live — different distributions

**The single most consequential issue:** You're treating "5/6 years positive" as evidence of edge. With 8 strategies and 6 years, the probability that *at least one* shows 5/6 by chance is non-trivial — and once you've already filtered to winners ("kept these because they passed"), the conditional probability is much higher. There's no out-of-sample test. The Phase 13 backtest *is* the in-sample test.

### Lens 2 — Data integrity: **C+**

**Strongest evidence:** Databento is a reputable source. You've got 5 years of clean OHLCV plus 2 months of TBBO ticks, and the loader [`tools/phoenix_real_backtest.py`](../../tools/phoenix_real_backtest.py:107) handles both column conventions. Trade memory has a single canonical reader after the 12-file audit ([`KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md), commit `c9099d7`). Bridge-side `PriceSanity` guard catches corrupt-stream incidents (the "$40K phantom trade" episode).

**Weakest evidence:**
- Bar-level CVD/delta proxy. [`phoenix_real_backtest.py:159-164`](../../tools/phoenix_real_backtest.py:159) sets `bar.delta = float(row.volume) * sign(close-open)`. The docstring at line 50 acknowledges this *"understates magnitude on inside bars."* But CVD-aligned gates (`orb_v2`, `opening_session.orb`, `orb_fade`, `bias_momentum`'s CVD veto, footprint patterns) are CORE to most strategies — if the backtest's CVD signal is structurally cleaner than live (no tick-level aggressor noise), then live performance will systematically underperform.
- No survivorship/point-in-time validation. There's no roll-aware contract concatenation visible — and yet 5 years means 20 MNQ contract rolls.
- 24/7 evaluation since 2026-05-13 (`1e07000`) was a behavioral change with no backtest re-validation noted. You removed prod's RTH window after a missed-window incident, but the strategies were tuned (gates, time-blocks) under the RTH assumption.
- The 2026-05-13 incident report explicitly says the trade-memory canonical reader broke silently for two weeks and 12 readers had drift. Same shape of bug almost certainly exists elsewhere — that's the *class* of failure, and you've designed the system in ways that propagate it (multiple readers of the same JSON-on-disk truth).

**Most consequential:** The bar-level CVD proxy interacting with backtest results. You don't have a tick-level CVD measurement in 5y of historical data, but a meaningful fraction of your live edge is gated on CVD alignment.

### Lens 3 — Execution & infrastructure: **D+**

**Strongest evidence:** Self-healing PhoenixWatcher (5-min `Repetition` pattern), bridge single-stream enforcement (`PHOENIX_BRIDGE_SINGLE_STREAM=1`), peer-MAD stream validator, post-mortem playbooks in [`KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md), atomic OIF writer, kill switch, multi-account routing.

**Weakest evidence:**
- **The execution path is a folder.** `OIF_OUTGOING = C:\Users\Trading PC\Documents\NinjaTrader 8\incoming\` ([`config/settings.py:60`](../../config/settings.py:60)). Phoenix drops a JSON file; NT8's ATI reads it. There is no synchronous order acknowledgment, no broker-side fill ID, no API-level reject. Reconciliation is a [`startup_reconciliation.py`](../../core/startup_reconciliation.py) pass against NT8's outgoing folder. The whole order pipeline is filesystem semantics on Windows, which is famous for partial writes, lock conflicts, and OneDrive surprises — which already bit you when NT8's data folder lived in OneDrive (migrated 2026-04-18, see `KNOWN_ISSUES`). Your own watchlist memory `oif_guard_race.md` is about a race condition between PhoenixOIFGuard and NT8 ATI.
- **No fill latency measurement.** Slippage is modeled as 2 ticks/side in backtest. In live, you have no record of "order written at T, NT8 acked at T+x." If MNQ has a fast move, you don't know if you got hit at signal or 200ms later. That's the single biggest backtest-to-live gap.
- **NT8 silent-stall is OPEN.** [`KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md) line 161 — NT8 reports "connected" but forwards 0 ticks. Bot has no auto-recovery; manual NT8 restart is the only fix. **This bit you on 2026-04-16 for 3.25 hours, the entire primary trading window.** It's still open. With live money this is "missed every move, didn't know."
- **The 106s reconnect cycle** ([`KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md) line 84) — 10 disconnect/restart cycles per 0-tick window. Even if auto-restart works, this thrashes state in `position_manager`, `risk_manager`, `RiskManager.hydrate_from_trades` — every restart is a hydration pass. Risk multiplies with state surface.
- **No idempotency at the order layer.** If Phoenix writes an OIF, crashes, comes back up, and re-evaluates the same bar, what guarantees you don't duplicate? You have `_initial_stop_frozen`, `dupe_test halt cleanup` (Phase 9.5), and trade_id deduplication in `trade_memory`. But I see no order-side idempotency token. The "CLOSEPOSITION-vs-OCO race fix" in Phase 9.5 suggests this class of bug is alive.

**Most consequential:** No live slippage/latency telemetry. The Phase 13 backtest projects ~$5K/yr of edge from a "universal 10:00-13:59 CT skip" — that's the noise level where 2 ticks of unmodeled slippage per trade could erase the whole edge.

### Lens 4 — Risk management: **C**

**Strongest evidence:** Multi-layer defense exists: per-trade max ($20 hard cap), daily ($200), weekly ($150), per-strategy ($200), VIX gate, cooloff after consecutive losses, trade spacing, recovery mode, daily flatten at 15:54 CT, NT8 Auto Close at 15:55 as backstop, B59 hard-guard on `LIVE_ACCOUNT=1590711`. [`MAX_ACTUAL_STOP_DOLLARS_PER_TRADE = 50.0`](../../config/settings.py:45) is a sane per-trade dollar veto. F-001 `tier_3000` adds DD scale-down at 85% of ATH and consecutive-loss halving.

**Weakest evidence:**
- **Phase 0 sim-testing overrides were on for months.** Per [`CURRENT_STATE.md`](../../memory/context/CURRENT_STATE.md:60): `DAILY_LOSS_LIMIT=$1M`, `PER_STRATEGY_DAILY_LOSS_CAP=$1M`, `MAX_ACTUAL_STOP_DOLLARS_PER_TRADE=$100`, `skip_on_stop_clamp=False` on 3 strategies, `validated=True` on 7 strategies "operator override". Yes, these were restored on 2026-05-20, but the *pattern* — operator override of validated gates "for sim testing" that "must be restored before live" — is exactly how trading bots blow up the day someone forgets. You already caught yourself missing one in the restore (the $100→$50 trade cap, only caught 3 days later, [`config/settings.py:39-44`](../../config/settings.py:39)).
- **Position sizing is fixed-fractional / fixed 1-contract.** [`risk_manager.calculate_contracts`](../../core/risk_manager.py:210) divides risk_dollars by `effective_stop * dollar_per_tick`. With MNQ at $0.50/tick, a 40-tick stop = $20/contract. There's no volatility adjustment, no correlation adjustment between simultaneously firing strategies. If `bias_momentum`, `vwap_pullback_v2`, and `g_inside_bar_breakout` all fire LONG at the open (very plausible — all three are continuation/breakout patterns), you're in 3 contracts of the *same* directional exposure.
- **No correlation matrix in the live risk path.** You have [`tools/strategy_correlation_audit.py`](../../tools/strategy_correlation_audit.py) (Jaccard co-fire matrix) but it's an analytical tool, not a runtime gate. Section 1.4 of the ship plan projects 30 contracts max on the tier_3000 curve. If those 30 are split across strategies that all reverse together, it's effectively a 30-contract directional bet.
- **The compounding curve assumes uncorrelated edge that probably isn't.** Per [`PHASE_13_IMPLEMENTATION_PLAN.md`](../../docs/PHASE_13_IMPLEMENTATION_PLAN.md:138): *"Correlation across the 3 winners is unknown — they all fire on momentum bars. May have overlapping signals = correlated DD."* You wrote this and then put compounding on the ship plan anyway.
- **No flash crash / API outage simulation.** What does Phoenix do if MNQ is down 5% in two minutes and NT8 is choking on tape? The hard daily cap will catch it eventually, but the trade-by-trade behavior in degraded data conditions is untested.
- **Per-strategy $200 daily cap × 11 strategies = $2,200 theoretical daily loss.** Yes the global $200 cap catches it, but only after one strategy has burned through. The per-strategy caps are independent floors not coordinated headroom.

**Most consequential:** Correlated multi-strategy fires aren't sized as a portfolio. The single largest non-modeled risk in the system.

### Lens 5 — AI/ML soundness: **D**

**Strongest evidence:** Code is clean and defensive. [`agents/pretrade_filter.py`](../../agents/pretrade_filter.py) has a hard 3-second timeout, fail-safe-to-CLEAR semantics, and a per-strategy `ai_filter_mode` ("advisory" / "blocking") with sensible defaults. The Gemini integration is properly bounded. The Council pattern with 7 voters is interesting in principle.

**Weakest evidence:**
- **The AI is decoration.** Default `DEFAULT_AI_FILTER_MODE = "advisory"` ([`config/strategies.py:60`](../../config/strategies.py:60)), and the docstring at line 12 says *"trade always proceeds (default)"*. Every strategy uses the default. The AI agents are running, logging, and producing no behavioral effect.
- **No proven uplift.** There is no A/B test comparing trades-where-AI-said-CLEAR vs trades-where-AI-said-SIT_OUT. You're paying API costs (Gemini, Anthropic, Groq, OpenAI keys all populated) and adding latency (3s budget per signal) for zero measured edge.
- **FinBERT is installed but not active.** [`SENTIMENT_FLOW_ACTIVE=false`](../../memory/context/CURRENT_STATE.md:423) — the model occupies disk, runs on init, contributes 0 signal weight.
- **No drift detection or retraining cadence on FinBERT.** It's a 2022/2023-era model on 2026 financial text.
- **Council Gate / pre-trade filter prompts are short-context, single-shot.** The model is given a signal blob plus 5 recent trades and asked to verdict CLEAR/CAUTION/SIT_OUT in 3 seconds. That's roughly what a human gut-check does in 3 seconds — and there's no evidence the model is better than chance at it.
- **The "advisory" → "blocking" promotion path has no acceptance criterion.** When would an AI be allowed to actually block? Not documented.
- **No expectancy attribution per AI voter.** You can't tell if Gemini's CAUTION is a useful signal because no one has computed P(loss | CAUTION) vs P(loss | overall).

**Most consequential:** This is dead weight masquerading as sophistication. The bot would behave identically if you ripped out every `agents/*` import tomorrow.

### Lens 6 — Observability & operations: **B-**

**Strongest evidence:** This is genuinely the strongest area. PhoenixWatcher escalates to Telegram + Twilio SMS, [`tools/daily_session_summary.py`](../../tools/daily_session_summary.py) does anomaly detection vs 7-day baselines, [`tools/validation_tracker.py`](../../tools/validation_tracker.py) tracks Wilson-CI tiers, scheduled tasks for daily debrief, morning ritual, weekly evolution. Dashboard panels (TODAY card, Daily Stats), `[CAP:...]` log signatures with once-per-state-transition logging to avoid spam, the `validate_backtest_quality.py` quality gate. Anti-mutation invariants on R-distance ([`#3 4d4e15d`](../../memory/context/CURRENT_STATE.md:138)). 188 test files, 2,110 passing tests.

**Weakest evidence:**
- **"Phoenix failures are silent" is its own memory file** (`feedback_silent_failures.md` <!-- LINK BROKEN 2026-05-25: was ../../memory/feedback_silent_failures.md -->, via the MEMORY index). The bot processes are alive, the dashboard says "running" — but the bot is *deaf*. You've codified the failure mode and still keep finding new instances of it. The recent week has at least three: Section V.3 silent-stop variant, B-030 ZERO_GATE neutering every protective gate (`3afb04d`), open_drive eval was dead code (`d71163d`).
- **No structured trace ID across the lifecycle.** When a bias_momentum trade fires, can you grep one ID and see signal-emit → council-vote → pre-trade-filter → OIF-write → NT8-fill → exit-trigger → trade-memory-row? I see lots of strategy-tagged logs but no per-signal correlation ID. (`account_id` and `bot_id` exist but not a request ID.)
- **No latency SLO / dashboard.** What's the p99 from tick-in to OIF-out? Not measured.
- **Dashboard is local Flask on :5000.** Can't be reached when you're not at the Trading PC.
- **Alerting is local Twilio + Telegram.** If the *Trading PC* itself loses power or network, alerts die with it. There is no remote heartbeat that pages you when the local heartbeat dies. (You'd find out when Telegram stops, but that's a derived signal.)

**Most consequential:** No external dead-man's switch. If the trading PC drops off the network, you only notice by absence.

### Lens 7 — Capital-at-risk discipline: **C**

**Strongest evidence:** Live trading is paused until account ≥ $2,000. Operator hasn't yielded to the temptation to flip `LIVE_TRADING=True` while the account is $300 and the testing isn't done. That alone is rarer than you'd think. The Wilson-CI promotion guardrail ([`#22 477e31d`](../../memory/context/CURRENT_STATE.md)) is the right mechanism. The 50-trade graduation gate is *correctly labeled* as PRELIMINARY by your own tier doc — meaning operator is statistically honest.

**Weakest evidence:**
- **Validated gates routinely operator-overridden.** [`config/strategies.py`](../../config/strategies.py) is full of comments like *"2026-05-17: was False — operator override (V2 deployment)"* and *"operator override 'all strategies firing'"*. The whole Wilson-CI mechanism existed to prevent this; the operator built it AND bypassed it within days.
- **`bias_momentum` is being demoted out of PROD** ([`PHASE_13_IMPLEMENTATION_PLAN.md` §A "Demote"](../../docs/PHASE_13_IMPLEMENTATION_PLAN.md:40)) — but in the *same plan* it's listed as the **#1 winner at +$178K** (Tier 1, top of [`PHOENIX_BEST_PLAN.md` §1.1](../../docs/PHOENIX_BEST_PLAN.md:36)). These two facts are in conflict in your own docs. Either the +$178K is a backtest artifact (Section S silent-stop fix) or it's real edge and there's no reason to demote — but you can't have both.
- **The honest answer to "would I bet my own money tomorrow":** You're already implicitly answering "no" by keeping LIVE_TRADING=False. That's the right call but it should have an explicit promotion criterion. There isn't one. ("$2,000 account size" is *necessary* not *sufficient*.)
- **No paper-vs-live divergence dashboard.** Sim_bot has been running for weeks with 16 sim accounts. There's no single panel saying "sim_bot expected $X, realized $Y, divergence $Z" — which is the *only* useful pre-live test you can run.
- **The compounding plan jumps straight to $1.5K → $1.09M.** No published "first month live target: +$100. Next: +$500. Promote real account from $300 to $500 after X." There's a $1.5K → $1.09M curve and a $300 sim account — and nothing in between.

**Most consequential:** No explicit live-promotion gate, and the existing validated-flag mechanism has been routinely operator-overridden.

---

## STEP 3 — FOUR BRUTAL QUESTIONS

### What's working?

1. **Observability discipline.** PhoenixWatcher with 5-min `Repetition`, daily session summary, validation tracker with Wilson CI tiers, [`tools/validate_backtest_quality.py`](../../tools/validate_backtest_quality.py). The backtest-quality validator catching new silent-stop variants is rare-good engineering.
2. **The `[CAP:...]` log signatures pattern** ([`risk_manager.py:96-126`](../../core/risk_manager.py:96)) — once-per-state-transition CRITICAL logs that watcher_agent greps. This is the right shape.
3. **Anti-mutation invariants on R-distance** (commit `4d4e15d`). Freezing the entry-time stop in `_initial_stop_frozen` is the right pattern; most retail bots get this wrong and end up with R-multiples that drift.
4. **Operator's honesty about uncertainty.** [`PHOENIX_BEST_PLAN.md §3.4 "What's still uncertain"](../../docs/PHOENIX_BEST_PLAN.md:181) explicitly lists "70-80% WR is suspicious." That intellectual honesty is your biggest asset.
5. **The discipline of `LIVE_TRADING=False` while account=$300.** Most retail builders would have flipped this six months ago.
6. **Per-bot trade_memory split** with a canonical reader ([`core.trade_memory.load_all_trades`](../../core/trade_memory.py)) is sound after the 12-file audit.

### What's not working?

1. **The 5,951-line [`bots/base_bot.py`](../../bots/base_bot.py).** This is your god-class. It contains the strategy dispatch, signal handling, risk gates, AI filter call site, OIF writing, daily flatten, hydration, market enrichment, sub-strategy override, and 2026-Phase-13-overrides hook. Every behavior-change bug you've reported in the last month touched this file. It is the single highest-leverage place a silent failure hides.
2. **The B-030 incident** (commit `3afb04d`, *"sim_bot ZERO_GATE was neutering every protective gate"*) — a sim-only code path was silently disabling every protective gate. This happened **this week**. The number of "test/sim override" branches sprinkled through the system means this class of bug is recurrent. There will be another B-030.
3. **The Phase 0 sim-testing override pattern.** Months of `DAILY_LOSS_LIMIT=$1M` in prod source. Even after the formal restore, [`config/settings.py:39-44`](../../config/settings.py:39) documents that ONE override (the $50 trade cap) was missed and ran for 3 extra days. The mechanism — text comments in config saying "RESTORE before live" — does not work.
4. **The compounding backtest claim** ($1.5K → $1.09M / 5y / 34% DD). I don't believe this number. Your own docs ([`PHASE_13_IMPLEMENTATION_PLAN.md §I caveats`](../../docs/PHASE_13_IMPLEMENTATION_PLAN.md:137)) telegraph the suspicion. Acting on it sized your strategy roster (you promoted 3 strategies into it) and your sizing policy (you wrote `tier_3000` for it). Either it's a real number — in which case post the equity curve, the trade list, and a $1K bounty for anyone who can find the bug — or it's not, in which case you've spent 2 sprints planning around a fantasy.
5. **The AI agent stack is currently free latency and free API spend.** Council, pre-trade filter, debriefer, historical learner — all running in advisory/log-only with no measured uplift. ([`config/strategies.py:60`](../../config/strategies.py:60))
6. **The OIF folder-based execution path** is fragile against the failure modes of Windows + OneDrive + NT8 multi-stream + scheduled task user-context. Each of these has bit you and is documented in [`KNOWN_ISSUES.md`](../../memory/context/KNOWN_ISSUES.md).
7. **NT8 silent-stall is still open.** That's the only failure mode that has *already* cost you a full primary trading window with the bot looking healthy. It will happen again.
8. **27 strategies for one operator on one instrument.** I count more strategies than you have trading hours in a day to debug them.

### What should you stop doing?

1. **Stop adding strategies until you have one with proven live edge.** You currently have zero strategies with TENTATIVE tier (n ≥ 100 live) at PF ≥ 1.3. You're researching strategies 12, 13, 14, 15 on the way to 27. Every new strategy doubles the config surface, doubles the gates, doubles the silent-failure target. **Phase 13 added 3 new strategies before you've validated any of the original ten in live-paper to TENTATIVE.**
2. **Stop running 5y backtests as the ship gate.** Backtests will tell you what worked in the past. They cannot tell you whether your live-execution path matches the simulated one. Section S found a silent-stop bug that turned bias_momentum from 40 trades to 13,790 trades. That number is *terrifying* — it means the backtester and the live bot were running materially different code logic. Until those two are reconciled (same code path, deterministic), every backtest dollar is suspect. The Phase 13 V.3 spawn found a *new* variant. There will be a fourth.
3. **Stop using operator override to flip `validated=True`.** You built Wilson-CI exactly to prevent this; the next time you click "operator override" you are throwing away the mechanism. If a strategy doesn't have n ≥ 100, it doesn't trade prod. If you don't have time to wait for n ≥ 100, the right answer is "fewer strategies, more time per strategy."
4. **Stop writing 200+ line plan docs in the same session as the code change.** Your sprint cadence (research → implement → ship pt2/pt3/pt4 → audit → bug → fix) has the *form* of discipline (commits, tests, docs) but the rhythm of haste. The B-030 ZERO_GATE bug was a sim-only branch that survived because nobody slowed down to ask "what does this branch do in prod?"
5. **Stop "AI council" until you can A/B prove uplift.** It's 4 agents, 5 Gemini-Flash budget calls per signal (Council, pre-trade, debriefer, historical learner, sentiment), and zero measured edge. Park it. Ship without it. Re-enable one agent at a time, measured.
6. **Stop maintaining the legacy `archive/`, `mnq_trading_bot/`, `archive_mnq_trading_bot/`, `phoenix_lsr_build/` directories** in the active working tree. They're context pollution.

### What are you not looking at?

1. **You have no roll-event handling I can find.** MNQ rolls quarterly (H/M/U/Z). The Phase 13 5y backtest covers 20 contract roll events. [`config/settings.py:17-22`](../../config/settings.py:17) hardcodes `INSTRUMENT = "MNQM6"` and `NEXT_CONTRACT = "MNQU6 09-26"`. There is a `ROLL_DAYS_BEFORE_EXPIRATION = 8` constant but I see no live position-flatten-and-reopen logic at roll. **If you carry a position through a roll, you carry it on the wrong contract.** Likely you'd flatten at EOD before roll anyway because of `DAILY_FLATTEN_HOUR_CT = 15`, but multi-day strategies (`e_multi_day_breakout`!) and overnight `vwap_pullback_v2` are explicitly *not* flat overnight per Phase 13. Roll Friday is a hidden landmine.
2. **You're sizing with `dollar_per_tick = TICK_SIZE * 2`** at [`risk_manager.py:240`](../../core/risk_manager.py:240). MNQ tick value is $0.50 ($2 multiplier × 0.25 tick size). That's correct *only as long as you trade MNQ*. Your roll-Friday risk for NQ vs MNQ symbols differing by 10x point value is undefended. If a Sept roll loads MES into the data path (per Phase 12C) and someone wires MES routing wrong, the dollar math is silent.
3. **Sim_bot has been running on stale code for 62+ hours** at the snapshot ([`CURRENT_STATE.md`](../../memory/context/CURRENT_STATE.md:56)). That's structural — there's no "auto-restart on new commit" so the human has to remember to restart bots after every config/code change. You already lost $106 on 2026-05-14 because of exactly this pattern (`memory/code_changes_dont_auto_deploy.md` <!-- LINK BROKEN 2026-05-25: was ../../memory/code_changes_dont_auto_deploy.md -->, referenced in MEMORY.md). It will keep happening.
4. **There is no `bots/prod_bot.py` audit for what happens if the Trading PC reboots while a position is open.** You have startup_reconciliation but it depends on NT8's outgoing folder being intact. Windows update reboot + NT8 not coming back up cleanly + you on a plane = open position with no risk daemon.
5. **The 2026-05-13 audit found 12 files raw-reading `trade_memory.json`.** The *class* of bug is "multiple readers of the same on-disk truth, silently drifting." [`logs/trade_memory.json`](../../logs/trade_memory.json) is one such file. `data/equity_state.json` <!-- LINK BROKEN 2026-05-25: was ../../data/equity_state.json --> (for `tier_3000`) is another. [`logs/strategy_halts.json`](../../logs/strategy_halts.json) is another. Each one is a future silent-drift bug.
6. **Test coverage of risk caps is not the same as test coverage of risk-cap *interactions*.** The 2,110 tests assert that `risk_manager.can_trade` returns False when daily_pnl ≤ -limit. They do *not* (I'd bet) test "fire 11 strategies in the same 10-second window, do they collectively respect the daily cap, or do 11 OIF files all get written before the trade_memory writeback catches up?" — that's a race-condition test. You almost certainly don't have it.
7. **You don't seem to be measuring the bid-ask spread you actually pay.** Slippage is modeled as 2t/side flat. Spread on MNQ is usually 1t, but balloons to 4-8t at news and overnight. If you trade through news (and your blackout is 5min ± news), you're paying 4-8t of spread and modeling 2t.
8. **Behavior under data outage.** What happens if Databento/NT8 stops sending bars for 30 seconds *while a position is open*? The strategy's exit logic depends on bar-close. The position manager will hold. Does the daily-loss circuit check fire if it can't see prices? I bet "no signal in" = "no eval cycle in" = "no risk check."
9. **Your `vwap_pullback` bleed pattern.** 65% WR but net -$169 because avg loser 2.3× avg winner. That's a *structural* asymmetric exit. It's documented in [`OPEN_QUESTIONS.md`](../../memory/context/OPEN_QUESTIONS.md) since at least 2026-05-13. Same pattern almost certainly exists in others; the diagnose tool is per-strategy. Run it on all 11.
10. **Your prod_bot uses the same `LIVE_ACCOUNT` constant + Sim101 routing.** When you flip `LIVE_TRADING=True`, all 11 strategies route to the real account simultaneously. There is no canary-trade phase, no "1 strategy goes live first" toggle. Day one of live trading is day one of 11 strategies in one account. That's bold.

---

## STEP 4 — 80/20 ANALYSIS

### The 20% producing 80% of the value

1. **The observability + memory layer** (`PhoenixWatcher`, `daily_session_summary`, `validation_tracker`, the `memory/` write-back hooks, the `[CAP:...]` log signatures). This is the *only* compounding asset in the codebase: every day you keep it running you accumulate more grounded P&L truth. It's also the *only* thing that gives you a real chance of catching the next silent-stop bug.
2. **Two or three strategies, not eleven.** Your data points to `opening_session.orb`, `bias_momentum`, and `g_inside_bar_breakout` as the spine. Everything else (1) is a smaller absolute $ contribution, (2) eats config + debug + risk budget, (3) increases correlated-DD risk. If you went prod with three strategies and nothing else, you'd capture most of the projected edge with a quarter of the surface area.
3. **The TickStreamer → bridge → OIF execution path being *reliable* enough to actually run live.** Boring. Critical. Eat your vegetables.

### The 20% producing 80% of the risk / drag / wasted effort

1. **The strategy zoo and its config sprawl.** [`config/strategies.py`](../../config/strategies.py) is now nearly 1,000 lines of toggleable, time-stamped parameter overrides with embedded operator-override comments. Every "ship pt2/pt3/pt4" cycle adds another decade of git-blame archaeology. The right number of strategies is closer to 3-5, and the right config file is closer to 200 lines.
2. **The `base_bot.py` god-class.** 5,951 lines is not a unit of code, it's a tar pit. Every behavior change risks regressions in unrelated paths. Every audit finds new dead code (the `d71153d` "open_drive sub-strategy override was dead code" — that was *in this file*).
3. **The Phase 13 backtest results being treated as ground truth before any tick-level reconciliation with live.** Every dollar of plan built on those numbers is a dollar of plan you'll have to redo.
4. **The AI council eating latency, API budget, and operator complexity for zero measured uplift.**
5. **Sprint cadence of "research → implement → ship pt2-pt7 → audit → bug → fix" inside a single week.** This is the rhythm that produces B-030.

### Single highest-leverage change in the next 7 days

**Build a deterministic live-vs-backtest reconciliation harness.** Specifically:
1. Pick ONE strategy (`bias_momentum`, since it's the largest claimed P&L line).
2. Take the last 30 days of sim_bot trades.
3. Re-feed the *exact same input bars* through `tools/phoenix_real_backtest.py` and assert that the entry timestamps, entry prices, stop placements, and exit reasons match within tolerance.
4. Any divergence is either a silent backtest bug, a silent live bug, or an enrichment-field gap. Catalog them.
5. Don't ship Phase 13. Don't tune another parameter. Don't add a strategy. Until this passes for one strategy, the rest of the work is castles in the air.

**Why this beats the alternatives:**
- *Alternative 1: Ship Phase 13 verdicts (kill list, promotions, exit policies).* This compounds whatever live-vs-backtest gap you have. You'd be shipping a configuration whose justification is a backtest you haven't tied to reality.
- *Alternative 2: Fix bug B2 + B3 + wire MES feed (D4) for `es_nq_confluence`.* These are real and need fixing, but they're each one-line / one-feature fixes that you can do in an afternoon. They don't *change the rate at which future B2s and B3s appear*. The reconciliation harness does.

### The one thing you should kill even though it feels productive

**Strategy research.** Stop labbing new strategies. Phase 13C just found 3 new winners with backtest PF 4-8 (suspiciously high, per your own docs). Phase 14 will find 3 more. None of them have a real claim on edge until validated tick-by-tick live. Every hour you spend in `phoenix_*_lab.py` is an hour not spent making the existing strategies actually deployable. **Cap the strategy roster at 5 max for the next quarter.** Cull, don't add.

---

## STEP 5 — FIX PLAN

### NOW (this week — max 5)

1. **Reconciliation harness: live-vs-backtest for ONE strategy (`bias_momentum`).**
   - What: Per-trade diff between sim_bot output and `phoenix_real_backtest.py` output on the same 30-day input.
   - Why: Until this passes, every other backtest-driven decision is unfounded. The Section S silent-stop bug already proved the code paths can diverge by 300x.
   - Effort: 2-3 days.
   - Done: A script that ingests sim_bot's trade_memory entries for `bias_momentum`, replays the same bars through the backtester, and exits 0 if entries/exits match within tolerance, non-zero otherwise. Output is `out/reconciliation_<date>_<strategy>.md` with any divergences listed.

2. **Hard-disable the operator-override flag pattern in `config/strategies.py`.**
   - What: Move every "operator override" inline-comment override into a single `config/sim_overrides.py` file that is *only* loaded when `PHOENIX_SIM_OVERRIDES=1` env var is set. Strip the values from `config/strategies.py` entirely. Make startup print which overrides are active.
   - Why: The "DAILY_LOSS_LIMIT=$1M / forgot to restore the $50 trade cap" class of bug only exists because overrides hide in config comments. Make them loud at process start. Make them require a flag.
   - Effort: 1 day.
   - Done: Bot startup prints `[CONFIG] sim_overrides active: N parameters` (or `none`) and refuses to start with `LIVE_TRADING=True` AND overrides=on.

3. **Fix B3 (`orb_fade` wallclock) — one-line — but verify live too.**
   - What: Replace `time.time() - last_bar_ts > bar_freshness` at [`strategies/orb_fade.py:162`](../../strategies/orb_fade.py:162) with comparison against `market["now_ct"]`. Then grep `logs/sim_bot.log` for any other strategy with a `time.time() - bar_ts` gate.
   - Why: Confirmed-zero-signals in backtest, *suspected* same gate biting live. This is the cheapest verified-positive ROI item in the project.
   - Effort: 1 hour.
   - Done: Live log shows `[EVAL] orb_fade: NO_SIGNAL ...` events (not silent), backtest shows non-zero trade count, grep of `time.time()` in strategies/ comes up clean.

4. **NT8 silent-stall auto-recovery.**
   - What: If `nt8_status: live` AND `tick_rate_10s == 0` for >180s, send SIGTERM to NinjaTrader.exe and relaunch via the existing PhoenixBoot shortcut. Halt Phoenix entries for 60s after relaunch.
   - Why: It already cost you the full primary trading window once and is recurring (`KNOWN_ISSUES.md` line 161, OPEN since 2026-04-16). With live money this is the worst-case "missed every move."
   - Effort: 1-2 days (the kill is easy; the relaunch verification is the work).
   - Done: A simulated NT8 hang (kill the indicator, leave NT8 process alive) triggers Phoenix to restart NT8 within 5 minutes; positions are reconciled afterward.

5. **External dead-man's switch.**
   - What: A tiny script running on *anything that isn't the Trading PC* (a free Cloudflare worker, an AWS Lambda, a free Heroku dyno, or even another Pi at the house) that pings Phoenix's `:8767/health` every 60s and sends you Telegram + SMS if it gets no response for 3 minutes.
   - Why: Today, if the Trading PC loses power, you find out because Telegram goes quiet. That's a derived signal. A positive heartbeat from outside the trading network is the only ground truth.
   - Effort: 1 day (most of it is making the Trading PC reachable from outside, which means probably a Tailscale or ngrok endpoint, not opening a port).
   - Done: Pull the Trading PC's ethernet cable for 4 minutes; SMS arrives.

### NEXT (this month — max 5)

1. **Decompose [`bots/base_bot.py`](../../bots/base_bot.py).**
   - What: Pull the strategy-dispatch loop, signal-handling pipeline, OIF writing, daily-flatten scheduler, and market-enrichment out into separate modules. Aim for `base_bot.py` < 1000 lines.
   - Why: Every bug in the last month has touched this file. It's the highest-leverage single file in the project and the most expensive one to read.
   - Effort: 2 weeks.
   - Done: `wc -l bots/base_bot.py` < 1000. Test suite still 2,110+ pass.

2. **Cull the strategy roster to 5.**
   - What: From the 11 Phase 13 ship list, keep the top 5 by 5y P&L *that don't share the same trigger pattern*. Disable the other 6 in code (`enabled=False`, not `validated=False` — eliminate them from the eval cycle entirely). Run the kill list reverification through the reconciliation harness.
   - Why: Each active strategy is config surface + log surface + silent-failure target + correlated-DD contributor. 5 strategies is enough to capture 80% of the projected edge while halving the operational complexity.
   - Effort: 1 day (mostly thinking about which 5).
   - Done: `grep enabled.*True config/strategies.py | wc -l` returns 5.

3. **Correlation-aware multi-strategy risk gate.**
   - What: Before writing OIF n+1 when n positions are already open, check the in-window co-fire Jaccard from [`tools/strategy_correlation_audit.py`](../../tools/strategy_correlation_audit.py) against historical data. If predicted correlation > 0.7 AND direction is same, reject the new entry (or halve sizing).
   - Why: The "30 contracts" cap on the tier_3000 compounding curve is a *contract* cap, not a *direction* cap. If your 5 strategies all fire LONG at 09:35 CT (which they will, because that's where breakouts and momentum overlap), you'll have a 30-contract directional bet sized as 5 independent decisions.
   - Effort: 3-5 days.
   - Done: Live test shows simultaneous bias_momentum + g_inside_bar_breakout + e_multi_day_breakout LONG fires reduces the third to 0 contracts when correlation Jaccard > 0.7.

4. **Roll Friday handling.**
   - What: Auto-flatten all positions 15 minutes before the front-month roll cutoff, refuse new entries from that point until next session, and switch `INSTRUMENT` in the strategy config to the next contract via [`core/contract_rollover.py`](../../core/contract_rollover.py) (which I see exists — verify it's actually wired).
   - Why: With multi-day and overnight strategies in the active set, carrying a position through a contract roll is a silent failure waiting to happen.
   - Effort: 3-4 days (including a test rollover dry-run).
   - Done: On a simulated roll day, Phoenix flattens at T-15, refuses new entries, swaps the instrument, and resumes next session against the new contract.

5. **A/B harness for the AI council, OR kill it.**
   - What: Pick one agent (pre-trade filter is the easiest). Flip half the trades to `ai_filter_mode: "blocking"` and half to "advisory". Over 100 trades, compare P&L. If blocking is net-negative or flat, kill the agent. Repeat for council, debriefer, sentiment.
   - Why: You're paying API + latency + complexity for zero proven uplift. Either prove it earns its keep or remove it.
   - Effort: 1 week per agent (mostly waiting for sample size).
   - Done: One agent has a published "uplift = $X over N trades, 95% CI [a, b]" file in `out/`. Decision documented.

### LATER (next quarter — max 5)

1. **Migrate trade-memory + halts + equity-state from JSON to SQLite.** [`OPEN_QUESTIONS.md` line 86](../../memory/context/OPEN_QUESTIONS.md:86) already has it on the deferred list. The 12-file reader audit was symptomatic — JSON files with multiple readers always drift. SQLite gives you ACID + a single connection per process.
2. **Walk-forward / CPCV validation harness.** Your `weekly_evolution.py` already emits the checkboxes — wire them up. Use scikit-learn `TimeSeriesSplit` with embargo. Reject any new strategy whose OOS Sharpe < 0.5× IS Sharpe.
3. **Real broker integration alternative to OIF folder.** Rithmic R|API or Tradovate WebSocket gives you fill ACKs and rejects synchronously. Keeps NT8 as a charting tool, removes the filesystem-as-IPC fragility.
4. **Live tick-CVD instead of bar-CVD proxy in backtest.** Either buy Databento MBO ($100-500/mo, per your open question 4) or start the volumetric snapshot recorder ([`tools/volumetric_snapshot_recorder.py`](../../tools/volumetric_snapshot_recorder.py)) immediately. Six months of recorded snapshots is enough to begin closing the backtest-vs-live CVD gap.
5. **`tier_3000` compounding rollout — but conservatively.** Only after items 1-3 of NOW and items 1-2 of NEXT are done, and the reconciliation harness has passed for *every* enabled strategy. Even then: cap real-money sizing at 3 contracts max for the first 60 days regardless of equity. The point of compounding is to scale *what works*, and you don't yet know what works at scale.

### NEVER

1. **Auto-promote a strategy to `validated=True` based on a backtest alone, even with `--check-promotion`.** Backtests can't prove edge; only live-traded n ≥ 100 with PF ≥ 1.3 and Wilson-CI lower bound > 0.5 can. Anything else is conjecture.
2. **Add another non-MNQ instrument until the MNQ pipeline is stable for 90 days.** You already have MES dormant for `es_nq_confluence`; do not add ES or NQ until MNQ is boring.
3. **A "global" AI verdict that vetoes the entire risk system.** The pre-trade filter and council are advisory by design; promoting any AI to blocking-of-all-trades introduces a black-box single point of failure.
4. **Custom NT8 indicators beyond `TickStreamer` and `PhoenixTradeOverlay`.** The NT8 surface area is already responsible for half of your `KNOWN_ISSUES`. Every new indicator is another file in `bin/Custom/` to confuse with a stale one.
5. **A second strategy zoo.** If Phase 14 wants to "test 7 new strategies" — don't. Test one. Validate live. Then maybe one more.

---

## STEP 6 — PRE-MORTEM

### "Six months from now, here's the post-mortem we don't want to write"

**Date:** 2026-11-25.
**Subject:** Phoenix bot lost $4,800 over four trading sessions (Nov 18-21), real account.

**What happened.** On 2026-09-15 the operator promoted Phoenix to live trading after the account hit $2,100. Phase 13 ship list was active: 11 strategies, `tier_3000` compounding. By mid-October the bot was running 4-7 contracts per fire on momentum days. On Nov 18 a CPI surprise at 07:30 CT caused a 40-point spike in MNQ over 90 seconds. Three strategies (`bias_momentum`, `g_inside_bar_breakout`, `e_multi_day_breakout`) all fired LONG within the same 30-second window, each at the tier_3000-allowed 5 contracts, total 15-contract directional exposure. NT8 silent-stalled at 07:32 (TickStreamer heartbeats fresh, 0 ticks for 4 minutes — the unresolved 2026-04-16 failure mode). All three positions ran their stops at the *retracement*, taking ~$2,400 of loss before the data feed recovered. Phoenix's circuit breakers caught it on the *fourth* trade, not the *first three* — because the daily cap was $200 *per strategy*, not $200 total *per simultaneous fire window*. Daily loss cap finally tripped at 07:48. Net session: -$2,420.

Three more sessions over the next week showed similar but smaller patterns. Bot was halted by operator on Nov 21. Cumulative loss: $4,820 from a $2,100 starting account → $0 active capital left to trade.

**What the warning signs were.**
1. The Phase 13 backtest projected 70-80% WR for breakout strategies. The first 40 live trades came in at 58% WR — a ~20pp drop. This is exactly the regression-to-mean that the operator's own [`lessons_learned.md`](../../memory/semantic/lessons_learned.md) warned about. The operator told themselves this was "still profitable" because per-trade expectancy was still positive. It was — until correlated DD hit.
2. Three "silent stall" alerts fired in October (60s threshold, Telegram only). Operator dismissed them — they always recovered. The Nov 18 stall went 4 minutes, well past the dismissal threshold.
3. The `tier_3000` consecutive-loss halving kicked in after the third loss on Nov 18, but only halved the *fourth* trade's size — by which point three full-size losses were already booked.
4. `strategy_correlation_audit.py` had shown Jaccard 0.74 between bias_momentum and inside_bar_breakout in the prior 30 days. Operator had seen it but the audit was an offline tool, not a runtime gate.

**What the operator ignored.**
- The 7-lens audit, Step 4, item 3: "Single highest-leverage change: reconciliation harness." Operator did B3 and MES feed instead.
- The Step 5 "NEVER #2": added a second instrument (MES + ES_NQ confluence going live) two months before MNQ was boring.
- The Step 4 "20% producing risk": didn't cull the strategy roster; "all 11 are positive in backtest, why drop any?"
- The Step 7 final question: never answered.

**What you instrument now so this post-mortem never gets written.**
1. **Reconciliation harness** (NOW #1) — would have surfaced the live-vs-backtest WR gap before going live.
2. **Sim_overrides as a startup flag** (NOW #2) — eliminates the "forgot to restore" class.
3. **NT8 silent-stall auto-recovery** (NOW #4) — would have killed and restarted at 07:33, missing one entry instead of three.
4. **External dead-man's switch** (NOW #5) — Telegram-out-from-the-trading-PC is *not* proof of trading-PC health; an external probe is.
5. **Correlation-aware multi-strategy risk gate** (NEXT #3) — would have refused the second and third correlated fire.
6. **Cull to 5 strategies** (NEXT #2) — reduces the simultaneous-fire window from 11 ways to 5.
7. **Per-window directional dollar cap, not per-strategy** — `if sum(|exposure_dollars| for open positions in last 60s) > $X: reject` is a one-line gate that would have caught Nov 18.

The bot doesn't need to be smarter than the market to make money. It needs to be *cheaper to operate than its noise floor* and *defended against its own correlated bad days*. Today it is neither.

---

## STEP 7 — THE FINAL QUESTION

**Have any of the 11 Phase 13 strategies, when actually run by `sim_bot` in live-paper over the past 30 days with no operator overrides active, produced trade counts, win rates, and net P&L that match — within a tolerance you'd defend in writing — the corresponding subset of trades from the 5-year backtest?**

If yes, with a specific divergence number per strategy: I'll pull this audit toward a "ship Phase 13, start live with a 3-contract cap" plan. The reconciliation harness becomes a confirmation pass instead of a prerequisite.

If no, or you don't know: nothing else in this plan matters until that's answered. Every recommendation I'd give changes from "fix this specific bug" to "stop and reconcile before shipping anything," and the highest-leverage move shifts from "build the harness" to "build the harness *and* freeze production gates while you build it."

I'll revise the fix plan based on your answer. If you can paste the sim_bot per-strategy 30-day numbers next to the corresponding 30-day slice from the Phase 13 backtest, I can probably tell you within an hour where the divergence is and whether it's in your favor (live underperforms because of slippage you can model) or against you (live underperforms because the backtest is finding edge that doesn't exist).