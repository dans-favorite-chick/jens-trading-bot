"""
Phoenix Bot -- Expert Trading Knowledge Base

Imported by Council Gate voters and Session Debriefer to inject deep
MNQ trading expertise into AI agent prompts.

This is structured data + prompt templates -- the collective brain
of the AI agents.  Everything here is MNQ-specific and tuned for
single-contract, NY-session scalping.
"""

from __future__ import annotations

# =====================================================================
# 1. MNQ Trading Rules of Thumb
# =====================================================================

MNQ_TRADING_RULES: str = """\
## MNQ Futures -- Rules of Thumb

### Contract Specifications
- Tick size: 0.25 index points
- Tick value: $0.50 per tick ($2.00 per point)
- Point value: $2.00 (4 ticks per point)
- Daily margin (day-trade): ~$50-100 depending on broker
- Overnight margin: ~$1,800+
- Contract months: March (H), June (M), September (U), December (Z)
- Primary exchange: CME Globex
- Trading hours: Sun 5 PM - Fri 4 PM CT (with 15-min daily halt at 3:15 PM CT)

### Key Price Levels to Watch
- Round numbers: every 100 points (e.g., 18500, 18600) act as magnets and S/R
- Half-rounds: every 50 points (e.g., 18550) are secondary S/R
- Prior Day High (PDH) / Prior Day Low (PDL): institutional memory levels
- Prior Day Close (PDC): sentiment pivot -- above = bullish, below = bearish
- VWAP: the institutional fair value line; price reverts to it
- Opening range high/low (first 15 min): defines the session's battlefield
- Overnight high/low: levels where Asian/European traders positioned
- Weekly open: swing traders anchor to this level

### Session Characteristics (all times Central)
- 8:30-10:00 AM: HIGHEST EDGE WINDOW. Volatility + volume + directional moves.
  This is where 80% of daily range is established. Trade aggressively here.
- 10:00-10:30 AM: Economic data releases (if any) cause spikes. Be positioned
  or be flat -- never get caught mid-entry.
- 10:30-11:30 AM: Mid-morning follow-through or reversal. Still tradeable but
  momentum fading. Tighten stops.
- 11:30 AM - 1:00 PM: LUNCH DEAD ZONE. Volume drops 40-60%. Spreads widen.
  Choppy, mean-reverting, stop-hunting price action. Reduce size or sit out.
- 1:00-2:00 PM: Early afternoon drift. Often continues morning direction but
  can set up late-day reversal. Low conviction.
- 2:00-3:00 PM: Institutional rebalancing window. MOC (market-on-close) orders
  start flowing. Can produce strong directional moves. Watch for the 2:00 PM
  reversal pattern.
- 3:00-3:15 PM: Final push -- often a fakeout or exhaustion move.
- 3:15 PM: Daily settlement halt. DO NOT hold through this.

### Volume Patterns
- Volume spikes on economic releases (CPI, FOMC, NFP) -- 3-10x normal
- Pre-market (6-8:30 AM CT) is thin; moves exaggerated but unreliable
- Overnight (5 PM - 6 AM CT) is extremely thin; gaps common
- Volume should CONFIRM breakouts -- breakout on declining volume = trap
- Delta divergence (price up, delta down) is a strong reversal warning

### Intermarket Correlations
- NQ and ES: ~0.92-0.96 correlation; NQ leads on tech momentum, ES leads on
  broad risk-off. If NQ diverges from ES, the outlier usually corrects.
- NQ and DXY (Dollar Index): inverse correlation. Strong dollar = NQ headwind.
  Watch DXY breakouts for NQ direction clues.
- NQ and 10Y Yields (ZN): inverse. Rising yields compress tech multiples.
  Sudden yield spikes (>5 bps intraday) = NQ selling pressure.
- NQ and VIX: inverse. VIX > 25 means wider ranges, bigger stops needed.
  VIX > 35 is crisis mode -- reduce size 50%+.
- NQ and AAPL/MSFT/NVDA: mega-cap tech drives NQ. If the big 3 are green
  and NQ is red, NQ will likely catch up (and vice versa).
- Bitcoin: weak positive correlation during risk-on. Crypto crash can spill
  into NQ via sentiment contagion.

### News Impact Guide (approximate MNQ point moves)
- FOMC rate decision + press conference: 50-150 points. DO NOT trade the
  announcement -- wait 15 minutes for the whipsaw to settle, then fade or follow.
- CPI / Core PCE: 30-80 points. Hot print = sell NQ, cool print = buy.
- Non-Farm Payrolls (NFP): 30-60 points. Knee-jerk is often wrong.
- Tariff/trade war tweets: 20-50 points. Fade the spike if no follow-through
  after 5 minutes.
- Earnings (AAPL, MSFT, NVDA, META): 20-40 points pre/post market.
- Geopolitical shocks: 30-100+ points. Always buy the dip on non-economic shocks
  (with stop protection) -- markets recover unless fundamentals change.
- Fed speaker comments: 10-30 points. Hawkish surprise = sell, dovish = buy.

### Stop Placement Rules
- NEVER place stops inside the noise. MNQ noise on 1m chart = 4-6 ticks.
- Minimum stop for any MNQ trade: 8-10 ticks ($4-5). Tighter stops get
  stopped out by normal fluctuation.
- Place stops behind structure: below swing low (long), above swing high (short).
- ATR-based stops: 1.5x ATR(14) on the entry timeframe is a good baseline.
- In high-VIX environments (VIX > 25), widen stops by 50% or reduce position.
- NEVER move a stop further away from entry. Only move it in your favor (trail).

### Position Sizing
- NEVER risk more than 2% of account on a single trade ($40 on $2,000 account).
- Scale risk to conviction: 1% for C-tier setups, 1.5% for B-tier, 2% for A-tier.
- After 2 consecutive losses, reduce size by 50% for next 2 trades.
- After daily max loss is hit ($45-50), STOP TRADING. No exceptions.
- Single contract only -- MNQ is volatile enough for edge capture without scaling.
"""


# =====================================================================
# 2. Technical Analysis Pattern Library
# =====================================================================

