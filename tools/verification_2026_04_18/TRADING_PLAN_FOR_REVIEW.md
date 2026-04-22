# Trading Plan — Consolidated for Review

_Generated: 2026-04-19 for an external review session._
_Aggregated from: `C:\Trading Project\phoenix_bot\`_

The trading plan does not live in a single file — it is distributed across prose docs (docs/, memory/semantic/), operational notes (memory/context/), strategy code docstrings (strategies/*.py), and YAML rule-encodings (memory/procedural/). This document aggregates all of it into one reviewable artifact. **Content is verbatim-quoted from source files** — no rewording.

---

## Sources

### Included

| Classification | File | Notes |
|---|---|---|
| PROSE_PLAN | `docs/momentum_days.md` | 18KB. THE densest single plan doc. Subtitled "What Works, What Doesn't, How to Win." Post-mortem analysis of the 2026-04-15 trading day with 416 trades of live lab data. |
| PROSE_PLAN | `memory/semantic/lessons_learned.md` | 8KB. Curated durable wisdom — market lessons, technical lessons, explicit user preferences. |
| MIXED | `memory/context/MONDAY_READINESS.md` | 6KB. Operational state + rules ("LIVE_TRADING=False until account ≥ $2,000", session window rules). Trading-relevant excerpts only. |
| MIXED | `memory/context/EVALUATION_2026-04-18.md` | Partial excerpt — the headline 697-trade finding that contextualizes everything else. |
| CODE_EMBEDDED_PLAN | `strategies/bias_momentum.py` | Module docstring + `_REGIME_OVERRIDES` dict (rules-as-data). |
| CODE_EMBEDDED_PLAN | `strategies/compression_breakout.py` | 60-line docstring with pattern description + three-phase explanation + `_REGIME_PARAMS`. |
| CODE_EMBEDDED_PLAN | `strategies/dom_pullback.py` | Docstring with 5-rule entry setup + user's own words quoted. |
| CODE_EMBEDDED_PLAN | `strategies/high_precision.py` | Brief docstring. |
| CODE_EMBEDDED_PLAN | `strategies/ib_breakout.py` | Docstring with NQ IB-break statistics. |
| CODE_EMBEDDED_PLAN | `strategies/spring_setup.py` | Docstring: "Rule of Three" pattern description. |
| CODE_EMBEDDED_PLAN | `strategies/vwap_pullback.py` | Brief docstring. |
| CONFIG_PLAN | `memory/procedural/targets.yaml` | Performance targets (60% WR, PF 2.25, Sharpe > 1). |
| CONFIG_PLAN | `memory/procedural/strategy_params.yaml` | Per-strategy enabled/validated state — snapshot of `config/strategies.py` at 2026-04-17. |
| CONFIG_PLAN | `memory/procedural/small_account_config.yaml` | $300-account risk rules. |
| CONFIG_PLAN | `memory/procedural/regime_matrix.yaml` | Strategy ON/REDUCED/OFF per gamma × VIX regime. |
| CONFIG_PLAN | `memory/procedural/regime_params.yaml` | Per-regime stop/target/sizing multipliers. |

### Excluded (with reason)

| File | Classification | Why excluded |
|---|---|---|
| `STRATEGY_KNOWLEDGE_INJECTION_PROMPT.md` | AMBIGUOUS | Implementation prompt for Claude Code to BUILD a strategy-knowledge infrastructure. Not trading rules. |
| `AI Trading Analysis System Research.md` | NOT_PLAN | Research on building AI-trading systems (XGBoost, HMM, ChromaDB). Meta, not rules. |
| `REBUILD_PLAN.md` | NOT_PLAN | System rebuild plan — architectural, not trading. |
| `phoenix_action_plan_v2_post_migration.md` | NOT_PLAN | Sprint implementation plan (P1–P27). |
| `docs/phase_c_architecture.md` / `docs/architecture.html` | NOT_PLAN | System architecture. |
| `docs/ACTION_PLAN_V2_1_DELTAS.md` | NOT_PLAN | Verification sprint deltas. |
| `CLAUDE.md` / `PROJECT_EXPORT_PROMPT.md` | NOT_PLAN | System/bot usage briefing for Claude. |
| `memory/context/{CURRENT_STATE, RECENT_CHANGES, KNOWN_ISSUES, OPEN_QUESTIONS, ROLLBACK_RUNBOOK}.md` | NOT_PLAN | Operational state / logs. |
| `SCRATCH_DIRS.md` | NOT_PLAN | Housekeeping. |

No standalone `.docx` / `.pdf` trading plan found in OneDrive Documents, Desktop, or `C:\Trading Project\`. The only PDF of interest was `ChartPatternsv2.pdf` in OneDrive — reference material, not plan.

---

## Section 1: Prose plan

### Source: `docs/momentum_days.md` (full, verbatim)

> _Original header from source:_
> # Momentum Days — What Works, What Doesn't, How to Win
> *Last updated: 2026-04-15. Based on live lab data from 416 trades (Apr 13-15, 2026).*

---

#### TL;DR

On a confirmed TREND/CONTINUATION day, the bot has a small positive gross edge (~-$0.40/trade gross avg). The entire loss comes from **COMMISSION DRAG** — 173 trades/day × $1.72 = **$297 in commissions alone**. Fix #1 is to trade 5-15 times per day, not 100+.

**The two rules that would flip today from -$368 to roughly break-even:**
1. **CVD must be positive** (net buying) before entering bias_momentum in afternoon regimes
2. **Trade 15 times per day max** — each unnecessary trade costs $1.72 before it even starts

---

#### Day Classification

A "momentum day" fires when:
- `day_type == "TREND"` AND
- `cr_verdict == "CONTINUATION"` (C/R score 4-5/5) AND
- `atr_regime == "HIGH"` (ATR_5m > 15 pts)

**NOT** every high-volatility day is a momentum day. The April 2026 tariff spike had HIGH ATR but VOLATILE/UNKNOWN day type — different rules apply (see volatile_days.md, not yet written).

---

#### Regime Performance — Live Data

| Regime | Time (CST) | Best Strategy | WR | P&L/Day | Verdict |
|--------|------------|--------------|-----|---------|---------|
| OVERNIGHT_RANGE | 00:00-06:00 | high_precision_only | 64% | +$7 | TRADE LIGHT |
| PREMARKET_DRIFT | 06:00-08:30 | nothing | n/a | n/a | SKIP |
| **OPEN_MOMENTUM** | **08:30-09:30** | **high_precision_only** | **73%** | **+$17** | **GO — primary window** |
| **MID_MORNING** | **09:30-10:30** | bias_momentum (if CVD positive) | 40-60% | varies | **GO with caution** |
| AFTERNOON_CHOP | 11:30-12:30 | spring_setup, dom_pullback only | ~35% | -$7 | LEVEL TRADES ONLY |
| **LATE_AFTERNOON** | **13:00-15:00** | **high_precision_only + bias_momentum** | **50-73%** | **+$66** | **GO if CVD positive** |
| CLOSE_CHOP | 15:00-16:00 | nothing | 0-14% | -$88 | DO NOT TRADE |

---

#### The CVD Gate — Proven 2026-04-15

**Rule:** In LATE_AFTERNOON, CLOSE_CHOP, and AFTERNOON_CHOP — if CVD is negative (net selling) for LONG entries, or positive (net buying) for SHORT entries, do NOT enter any bias_momentum trade.

**Evidence from Apr 15:**
- 43 bias_momentum trades in these regimes, ALL entered while CVD was negative all afternoon
- Result: 43 entries, ~31% average WR, **-$204 total loss**
- Zero exceptions — not one "CVD-opposing afternoon regime" trade was profitable as a group

**Why it works:**
CVD (Cumulative Volume Delta) shows actual institutional buying vs. selling at the session level. On Apr 15, price was showing micro-bullish 1m/5m signals (higher highs) while CVD was -92M and falling all afternoon. That's a classic distribution pattern — smart money selling into retail buying. The CVD gate blocks all these noise entries.

**Critical note — why NOT the 60m tf_bias:**
The `tf_bias["60m"]` metric uses "2 of last 3 completed 60m bars rising." Intraday with only 3-6 completed 60m bars, this typically stays NEUTRAL even on genuinely bullish days. Apr 15 was a bullish day on the hourly chart — but `tf_bias["60m"]` showed NEUTRAL all session. The CVD approach is a far more reliable real-time signal.

**Implementation:** `strategies/bias_momentum.py` — CVD check at the top of `generate()` before any direction or scoring logic runs. LONG blocked when CVD ≤ 0 in chop/afternoon regimes. SHORT blocked when CVD ≥ 0 in same regimes.

---

#### New Scoring Signals (2026-04-15)

##### MACD Histogram Scoring

MACD uses the existing EMA9 and EMA21 already computed in tick_aggregator.py:
- `macd_line = ema9 - ema21` (spread between fast/slow EMA)
- `macd_signal` = 9-period EMA of macd_line (needs 30+ five-minute bars to warm up)
- `macd_histogram = macd_line - macd_signal` (momentum acceleration)

**Scoring applied in bias_momentum (only when `macd_warm=True`):**

| Condition | LONG pts | SHORT pts |
|-----------|----------|-----------|
| Histogram positive AND expanding (prev < curr) | +12 | -12 |
| Histogram positive but shrinking | -8 | — |
| Histogram negative | -15 | — |
| Histogram negative AND expanding negative (prev > curr) | — | +12 |
| Histogram negative but shrinking | — | -8 |
| Histogram positive | — | -15 |

**MACD warm-up:** Requires 30+ five-minute bars total (21 to warm EMA21, then 9 more for the signal line). On a **cold start from blank state**, this takes ~2.5 hours. In normal operation, state is saved and restored between sessions, so MACD is already warm at 8:30 AM open. The `macd_warm` flag is just a safety guard — once the signal line has seen 9+ bars ever, it stays warm permanently across restarts.

**Why MACD helps:**
On Apr 15, the MACD histogram was negative from ~11:00 AM onward (momentum had peaked). All afternoon bias_momentum LONG entries went against negative MACD. The -15 penalty on a negative histogram would have blocked most of these since effective_min_momentum is only 20 on TREND days — a -15 hit would make most signals unscorable.

##### DOM Directional Scoring

Uses real-time order book data from NT8:

| Condition | LONG pts | SHORT pts |
|-----------|----------|-----------|
| dom_imbalance > 0.6 (bid-heavy, buyers leading) | +10 | -10 |
| dom_imbalance < 0.4 (ask-heavy, sellers leading) | -12 | +10 |
| dom_signal LONG + absorption strength ≥ 40 | +15 | -10 |
| dom_signal SHORT + absorption strength ≥ 40 | -10 | +15 |

**DOM absorption signals:**
When a large passive order absorbs aggressive pressure (e.g., a big bid stack absorbing market sell orders), this shows institutional accumulation. Absorption strength ≥ 40 = strong signal.

##### Q-Level Proximity Scoring

Menthor Q levels (gamma flip HVL, resistance/support walls) act as structural price magnets. Price near a wall tends to stall or reverse:

| Distance to nearest wall | Score |
|--------------------------|-------|
| Within 20 ticks (5 pts) | -15 |
| Within 40 ticks (10 pts) | -5 |
| Within 16 ticks of HVL | note logged (no direct score, context) |

The -15 for proximity to a major wall will block many entries that would otherwise look valid on the 1m/5m charts. This is correct — you don't want to enter a momentum trade right into a known absorption level.

**Note:** Q-level data requires the MenthorQ feed to be running. If unavailable, scoring defaults to 0 (no penalty applied).

---

#### Commission — The Hidden Killer

**173 trades today × $1.72/trade = $297.56 in commissions alone.**

The gross P&L before commissions was only -$70. Net was -$368. This means:
- 81% of today's loss is commission, not bad trading
- The bot has roughly BREAK-EVEN gross edge (when it trades right)
- Every low-quality entry costs $1.72 before the trade even starts

**Target: 5-15 trades per day maximum on momentum days.**

The 10 best setups on a momentum day earn far more than 100 mediocre entries. A single 20-point bias_momentum win = +$40 net. Ten 1.5:1 scalps = maybe +$4 net at 55% WR. The math strongly favors fewer, higher-quality, longer-held trades.

---

#### Strategy Breakdown

##### bias_momentum — The Momentum Rider

**Overall today:** 43 trades, 27.9% WR, -$178.96

**When it works (all criteria must be met):**
- Regime: OPEN_MOMENTUM or MID_MORNING (golden windows)
- CVD: positive for LONG, negative for SHORT in afternoon regimes
- MACD histogram: positive and expanding (when warm)
- DOM: bid-heavy imbalance for LONG, ask-heavy for SHORT
- No Q-level wall within 20 ticks of current price
- Stop: 20t (prod) or 14t (lab)
- Target: 20:1 with rider mode — stall detector exits, NOT fixed OCO

**When it fails:**
- LATE_AFTERNOON with CVD negative: was 100% failure rate on Apr 15 (43/43 trades losing)
- CLOSE_CHOP: 0% WR regardless of anything — never trade this window
- When MACD histogram is negative: momentum has peaked, chasing the tail
- When DOM shows opposing absorption: institutional players fighting the direction

**Key insight — TREND day gate is too loose:**
On TREND days, `effective_min_momentum = 20` — essentially any aligned TF fires a signal. The new scoring signals (CVD gate, MACD, DOM, Q-levels) now provide the selectivity needed to prevent 35+ garbage entries in afternoon regimes.

**Fixes implemented (2026-04-15):**
- CVD institutional flow gate for afternoon regimes
- MACD histogram scoring (warm after 30 bars)
- DOM imbalance + absorption scoring
- Q-level proximity scoring
- Rider mode (target 20:1, stall detector exit)
- Stop floor: 14t lab, 20t prod

##### high_precision_only — The Sleeper Winner

**Overall today:** 98 trades, 50.0% WR, +$10.94

**This is the best performing strategy when filtered properly:**
- OPEN_MOMENTUM: 73% WR, +$17 — strongest single-regime performance
- LATE_AFTERNOON (post-14:31 new code): +$65.72 on just 24 trades

**Why it works in OPEN_MOMENTUM:**
High volatility at open creates overshoots and instant reversals. With 8t stop and 5:1 target (or ema_dom_exit for large wins), the 73% WR generates consistent positive expectancy.

**The big winners that save the strategy:**
The ema_dom_exit (min_profit=40t on TREND days) catches real moves:
- Apr 15 big wins: +35t, +41t, +46t, +70t, +70t, +45t — average hold 3 min
- These 6 trades earned +$130 in net P&L
- Without them, the strategy would be -$120 from the 1.5:1 OCO scalps

**The problem:**
- OCO scalps (12t wins at 1.5:1) need 57% WR to break even with $1.72 commission
- Actual WR: 50% — slightly below break-even
- The ema_dom_exit upgrades profitable trades to 35-70t wins, saving the day
- The 8t/12t OCO bracket is effectively a stop-loss mechanism, not a profit target

**The trade-count problem:**
98 trades at $1.72 = $168.56 in commission. Strategy made +$10.94. Without commission: +$179. Fix needed: require at least 3 TF votes (not 1) even in lab mode to reduce firing rate 50-60%.

##### spring_setup — Counter-Trend Reversal

**Overall today:** 29 trades, 37.9% WR, -$174.38

**Math check:**
- Stop: 40t (capped from ATR-based, which often computes to 80-100t)
- Target: 60t (1.5:1 RR) — lab override to 5:1 may not be applying consistently
- Break-even WR at 1.5:1 with $1.72 commission: **43.4%**
- Actual WR: 37.9% — solidly below break-even

**When spring_setup wins:**
- +60t target_hit (15 points) holds: spring at OPEN_MOMENTUM extreme (+$28.28 × 6 wins)
- Must coincide with genuine exhaustion (DOM absorption at wick low/high + TF alignment)

**The counter-trend problem on TREND days:**
Several SHORT spring_setups fired on a TREND UP day. Counter-trend springs have lower base rates since the prevailing trend fights the reversal.

**Fix needed (not yet implemented):**
On TREND LONG days, require spring_setup direction == LONG (WITH the trend). On TREND SHORT days, require spring_setup direction == SHORT. Counter-trend springs can still fire in RANGE/VOLATILE days where trend context is weak.

**Target RR:**
The lab override sets target_rr=5.0 (200t = 50pt target). With ATR_5m ≈ 22pt, these are legitimate expectations. A 40t stop (10pt risk) at 5:1 = 200t target (50pt). The ema_dom_exit drives actual exits (rider principle), producing 20-50pt exits when the spring works.

##### compression_breakout — Pre-Explosion Entry

**Today:** 3 trades, 33.3% WR, -$25.16 (1 win at +$18.28, 2 losses)

The one win (+$18.28, +40t) suggests the concept is sound. Needs more sample data before drawing conclusions.

---

#### The Rider Principle — Hold for 20+ Points

**Setting up an OCO bracket at 1.5:1 or even 5:1 and walking away is wrong.** A genuine momentum move on a TREND day runs 20-80+ points. An OCO bracket at 25pts closes you out just as the real move begins.

**The rider approach:**
1. Enter with normal setup (14-20t stop)
2. Set OCO target VERY wide (20:1 = 280t = 70pts) — it's a safety net, not a target
3. Once trade is profitable, move stop to break-even (0.5R on RANGE days, 1R on TREND days)
4. Exit on: stall detector (TF fade + CVD divergence + price not advancing) OR ema_dom_exit (extended from EMA9 + DOM reversal stacking + candle wicking)
5. Never exit just because price is at a round number or "looks extended"

**Evidence from today:**
- high_precision_only ema_dom_exit: exited at 35-70t with holds of 2-13 minutes
- These are the ONLY trades making meaningful money
- All the OCO scalp exits (12t) barely cover commission

---

#### Time-of-Day Rules — Momentum Days

##### DO TRADE:

**08:30-09:30 (OPEN_MOMENTUM)**
- Highest edge window of the day
- All strategies can fire; high_precision_only at 73% WR is the anchor
- bias_momentum valid; CVD gate typically not restrictive at open (session CVD starting at 0)
- Take max 3-5 trades, hold until stall detector fires

**09:30-10:30 (MID_MORNING)**
- Secondary window; still excellent
- By now CVD should be solidly positive on a genuine momentum day
- If CVD is still near 0 or negative at 9:30, the trend may be failing — reduce size
- spring_setup VALID for extensions to wick levels

**13:00-15:00 (LATE_AFTERNOON) — ONLY if CVD is positive**
- Institutional repositioning window
- On strong momentum days (like Apr 14 +300pt), the biggest sustained moves happen here
- CVD MUST be positive (confirming hours-long net buying), not just 1m/5m signals
- high_precision_only + ema_dom_exit has shown +$65 in a single session here
- bias_momentum valid with rider mode and positive CVD

##### DO NOT TRADE:

**11:00-12:30 (AFTERNOON_CHOP)**
- Lunch chop: narrow range, random direction, stop-grinding
- ONLY: spring_setup / dom_pullback / ib_breakout at extremes
- NEVER: bias_momentum or high_precision_only

**15:00-16:00 (CLOSE_CHOP)**
- 0% WR for bias_momentum (data: Apr 15)
- Market flattening into EOD, random noise
- DO NOT TRADE this window

---

#### The Ideal Momentum Day Trade Plan

**Target: 5-8 trades total, $50-$200 P&L potential**

1. **8:30-8:45** — OPEN_MOMENTUM. Watch first candle + DOM. Enter first high_precision_only or compression_breakout signal. Rider mode on, hold until stall detector fires.
2. **9:00-10:00** — MID_MORNING. Confirm CVD is positive and climbing. Add 1-2 bias_momentum entries on pullbacks to EMA9. Check MACD histogram direction (warm after ~30 bars). These are the potential 20-50pt runners.
3. **10:00-12:30** — Rest. Watch from sidelines unless strong reversal spring forms at a key level. Check if CVD is still rising or starting to roll over.
4. **13:00-14:30 (only if CVD still positive)** — Late afternoon continuation. 1-2 more entries. If CVD has turned negative, the momentum day has stalled. Stop trading.
5. **14:30+** — Stop. CLOSE_CHOP eats commissions and wins back nothing.

---

#### What to Monitor for Validity

**Signs the momentum day is real:**
- CVD consistently climbing (net buying throughout session)
- MACD histogram positive and expanding after 10:00 AM
- Each higher-timeframe bar closes bullish (15m, 60m visual check)
- Price stays above VWAP after OPEN_MOMENTUM
- DOM showing bid-heavy imbalance on pullbacks

**Signs the momentum is fading (stop trading):**
- CVD flattens or goes negative (distribution — smart money selling)
- MACD histogram turns negative (momentum peaked)
- Price repeatedly failing at VWAP reclaims
- DOM showing heavy ask-stack absorption into rallies
- Stall detector STRONG fires with 3+ reasons

---

#### Key Numbers — MNQ Quick Reference

| Measurement | Ticks | Points | $ (1 contract) |
|-------------|-------|--------|-----------------|
| 1 tick | 1t | 0.25pt | $0.50 |
| Commission (round trip) | — | — | $1.72 |
| Break-even trade (1.5:1 RR) | need 57% WR | — | — |
| Break-even trade (3:1 RR) | need 37% WR | — | — |
| Good scalp | 12t | 3pt | $4.28 net |
| Strong runner | 80t | 20pt | $38.28 net |
| Monster day | 400t | 100pt | $198.28 net |
| Stop (lab) | 14t | 3.5pt | -$8.72 net |
| Stop (prod) | 20t | 5pt | -$11.72 net |

---

#### Implemented Changes (2026-04-15)

| Change | File | Effect |
|--------|------|--------|
| CVD gate: LATE_AFTERNOON/CLOSE_CHOP/AFTERNOON_CHOP | `strategies/bias_momentum.py` | Blocks 43 bad trades (~-$204) |
| MACD histogram scoring (±12/±8/±15) | `strategies/bias_momentum.py` | Filters momentum-peaked entries |
| MACD computation (EMA9-EMA21 based) | `core/tick_aggregator.py` | No new EMA periods needed |
| DOM imbalance + absorption scoring (±10/±12/±15) | `strategies/bias_momentum.py` | Reads real-time order book |
| Q-level proximity scoring (-15 within 20t, -5 within 40t) | `strategies/bias_momentum.py` | Avoids entering into walls |
| Rider mode on all days for bias_momentum + dom_pullback | `bots/base_bot.py` | Holds winners for 20+ pts |
| Day-type aware BE stop (0.5R RANGE, 1R TREND) | `bots/base_bot.py` | Protects gains in chop |
| Smart exit min_profit: 40t TREND, 20t other | `bots/base_bot.py` | Correct threshold per day type |
| Smart exit: AND logic (DOM AND wick, not OR) | `core/trend_stall.py` | Stops 1-second exit bug |
| Smart exit min_hold: 120s, min_profit: 40t, EMA ext: 40t | `core/trend_stall.py` | Prevents premature exits |

---

#### Open Issues / Next Improvements

1. **spring_setup trend direction filter** — on TREND days, only take springs WITH the trend direction. Counter-trend springs need stronger confirmation. Not yet implemented.
2. **high_precision_only trade frequency** — 98 trades at $168 commission. Need to require 3+ TF votes even in lab mode to reduce firing rate by 50-60%.
3. **Prod bot: 0 trades** — prod thresholds too strict (min_confluence=5.5, min_momentum=80). After validating changes on lab, investigate why prod never fires.
4. **Backtesting** — validate CVD gate, MACD, DOM scoring, and spring direction filter against historical data before scaling.
5. **Trade spacing enforcement** — lab bot has `trade_spacing_min=5` but fires every 1 minute. Check if spacing is actually being enforced in lab_bot._evaluate_strategies().
6. **P&L accuracy** — commission blindness (~$531 gap vs NT8) and slippage blindness (~$227 gap).

_(End of `docs/momentum_days.md`.)_

---

### Source: `memory/semantic/lessons_learned.md` (full, verbatim)

> _Header from source: Phoenix Bot — Lessons Learned_
> _Curated, long-term knowledge. Distinct from `RECENT_CHANGES.md` (operational log). Durable observations that should survive across many sessions._

#### Meta / process lessons

**Scope sprawl is a documented failure pattern** — Over 11 research rounds on 2026-04-17, the weekend rebuild grew from "add memory so the bot remembers" (original ask) to a 20+ hour infrastructure overhaul. Every individual addition was justified. Cumulatively the scope was near-overwhelming. Discipline going forward: commit → execute → observe 2-4 weeks → iterate, not continuous research.

**The "bot forgets" problem wasn't a memory problem — it was a write-back problem** — Root cause: settings.json had zero hooks configured, so no SessionStart auto-loading of memory and no SessionEnd auto-writeback. Claude relied on user memory to know what to write back. Fixing this via hooks (installed 2026-04-17) is the actual solution.

**MenthorQ data staleness is a critical silent failure** — 2026-04-15 → 2026-04-17: `C:\temp\menthorq_levels.json` went 2 days without updating because NT8 MQBridge indicator was uninstalled/removed from chart. The bot continued trading with stale gamma levels. Staleness checks (file age > 24h → regime=UNKNOWN) were added in weekend build. Still need UI indicator on dashboard to flag staleness visually.

#### Market / strategy lessons

**97% of retail algo traders lose money** — Research across multiple 2026 sources converges: win rate isn't the problem, risk management is. Renaissance Medallion operates at ~51% WR. Profit factor ≥ 2.0 + Sharpe > 1 matters more than high WR.

**Chasing 80%+ WR is a retail trap** — Achieved only by tiny profit targets + wide stops. One bad streak = blowup. Target instead: 55-65% WR with 1:1.5 to 1:2 R:R → profit factor 2.0+.

**Candlestick patterns in isolation are ~55% reliable** — Barely better than coin flip. Context (at S/R, after N trending bars, with volume confirmation) raises to 73%+. Pattern detectors without context weighting are weak. V1 patterns ship without weighting; v2 adds context.

**Positive gamma days are fundamentally different from negative gamma days** —
- Pos GEX → dealer counter-trend flow → mean reversion → tight ATR stops, small targets
- Neg GEX → dealer procyclical flow → trend acceleration → wide stops, large targets
- Same strategies, different parameters. Encoded in `memory/procedural/regime_matrix.yaml` (weekend build).

**The "catch the top/bottom" framing is dangerous** — 40% of all stocks never recover from a -70% drawdown. Successful reversal traders wait for confirmation (secondary test, BOS, CVD divergence) — they don't enter on the climax bar. Hard architectural rule in `core/reversal_detector.py`: entry ONLY on secondary test.

**MNQ liquidity has sessions** —
- 08:30-11:30 CDT (US open): 40% of daily volume — highest edge window
- 13:00-15:00 CDT: secondary institutional window
- Overnight (Asia/London): thinner, wider spreads, gap risk
- Lab bot runs 24/7 for data gathering; prod RTH-only windows

#### Technical / infrastructure lessons

**NT8 indicator state is fragile** — Indicators can be removed from a chart without warning. `TickStreamer.cs` showed up in OneDrive install; `MQBridge.cs` had been silently uninstalled. Both need to be confirmed applied each morning pre-open.

> **Update (2026-04-18):** The "OneDrive install" reference reflects the pre-migration layout. NT8 data folder has since moved to `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\`. The underlying lesson — fragile indicator state, confirm each morning pre-open — still applies.

**Watchdog detecting "NT8:live ticks:0/s" is insufficient** — The bot considered itself "connected" but received zero data. Bot kept running, waiting forever. Need anomaly detection that triggers remediation (restart indicator, alert user) not just observes. Planned for Saturday.

**Kelly sizing requires granular position sizing** — Below ~$1500 account size, you can't fractionally size MNQ contracts (minimum 1). Kelly math becomes cosmetic. `simple_sizing.py` with fixed 1-contract + small_account_config is the right abstraction for small accounts.

**Markdown in Telegram breaks on user-generated text** — Strategy names with underscores (`bias_momentum`, `high_precision_only`) break `parse_mode="Markdown"`. HTML is much more forgiving. Switched on 2026-04-17. 22/29 dropped messages yesterday is the cost of learning this.

#### User-specific preferences

- Local-first architecture (per `user_profile.md` — no cloud dependencies, no Google Sheets/Docs as truth)
- Single-contract MNQ trading (account size appropriate, Kelly inappropriate)
- **Target 60% WR + 1:1.5 R:R (decided 2026-04-17)**
- Shadow mode 1-2 weeks before activating any new signal gate
- User approval required for strategy demotion/promotion
- Prod LIVE_TRADING=False until account ≥ $2,000

_(End of `memory/semantic/lessons_learned.md`.)_

---

## Section 2: Pseudocode / structured rules

### Source: `memory/context/MONDAY_READINESS.md` (trading-relevant excerpts)

> _Header from source: Monday Readiness Report — 2026-04-17 through 2026-04-20_

**Bot state going into Monday 2026-04-20 open:**

- **LIVE_TRADING=False** — prod bot stays in Sim101 account until user's real account reaches $2,000
- No new strategies gated by structural_bias (dual-write only)
- No auto-demotion by decay monitor (observe mode 2 weeks)
- No auto-halt by circuit breakers (observe mode 2 weeks)
- Kelly sizing — intentionally not built; waiting on account ≥ $1500

**WFO baseline (placeholder strategy, for reference):**

Run against 5 clean days of history (2026-04-13, 14, 15, 16, 17):

| Metric | Value |
|---|---|
| Trades | 18 |
| Win rate | 50% |
| Profit factor | 1.10 |
| Sharpe | 0.039 |
| Sortino | 0.083 |
| Break-even WR (given R:R) | 47.6% |
| Monte Carlo risk of ruin (2000 iterations) | 10.7% |
| OOS Sharpe | -0.109 (degraded vs in-sample) |

**Interpretation:** The placeholder EMA-crossover strategy is a baseline for the harness itself, NOT a real strategy. OOS degradation is the harness correctly identifying an overfit. Real strategies will replace this on April 25 validation session.

**Monday pre-open checklist:**
- 07:32 CDT: MenthorQ morning refresh scheduled task fires. Paste today's MQ analysis (GEX, levels, Q-Score).
- Verify MQBridge still running.
- Verify bots UP.
- Verify zero errors since startup.
- Verify LIVE_TRADING=False in config/settings.py.
- 08:15 CDT: verify bias_momentum ran at least one evaluation cleanly.
- 08:30 CDT: primary window opens, bot begins trading in Sim101.

**April 25 validation review agenda:**
- Compare structural_bias vs tf_bias (how often agreed, accuracy vs actual moves)
- Per-strategy P&L review, demotion candidates
- Reflector agent introduction (propose-only)
- Kelly activation gate (if account ≥ $1500)
- Blind spot discovery from live operation

---

### Source: `memory/context/EVALUATION_2026-04-18.md` (headline excerpt)

> _The framing finding that contextualizes everything else in this plan._

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

**This is THE critical insight from the weekend. Every other finding is secondary.**

---

## Section 3: Strategy code intent

> _Module-level docstrings + rules-as-data dicts only. Implementation (evaluate() bodies, helper functions) intentionally omitted — this section captures trading intent, not execution mechanics._

### `strategies/bias_momentum.py`

```
"""
Phoenix Bot — Bias Momentum Follow Strategy

Port from V3 BiasMomentumFollow. Trades in the direction of multi-TF
bias when momentum confirms. Baseline validated strategy.

REGIME-AWARE: Loosens gates in golden windows (OPEN_MOMENTUM, MID_MORNING)
to maximize signal generation when edge is highest.
"""

