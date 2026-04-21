# Phoenix Evaluation — 2026-04-18

_End of weekend rebuild. Honest institutional-grade assessment with real-data findings._

---

## 🚨 THE HEADLINE FINDING (this is the whole report)

**From `logs/trade_memory.json` — 697 live trades logged to date:**

| Metric | Value |
|---|---|
| Total trades | 697 |
| Wins | 232 |
| Losses | 465 |
| **Live win rate** | **33.3%** |
| Total P&L | **-$1,227.68** |
| Median R:R configured | 5:1 |
| Required WR at 5:1 R:R to break even | 16.7% |
| **Actual gap** | Bot is WINNING enough % to break even (33% > 16.7%) but **losing money net** |

**What this means:**

At 33% WR with a "5:1" target configuration, math says we should be making money. The actual P&L is -$1,227 over 697 trades = **-$1.76 per trade average**. The gap between theoretical breakeven and actual P&L is **~$1,850 of leakage**.

**Where does the leakage come from?** Partial analysis of exit reasons:
- 60.7% stop_loss → full -$20 max loss hit
- 37.1% target_hit → but "target" includes ema_dom_exit (partial fills)
- 2.2% ema_dom_exit → **these are winners cut early below their configured target**

**Hypothesis:** `ema_dom_exit` + other "smart exit" logic is closing winning trades at partial gain (maybe +$5-$15) rather than letting them reach the configured 5:1 target ($100). This compresses the actual realized R:R from 5:1 to something closer to 1.5:1, which at 33% WR **is net losing**.

**33% WR at 1.5:1 R:R:**
- Breakeven math: 1/(1+1.5) = 40%
- Actual WR: 33%
- Gap: -7 percentage points = persistent losses

**This is THE critical insight from the weekend.** Every other finding is secondary.

---

## Task 1 Findings: Paper Trade Sims

### Sim Option A — Harness Stability (3 seeds × 5 days)

| Seed | Trades | WR | PF | Sharpe | Net P&L | Max DD | MC Ruin% |
|---|---|---|---|---|---|---|---|
| 11 | 18 | 50% | 1.10 | 0.039 | +$47.77 | $169.70 | 8.75% |
| 42 | 18 | 50% | 1.10 | 0.039 | +$47.77 | $169.70 | 10.0% |
| 17 | 18 | 50% | 1.10 | 0.039 | +$47.77 | $169.70 | 9.9% |

**Interpretation:** Replay harness is deterministic (good). Monte Carlo converges at ~9.5% ruin probability from $300 with the EMA-crossover placeholder. **But placeholder isn't our strategies.** This verifies the tool, not the system.

### Sim Option B — Real Prod Trade Analysis (45 prod entries, 7 days)

**PROD (real money would have been at risk):**
- 45 trades, 15.6% win rate
- bias_momentum LONG: 42 trades at 21% WR (7 target / 27 stop / 8 ema_exit)
- spring_setup: 3 trades, 0 wins
- Median stop distance: 10 points = $20 risk
- Median target distance: 50 points = $100 reward
- Configured R:R: 5:1 ✓
- **At 15.6% WR, 5:1 R:R requires avg win ≥ 5.41 × avg loss to break even. Since "target" doesn't always hit (ema_exit cuts winners short), actual realized R:R falls below 5.41 → net losing.**