PATTERN_LIBRARY: list[dict] = [
    {
        "name": "Opening Range Breakout",
        "description": (
            "Define the range of the first 15-minute candle (8:30-8:45 AM CT). "
            "A decisive break above the high = long, below the low = short. "
            "The opening range sets the session's initial battlefield."
        ),
        "best_regime": "OPEN_MOMENTUM",
        "win_rate_historical": "55-65%",
        "stop_placement": "Opposite side of the opening range (tight) or midpoint (conservative)",
        "target": "1.5-2x the opening range width",
        "key_filter": (
            "Only trade in the direction of overnight bias. If overnight was bullish "
            "(price above PDC at open), only take long breakouts. Volume must expand "
            "on the breakout candle vs. the range candles."
        ),
        "invalidation": "If price re-enters the range and closes inside it, the breakout failed.",
    },
    {
        "name": "VWAP Reclaim",
        "description": (
            "Price drops below VWAP, undercuts by 2-5 points, then reclaims VWAP "
            "with a strong close above it. This traps shorts who sold the VWAP break "
            "and triggers a squeeze back up."
        ),
        "best_regime": "OPEN_MOMENTUM",
        "win_rate_historical": "58-68%",
        "stop_placement": "Below the VWAP undercut low, plus 2 ticks of buffer",
        "target": "Prior swing high or VWAP + ATR(14)",
        "key_filter": (
            "CVD must show buying absorption during the undercut (delta positive or "
            "flat while price drops). DOM should show bid stacking near the low."
        ),
        "invalidation": "If price fails to hold above VWAP for 2 consecutive 1-min candles.",
    },
    {
        "name": "VWAP Rejection",
        "description": (
            "Price rallies into VWAP from below but cannot close above it on 5-min. "
            "Multiple wicks above VWAP with closes below = institutional selling at "
            "fair value. Short on the third rejection."
        ),
        "best_regime": "MID_MORNING",
        "win_rate_historical": "55-62%",
        "stop_placement": "Above VWAP + 3-4 points (above the wick highs)",
        "target": "Prior swing low or VWAP - ATR(14)",
        "key_filter": (
            "Bearish CVD divergence: price tests VWAP multiple times but CVD makes "
            "lower highs. EMA9 on 5m should be below EMA21 (trend confirmation)."
        ),
        "invalidation": "A 5-min close above VWAP with expanding volume.",
    },
    {
        "name": "First Pullback to EMA",
        "description": (
            "After a strong directional move off the open, price pulls back to the "
            "9 EMA on the 5-min chart for the first time. This is the highest-"
            "probability pullback entry because the trend is fresh and momentum "
            "is strong."
        ),
        "best_regime": "OPEN_MOMENTUM",
        "win_rate_historical": "60-70%",
        "stop_placement": "Below the 21 EMA on 5-min (long) or above it (short)",
        "target": "New high/low beyond the prior swing, or 2x risk",
        "key_filter": (
            "The pullback must be orderly (declining volume, small candles) not "
            "a panicked reversal. The 9 EMA must not have crossed below the 21 EMA. "
            "Best when pullback coincides with a round number or prior S/R level."
        ),
        "invalidation": "If price closes below the 21 EMA on 5-min, the trend is weakening.",
    },
    {
        "name": "Spring / Liquidity Sweep",
        "description": (
            "Price briefly undercuts a well-defined support level (prior low, round "
            "number, or overnight low), triggering stop-loss orders, then immediately "
            "reverses back above the level. The sweep 'springs' trapped sellers."
        ),
        "best_regime": "OPEN_MOMENTUM",
        "win_rate_historical": "60-68%",
        "stop_placement": "Below the spring low, plus 2-3 ticks (the sweep must hold)",
        "target": "The prior range high, or the level that spawned the sell-off",
        "key_filter": (
            "Volume spike on the sweep followed by immediate reversal (1-2 candles). "
            "CVD must flip from negative to positive. DOM shows aggressive bid lifting "
            "after the sweep. Best at session-defined levels (PDL, overnight low)."
        ),
        "invalidation": "If price retests the spring low and breaks through again.",
    },
    {
        "name": "Failed Breakdown (Trapped Shorts)",
        "description": (
            "Price breaks below support on volume, shorts pile in, but it immediately "
            "reverses and closes back above the level. Shorts are trapped and forced "
            "to cover, fueling a squeeze higher."
        ),
        "best_regime": "MID_MORNING",
        "win_rate_historical": "57-65%",
        "stop_placement": "Below the breakdown low minus 2 ticks",
        "target": "The high of the range before the breakdown attempt, or 2x risk",
        "key_filter": (
            "The reclaim must happen within 3-5 bars (on 1-min). If it takes longer, "
            "the breakdown may be real. CVD should show aggressive buying on the "
            "reclaim candle. Higher timeframe trend should be bullish."
        ),
        "invalidation": "If price re-breaks support with even higher volume on the second attempt.",
    },
    {
        "name": "Momentum Ignition",
        "description": (
            "A sudden, outsized move on heavy volume that clears multiple levels in "
            "2-3 candles. Often caused by a news catalyst or large institutional order. "
            "Join the move on the first shallow pullback (1-2 candle dip)."
        ),
        "best_regime": "OPEN_MOMENTUM",
        "win_rate_historical": "55-63%",
        "stop_placement": "Below the pullback low (tight) or below the ignition candle midpoint",
        "target": "Extension measured from the ignition base (1:1 or 1.5:1)",
        "key_filter": (
            "Volume on the ignition candle must be 2x+ the 20-period average. "
            "The move must break a prior swing high/low, not just chop. "
            "Avoid joining if the ignition already traveled more than 2x ATR "
            "(likely exhausted)."
        ),
        "invalidation": "If the pullback retraces more than 61.8% of the ignition move.",
    },
    {
        "name": "Mean Reversion at ATR Extremes",
        "description": (
            "Price has moved more than 2x the 15-min ATR(14) from VWAP in a single "
            "session direction. At these extremes, a reversion toward VWAP is likely. "
            "Fade the move with a tight stop beyond the extreme."
        ),
        "best_regime": "AFTERNOON_CHOP",
        "win_rate_historical": "55-62%",
        "stop_placement": "Beyond the extreme (high/low) + 4-5 ticks",
        "target": "VWAP (partial), then 50% of the move (runner)",
        "key_filter": (
            "Must see exhaustion signals: volume declining, candle bodies shrinking, "
            "CVD flattening or diverging. Time of day matters: best after 11 AM CT "
            "when the initial directional move is done. Do NOT fade during OPEN_MOMENTUM."
        ),
        "invalidation": "A fresh volume spike in the trending direction = trend resumption, not exhaustion.",
    },
    {
        "name": "Double Top/Bottom with CVD Divergence",
        "description": (
            "Price makes two equal highs (or lows) but CVD makes a lower high "
            "(or higher low) on the second test. The divergence reveals weakening "
            "buying (or selling) pressure behind the apparent strength (or weakness)."
        ),
        "best_regime": "MID_MORNING",
        "win_rate_historical": "58-66%",
        "stop_placement": "Above the double top (short) or below the double bottom (long), plus 3 ticks",
        "target": "The range depth (distance from top to neckline) projected from the break",
        "key_filter": (
            "The two peaks/troughs should be within 2-3 points of each other. "
            "CVD divergence must be clear (not just noise). Best with a volume spike "
            "on the first test and declining volume on the second. "
            "EMA alignment on 5-min should confirm the reversal direction."
        ),
        "invalidation": "A close above the double top (short) or below the double bottom (long).",
    },
    {
        "name": "Ascending/Descending Triangle Break",
        "description": (
            "Price makes higher lows squeezing into a flat resistance (ascending) or "
            "lower highs pressing against flat support (descending). The compression "
            "stores energy; the break is explosive."
        ),
        "best_regime": "MID_MORNING",
        "win_rate_historical": "55-63%",
        "stop_placement": "Below the last higher low (ascending break) or above last lower high (descending)",
        "target": "The height of the triangle projected from the breakout point",
        "key_filter": (
            "Needs at least 3 touches on the flat side and 2 on the angled side. "
            "Volume should decline during the compression and expand on the break. "
            "Best when the triangle forms within the first 60 minutes of the session."
        ),
        "invalidation": "If the break reverses and closes back inside the triangle within 2 bars.",
    },
    {
        "name": "Gap Fill Setup",
        "description": (
            "Overnight gap (open vs. prior close) that starts to fill in the first "
            "30 minutes. Gaps under 15 points fill ~70% of the time by end of session. "
            "Trade in the gap-fill direction after seeing the first reversal candle."
        ),
        "best_regime": "OPEN_MOMENTUM",
        "win_rate_historical": "60-70%",
        "stop_placement": "Beyond the overnight extreme (gap high for shorts, gap low for longs), plus 3 ticks",
        "target": "Prior day close (full fill) with partial at 50% fill",
        "key_filter": (
            "Gap must be under 15-20 points for high-probability fill. Larger gaps "
            "(30+ points) are often trend gaps that do NOT fill same-day. "
            "Need a reversal candle or CVD shift in the fill direction within "
            "first 15 minutes. Avoid if strong trend context supports the gap direction."
        ),
        "invalidation": "If price moves further in the gap direction past the overnight extreme.",
    },
    {
        "name": "Afternoon Reversal (2:00-2:30 PM CT)",
        "description": (
            "Between 2:00 and 2:30 PM CT, institutional MOC (market-on-close) orders "
            "begin flowing. If the day has been trending one direction, this window "
            "frequently produces a reversal or at least a strong counter-move."
        ),
        "best_regime": "LATE_AFTERNOON",
        "win_rate_historical": "52-58%",
        "stop_placement": "Beyond the 2 PM extreme + 5 ticks (these are wider moves)",
        "target": "50% retracement of the day's range, or VWAP",
        "key_filter": (
            "Day must have been directionally trending (not choppy). "
            "Look for CVD divergence at the 2 PM extreme. Volume pickup after 2 PM "
            "confirms institutional flow. Avoid if there is a major catalyst still "
            "pending (e.g., 2:30 PM auction results)."
        ),
        "invalidation": "If the directional trend resumes with new highs/lows after 2:15 PM.",
    },
    {
        "name": "FOMC Fade",
        "description": (
            "On FOMC announcement days (2:00 PM ET / 1:00 PM CT), the initial spike "
            "is almost always wrong. Wait 10-15 minutes for the whipsaw, then fade "
            "the initial direction. The 'real' move comes during the press conference."
        ),
        "best_regime": "LATE_AFTERNOON",
        "win_rate_historical": "55-65%",
        "stop_placement": "Beyond the initial spike extreme + 5-10 ticks (wide stops required)",
        "target": "Return to pre-FOMC price, then reassess during press conference",
        "key_filter": (
            "ONLY trade this if the initial spike exceeds 20+ points. Small reactions "
            "are ambiguous. Wait for at least one full 5-min candle to close after the "
            "announcement before entering. Reduce position size by 50% on FOMC days."
        ),
        "invalidation": "If the initial direction holds through 3+ five-minute candles with volume.",
    },
    {
        "name": "EMA9/21 Crossover Pullback",
        "description": (
            "After a fresh EMA9/21 crossover on the 5-min chart, wait for the first "
            "pullback to the 9 EMA that holds. This confirms the new trend direction "
            "and offers a low-risk entry point."
        ),
        "best_regime": "MID_MORNING",
        "win_rate_historical": "55-62%",
        "stop_placement": "Below the 21 EMA (long after bullish cross) or above it (short after bearish)",
        "target": "Next key level or 2x risk",
        "key_filter": (
            "The crossover must be clean (not a series of intertwined crosses). "
            "Prefer the first pullback only; the second and third pullbacks have "
            "diminishing returns. ATR should be expanding (trending, not ranging)."
        ),
        "invalidation": "If the 9 EMA crosses back through the 21 EMA (crossover failed).",
    },
]