# Regime-specific overrides — BE AGGRESSIVE in golden windows
# Non-golden regimes use strategy config defaults (tighter gates)
_REGIME_OVERRIDES = {
    # Direction gate: 15m + 5m + 1m must ALL align (hardcoded in evaluate(), not here).
    # These overrides control momentum strength and confluence threshold per regime.
    # Goal: 2-5 signals/day on genuine trending days. Zero on choppy days.
    "OPEN_MOMENTUM": {"min_momentum": 80, "min_confluence": 5.5},
    "MID_MORNING":   {"min_momentum": 80, "min_confluence": 5.5},
    # Secondary windows
    "LATE_AFTERNOON": {"min_momentum": 75, "min_confluence": 5.0},
    # Chop zones — keep thresholds high
    "AFTERNOON_CHOP":   {"min_momentum": 80, "min_confluence": 5.5},
    "CLOSE_CHOP":       {"min_momentum": 80, "min_confluence": 5.5},
    # Off-hours — slightly looser for lab data collection
    "OVERNIGHT_RANGE": {"min_momentum": 60, "min_confluence": 4.0},
    "AFTERHOURS":      {"min_momentum": 60, "min_confluence": 4.0},
    "PREMARKET_DRIFT":  {"min_momentum": 60, "min_confluence": 4.0},
}
```

### `strategies/compression_breakout.py`

```
"""
Phoenix Bot — Compression Breakout Strategy  (PRE-explosion entry)

Detects the coil-then-explode pattern and enters DURING the coil —
before the explosion bar fires — so we ride the explosion itself.

Three moves on 2026-04-13 illustrate the opportunity:

  08:49  Squeeze 1: spring/reversal at open (handled by bias_momentum/spring_setup)
  11:07  Squeeze 2: 5-bar coil → 57pt explosion → 109pt total move  ($218 MNQ)
  14:51  Squeeze 3: 12-bar coil → 42pt explosion → 101pt total move ($202 MNQ)

PRE vs POST entry comparison (why pre matters):
  Squeeze 2 PRE: entry 25288.50, stop $14, 506t profit available
  Squeeze 2 POST: entry 25310.75, stop $58, 417t profit available
  Squeeze 3 PRE: entry 25471.25, stop $32, 476t profit available
  Squeeze 3 POST: entry 25509.50, stop $108, 323t profit available

Pre-explosion entry gives 2-4x tighter stops AND catches the explosion
bar itself as profit — the biggest bar of the entire move.

═══════════════════════════════════════════════════════════════════════
THE THREE PHASES (context only — we never wait for phase 3 to enter):

  COIL   : Market compresses. Consecutive bars with range well below
            session baseline ATR. Price holds its level — sellers
            failing to push down (or buyers failing to push up).

  SIGNAL : Pre-explosion tells appear inside the coil:
            A) VRR Absorption  — massive volume, tiny range = institutional
               absorption. Someone is loading a position against the flow.
            B) Exhaustion Turn — after 5+ consecutive one-direction bars,
               the first reversal signals the move has exhausted itself.
            C) Close Breakout  — bar closes at highest (or lowest) level
               of the last 5 coil bars = buyers (sellers) quietly taking
               control inside the apparently quiet coil.
            D) ATR Declining   — coil range is shrinking bar-over-bar =
               energy is building. Not directional alone but validates quality.

  EXPLOSION: The bar we ride through, not enter at.

═══════════════════════════════════════════════════════════════════════
ENTRY LOGIC:

  1. Coil established: N consecutive tight bars (range <= baseline * tight_mult)
  2. ONE OR MORE pre-explosion signals fire on the CURRENT bar
  3. Direction confirmed by TF votes OR by the exhaustion/close signal itself
  4. Enter at current bar close. Stop at coil structural boundary (tight!).
  5. Target: 3:1 RR minimum — these moves run 400+ ticks

KEY DESIGN — pre-explosion ATR:
  We compute baseline ATR from bars BEFORE the current bar, NOT from
  market["atr_1m"] which includes the current bar. This keeps the tight-bar
  threshold accurate through the full coil life.

REGIME-AWARE:
  Primary (OPEN_MOMENTUM, MID_MORNING):  3 coil bars, tight_mult 0.90
  Afternoon (AFTERNOON_CHOP):            5 coil bars, tight_mult 1.20
  Late/Close (LATE_AFTERNOON, CLOSE_CHOP): 5-6 coil bars, tight_mult 1.50

NOTE: validated=False — run in lab bot to build sample before prod promotion.
"""

