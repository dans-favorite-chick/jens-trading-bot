# Momentum Days — What Works, What Doesn't, How to Win

*Last updated: 2026-04-15. Based on live lab data from 416 trades (Apr 13-15, 2026).*

---

## TL;DR

On a confirmed TREND/CONTINUATION day, the bot has a small positive gross edge (~-$0.40/trade
gross avg). The entire loss comes from **COMMISSION DRAG** — 173 trades/day × $1.72 = **$297 in
commissions alone**. Fix #1 is to trade 5-15 times per day, not 100+.

**The two rules that would flip today from -$368 to roughly break-even:**
1. **CVD must be positive** (net buying) before entering bias_momentum in afternoon regimes
2. **Trade 15 times per day max** — each unnecessary trade costs $1.72 before it even starts

---

## Day Classification

A "momentum day" fires when:
- `day_type == "TREND"` AND
- `cr_verdict == "CONTINUATION"` (C/R score 4-5/5) AND
- `atr_regime == "HIGH"` (ATR_5m > 15 pts)

**NOT** every high-volatility day is a momentum day. The April 2026 tariff spike had HIGH ATR but
VOLATILE/UNKNOWN day type — different rules apply (see volatile_days.md, not yet written).

---

## Regime Performance — Live Data

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

## The CVD Gate — Proven 2026-04-15

**Rule:** In LATE_AFTERNOON, CLOSE_CHOP, and AFTERNOON_CHOP — if CVD is negative (net selling)
for LONG entries, or positive (net buying) for SHORT entries, do NOT enter any bias_momentum trade.

**Evidence from Apr 15:**
- 43 bias_momentum trades in these regimes, ALL entered while CVD was negative all afternoon
- Result: 43 entries, ~31% average WR, **-$204 total loss**
- Zero exceptions — not one "CVD-opposing afternoon regime" trade was profitable as a group

**Why it works:**
CVD (Cumulative Volume Delta) shows actual institutional buying vs. selling at the session level.
On Apr 15, price was showing micro-bullish 1m/5m signals (higher highs) while CVD was -92M and
falling all afternoon. That's a classic distribution pattern — smart money selling into retail
buying. The CVD gate blocks all these noise entries.

**Critical note — why NOT the 60m tf_bias:**
The `tf_bias["60m"]` metric uses "2 of last 3 completed 60m bars rising." Intraday with only
3-6 completed 60m bars, this typically stays NEUTRAL even on genuinely bullish days. Apr 15 was
a bullish day on the hourly chart — but `tf_bias["60m"]` showed NEUTRAL all session. The CVD
approach is a far more reliable real-time signal.

**Implementation:** `strategies/bias_momentum.py` — CVD check at the top of `generate()` before
any direction or scoring logic runs. LONG blocked when CVD ≤ 0 in chop/afternoon regimes.
SHORT blocked when CVD ≥ 0 in same regimes.

---

## New Scoring Signals (2026-04-15)

### MACD Histogram Scoring

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

**MACD warm-up:** Requires 30+ five-minute bars total (21 to warm EMA21, then 9 more for the
signal line). On a **cold start from blank state**, this takes ~2.5 hours. In normal operation,
state is saved and restored between sessions, so MACD is already warm at 8:30 AM open. The
`macd_warm` flag is just a safety guard — once the signal line has seen 9+ bars ever, it stays
warm permanently across restarts.

**Why MACD helps:**
On Apr 15, the MACD histogram was negative from ~11:00 AM onward (momentum had peaked).
All afternoon bias_momentum LONG entries went against negative MACD. The -15 penalty on a
negative histogram would have blocked most of these since effective_min_momentum is only 20
on TREND days — a -15 hit would make most signals unscorable.

### DOM Directional Scoring

Uses real-time order book data from NT8:

| Condition | LONG pts | SHORT pts |
|-----------|----------|-----------|
| dom_imbalance > 0.6 (bid-heavy, buyers leading) | +10 | -10 |
| dom_imbalance < 0.4 (ask-heavy, sellers leading) | -12 | +10 |
| dom_signal LONG + absorption strength ≥ 40 | +15 | -10 |
| dom_signal SHORT + absorption strength ≥ 40 | -10 | +15 |