# =====================================================================
# 3. Risk Management Wisdom
# =====================================================================

RISK_WISDOM: str = """\
## Risk Management -- Hard-Won Wisdom for MNQ Scalping

### The 2% Rule
Never risk more than 2% of your account on a single trade. On a $2,000 account,
that is $40 max risk per trade. This is not conservative -- it is survival math.
At 2% risk per trade, you can endure 10 consecutive losers and still have 80% of
your account. At 5% risk, 10 losers wipes out 40%. The market WILL test you with
losing streaks.

### Why Overtrading Kills More Accounts Than Bad Entries
The #1 account killer is not bad analysis -- it is taking too many trades. Every
trade incurs spread cost, slippage, and psychological toll. A trader who takes
3 high-quality trades at 60% win rate will massively outperform a trader who
takes 15 mediocre trades at 50% win rate. The math: 3 trades x 60% x 2R = +1.8R
net. 15 trades x 50% x 1.5R = +3.75R gross, but after spread/slippage costs and
tilt-induced errors, often net negative.

RULE: If you cannot articulate the setup name, the stop, and the target in 5
seconds, you do not have a trade. You have a gamble.

### Recovery Math (Why Drawdowns Are Asymmetric)
- Lose 10% -> need 11.1% gain to recover
- Lose 20% -> need 25.0% gain to recover
- Lose 30% -> need 42.9% gain to recover
- Lose 50% -> need 100% gain to recover (you are done)

This is why capital preservation matters more than profit maximization. A 20%
drawdown is recoverable. A 50% drawdown is a funeral. Protect the account at
all costs.

### Trade Smaller in Choppy Regimes, Don't Stop Trading
When the market is choppy (low ATR, AFTERNOON_CHOP regime, lunch hours), the
instinct is to stop trading entirely. This is wrong for two reasons:
1. You miss the occasional clean setup that DOES emerge
2. You lose your "feel" for the market and re-entry is harder

Instead: reduce position size by 50-70% and tighten your filters. Take only A+
setups. This keeps you engaged while protecting capital.

### The Three-Strikes Rule
After 3 consecutive losses in a single session:
1. STOP trading immediately
2. Walk away from the screen for 15 minutes
3. When you return, reduce size by 50% for the rest of the session
4. If the next trade also loses, you are DONE for the day

Why: 3 losses in a row usually means one of these:
- Your read on the market is wrong today
- The regime has shifted and your strategies have not adapted
- You are tilting and making revenge trades

In all three cases, continuing to trade makes it worse.

### NEVER Average Down on Futures
Averaging down on a losing futures position is the fastest way to blow up an
account. This is not stock investing where "buying the dip" can work because
you own an asset. Futures are leveraged derivatives with daily settlement.
Averaging down means:
- You are doubling your exposure to a move that is already going against you
- Your margin requirement doubles
- If price continues against you, your loss is now 2x what it would have been
- A margin call forces liquidation at the worst possible price

If your trade is wrong, take the loss. One small loss is recoverable. A doubled-
down catastrophic loss can end your trading career.

### Correlation Risk
NQ and ES are 92-96% correlated. If you are long NQ and long ES simultaneously,
you effectively have DOUBLE the exposure to the same move. This is not
diversification -- it is concentration disguised as two trades.

If you want to hedge or diversify:
- Trade NQ against DXY (inverse correlation)
- Trade NQ against bonds/ZN (inverse correlation)
- If you must trade NQ and ES, ensure one is a hedge (opposite direction)

### Daily Stop-Loss Is Non-Negotiable
Set a daily maximum loss ($45-50 on $2,000 account = ~2.5%) and NEVER exceed it.
When you hit the daily stop:
- Close all positions immediately
- Shut down the bot
- Do NOT re-enable trading "just for one more try"
- Write in your journal what happened
- Tomorrow is a fresh day with full capital

The daily stop exists because humans (and bots) make worse decisions as losses
accumulate. The P&L pressure creates urgency that overrides discipline. The daily
stop removes you from the equation before the damage compounds.

### Size Down After Drawdowns
If the account drops 10% from its high-water mark:
- Reduce all position sizes by 50%
- Tighten all entry filters (raise minimum confluence scores)
- Trade only in OPEN_MOMENTUM and MID_MORNING regimes
- Only return to full size after recovering 50% of the drawdown

This is recovery mode. The goal is not to make money -- it is to stop losing money
and rebuild confidence through small, clean wins.
"""