# ── Regime-specific thresholds ────────────────────────────────────────────────
# tight_mult: a bar qualifies as "tight" if range <= baseline_atr * tight_mult
#   Primary session: bars need to be noticeably below baseline (0.90x)
#   Afternoon: market is quieter, 1.20-1.50x catches the genuine coil pattern
_REGIME_PARAMS = {
    "OPEN_MOMENTUM":  {"min_coil_bars": 3, "min_tf_votes": 2, "tight_mult": 0.90},
    "MID_MORNING":    {"min_coil_bars": 3, "min_tf_votes": 2, "tight_mult": 0.90},
    "AFTERNOON_CHOP": {"min_coil_bars": 5, "min_tf_votes": 2, "tight_mult": 1.20},
    "LATE_AFTERNOON": {"min_coil_bars": 5, "min_tf_votes": 2, "tight_mult": 1.50},
    "CLOSE_CHOP":     {"min_coil_bars": 6, "min_tf_votes": 2, "tight_mult": 1.50},
    # Overnight/premarket/afterhours: not traded — too thin, random breaks
}
```

### `strategies/dom_pullback.py`

```
"""
Phoenix Bot — DOM Absorption Pullback Strategy

Replicates the user's manual trading pattern:

  Entry Setup (all required):
    1. Context: MenthorQ LONG bias (or 2/4 TF vote majority)
    2. Level: Price at EMA9 or VWAP — the pullback touches a key level
    3. Pullback quality: The pullback bars are QUIET (small body, weak volume)
       Not an aggressive reversal — just a clean, low-volume retest
    4. DOM absorption: Sell orders being PULLED or EATEN at the level
       The key signal — sellers can't push through, buyers absorbing
    5. Bounce confirmation: Current bar closes strongly (close near high)
       with volume >= pullback bar (buyers stepping in decisively)

  Exit: Managed by base_bot smart exit — EMA extension + DOM stalling + wick

User description:
  "I bought in on a pullback, I entered the trade when I confirmed momentum
   picking up, the sell orders on the depth of market started getting pulled /
   and eaten through. I knew my direction was bullish bc of the menthor q road
   map... so I enter. It took off."
"""
```

### `strategies/high_precision.py`

```
"""
Phoenix Bot — High Precision Only Strategy

Very selective — requires high TF alignment and momentum.
Quick target, tight stop.
"""
```

### `strategies/ib_breakout.py`

```
"""
Phoenix Bot — Initial Balance Breakout Strategy

The most statistically validated NQ strategy:
- 96.2% of NQ days break the Initial Balance
- 74.56% win rate on 15-min ORB
- Narrow IB (< 0.5x ATR): 98.7% break probability, bigger extensions
- Wide IB (> 1.5x ATR): 66.7% break, smaller extensions

REGIME-AWARE: Only trades during OPEN_MOMENTUM and MID_MORNING.
"""