**DOM absorption signals:**
When a large passive order absorbs aggressive pressure (e.g., a big bid stack absorbing market
sell orders), this shows institutional accumulation. Absorption strength ≥ 40 = strong signal.

### Q-Level Proximity Scoring

Menthor Q levels (gamma flip HVL, resistance/support walls) act as structural price magnets.
Price near a wall tends to stall or reverse:

| Distance to nearest wall | Score |
|--------------------------|-------|
| Within 20 ticks (5 pts) | -15 |
| Within 40 ticks (10 pts) | -5 |
| Within 16 ticks of HVL | note logged (no direct score, context) |

The -15 for proximity to a major wall will block many entries that would otherwise look valid
on the 1m/5m charts. This is correct — you don't want to enter a momentum trade right into
a known absorption level.

**Note:** Q-level data requires the MenthorQ feed to be running. If unavailable, scoring
defaults to 0 (no penalty applied).

---

## Commission — The Hidden Killer

**173 trades today × $1.72/trade = $297.56 in commissions alone.**

The gross P&L before commissions was only -$70. Net was -$368. This means:
- 81% of today's loss is commission, not bad trading
- The bot has roughly BREAK-EVEN gross edge (when it trades right)
- Every low-quality entry costs $1.72 before the trade even starts

**Target: 5-15 trades per day maximum on momentum days.**

The 10 best setups on a momentum day earn far more than 100 mediocre entries.
A single 20-point bias_momentum win = +$40 net. Ten 1.5:1 scalps = maybe +$4 net at 55% WR.
The math strongly favors fewer, higher-quality, longer-held trades.

---

## Strategy Breakdown

### bias_momentum — The Momentum Rider

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
On TREND days, `effective_min_momentum = 20` — essentially any aligned TF fires a signal.
The new scoring signals (CVD gate, MACD, DOM, Q-levels) now provide the selectivity needed to
prevent 35+ garbage entries in afternoon regimes.

**Fixes implemented (2026-04-15):**
- CVD institutional flow gate for afternoon regimes
- MACD histogram scoring (warm after 30 bars)
- DOM imbalance + absorption scoring
- Q-level proximity scoring
- Rider mode (target 20:1, stall detector exit)
- Stop floor: 14t lab, 20t prod

---

### high_precision_only — The Sleeper Winner

**Overall today:** 98 trades, 50.0% WR, +$10.94

**This is the best performing strategy when filtered properly:**
- OPEN_MOMENTUM: 73% WR, +$17 — strongest single-regime performance
- LATE_AFTERNOON (post-14:31 new code): +$65.72 on just 24 trades

**Why it works in OPEN_MOMENTUM:**
High volatility at open creates overshoots and instant reversals. With 8t stop and 5:1 target
(or ema_dom_exit for large wins), the 73% WR generates consistent positive expectancy.

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
98 trades at $1.72 = $168.56 in commission. Strategy made +$10.94. Without commission: +$179.
Fix needed: require at least 3 TF votes (not 1) even in lab mode to reduce firing rate 50-60%.

---

### spring_setup — Counter-Trend Reversal

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
Several SHORT spring_setups fired on a TREND UP day. Counter-trend springs have lower base rates
since the prevailing trend fights the reversal.

**Fix needed (not yet implemented):**
On TREND LONG days, require spring_setup direction == LONG (WITH the trend).
On TREND SHORT days, require spring_setup direction == SHORT.
Counter-trend springs can still fire in RANGE/VOLATILE days where trend context is weak.

**Target RR:**
The lab override sets target_rr=5.0 (200t = 50pt target). With ATR_5m ≈ 22pt, these are
legitimate expectations. A 40t stop (10pt risk) at 5:1 = 200t target (50pt). The ema_dom_exit
drives actual exits (rider principle), producing 20-50pt exits when the spring works.

---