# =====================================================================
# 4. Regime-Specific Playbooks
# =====================================================================

REGIME_PLAYBOOKS: dict[str, str] = {
    "OVERNIGHT_RANGE": """\
Overnight Range (5:00 PM - 7:00 AM CT)

Thin volume, wide spreads, unreliable moves. This is NOT a trading regime --
it is a level-setting regime. Use it to prepare, not to trade.

Rules:
- Only spring_setup allowed (fade extremes at defined levels)
- Position size: 50% of normal (size_multiplier 0.5)
- Stops must be wider than normal (1.5x ATR minimum) because overnight spikes
  are common and often retrace
- Mark the overnight high and low -- these become key levels for the session
- If the overnight range is unusually wide (>30 MNQ points), expect a
  range-bound morning. If tight (<10 points), expect a breakout at 8:30

Key levels to set:
- Overnight high (ONH) and overnight low (ONL)
- Globex session VWAP (separate from RTH VWAP)
- Asian session pivot levels (relevant for correlated FX moves)

Psychological note: the temptation to "get a head start" by trading overnight
is strong. Resist it. The edge is near zero and the execution risk is high.
""",

    "PREMARKET_DRIFT": """\
Pre-Market Drift (7:00 - 8:30 AM CT)

Volume building but still thin. Economic data releases at 7:30 AM (if any) cause
spikes. This regime is a KNOWN BLEEDER in backtesting (37.5% WR, -$13 P&L).

Rules:
- Only bias_momentum allowed, and ONLY in the data-release spike direction
- Position size: 30% of normal (heavily reduced after backtest showed bleeding)
- Minimum confluence raised to 4.2 (very high bar)
- DO NOT trade the 7:30 data release itself -- wait for the dust to settle
  (at least 2 five-minute candles after the release)
- The real purpose of this window: READ the market. Observe how price reacts
  to pre-market levels and data. This informs your 8:30 bias.

What to observe:
- Is price above or below PDC? (Bullish/bearish pre-market sentiment)
- How did price react to 7:30 data? (Conviction or fade?)
- Where is VWAP relative to the overnight range? (Institutional positioning)
- Are the big tech names (AAPL, MSFT, NVDA) green or red pre-market?
- Any tariff/geopolitical headlines overnight?

If you must trade here, accept that this is a data-gathering exercise, not a
profit-generation window.
""",

    "OPEN_MOMENTUM": """\
Open Momentum (8:30 - 9:30 AM CT)

This is the HIGHEST EDGE period. The opening 60 minutes produce the strongest
directional moves and the highest volume. Everything the bot does is optimized
for this window.

Rules:
- ALL strategies are allowed -- gates are at their lowest (confluence 2.8)
- Full position size (1.0 multiplier)
- Wait for the first 5-min candle to CLOSE before entering. The 8:30 candle
  is pure chaos -- let it resolve.
- Trade in the direction of the 5-min opening bar (if convincing)
- First 15 minutes (8:30-8:45): pure momentum plays. Breakout entries.
- After 9:00: switch to pullback entries. The first pullback to 9 EMA on 5-min
  is the highest-probability trade of the day.
- If the first 3 five-minute candles are dojis or inside bars, SIT OUT.
  No conviction = no edge.
- Volume MUST confirm: increasing volume on breakout, declining on pullback.
  A breakout on declining volume is a trap.

Key setups for this regime:
1. Opening Range Breakout (best after 8:45 close)
2. First Pullback to 9 EMA (best 9:00-9:15)
3. Gap Fill (if overnight gap < 15 points)
4. Spring at overnight high/low
5. Momentum Ignition (if news catalyst)

Warning signs to reduce exposure:
- Opening range is extremely tight (<5 points) -- expect a violent breakout
  but direction is uncertain. Wait for confirmation.
- Opening range is extremely wide (>20 points) -- momentum is spent.
  Switch to mean reversion.
- VIX gap up >10% -- extra wide stops required.
""",

    "MID_MORNING": """\
Mid-Morning (9:30 - 11:30 AM CT)

GOLD REGIME in backtesting (100% WR, +$120 P&L). This is where follow-through
trades produce the cleanest profits. The opening chaos is over; the trend is
established; pullbacks are orderly.

Rules:
- ALL strategies allowed -- wide open regime
- Full position size (1.0 multiplier)
- Minimum confluence lowered to 2.5 (maximize signal generation)
- Trade WITH the established trend. If the morning was bullish, look for
  long pullback entries. Do not try to pick the top.
- EMA9/21 crossovers on 5-min are highly reliable in this window.
- VWAP rejections/reclaims are high-probability here.

Key setups for this regime:
1. First Pullback to EMA (continuation of morning trend)
2. VWAP Reclaim (if price dipped below in the open)
3. Double Top/Bottom with CVD Divergence (reversal signals)
4. Failed Breakdown (trapped traders)
5. Triangle Breakout (compression patterns)

What makes this regime special:
- Volume is still high but the "crazy" is gone
- Trends established in the open have follow-through here
- Institutions are done with their opening orders and now manage positions
- Stop runs are less frequent; price respects levels better

Risk adjustment:
- If 10:00 AM economic data is due, flatten or tighten stops at 9:55
- After 10:30, momentum starts to fade. Tighten targets.
- If you have 2+ winners already, protect profits aggressively.
""",

    "AFTERNOON_CHOP": """\
Afternoon Chop / Lunch Lull (11:30 AM - 1:00 PM CT)

THE DEATH ZONE. Volume drops 40-60%. Price chops in a range. Stop hunts are
frequent. Spreads can widen. This is where profitable mornings go to die.

Rules:
- Confluence raised to 4.0 (very selective)
- Position size: 50% of normal
- Lab bot trades here for data collection, but PROD should be extremely
  selective or completely flat
- Mean reversion ONLY. Do not trade breakouts during lunch.
- If you are flat, STAY FLAT. The edge is near zero.

What typically happens:
- Price oscillates between morning high and morning low in a narrowing range
- Volume dries up; individual orders move price disproportionately
- Algorithms run stop hunts at prior swing highs/lows
- Dojis and spinning tops everywhere on 5-min chart
- VWAP becomes a magnet -- price hovers around it

The ONLY acceptable trade:
- Mean reversion at the morning's extreme (high or low) with clear CVD
  exhaustion. Target: VWAP. Stop: beyond the extreme + 5 ticks.

Psychological note: boredom is the enemy here. The temptation to "find" a
trade is overwhelming. Every choppy candle looks like a setup if you stare
at it long enough. This is where discipline earns its keep.
""",

    "LATE_AFTERNOON": """\
Late Afternoon (1:00 - 3:00 PM CT)

Institutional rebalancing window. MOC (market-on-close) orders start flowing
around 2:00 PM. This can produce genuine directional moves, especially the
2:00-2:30 PM reversal pattern.

Rules:
- Strategies: bias_momentum and spring_setup only
- Position size: 80% of normal
- Watch for the 2:00 PM reversal: if the day was trending, institutions often
  rebalance against the trend here
- FOMC days: the announcement is at 1:00 PM CT. See the FOMC Fade pattern.
  Reduce size by 50% and use wide stops.

Key setups:
1. Afternoon Reversal (2:00-2:30 PM)
2. FOMC Fade (on FOMC days)
3. Spring at day's high/low (institutional stop run before close)

What to watch:
- MOC imbalance data (published around 2:45 PM) indicates closing flow direction
- Bond market closes at 2:00 PM CT -- this can shift correlations
- Options market makers delta-hedge more aggressively toward close

Risk management:
- Do NOT enter new positions after 2:45 PM CT unless you plan to close
  before 3:00 PM
- Flatten everything before 3:15 PM (daily settlement halt)
- If you are profitable for the day, protect profits. Do not give back
  the morning's gains chasing afternoon setups.
""",

    "CLOSE_CHOP": """\
Close Chop (3:00 - 3:15 PM CT)

Final 15 minutes before the daily settlement halt. This is NOT a trading window.
It is a position-management window.

Rules:
- Confluence raised to 4.0 (effectively: no new trades)
- Position size: 30% of normal
- Lab bot may trade for data, but PROD should be FLAT
- Priority #1: close all open positions before 3:15 PM

What happens here:
- MOC orders execute, causing spikes in either direction
- Spread widens as market makers reduce risk
- Last-minute stop hunts are common
- Price can move 5-10 points in seconds with no follow-through

The ONLY reason to be in a position here is if you are nursing a runner
from an earlier trade with a trailing stop. Otherwise: be flat.

End-of-day routine:
1. Close all positions by 3:10 PM at the latest
2. Record final P&L
3. Note the closing price, VWAP close, and day's range
4. These become tomorrow's reference levels (PDH, PDL, PDC)
""",

    "POST_MARKET": """\
Post-Market (3:15 PM - 5:00 PM CT)

Session is over. No trading. This is analysis and preparation time.

Post-session checklist:
1. Review all trades: entries, exits, what worked, what did not
2. Check if the Session Debriefer ran and review its output
3. Note any regime-specific observations (was OPEN_MOMENTUM clean today?)
4. Mark tomorrow's key levels: today's high, low, close, VWAP close
5. Check the economic calendar for tomorrow's events
6. Review overnight news for potential gap scenarios
7. Update strategy parameters if the debrief suggests changes

This is where the learning happens. Trading is execution; post-market is
education. The best traders spend more time reviewing than trading.
""",
}