# Regime-specific overrides — only fire in morning windows
_REGIME_OVERRIDES = {
    "OPEN_MOMENTUM": {"allowed": True, "min_confluence": 2.0},
    "MID_MORNING":   {"allowed": True, "min_confluence": 2.5},
    # All other regimes: not allowed
}
```

### `strategies/spring_setup.py`

```
"""
Phoenix Bot — Spring Setup Strategy

Port from MNQ v5 Elite Spring pattern. The "Rule of Three":
1. Spring wick (liquidity grab at S/R) >= 6 ticks
2. VWAP reclaim (price back above/below VWAP after sweep)
3. Delta flip (CVD direction confirms reversal)

All three must confirm. Stop = structure low/high +/- buffer (not wick multiplier).
Target = 1.5:1 RR minimum.

v2 fixes (2026-04-14):
- TF alignment gate: spring must fire WITH dominant trend (3/4 TF votes)
- Structure-based stop: stop placed at min/max(last_bar, prev_bar) low/high ±2t
  rather than wick×1.5 — avoids getting stopped at exact session low
"""
```

### `strategies/vwap_pullback.py`

```
"""
Phoenix Bot — VWAP Pullback Strategy

Enters on first pullback to VWAP in a trending market.
Best during MID_MORNING regime (9:30-11:00 CST).

Logic: TF bias says bullish → price pulled back to/below VWAP → bounce candle confirms.
The entry is ON the pullback touch, not after price already reclaimed.
"""
```

---

## Section 4: Config-encoded rules

### `memory/procedural/targets.yaml`

```yaml
# Phoenix Bot — Performance Targets
#
# Codified goals for the system. Decay monitor + WFO harness use these.
# Strategies consistently missing these over 100+ trades → auto-demote candidate.