### compression_breakout — Pre-Explosion Entry

**Today:** 3 trades, 33.3% WR, -$25.16 (1 win at +$18.28, 2 losses)

The one win (+$18.28, +40t) suggests the concept is sound.
Needs more sample data before drawing conclusions.

---

## The Rider Principle — Hold for 20+ Points

**Setting up an OCO bracket at 1.5:1 or even 5:1 and walking away is wrong.**
A genuine momentum move on a TREND day runs 20-80+ points. An OCO bracket at 25pts
closes you out just as the real move begins.

**The rider approach:**
1. Enter with normal setup (14-20t stop)
2. Set OCO target VERY wide (20:1 = 280t = 70pts) — it's a safety net, not a target
3. Once trade is profitable, move stop to break-even (0.5R on RANGE days, 1R on TREND days)
4. Exit on: stall detector (TF fade + CVD divergence + price not advancing) OR ema_dom_exit
   (extended from EMA9 + DOM reversal stacking + candle wicking)
5. Never exit just because price is at a round number or "looks extended"

**Evidence from today:**
- high_precision_only ema_dom_exit: exited at 35-70t with holds of 2-13 minutes
- These are the ONLY trades making meaningful money
- All the OCO scalp exits (12t) barely cover commission

---

## Time-of-Day Rules — Momentum Days

### DO TRADE:

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

### DO NOT TRADE:

**11:00-12:30 (AFTERNOON_CHOP)**
- Lunch chop: narrow range, random direction, stop-grinding
- ONLY: spring_setup / dom_pullback / ib_breakout at extremes
- NEVER: bias_momentum or high_precision_only

**15:00-16:00 (CLOSE_CHOP)**
- 0% WR for bias_momentum (data: Apr 15)
- Market flattening into EOD, random noise
- DO NOT TRADE this window

---

## The Ideal Momentum Day Trade Plan

**Target: 5-8 trades total, $50-$200 P&L potential**

1. **8:30-8:45** — OPEN_MOMENTUM. Watch first candle + DOM. Enter first high_precision_only
   or compression_breakout signal. Rider mode on, hold until stall detector fires.

2. **9:00-10:00** — MID_MORNING. Confirm CVD is positive and climbing. Add 1-2 bias_momentum
   entries on pullbacks to EMA9. Check MACD histogram direction (warm after ~30 bars).
   These are the potential 20-50pt runners.

3. **10:00-12:30** — Rest. Watch from sidelines unless strong reversal spring forms at a key level.
   Check if CVD is still rising or starting to roll over.

4. **13:00-14:30 (only if CVD still positive)** — Late afternoon continuation. 1-2 more entries.
   If CVD has turned negative, the momentum day has stalled. Stop trading.

5. **14:30+** — Stop. CLOSE_CHOP eats commissions and wins back nothing.

---

## What to Monitor for Validity

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

## Key Numbers — MNQ Quick Reference

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

## Implemented Changes (2026-04-15)

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

## Open Issues / Next Improvements

1. **spring_setup trend direction filter** — on TREND days, only take springs WITH the trend
   direction. Counter-trend springs need stronger confirmation. Not yet implemented.

2. **high_precision_only trade frequency** — 98 trades at $168 commission. Need to require
   3+ TF votes even in lab mode to reduce firing rate by 50-60%.

3. **Prod bot: 0 trades** — prod thresholds too strict (min_confluence=5.5, min_momentum=80).
   After validating changes on lab, investigate why prod never fires.

4. **Backtesting** — validate CVD gate, MACD, DOM scoring, and spring direction filter against
   historical data before scaling. See plan: whimsical-chasing-waterfall.md.

5. **Trade spacing enforcement** — lab bot has `trade_spacing_min=5` but fires every 1 minute.
   Check if spacing is actually being enforced in lab_bot._evaluate_strategies().

6. **P&L accuracy** — commission blindness (~$531 gap vs NT8) and slippage blindness (~$227 gap).
   Fix plan at: compiled-growing-lark.md. Implement commission deduction + limit orders.