# =====================================================================
# 5. Market Condition Interpreter
# =====================================================================

def interpret_market_conditions(intel: dict) -> str:
    """
    Analyze the full market intel dict (from market_intel.get_full_intel())
    and produce a concise trading assessment for injection into AI agent prompts.

    Returns a 3-5 sentence summary covering risk posture, key drivers, and
    actionable context.

    Args:
        intel: The full intel dict with keys: vix, news, calendar,
               market_context, trump, reddit, fred, trade_ok,
               restriction_reason, trump_warning
    """
    lines: list[str] = []

    # ── VIX Assessment ──
    vix_data = intel.get("vix", {})
    vix_level = vix_data.get("vix", 0) if isinstance(vix_data, dict) else 0
    if vix_level > 35:
        lines.append(
            f"DANGER: VIX at {vix_level:.1f} -- crisis-level volatility. "
            "Reduce all positions 50%, widen stops 50%, trade only A+ setups."
        )
    elif vix_level > 25:
        lines.append(
            f"CAUTION: VIX elevated at {vix_level:.1f}. "
            "Expect wide swings and stop hunts. Widen stops, reduce size 30%."
        )
    elif vix_level > 18:
        lines.append(
            f"VIX at {vix_level:.1f} -- normal elevated range. "
            "Good for momentum trades; trends tend to follow through."
        )
    elif vix_level > 0:
        lines.append(
            f"VIX at {vix_level:.1f} -- low volatility. "
            "Expect tighter ranges and more mean reversion. Reduce targets."
        )

    # ── DXY (Dollar) Assessment ──
    context = intel.get("market_context", {})
    if isinstance(context, dict):
        dxy_data = context.get("dxy", {})
        if isinstance(dxy_data, dict):
            dxy_change = dxy_data.get("change_pct", 0)
            if dxy_change and abs(dxy_change) > 0.3:
                direction = "strengthening" if dxy_change > 0 else "weakening"
                nq_impact = "headwind for NQ" if dxy_change > 0 else "tailwind for NQ"
                lines.append(
                    f"Dollar {direction} ({dxy_change:+.2f}%) -- {nq_impact}."
                )

    # ── Bond Yield Assessment ──
    if isinstance(context, dict):
        yield_data = context.get("yields_10y", {})
        if isinstance(yield_data, dict):
            yield_change = yield_data.get("change_bps", 0)
            if yield_change and abs(yield_change) > 3:
                direction = "rising" if yield_change > 0 else "falling"
                nq_impact = "bearish for NQ" if yield_change > 0 else "bullish for NQ"
                lines.append(
                    f"10Y yields {direction} ({yield_change:+.1f} bps) -- {nq_impact}."
                )

    # ── News / Calendar Assessment ──
    news = intel.get("news", {})
    if isinstance(news, dict):
        if news.get("tier1_active"):
            lines.append(
                f"TIER-1 NEWS ACTIVE: {news.get('summary', 'Major event in progress')[:80]}. "
                "Expect 30-100 point swings. Widen stops or stay flat."
            )
        elif news.get("tier2_active"):
            lines.append(
                f"Tier-2 news: {news.get('summary', 'Notable headlines')[:60]}. "
                "Monitor for escalation."
            )

    calendar = intel.get("calendar", {})
    if isinstance(calendar, dict):
        if calendar.get("trade_restricted"):
            next_event = calendar.get("next_event", {})
            event_name = next_event.get("name", "unknown") if isinstance(next_event, dict) else str(next_event)
            lines.append(
                f"TRADE RESTRICTION: High-impact event pending ({event_name}). "
                "Flatten or reduce before the release."
            )
        elif calendar.get("count", 0) > 0:
            next_event = calendar.get("next_event", {})
            if isinstance(next_event, dict) and next_event.get("minutes_until", 999) < 60:
                lines.append(
                    f"Economic event in {next_event.get('minutes_until', '?')} min: "
                    f"{next_event.get('name', 'unknown')}. Tighten stops."
                )

    # ── Trump / Tariff Assessment ──
    trump_warning = intel.get("trump_warning")
    if trump_warning:
        lines.append(f"ALERT: {trump_warning}. Watch for 20-50 point NQ reaction.")

    trump_data = intel.get("trump", {})
    if isinstance(trump_data, dict) and not trump_warning:
        if trump_data.get("tariff_mentioned"):
            score = trump_data.get("score", 0)
            sentiment = "negative" if score < -0.1 else "positive" if score > 0.1 else "neutral"
            lines.append(
                f"Trump tariff mention detected (sentiment: {sentiment}). "
                "Markets may gap on follow-up posts."
            )

    # ── Reddit / Retail Momentum ──
    reddit = intel.get("reddit", {})
    if isinstance(reddit, dict):
        top_tickers = reddit.get("top_tickers", [])
        if top_tickers and any(t.get("ticker") in ("QQQ", "TQQQ", "SQQQ", "NQ") for t in top_tickers if isinstance(t, dict)):
            lines.append(
                "NQ/QQQ trending on Reddit -- retail momentum building. "
                "Watch for crowded-trade reversals if everyone is on one side."
            )

    # ── Crypto Fear & Greed (risk sentiment proxy) ──
    crypto_fg = intel.get("market_context", {})
    if isinstance(crypto_fg, dict):
        fear_greed = crypto_fg.get("crypto_fear_greed", {})
        if isinstance(fear_greed, dict):
            fg_value = fear_greed.get("value", 50)
            if isinstance(fg_value, (int, float)):
                if fg_value < 20:
                    lines.append("Crypto fear/greed at extreme fear -- broad risk-off sentiment.")
                elif fg_value > 80:
                    lines.append("Crypto fear/greed at extreme greed -- complacency risk, watch for reversal.")

    # ── Overnight Gap Context ──
    if isinstance(context, dict):
        gap_pct = context.get("gap_pct", 0)
        if gap_pct and abs(gap_pct) > 0.3:
            direction = "up" if gap_pct > 0 else "down"
            fill_prob = "high (70%+)" if abs(gap_pct) < 0.8 else "moderate (50%)" if abs(gap_pct) < 1.5 else "low (<40%)"
            lines.append(
                f"Overnight gap {direction} {abs(gap_pct):.1f}%. "
                f"Gap fill probability: {fill_prob}."
            )

    # ── Trade OK / Overall ──
    if not intel.get("trade_ok", True):
        reason = intel.get("restriction_reason", "Unknown restriction")
        lines.insert(0, f"*** TRADING RESTRICTED: {reason} ***")

    # ── Fallback ──
    if not lines:
        lines.append(
            "Market conditions nominal. No unusual signals detected. "
            "Trade normal playbook per current regime."
        )

    return " ".join(lines)