performance_targets:
  win_rate: 0.60           # 60% target
  avg_rr: 1.5              # 1:1.5 R:R minimum
  profit_factor: 2.25      # = WR × avg_win / ((1-WR) × avg_loss). Required minimum.
  sharpe: 1.0              # Rolling 30-day, minimum acceptable
  sortino: 1.5             # Downside-adjusted Sharpe
  max_drawdown_pct: 0.15   # 15% max account drawdown
  risk_of_ruin_pct: 0.05   # < 5% Monte Carlo risk of ruin

# Warning thresholds — below these, decay monitor flags
warning_thresholds:
  win_rate: 0.50
  profit_factor: 1.5
  sharpe: 0.5
  max_drawdown_pct: 0.20

# Critical thresholds — below these, auto-demote (after 2-week shadow period)
critical_thresholds:
  win_rate: 0.40
  profit_factor: 1.0       # Breakeven before costs = loser after costs
  sharpe: 0.0              # Negative Sharpe = unambiguously bad
  max_drawdown_pct: 0.25

# Tradeoff notes
#   60% WR + 1:1.5 R:R = PF 2.25 = healthy
#   55% WR + 1:2 R:R = PF 2.44 = also healthy (what Renaissance-class aims for)
#   70% WR + 1:1 R:R = PF 2.33 = similar expected value, tighter distribution
#   Chasing 80% WR requires tiny targets = fragile to bad streaks
```

### `memory/procedural/strategy_params.yaml`

```yaml
# Phoenix Bot — Strategy Parameters (Procedural Memory)
#
# Versioned snapshot of config/strategies.py as of 2026-04-17.
# If code/strategies.py drifts from this, nightly integrity check regenerates.
# Manual edits: update this file, run tools/memory_writeback.py --sync-procedural