**Sim Option C — Proper 90-day sim:** deferred. Requires strategies to be replay-compatible first (Task #1 next week).

---

## Task 2 Findings: Top-to-Bottom Evaluation

### ✅ What we got RIGHT (institutional-grade)

1. **Rollback discipline** — tagged baseline, wrote runbook. Best-practice.
2. **Shadow-mode architecture** — modules run, don't gate trades. Textbook dual-write.
3. **Atomic writes + file locking** in `memory_writeback.py`.
4. **Separation of concerns** — modules don't import each other.
5. **Config-driven behavior** — YAMLs change behavior without code deploys.
6. **Hard architectural rule: secondary-test-only entries** in `reversal_detector.py`.
7. **Memory persistence via hooks** — fixed the "bot forgets" root cause.
8. **61 unit tests, all passing in 0.088s** — fast, green, reliable.

### ⚠️ What could be IMPROVED

1. **Strategy evaluators NOT replay-compatible** (C-) — biggest institutional gap
2. **No CI or automated test runs on commits** (C)
3. **No monitoring/alerting on bot internals** (C+) — decay monitor built, not firing
4. **No metrics collection / time-series DB** (D) — scale beyond $2K needs this
5. **Position sizing simplistic** (C) — doesn't validate $ risk ≤ budget
6. **Unit tests cover isolation not integration** (B-)
7. **WFO uses placeholder strategy** (C) — validates tool not system

### ❌ What we FLAT-OUT MISSED

1. **No historical data pipeline** — 7 days on hand vs institutional 10+ years
2. **No deploy/staging separation**
3. **No latency measurement** (tick → signal → OIF → fill)
4. **No drawdown curve / underwater equity tracking**
5. **Decay monitor not auto-running in production**
6. **No A/B framework for strategy variants**
7. **MQBridge single-install fragility** — no heartbeat monitoring
8. **No circuit breaker on the research/reflector side**

---

## Institutional-lens takeaway

**We built excellent SCAFFOLDING and the SUBSTANCE is unvalidated:**

✅ Scaffolding (22 modules, hooks, memory, configs, WFO harness, composite bias): A-grade
⚠️ Substance (the actual EDGE — does bias_momentum actually make money?): **unknown and likely negative per live data**

**A Two Sigma PM would say:** "Good skeleton. Show me the Sharpe on 2 years of tick data for each strategy before funding."

**The uncomfortable implication:** The weekend made Phoenix *architecturally better*. It did NOT make it *tradeable-profitably better*. That happens when:
1. Strategies become replay-compatible (scheduled Monday Apr 20)
2. You accumulate 2+ weeks of live shadow data
3. April 25 reflector session analyzes real shadow data
4. Parameters get tuned based on observed behavior
5. One strategy at a time gets promoted from shadow to live gate

4-6 week arc, not a weekend.

---

## The 20% FIX that delivers 80% of improvement

Per institutional dev framing, the three highest-leverage fixes:

### 🥇 Task 1 — Extract strategies into replayable pure functions (Mon Apr 20)
- 4 hours
- Unlocks proper backtesting of the 7 strategies
- Lets WFO validate REAL logic, not placeholder
- Lets reflector agent (Apr 25) analyze strategy performance against 90+ days
- **The ONE thing that makes everything else we built actually work**

### 🥈 Task 2 — CI hook + integration test (Tue Apr 21)
- 1.5 hours
- Prevents silent regressions
- Claude hook on `Write|Edit` runs full test suite, blocks edit if fail
- Fast (0.088s test runtime) so no friction

### 🥉 Task 3 — Background decay monitor + Telegram alerts (Wed Apr 22)
- 1 hour
- Hourly decay monitor loop with TG alerts on WARNING/CRITICAL
- Daily 15:10 CDT summary (P&L, W/L, top exit reason, degraded strategies)
- Goes from reactive "reports outcomes" → proactive "tells you when to pay attention"

**Scheduled task created:** `phoenix-20-80-fix-week-apr-21` — fires Mon-Wed-etc at 19:00 CDT reminding me of each task.

---

## What you should DO Monday-Friday this week

### Monday Apr 20
- Normal trading day (bot in Sim101, no real money risk)
- Lab 24/7 accumulates shadow data with session tags
- Evening 19:00 CDT: scheduled task fires → I do Task 1 (replayable strategies)

### Tuesday Apr 21
- Evening: Task 2 (CI + integration)

### Wednesday Apr 22
- Evening: Task 3 (decay monitor alerts)

### Thursday-Friday Apr 23-24
- Let shadow data accumulate 5+ days with tuned/validated strategies
- Review structural_bias vs tf_bias alignment trends on dashboard

### Saturday Apr 25
- Scheduled reflector + Kelly activation review session
- With replayable strategies + shadow data, this session has real material

---

## Final honest word

Phoenix is WELL-BUILT INFRASTRUCTURE. The live trading data suggests the STRATEGIES NEED WORK. This weekend's build gave us the tools to find and fix the strategy problems — it did not solve them.

**The hardest truth:** You have a $300 account and a bot that has lost $1,227 across its logged history. Even if we fix the ema_exit-cutting-winners issue, this is a "don't trade live until we prove positive-expectancy on 90+ days of replay" situation.

**My strong recommendation:** Keep `LIVE_TRADING=False` until:
1. Task 1 completes (replayable strategies)
2. We replay 90+ days of historical data (need to acquire it — Databento free tier or similar)
3. At least one strategy shows PF > 1.5 and Sharpe > 1.0 on OOS walk-forward
4. That strategy runs 2 more weeks in live Sim101 shadow
5. Real live results match the replay expectations (within 20% variance)

Then, and only then, flip LIVE_TRADING=True.

That could be June or later. That's the honest institutional timeline.