# =====================================================================
# 6. Session Debrief Questions
# =====================================================================

DEBRIEF_QUESTIONS: list[str] = [
    # Trade quality assessment
    "Were there trades taken today that should NOT have been taken? "
    "Identify the specific setup, explain why it was suboptimal, and what "
    "filter or rule would have prevented the entry.",

    "Were there clear, high-probability setups that the bot MISSED? "
    "What blocked them -- was it a filter being too tight, a regime restriction, "
    "or a timing issue?",

    # Regime and trend alignment
    "Did the bot trade in the correct direction for the prevailing trend, "
    "or did it take counter-trend trades? If counter-trend, was the setup "
    "strong enough to justify fading the trend?",

    "How well did the bot adapt to regime transitions? When the market shifted "
    "from OPEN_MOMENTUM to MID_MORNING (or to AFTERNOON_CHOP), did the bot's "
    "behavior change appropriately?",

    # Risk management
    "Was position sizing appropriate for today's volatility? If VIX was elevated "
    "or ATR was expanding, did the bot reduce size accordingly?",

    "Were stops placed correctly? Were any trades stopped out by normal noise "
    "(stop too tight) or did any trade run too far against before stopping "
    "(stop too loose)?",

    "Did the bot respect the daily loss limit? If recovery mode was triggered, "
    "was the reduction in size and filter tightening appropriate?",

    # Strategy performance
    "Which strategies performed best today? Which underperformed? Is this "
    "consistent with the current market regime, or is there a strategy that "
    "is no longer working?",

    "Were confluence scores predictive of trade quality? Did high-confluence "
    "trades win more often than low-confluence trades?",

    # Timing and execution
    "Were entries well-timed, or did the bot enter too early (before confirmation) "
    "or too late (after the move was mostly done)?",

    "Were exits optimal? Did the bot leave money on the table by exiting too "
    "early, or did it hold too long and give back profits?",

    # Pattern and behavior analysis
    "Were there any recurring patterns in the losses? Same strategy, same time "
    "of day, same market condition? This suggests a systematic issue.",

    "Did the bot show signs of overtrading? How many evaluations resulted in "
    "no signal vs. signal? A healthy ratio is 5:1 or higher (5 passes for "
    "every 1 trade).",

    # Market condition awareness
    "Did the market intelligence (VIX, news, DXY, yields) correctly predict "
    "today's trading conditions? Were there signals the bot should have "
    "weighted more heavily?",

    # Forward-looking
    "Based on today's session, what ONE thing should be adjusted for tomorrow? "
    "Be specific: a parameter change, a strategy toggle, a regime threshold, "
    "or a behavioral rule.",
]


# =====================================================================
# 7. Algorithmic Trading Edge Concepts
# =====================================================================