generated_from: "config/strategies.py"
generated_at: "2026-04-17T17:35:00-05:00"

# Global thresholds (dashboard sliders can override at runtime)
globals:
  min_confluence: 5.0
  min_momentum_confidence: 80
  min_precision: 48
  risk_per_trade_usd: 15.0   # NOTE: small_account_config overrides this to 5.0 when account < 1500
  max_daily_loss_usd: 45.0   # NOTE: small_account_config overrides this to 15.0 when account < 1500
  base_rr_ratio: 5.0

# Aggression profiles (dashboard buttons)
profiles:
  safe:
    min_confluence: 6.0
    min_momentum_confidence: 85
    min_precision: 55
    risk_per_trade_usd: 8.0
    max_daily_loss_usd: 25.0
  balanced:
    min_confluence: 5.0
    min_momentum_confidence: 80
    min_precision: 48
    risk_per_trade_usd: 15.0
    max_daily_loss_usd: 45.0
  aggressive:
    min_confluence: 4.0
    min_momentum_confidence: 70
    min_precision: 35
    risk_per_trade_usd: 20.0
    max_daily_loss_usd: 50.0

# Per-strategy parameters (only non-sensitive fields)
# Full state in config/strategies.py — this is summary for Claude context
strategies:
  bias_momentum:
    enabled: true
    validated: true        # Runs in prod bot
    stop_ticks: 20
    target_rr: 5.0
    notes: "Hotfixed 2026-04-17 for missing 'price'/'vwap' variable bug"

  spring_setup:
    enabled: true
    validated: true
    notes: "Rule of Three: spring wick + VWAP reclaim + CVD flip"

  ib_breakout:
    enabled: true
    validated: true
    notes: "Published 74.56% WR baseline. Proper ORB rewrite deferred to April 25."

  dom_pullback:
    enabled: true
    validated: false       # Lab only
    notes: "DOM absorption at EMA9/VWAP pullback"

  vwap_pullback:
    enabled: true
    validated: false       # Lab only
    notes: "First pullback to VWAP in trend; MID_MORNING regime"

  high_precision_only:
    enabled: true
    validated: false       # Lab only — DEMOTION CANDIDATE
    notes: "Firing at conf=30 generates mostly losing trades. Threshold should be raised to ≥60, or strategy retired."

  compression_breakout:
    enabled: true
    validated: false       # Lab only
    notes: "Pre-explosion coil entry"
```

### `memory/procedural/small_account_config.yaml`

```yaml
# Phoenix Bot — Small Account Configuration
#
# Active when account_balance < 1500.
# core/simple_sizing.py reads this YAML via PyYAML.
# Strategies + circuit breakers also consume these values.
#
# Why fixed sizing: below ~$1500 account you can't fractionally size MNQ
# contracts (minimum 1), so Kelly math becomes cosmetic. Fixed discipline
# + tight risk = best you can do until account grows.