ALGO_EDGE_CONCEPTS: str = """\
## Algorithmic Trading Edge Concepts for MNQ

### Mean Reversion vs. Momentum: When to Use Each
- MOMENTUM works best during: opening 60 min, trend days, post-news breakouts,
  high-ATR environments, when EMA9 > EMA21 with separation.
- MEAN REVERSION works best during: lunch hours, range-bound days, low-ATR
  environments, at 2+ standard deviations from VWAP, after ATR exhaustion.
- The regime system handles this automatically: OPEN_MOMENTUM and MID_MORNING
  favor momentum; AFTERNOON_CHOP and CLOSE_CHOP favor mean reversion.
- Key insight: a strategy that works in one regime ACTIVELY LOSES MONEY in the
  wrong regime. This is why regime detection is the most important component.

### Order Flow Imbalance as a Leading Indicator
- CVD (Cumulative Volume Delta) measures net buying vs. selling pressure.
  Rising CVD = buyers in control. Falling CVD = sellers in control.
- Price/CVD divergence is the most reliable leading signal:
  - Price making new highs but CVD making lower highs = buying exhaustion (bearish)
  - Price making new lows but CVD making higher lows = selling exhaustion (bullish)
- Per-bar delta spikes (>2x average) indicate aggressive institutional activity.
  These bars often mark turning points or continuation impulses.
- DOM (Depth of Market) imbalance: when bids outweigh asks by 1.5x+, expect
  upward pressure. Vice versa for ask-heavy DOM. But beware: DOM can be spoofed.

### Microstructure: What the Bid/Ask Spread Tells You
- Normal MNQ spread: 0.25 (1 tick). When the spread widens to 0.50+ (2+ ticks),
  it means market makers are uncertain. This happens before news events, during
  thin volume, and right before large moves.
- Widening spread = REDUCE EXPOSURE. The cost of entry and exit just doubled.
- Tightening spread after a volatile period = stability returning, good time to
  re-enter with normal sizing.
- Quote stuffing / flickering: rapid bid/ask changes (100+ per second) indicate
  HFT activity. This is noise, not signal. Ignore it.

### Gamma Exposure (GEX) and Options Market Maker Hedging
- Options market makers are short gamma when there are many options near the money.
  They must buy when price rises and sell when price falls (reinforcing momentum).
- When GEX is highly positive, market makers dampen volatility (sell rallies, buy
  dips). This creates range-bound, mean-reverting price action.
- When GEX is negative, market makers amplify volatility. This creates trend days
  and outsized moves.
- Key GEX flip levels (published by services like SpotGamma) act as regime
  boundaries. Above the flip = dampened. Below = amplified.
- For MNQ traders: if you know GEX is positive, favor mean reversion. If GEX is
  negative, favor momentum and widen stops.

### TWAP/VWAP Algorithms: How Institutions Trade
- Large institutions use TWAP (Time-Weighted Average Price) and VWAP (Volume-
  Weighted Average Price) algorithms to execute large orders without moving the
  market.
- VWAP algo: buys/sells proportional to volume. This is why price gravitates
  toward VWAP -- there are literally VWAP algo orders clustered around it.
- TWAP algo: buys/sells evenly over time. This creates steady, mechanical flow
  that does not react to price. You see this as persistent, non-impulsive buying
  or selling pressure.
- Implication: VWAP is not just a technical indicator -- it is the actual price
  institutions are targeting. Trading against VWAP means trading against
  institutional order flow.

### Dark Pool Prints: Institutional Intent
- Dark pools execute large block trades off-exchange to avoid slippage.
- Large dark pool prints (>$10M notional) at specific price levels indicate
  institutional positioning. If a large print appears at the day's high,
  institutions are likely distributing (selling). At the low, accumulating (buying).
- Dark pool prints are reported with delay (15-30 min). They are backward-looking
  but still useful for identifying key levels.
- If price revisits a level where a large dark pool print occurred, expect
  support/resistance at that level.

### Retail vs. Institutional Order Flow
- Retail traders: trade market orders, cluster at round numbers, buy calls on
  green days and puts on red days, chase momentum late.
- Institutional traders: use limit orders, execute via algorithms (TWAP/VWAP),
  accumulate over hours/days, fade retail momentum.
- Retail sentiment indicators (options flow, Reddit, put/call ratio) are useful
  as CONTRARIAN signals. When retail is extremely bullish, the smart money is
  likely selling to them.
- The "pain trade": price moves to where it hurts the most participants. If
  retail is massively long calls, the pain trade is down. If puts are crowded,
  the pain trade is up. Markets transfer wealth from the impatient to the patient.

### The Pain Trade Principle
- Markets are NOT random -- they are adversarial. Price seeks the level that
  causes maximum financial pain to the most participants.
- Identify where the crowd is positioned (options open interest, retail sentiment,
  COT reports for futures) and expect price to move against them.
- After a strong trend that everyone chases: expect a sharp reversal that traps
  late entrants.
- After a long range that everyone fades: expect a breakout that traps the mean
  reverters.
- This is why the contrarian council voter exists: to challenge consensus and
  look for trap setups.

### Execution Edge: Why Speed and Precision Matter
- On MNQ, a 1-tick improvement in entry saves $0.50 per trade. Over 500 trades
  per year, that is $250 -- meaningful on a $2,000 account.
- Limit orders save the spread cost (~$0.50 per trade) vs. market orders. Use
  limit orders for planned entries; market orders only for urgent stops.
- Partial fills on limit orders: in MNQ, this is rare (1 contract = small size).
  But if scaling up, be aware that a partial fill leaves you with an undersized
  position and a skewed risk/reward.
- OIF file execution: there is inherent latency in the file-based order system.
  Account for 100-500ms of delay between signal and fill. This means:
  - Do not chase fast-moving breakouts with tight entries
  - Prefer pullback entries where the market comes to your level
  - Factor execution delay into stop placement (add 1-2 tick buffer)
"""


# =====================================================================
# Utility: Get all knowledge as a single string (for prompt injection)
# =====================================================================

def get_full_knowledge_prompt() -> str:
    """
    Return the complete knowledge base as a single string suitable for
    injection into an AI agent's system or user prompt.

    This is a large string (~15k tokens). Use selectively -- inject
    MNQ_TRADING_RULES + the relevant REGIME_PLAYBOOK for most prompts,
    and the full knowledge base only for the session debriefer.
    """
    sections = [
        MNQ_TRADING_RULES,
        RISK_WISDOM,
        ALGO_EDGE_CONCEPTS,
    ]
    return "\n\n".join(sections)


def get_regime_knowledge(regime: str) -> str:
    """
    Return the playbook for a specific regime, with fallback.

    Args:
        regime: One of the 8 regime names (e.g., "OPEN_MOMENTUM")

    Returns:
        The playbook string, or a generic caution message if unknown.
    """
    playbook = REGIME_PLAYBOOKS.get(regime)
    if playbook:
        return playbook.strip()
    return (
        f"Unknown regime '{regime}'. No specific playbook available. "
        "Trade cautiously with reduced size and elevated confluence requirements."
    )


def get_patterns_for_regime(regime: str) -> list[dict]:
    """
    Return patterns from the library that are best suited for a given regime.

    Args:
        regime: The current market regime name.

    Returns:
        List of pattern dicts whose best_regime matches.
    """
    return [p for p in PATTERN_LIBRARY if p["best_regime"] == regime]


def format_patterns_for_prompt(patterns: list[dict]) -> str:
    """
    Format a list of patterns into a readable string for AI agent prompts.
    """
    if not patterns:
        return "No specific patterns highlighted for current regime."

    lines = []
    for p in patterns:
        lines.append(f"### {p['name']}")
        lines.append(f"  Setup: {p['description']}")
        lines.append(f"  Win Rate: {p['win_rate_historical']}")
        lines.append(f"  Stop: {p['stop_placement']}")
        lines.append(f"  Target: {p['target']}")
        lines.append(f"  Filter: {p['key_filter']}")
        lines.append(f"  Invalidation: {p['invalidation']}")
        lines.append("")

    return "\n".join(lines)


# =====================================================================
# 8. Menthor Q Options Flow Expert Knowledge
# =====================================================================