small_account_mode:
  # Risk limits — extremely tight for $300 account
  max_loss_per_trade_usd: 5.0       # 1.7% of $300
  max_daily_loss_usd: 15.0          # 5% of $300 = 3 max-loss trades
  recovery_mode_trigger_usd: -10.0  # At -$10 daily: cut size, raise thresholds

  # Sizing
  contracts_per_trade: 1            # MNQ only allows whole contracts
  max_trades_per_day: 4             # Forces patience, only A+ setups

  # Signal conviction
  veto_low_conviction_threshold: 80 # Need 80+ composite score (vs 65 default)
                                    # Filters most non-A-setup signals

  # Cooldowns
  loss_streak_cooldown_minutes: 5   # After 2 consecutive losses → 5 min pause
  min_trade_spacing_minutes: 15     # Mandatory gap between trades

  # Target performance (60% WR + 1:1.5 R:R → PF 2.25)
  min_rr: 1.5                        # R:R ratio enforced in strategy execution
  preferred_rr: 2.0                  # Sweet spot

# Auto-upgrade thresholds — when account grows, different configs apply
# These are the INTENDED future states, not currently wired.
account_tier_thresholds:
  # Tier 1: $0-1500 (current) — this file's `small_account_mode` section
  # Tier 2: $1500-3000 — moderate_account_config.yaml (not yet created)
  # Tier 3: $3000-5000 — standard_account_config.yaml
  # Tier 4: $5000+ — full_account_config.yaml

  tier_1_max_usd: 1500
  tier_2_max_usd: 3000
  tier_3_max_usd: 5000
  auto_upgrade_policy: "alert_only"   # vs "auto_apply" (dangerous)
```

### `memory/procedural/regime_matrix.yaml`

```yaml
# Phoenix Bot — Strategy Activation Matrix per Regime
#
# Regime classes (rows):
#   POS_GEX_LOW_VIX  = positive gamma, VIX < 20   — mean-reversion dominant
#   POS_GEX_HIGH_VIX = positive gamma, VIX 20-30  — mean-reversion but wider swings
#   NEG_GEX_LOW_VIX  = negative gamma, VIX < 20   — trending, vol rising
#   NEG_GEX_HIGH_VIX = negative gamma, VIX > 20   — trend + amplified moves
#   UNKNOWN          = MenthorQ data stale/unavailable — conservative defaults
#
# States: ON = full conviction, REDUCED = +10 pts needed + smaller size, OFF = blocked

strategy_matrix:
  # ─── Mean-reversion strategies ─────────────────────────────────────
  vwap_pullback:
    POS_GEX_LOW_VIX:  ON
    POS_GEX_HIGH_VIX: REDUCED
    NEG_GEX_LOW_VIX:  OFF
    NEG_GEX_HIGH_VIX: OFF
    UNKNOWN:          REDUCED

  dom_pullback:
    POS_GEX_LOW_VIX:  ON
    POS_GEX_HIGH_VIX: ON
    NEG_GEX_LOW_VIX:  REDUCED
    NEG_GEX_HIGH_VIX: OFF
    UNKNOWN:          REDUCED

  spring_setup:
    POS_GEX_LOW_VIX:  ON
    POS_GEX_HIGH_VIX: ON
    NEG_GEX_LOW_VIX:  REDUCED
    NEG_GEX_HIGH_VIX: ON        # Fear-driven bounces have edge
    UNKNOWN:          REDUCED

  # ─── Trend-following strategies ────────────────────────────────────
  bias_momentum:
    POS_GEX_LOW_VIX:  REDUCED   # Small moves, limited edge
    POS_GEX_HIGH_VIX: REDUCED
    NEG_GEX_LOW_VIX:  ON        # Prime environment
    NEG_GEX_HIGH_VIX: ON
    UNKNOWN:          REDUCED

  ib_breakout:
    POS_GEX_LOW_VIX:  REDUCED
    POS_GEX_HIGH_VIX: REDUCED
    NEG_GEX_LOW_VIX:  ON
    NEG_GEX_HIGH_VIX: ON
    UNKNOWN:          REDUCED

  compression_breakout:
    POS_GEX_LOW_VIX:  OFF       # Breakouts fail in compressed regime
    POS_GEX_HIGH_VIX: OFF
    NEG_GEX_LOW_VIX:  ON
    NEG_GEX_HIGH_VIX: ON
    UNKNOWN:          OFF

  high_precision_only:
    # DEMOTION CANDIDATE — keep OFF until param tuned or retired.
    POS_GEX_LOW_VIX:  OFF
    POS_GEX_HIGH_VIX: OFF
    NEG_GEX_LOW_VIX:  OFF
    NEG_GEX_HIGH_VIX: OFF
    UNKNOWN:          OFF

  climax_reversal_long:
    POS_GEX_LOW_VIX:  ON
    POS_GEX_HIGH_VIX: ON
    NEG_GEX_LOW_VIX:  REDUCED
    NEG_GEX_HIGH_VIX: REDUCED
    UNKNOWN:          REDUCED

  climax_reversal_short:
    POS_GEX_LOW_VIX:  ON
    POS_GEX_HIGH_VIX: ON
    NEG_GEX_LOW_VIX:  REDUCED
    NEG_GEX_HIGH_VIX: REDUCED
    UNKNOWN:          REDUCED

global_modifiers:
  POS_GEX_LOW_VIX:    { max_concurrent_strategies: 3, signal_threshold_bonus: 0 }
  POS_GEX_HIGH_VIX:   { max_concurrent_strategies: 2, signal_threshold_bonus: 5 }
  NEG_GEX_LOW_VIX:    { max_concurrent_strategies: 2, signal_threshold_bonus: 0 }
  NEG_GEX_HIGH_VIX:   { max_concurrent_strategies: 1, signal_threshold_bonus: 10 }
  UNKNOWN:            { max_concurrent_strategies: 1, signal_threshold_bonus: 10 }
```

### `memory/procedural/regime_params.yaml`

```yaml
# Phoenix Bot — Regime-Specific Parameters
# Multipliers applied to stop/target/sizing based on gamma × VIX regime.

regimes:
  POS_GEX_LOW_VIX:
    stop_multiplier: 0.8             # Tight stops — dealers revert quickly
    target_multiplier: 1.0
    expected_daily_range_nq_pts: 250
    breakout_probability_bias: 0.3   # Fade breakouts
    mean_reversion_bias: 1.3
    chandelier_atr_mult: 2.5
    pin_risk_last_90_min: true
    notes: "Compressed range, mean-rev dominant. Ideal for vwap_pullback + spring + climax-at-wall."

  POS_GEX_HIGH_VIX:
    stop_multiplier: 1.0
    target_multiplier: 1.2
    expected_daily_range_nq_pts: 400
    breakout_probability_bias: 0.5
    mean_reversion_bias: 1.1
    chandelier_atr_mult: 3.0
    pin_risk_last_90_min: true
    notes: "Pos gamma but elevated VIX. Reduce frequency."

  NEG_GEX_LOW_VIX:
    stop_multiplier: 1.3             # Wider stops — overshoots
    target_multiplier: 2.0           # Let winners run
    expected_daily_range_nq_pts: 450
    breakout_probability_bias: 1.4   # Join breakouts
    mean_reversion_bias: 0.7
    chandelier_atr_mult: 3.5
    pin_risk_last_90_min: false
    notes: "Trending. Favor bias_momentum + ib_breakout + compression_breakout."

  NEG_GEX_HIGH_VIX:
    stop_multiplier: 1.8             # Wide stops — whip risk high
    target_multiplier: 3.0
    expected_daily_range_nq_pts: 700
    breakout_probability_bias: 1.2
    mean_reversion_bias: 0.5
    chandelier_atr_mult: 4.0
    pin_risk_last_90_min: false
    sizing_reduction_mult: 0.5       # Cut size further
    notes: "Amplified trends, whip extreme. One strategy at a time."

  UNKNOWN:
    stop_multiplier: 1.0
    target_multiplier: 1.5
    expected_daily_range_nq_pts: 300
    breakout_probability_bias: 1.0
    mean_reversion_bias: 1.0
    chandelier_atr_mult: 3.0
    pin_risk_last_90_min: false
    notes: "MenthorQ unavailable. Conservative defaults."

detection:
  menthorq_staleness_hours: 24
  vix_low_max: 20.0
  vix_high_min: 20.0
  respect_live_hvl: true
```

---

## Section 5: Gaps and questions

Observations that emerged during aggregation. Listed for the review partner's attention — not assertions that things are broken, just surface-level inconsistencies worth thinking about.

### Temporal / versioning gaps

1. **`docs/momentum_days.md` is dated 2026-04-15** and framed as analysis of a single trading day (416 trades from Apr 13-15). It's the densest plan doc. The operational reality per `EVALUATION_2026-04-18.md` (697 trades, 33.3% WR, -$1,227) is **three days newer** and the broader picture. The two tell different stories about the same system: momentum_days frames the Apr 15 loss as 81% commission drag; the Apr 18 eval frames the 697-trade loss as exit-mechanism drag (ema_dom_exit cutting winners short). Reviewer: treat momentum_days as rules-as-of-Apr-15 and the Apr 18 eval as the true scorecard.

2. **`strategy_params.yaml` is a snapshot dated 2026-04-17** of `config/strategies.py`. The live Python config was modified in commit `73921e4` on 2026-04-18 (strategy param rework + two new strategies added: `compression_breakout` and `dom_pullback`). The YAML snapshot includes these strategies but may not reflect every parameter change from the 04-18 rework. Drift check via `tools/memory_writeback.py --sync-procedural` would resolve this.

### Cross-source consistency

3. **`high_precision_only` — conflicting states.** `strategy_params.yaml` marks it `enabled: true, validated: false` with a "demotion candidate" note. `regime_matrix.yaml` marks it `OFF` across every regime. momentum_days calls it "The Sleeper Winner" with 73% WR in OPEN_MOMENTUM. Depending on the bot's gate-composition (does `enabled+lab_only` override `matrix=OFF`?), it either fires in lab and is profitable, or doesn't fire at all. Worth confirming which is actually the active behavior.

4. **MACD scoring — lives only in prose, not config.** `docs/momentum_days.md` spends several hundred words on MACD histogram scoring with specific point values (±12/±8/±15). Nothing in `strategy_params.yaml` or `bias_momentum.py`'s module-level constants references MACD. The implementation would be inside `evaluate()` (not shown in Section 3 per the "docstrings only" rule). Reviewer: you may want to open `strategies/bias_momentum.py` separately to verify MACD scoring is actually wired, not just planned.

5. **CVD gate — same story.** momentum_days says CVD gate is the #1 rule and blocks 43 bad trades historically. Not visible in the `_REGIME_OVERRIDES` dict; presumably inside `evaluate()`. Same caveat: reviewer may want to verify the plan matches code.

### Strategy-doc density imbalance

6. **Seven strategies, dramatically different documentation depth.**
   - `compression_breakout.py` has a 60-line docstring with worked examples and three-phase explanation — the clearest written rulebook in the whole repo.
   - `dom_pullback.py` docstring includes the user's own words describing a real trade ("I bought in on a pullback…") — closest to "this is my personal trading plan" in the whole codebase.
   - `high_precision.py` has a 4-line docstring that says almost nothing about when/why to use it.
   - `spring_setup.py` has the "Rule of Three" description (6 ticks wick, VWAP reclaim, CVD flip) + v2 fixes noted.
   - `vwap_pullback.py` has 4 lines.
   - `bias_momentum.py` docstring is minimal but the `_REGIME_OVERRIDES` dict + the prose in momentum_days fill it in.
   - `ib_breakout.py` has the 74.56% WR published baseline referenced.
   - If the reviewer asks "what rule does `high_precision_only` actually use?", the only answer is the code. There is no written plan for it.

### Known open issues (from momentum_days' own "Open Issues" list)

7. **Counter-trend spring problem not yet fixed** — momentum_days flags it as open; spring_setup v2 fixes mention TF alignment but not specifically the "spring WITH trend on TREND days" rule. Possible gap between intent (plan says filter counter-trend) and implementation (code has TF alignment but maybe not day-type alignment).

8. **Prod bot: 0 trades** — the plan says prod thresholds (`min_confluence=5.5, min_momentum=80`) are "too strict" and prod never fires. This is the elephant: if the prod bot has been running for weeks without trading, the entire live-data feedback loop is lab-only. Reviewer: is this still true, or has prod been tuned looser?

9. **Trade-spacing enforcement** — momentum_days says "lab bot has `trade_spacing_min=5` but fires every 1 minute. Check if spacing is actually being enforced." Open question, possibly invalidates the 416-trade Apr-15 sample if spacing was silently disabled.

### Commission / P&L accuracy

10. **Commission blindness (~$531) + slippage blindness (~$227)** — per momentum_days open issue #6. The current bot does NOT deduct commission from reported P&L. The -$1,227 EVALUATION number is **after NT8's commission application**, but strategy-level P&L attributions may not be. Reviewer should treat strategy WR / P&L numbers in momentum_days as approximate.

### What's deliberately missing

11. **No risk-of-ruin stop rule.** `targets.yaml` sets `risk_of_ruin_pct < 5%` as a target but nothing in the plan says what the bot does if account equity crosses a drawdown threshold. `small_account_config.yaml` has `max_daily_loss_usd: 15` (= halt for the day). But what about a 3-day cumulative drawdown of $30? The circuit breaker system (referenced in MONDAY_READINESS) may cover this but is currently in observe-mode-only per the 2-week shadow rule.

12. **No psychology / tilt rules in writing.** Most institutional trade plans have a "what to do when you're on tilt / after 2 losses / after a win streak" section. The 5-min cooldown after 2 losses (small_account_config) is the only behavioral gate. Whether this is adequate is a reviewer judgment call.

13. **No written entry/exit for news days.** `momentum_days.md` says "The April 2026 tariff spike had HIGH ATR but VOLATILE/UNKNOWN day type — different rules apply (see volatile_days.md, not yet written)." The file doesn't exist yet.

14. **No account-size graduation plan in writing.** `small_account_config.yaml` mentions tiers 2/3/4 (configs for larger accounts) but the files don't exist and the trigger logic is `auto_upgrade_policy: "alert_only"` — user-approved manual promotion. That's deliberate but not spelled out as a written plan.

### Question for the reviewer

This plan describes a system that is:
- Fully automated (no human discretion at entry time)
- Running on live lab data daily
- Paper-only in prod (Sim101, LIVE_TRADING=False) until the account reaches $2,000
- Currently losing money net (33.3% WR / -$1,227 over 697 trades) with the hypothesis that exit-mechanism drag (not entry quality) is the main culprit

**The reviewer's highest-value contribution is probably** adjudicating whether the hypothesis ("exits cut winners short") is correct before more entry-logic complexity is added, and whether the 60% WR / 1:1.5 R:R target is actually realistic given the current 33% WR baseline with the specific strategies as written.

---

_(End of consolidated trading plan.)_