MENTHORQ_EXPERT_KNOWLEDGE: str = """\
## Menthor Q Options Flow — Expert Knowledge Base

Menthor Q is an institutional-grade options analytics platform providing gamma exposure (GEX),
delta exposure (DEX), dealer positioning, and flow data for NQ/MNQ futures.
This knowledge base trains the AI to reason like a Menthor Q expert.

### THE CORE PRINCIPLE: Dealers Always Hedge
Market makers (dealers) ALWAYS delta-hedge their options inventory. This is mechanical,
predictable, and creates reliable price effects. GEX tells you HOW they hedge:
  - Dealers LONG gamma (positive GEX): sell rallies, buy dips → price suppressed
  - Dealers SHORT gamma (negative GEX): buy rallies, sell drops → price amplified

### THE HVL (High Vol Level) — Single Most Important Number
The exact price where dealer gamma flips positive to negative.
  - ABOVE HVL: Positive gamma regime. Dealers stabilize. Use fade/mean-reversion strategies.
  - BELOW HVL: Negative gamma regime. Dealers amplify. Use momentum strategies. NO FADING.
  - HVL itself: Major support/resistance. Test of HVL from either side = high-conviction setup.
  - HVL Reclaim (price crosses above): Dealers flip to buyers. Strong long setup.
  - HVL Loss (price crosses below): Dealers flip to sellers. Strong short setup.

### GEX REGIME RULES
NEGATIVE GEX (Red bars — price below HVL):
  - NEVER fade moves. Dealers amplify every tick in the trend direction.
  - Widen stops 1.5x minimum. Moves are larger than normal ATR suggests.
  - SHORT bias: Every rally is a sell. Target GEX levels below as exit.
  - LONG only: If clear HVL reclaim with CVD confirming + bullish catalyst.
  - Day type: Trending, volatile, fast. Risk of waterfall drops or gamma squeezes.

POSITIVE GEX (Green bars — price above HVL):
  - Fade extremes. Dealers cap moves at GEX resistance / support.
  - Tighten stops 0.75x. Moves are suppressed.
  - Range trading: Enter near GEX support, exit at GEX resistance.
  - Both directions OK. DEX determines the lean (positive DEX = bullish drift).
  - Day type: Range-bound, sticky, predictable. Options sellers thrive.

### DEX (Delta Exposure) — Directional Bias
  - Positive DEX: Structural buying pressure. Dealers mechanically buy dips.
  - Negative DEX: Structural selling pressure. Dealers mechanically sell rallies.
  - Negative GEX + Negative DEX = WORST for longs. Dealers amplify AND sell strength.
  - Negative GEX + Positive DEX = Volatile but bullish potential (squeeze setup).
  - Positive GEX + Positive DEX = Most stable bullish environment. Dip-buying works.
  - Positive GEX + Negative DEX = Stable but bearish lean. Fade rallies.

### GEX LEVELS 1-10 — Intraday S/R
Strikes with highest gamma cluster. Level 1 = strongest.
  - Positive GEX: Levels act as real S/R. Fade into them. Bounce from them.
  - Negative GEX: Breaking Level 1 triggers cascading dealer re-hedging.
    Level 1 break down → dealers forced to sell more → fast drop to Level 2.
  - 0DTE levels: Most powerful in last 2 hours. 0DTE gamma is exponential near expiry.
  - 0DTE Put Wall break in afternoon = high-conviction SHORT trigger.
  - 0DTE Call Wall break in morning = high-conviction LONG (gamma squeeze).

### VANNA FLOWS
  - Bearish Vanna: Rising VIX + large short put OI. Dealers must sell into drops.
    Creates self-reinforcing cascade. Do NOT long when Vanna = BEARISH.
  - Bullish Vanna: Falling VIX + OTM put decay. Dealers buy back hedges.
    Creates "vanna bid" — market grinds up mechanically. Buy dips aggressively.
  - Post-VIX-spike vanna unwind (VIX falling fast) = fastest rallies in the market.

### CHARM FLOWS
  - Post-OPEX (week after monthly options expiration): Vanna/charm stabilizers expire.
    Price action becomes MORE volatile and unpredictable. Widen stops.
  - OTM puts decaying → "charm bid" → structural buying. Bullish drift post-OPEX.
  - Monthly OPEX = 3rd Friday. Quarterly OPEX (Mar/Jun/Sep/Dec) = most powerful.

### CTA MODEL
  - CTAs BUYING: Systematic funds adding longs. Pullbacks are mechanical buys.
  - CTAs SELLING: Systematic funds adding shorts. Rallies are mechanical sells.
  - CTAs MAX SHORT (extreme) + Negative GEX + Catalyst = GAMMA SQUEEZE setup.
    This is the setup that produces 3-8% ramps in 2-4 hours. DO NOT short these.
  - CTAs MAX LONG + Negative GEX = Mean reversion warning. Any negative catalyst = sharp selloff.

### PRE-TRADE FILTER DECISIONS WITH MENTHOR Q
LONG signal in NEGATIVE GEX + price BELOW HVL:
  → Default: CAUTION or SIT_OUT. Mechanical headwind from dealers.
  → SIT_OUT if: CVD negative, DEX negative, Vanna bearish (triple headwind).
  → CLEAR only if: HVL reclaimed + CVD positive + strong catalyst.

SHORT signal in NEGATIVE GEX + price BELOW HVL:
  → Default: CLEAR. Mechanical tailwind from dealers.
  → Apply 1.5x stop multiplier. Wider stop = fewer whipsaws.
  → SIT_OUT only if: imminent news event or daily loss limit hit.

Any signal in POSITIVE GEX:
  → Both directions OK. Use GEX levels as entries and exits.
  → Tighten stops 25%. Moves are suppressed.
  → Fade into GEX levels. Do not chase breakouts.

### COUNCIL VOTING GUIDANCE
When voting on session bias with MQ data:
  Negative GEX + Below HVL + Negative DEX + Vanna Bearish → BEARISH vote, confidence 80-90
  Negative GEX + Below HVL + Positive DEX + CTA Buying → BULLISH vote (squeeze), confidence 60
  Positive GEX + Above HVL + Positive DEX → BULLISH vote, confidence 70-80
  Positive GEX + Above HVL + Negative DEX → BEARISH lean, confidence 55-65
  Price AT HVL (within 0.5x ATR) → NEUTRAL, wait for confirmation

### DEBRIEF EVALUATION CHECKLIST
For each trade in the session:
  1. Was direction aligned with GEX regime? (LONG in negative GEX = misaligned)
  2. Was stop sized for the regime? (Negative GEX needs 1.5x wider stops)
  3. Was entry near a GEX level? (Best entries are at/near GEX levels)
  4. Did vanna/charm context match? (Vanna bearish + long = mechanical headwind)
  5. Did CTA model support direction? (CTA selling + long = institutional headwind)
Most common MQ mistakes: regime-blind LONGs below HVL, tight stops in negative gamma.
"""


def get_menthorq_knowledge() -> str:
    """Return the full Menthor Q expert knowledge block for prompt injection."""
    return MENTHORQ_EXPERT_KNOWLEDGE


def get_menthorq_pretrade_rules(direction: str, gex_regime: str, above_hvl: bool) -> str:
    """
    Return targeted Menthor Q rules for a specific trade scenario.
    Used by the pre-trade filter to inject only the relevant rules.
    """
    lines = ["## Menthor Q Pre-Trade Context"]

    if gex_regime == "NEGATIVE":
        if not above_hvl:
            if direction == "LONG":
                lines.append(
                    "WARNING: LONG signal in negative GEX regime with price BELOW HVL. "
                    "Dealers are short gamma AND below the regime flip level. "
                    "Every tick down triggers more dealer selling. "
                    "This LONG faces double mechanical headwind. "
                    "Lean toward CAUTION or SIT_OUT unless very strong catalyst exists."
                )
            else:  # SHORT
                lines.append(
                    "CONFIRM: SHORT signal in negative GEX with price below HVL. "
                    "Dealers short gamma amplify every move down. "
                    "This is the highest-conviction MQ short setup. "
                    "Apply 1.5x stop width. Target next GEX level below. CLEAR."
                )
        else:  # above HVL but negative GEX
            lines.append(
                f"NOTE: Price above HVL but GEX still negative. Transitional zone. "
                f"A {direction} here is valid but volatile — negative GEX amplifies both directions. "
                f"Use wider stops."
            )
    elif gex_regime == "POSITIVE":
        lines.append(
            f"POSITIVE GEX regime: Dealers suppress moves. "
            f"{'Fade into resistance, tight stop.' if direction == 'SHORT' else 'Buy near support, tight stop.'} "
            f"GEX levels are reliable S/R today. Do not chase breakouts."
        )
    else:
        lines.append("MQ regime unknown — no directional restriction applied.")

    return "\n".join(lines)
