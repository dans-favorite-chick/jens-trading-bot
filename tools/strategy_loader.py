"""
Phoenix Bot — Strategy Knowledge Bulk Loader

Production-grade strategy library covering futures (NQ/MNQ, ES, CL, GC)
and equities (NASDAQ, large-cap). Each strategy has structured metadata
for regime, ATR, time-of-day, and asset class so AI agents can query:
"What strategies work in trending NQ with low ATR at market open?"

Usage:
    python -m tools.strategy_loader              # Load all strategies
    python -m tools.strategy_loader --list       # Show loaded strategies
    python -m tools.strategy_loader --query "trending NQ low ATR"
    python -m tools.strategy_loader --stats      # Category/regime breakdown
    python -m tools.strategy_loader --reset      # Clear and reload all
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("StrategyLoader")


@dataclass
class Strategy:
    name: str
    category: str       # breakout, mean_reversion, momentum, scalp, vwap, pattern, smc, stat_arb, event, swing
    description: str
    entry_logic: str
    exit_logic: str
    regimes: list[str]          # trending_up, trending_down, range, volatile, low_vol, any
    time_windows: list[str]     # pre_market, open_drive, morning, midday, afternoon, close, overnight, any
    asset_class: list[str]      # nq, es, cl, gc, nasdaq_stocks, large_cap, any
    atr_preference: str         # low, medium, high, any
    volume_preference: str      # high, normal, low, any
    win_rate_estimate: str
    risk_reward: str
    key_indicators: list[str]
    strengths: str
    weaknesses: str


def _build_document(s: Strategy) -> str:
    """Build searchable text document for ChromaDB embedding."""
    return (
        f"Strategy: {s.name}\n"
        f"Category: {s.category}\n"
        f"Description: {s.description}\n"
        f"Entry Logic: {s.entry_logic}\n"
        f"Exit Logic: {s.exit_logic}\n"
        f"Best Regimes: {', '.join(s.regimes)}\n"
        f"Time Windows: {', '.join(s.time_windows)}\n"
        f"Asset Class: {', '.join(s.asset_class)}\n"
        f"ATR Preference: {s.atr_preference}\n"
        f"Volume: {s.volume_preference}\n"
        f"Historical Win Rate: {s.win_rate_estimate}\n"
        f"Risk:Reward: {s.risk_reward}\n"
        f"Key Indicators: {', '.join(s.key_indicators)}\n"
        f"Strengths: {s.strengths}\n"
        f"Weaknesses: {s.weaknesses}"
    )


def _build_metadata(s: Strategy) -> dict:
    """Build ChromaDB metadata dict (all values must be str/int/float)."""
    return {
        "category": s.category,
        "regimes": ",".join(s.regimes),
        "time_windows": ",".join(s.time_windows),
        "asset_class": ",".join(s.asset_class),
        "atr_preference": s.atr_preference,
        "volume_preference": s.volume_preference,
        "risk_reward": s.risk_reward,
    }


# ═══════════════════════════════════════════════════════════════════════
#  STRATEGY CATALOG
# ═══════════════════════════════════════════════════════════════════════

STRATEGIES: list[Strategy] = [

    # ── BREAKOUT STRATEGIES ────────────────────────────────────────────

    Strategy(
        name="Opening Range Breakout (ORB)",
        category="breakout",
        description="Trade the break of the first 15 or 30-minute range after market open. One of the most well-documented intraday strategies with strong statistical edges on index futures.",
        entry_logic="Mark the high and low of the first 15 or 30 minutes (IB). Enter long on close above IB high with volume > 1.5x average. Enter short on close below IB low. Require ATR confirmation — narrow IB (<0.5x daily ATR) has the highest break probability.",
        exit_logic="Target: 1x IB width for conservative, 2x for aggressive. Stop: opposite side of IB or midpoint. Trail stop to breakeven after 1x extension. Time stop: exit if no follow-through within 30 minutes.",
        regimes=["trending_up", "trending_down", "volatile"],
        time_windows=["open_drive", "morning"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="55-65%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["ib_range", "atr", "volume", "vwap"],
        strengths="High win rate on narrow IB days. Clear entry/exit rules. Works across all major indices.",
        weaknesses="False breakouts on range days. Wide IB days have poor extensions. Requires quick execution.",
    ),

    Strategy(
        name="Initial Balance Extension",
        category="breakout",
        description="After the IB breaks, trade the measured move equal to IB width. Based on market profile auction theory — narrow IB implies high directional conviction.",
        entry_logic="Wait for IB break (first 30-60 min range). Enter on first pullback after the break holds. Confirm with delta and TF alignment. Narrow IB (<0.5x ATR) has 98.7% break probability.",
        exit_logic="Target: 1x IB extension from break point. Stop: inside IB (midpoint or opposite side). Time stop: 45 minutes.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning"],
        asset_class=["nq", "es", "any"],
        atr_preference="low",
        volume_preference="normal",
        win_rate_estimate="60-70%",
        risk_reward="1:1.5",
        key_indicators=["ib_range", "atr", "volume_profile"],
        strengths="Very high probability on narrow IB days. Based on solid auction theory.",
        weaknesses="Wide IB days have small extensions. Requires patience to wait for the break.",
    ),

    Strategy(
        name="Breakout Pullback",
        category="breakout",
        description="After a clean structure break (swing high/low), enter on the first pullback to the breakout level. The retest of broken support/resistance is a high-probability entry.",
        entry_logic="Identify a clean break of structure (previous swing high or low). Wait for price to pull back to the broken level. Enter on rejection at the level (hammer/shooting star). Volume should decline on pullback, then increase on bounce.",
        exit_logic="Stop: below/above the pullback low/high. Target: measured move equal to the impulse leg. Trail after 1R profit.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning", "afternoon", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["swing_highs_lows", "volume", "ema9", "ema21"],
        strengths="Classic technical setup. Works across all markets and timeframes. Clear invalidation.",
        weaknesses="Not all breakouts pull back. Can miss fast moves. Requires patience.",
    ),

    Strategy(
        name="Asian Range Breakout",
        category="breakout",
        description="Trade the break of the Asian session range (8 PM - midnight EST) during London or NY open. The Asian range defines overnight value, and the London/NY break determines daily direction.",
        entry_logic="Mark Asian session high/low (8 PM - midnight EST). Enter on first close outside the range during London (2-5 AM EST) or NY open (8:30-9:30 AM EST). Confirm with delta direction.",
        exit_logic="Target: 1.5x Asian range width. Stop: opposite side of range or midpoint. Time stop: 2 hours.",
        regimes=["trending_up", "trending_down", "any"],
        time_windows=["pre_market", "open_drive"],
        asset_class=["nq", "es"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="50-55%",
        risk_reward="1:1.5",
        key_indicators=["session_range", "volume", "delta"],
        strengths="Defines early directional bias. Narrow Asian ranges = strong directional moves.",
        weaknesses="Wide Asian ranges reduce edge. Pre-market gaps can invalidate.",
    ),

    Strategy(
        name="Volatility Squeeze Breakout",
        category="breakout",
        description="Enter when Bollinger Bands squeeze inside Keltner Channels, then expand. The squeeze indicates compression before an explosive move. Works on all timeframes.",
        entry_logic="Identify BB squeeze (BB inside KC). Wait for BB to expand outside KC. Enter in direction of the first bar that closes outside the squeeze. Momentum (MACD or RSI) confirms direction.",
        exit_logic="Target: 2x the squeeze range. Stop: opposite BB. Trail after 1.5x squeeze range.",
        regimes=["range", "low_vol"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="low",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["bollinger_bands", "keltner_channels", "macd", "atr"],
        strengths="Catches major moves after consolidation. Works across all assets. Clear visual setup.",
        weaknesses="Not all squeezes break. Direction can be wrong. Needs momentum confirmation.",
    ),

    # ── MEAN REVERSION STRATEGIES ──────────────────────────────────────

    Strategy(
        name="VWAP Mean Reversion",
        category="mean_reversion",
        description="Fade extended moves away from VWAP when price is 1.5+ ATR from anchor. VWAP is the institutional fair price — extreme deviations tend to revert.",
        entry_logic="Price must be 1.5+ ATR from VWAP. Enter on reversal candle (hammer/shooting star) with declining momentum. CVD should diverge from price. DOM should show absorption at the extreme.",
        exit_logic="Target: VWAP or 50% of the distance to VWAP. Stop: beyond the extreme (high/low of the rejection candle). Time stop: 15 minutes.",
        regimes=["range", "low_vol"],
        time_windows=["midday", "afternoon"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="60-65%",
        risk_reward="1:1 to 1:1.5",
        key_indicators=["vwap", "atr", "cvd", "dom_imbalance"],
        strengths="High win rate in range-bound markets. Clear target (VWAP). Institutional logic.",
        weaknesses="Terrible on trending days — fighting the trend. Requires range-bound regime identification.",
    ),

    Strategy(
        name="VWAP Standard Deviation Bands",
        category="mean_reversion",
        description="Trade bounces off VWAP +/- 1 and 2 standard deviation bands. In ranging markets, price oscillates between bands. The 2SD band marks extreme extension.",
        entry_logic="Long at VWAP -1SD with reversal confirmation. Short at VWAP +1SD. Aggressive: fade touches of 2SD bands. Require declining delta momentum on the approach.",
        exit_logic="Target: VWAP (from 1SD) or VWAP+/-1SD (from 2SD). Stop: 0.5 ATR beyond the band. Time stop: 20 minutes.",
        regimes=["range", "low_vol"],
        time_windows=["midday", "afternoon"],
        asset_class=["nq", "es", "nasdaq_stocks", "any"],
        atr_preference="low",
        volume_preference="normal",
        win_rate_estimate="60-65%",
        risk_reward="1:1.5",
        key_indicators=["vwap", "vwap_bands", "atr", "delta"],
        strengths="Statistical edge from VWAP reversion. Multiple band levels for scaling. Clean R:R at 2SD.",
        weaknesses="Bands are dynamic — levels shift. Trending days blow through bands. Needs regime filter.",
    ),

    Strategy(
        name="Range Fade",
        category="mean_reversion",
        description="In established ranges, fade moves to range extremes. Classic mean reversion at support/resistance with tight risk.",
        entry_logic="Identify a clear range (at least 3 touches of support and resistance). Enter long at range low with reversal confirmation. Enter short at range high. Volume should decline at extremes.",
        exit_logic="Target: opposite side of range or midpoint. Stop: 0.5 ATR beyond range extreme. Partial profit at midpoint.",
        regimes=["range"],
        time_windows=["midday", "afternoon", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="low",
        volume_preference="low",
        win_rate_estimate="60-65%",
        risk_reward="1:2 to 1:3",
        key_indicators=["support_resistance", "volume", "rsi", "atr"],
        strengths="High win rate in confirmed ranges. Great R:R from extreme to extreme. Clear invalidation.",
        weaknesses="Ranges eventually break. Catching falling knives if range breaks. Needs range confirmation.",
    ),

    Strategy(
        name="Bollinger Band Reversal",
        category="mean_reversion",
        description="Enter reversal trades when price closes outside Bollinger Bands (2SD) and shows rejection. Statistically, price stays within 2SD bands 95% of the time.",
        entry_logic="Price closes outside BB 2SD. Next candle shows rejection (long wick back inside). Enter on the reversal candle close. RSI should be oversold (<30) or overbought (>70).",
        exit_logic="Target: BB midline (20-period MA). Stop: beyond the extreme candle. Time stop: 30 minutes.",
        regimes=["range", "low_vol"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["bollinger_bands", "rsi", "volume"],
        strengths="Statistical backing (95% containment). Works on all timeframes. Clear setup.",
        weaknesses="Trending markets produce serial touches of outer band. Needs regime filter.",
    ),

    Strategy(
        name="RSI Divergence Entry",
        category="mean_reversion",
        description="Enter when RSI shows bullish or bearish divergence at key support/resistance levels. Divergence signals momentum exhaustion before price reverses.",
        entry_logic="Price makes new low but RSI makes higher low (bullish div) or price makes new high but RSI makes lower high (bearish div). Must occur at a key level (S/R, VWAP, or moving average). Enter on the next candle that confirms the reversal direction.",
        exit_logic="Target: previous swing high/low. Stop: below/above the divergence low/high. Trail after 1R.",
        regimes=["range", "trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["rsi", "swing_highs_lows", "support_resistance"],
        strengths="Catches exhaustion moves. Works across all assets and timeframes. High R:R.",
        weaknesses="Divergence can persist in strong trends. Timing is tricky. Multiple divergences before reversal.",
    ),

    Strategy(
        name="Volume Profile POC Reversion",
        category="mean_reversion",
        description="Price tends to revert to the Point of Control (highest volume node). Trade back toward POC when price is extended and shows rejection.",
        entry_logic="Price is 1+ ATR from the session or prior day POC. Enter on reversal candle pointing toward POC. Volume should decline on the extension and pick up on the reversal.",
        exit_logic="Target: POC level. Stop: beyond the extension extreme. Time stop: 30 minutes.",
        regimes=["range"],
        time_windows=["midday", "afternoon"],
        asset_class=["nq", "es", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["volume_profile", "poc", "atr"],
        strengths="Based on market microstructure theory. POC is a proven magnet level. Clear target.",
        weaknesses="POC is dynamic intraday. Trending days shift POC away from entries. Needs volume profile data.",
    ),

    # ── MOMENTUM / TREND STRATEGIES ────────────────────────────────────

    Strategy(
        name="EMA 9/21 Crossover Momentum",
        category="momentum",
        description="Enter on EMA9 crossing EMA21 with multi-timeframe alignment. Classic trend-following entry with moving average confirmation across timeframes.",
        entry_logic="EMA9 crosses above EMA21 for longs (below for shorts). Require 3+ timeframe alignment (1m, 5m, 15m, 60m all bullish/bearish). Price must be above/below VWAP. ATR should be expanding.",
        exit_logic="Trail stop at EMA21. Target: 2-3x ATR from entry. Exit on EMA9 crossing back below EMA21.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="45-55%",
        risk_reward="1:2 to 1:3",
        key_indicators=["ema9", "ema21", "vwap", "atr", "tf_alignment"],
        strengths="Catches sustained trends. Multi-TF confirmation reduces false signals. Objective rules.",
        weaknesses="Late entries on fast moves. Choppy markets produce whipsaws. Lower win rate, compensated by R:R.",
    ),

    Strategy(
        name="Micro Pullback (Trend Continuation)",
        category="momentum",
        description="In strong trends, enter on 2-3 bar pullbacks that hold above EMA9. The micro pullback is the highest-probability trend continuation pattern.",
        entry_logic="Active uptrend (3/4 TF bullish, price > VWAP > EMA21). Wait for 2-3 bar pullback to EMA9 or 50% of last impulse leg. Enter on first bar that closes back in trend direction. CVD should stay positive on pullback.",
        exit_logic="Stop: below EMA9 or pullback low. Target: new swing high (measured move). Trail at EMA9.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="60-65%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["ema9", "ema21", "vwap", "cvd", "tf_alignment"],
        strengths="High win rate in established trends. Tight stops. Frequent setups in trending markets.",
        weaknesses="Trend must already be established. Late in the trend, pullbacks become reversals.",
    ),

    Strategy(
        name="Momentum Ignition",
        category="momentum",
        description="Enter on sudden volume spike with price acceleration (3+ ATR bar). Institutional momentum ignition creates follow-through. Ride the surge with trailing stop.",
        entry_logic="Detect a bar with range > 3x ATR and volume > 3x average. Enter in direction of the bar on the next bar's open. Must align with TF bias. CVD must confirm (positive for longs).",
        exit_logic="Trail stop at 1 ATR. Target: 2-3x the ignition bar's range. Time stop: 10 minutes — if no follow-through, exit.",
        regimes=["volatile", "trending_up", "trending_down"],
        time_windows=["open_drive", "morning", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "any"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="50-55%",
        risk_reward="1:2 to 1:3",
        key_indicators=["atr", "volume", "cvd", "delta"],
        strengths="Catches explosive moves. High R:R. Clear trigger signal.",
        weaknesses="Can be fake institutional probe (iceberg). Stop-hunts look like ignitions. Requires fast execution.",
    ),

    Strategy(
        name="Session Open Drive",
        category="momentum",
        description="Trade the first 5-10 minutes directional move with heavy volume at session open. The open drive sets the tone for the morning session.",
        entry_logic="First 5 minutes show strong directional movement (all candles same direction). Volume is 2x+ average. Enter with the drive direction. Stop at VWAP or session open price.",
        exit_logic="Target: 1.5x ATR from open. Trail at 5-min EMA. Time stop: 30 minutes.",
        regimes=["trending_up", "trending_down", "volatile"],
        time_windows=["open_drive"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["volume", "vwap", "ema9", "atr"],
        strengths="Captures the strongest move of the day. Clear institutional participation. Quick profits.",
        weaknesses="False opens (gap and reverse). Requires instant decision-making. High slippage risk.",
    ),

    Strategy(
        name="Measured Move Continuation",
        category="momentum",
        description="After an AB impulse leg and BC correction, enter for the CD leg equal to AB. Fibonacci extension confirmation at 100% and 161.8%.",
        entry_logic="Identify AB impulse leg. Wait for BC retracement (38-62% of AB). Enter at C when price shows reversal back in trend direction. Fibonacci 100% extension of AB from C is the target.",
        exit_logic="Target: 100% extension of AB from C. Aggressive target: 161.8% extension. Stop: below/above C.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="50-55%",
        risk_reward="1:2",
        key_indicators=["fibonacci", "swing_highs_lows", "trend_lines"],
        strengths="Clear geometric target. Based on natural market structure. Works on all timeframes.",
        weaknesses="Pattern identification is subjective. Not all AB=CD patterns complete. Requires patience.",
    ),

    # ── VWAP STRATEGIES ────────────────────────────────────────────────

    Strategy(
        name="VWAP Bounce (Trend)",
        category="vwap",
        description="In trending markets, buy the first pullback to VWAP. VWAP acts as dynamic support in uptrends and resistance in downtrends. Institutions defend VWAP.",
        entry_logic="Price is trending (above VWAP with rising EMA9). Wait for first pullback to touch VWAP. Enter on bounce candle (hammer at VWAP for longs). CVD should stay positive. DOM should show bid absorption at VWAP.",
        exit_logic="Stop: 1 ATR below VWAP. Target: new high or 1.5x risk. Trail at EMA9.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning", "midday"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="60-65%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["vwap", "ema9", "cvd", "dom_imbalance"],
        strengths="Institutional logic — VWAP is THE institutional price. High win rate on first touch. Clear level.",
        weaknesses="Second/third touches of VWAP have lower probability. Fails on range days.",
    ),

    Strategy(
        name="VWAP Cross Momentum",
        category="vwap",
        description="Enter when price crosses VWAP with volume confirmation. The cross signals a shift in institutional fair value assessment.",
        entry_logic="Price crosses above VWAP (long) or below (short) on a bar with 1.5x+ average volume. EMA9 must be trending in the cross direction. Wait for the cross bar to close, then enter on the next bar.",
        exit_logic="Stop: opposite side of VWAP (the cross must hold). Target: 1.5-2x ATR. Trail at VWAP.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="50-55%",
        risk_reward="1:1.5",
        key_indicators=["vwap", "volume", "ema9"],
        strengths="Clear signal. Volume confirmation reduces false crosses. Works on all indices.",
        weaknesses="Choppy around VWAP produces whipsaws. Multiple crosses in range days.",
    ),

    # ── SCALPING STRATEGIES ────────────────────────────────────────────

    Strategy(
        name="Delta Divergence Reversal",
        category="scalp",
        description="Enter reversal when price makes new extreme but cumulative delta diverges. Institutions are distributing/accumulating against the visible price trend.",
        entry_logic="Price makes new session high but CVD is flat or declining (bearish div) or price makes new low but CVD is rising (bullish div). Enter on the first reversal candle after divergence is confirmed. DOM should show absorption at the extreme.",
        exit_logic="Target: 4-8 ticks (scalp). Stop: beyond the divergence extreme (2-3 ticks). Time stop: 5 minutes.",
        regimes=["range", "volatile"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["cvd", "delta", "dom_imbalance", "volume"],
        strengths="Order flow edge. Catches exhaustion precisely. Tight stops.",
        weaknesses="Requires real-time delta/CVD. Divergence can persist. High frequency, high transaction costs.",
    ),

    Strategy(
        name="DOM Absorption Scalp",
        category="scalp",
        description="Enter when large resting orders absorb aggressive flow without price moving. Institutional accumulation/distribution detected via DOM depth.",
        entry_logic="DOM shows large resting orders (bid_heavy > 0.7 or ask_heavy > 0.7 imbalance). Aggressive flow is being absorbed (high volume but no price movement). Enter in direction of the resting orders when absorption completes (volume drops).",
        exit_logic="Target: 4-8 ticks. Stop: 2-3 ticks beyond absorption level. Time stop: 3 minutes.",
        regimes=["range", "any"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="low",
        volume_preference="high",
        win_rate_estimate="55-65%",
        risk_reward="1:1.5",
        key_indicators=["dom_imbalance", "volume", "delta"],
        strengths="Pure order flow edge. Very tight stops. Frequent setups.",
        weaknesses="Requires Level 2 data. Iceberg orders can fool you. Very short hold time.",
    ),

    Strategy(
        name="Tick Scalp (Spread Capture)",
        category="scalp",
        description="Ultra-short-term scalp capturing 2-4 ticks on mean reversion within the bid-ask spread. Works in liquid markets with tight spreads.",
        entry_logic="Identify micro-range (4-6 tick range). Enter at range extremes when DOM shows heavy resting orders. Require imbalance > 0.65. Enter toward the heavy side.",
        exit_logic="Target: 2-4 ticks. Stop: 2 ticks. Time stop: 60 seconds. Exit immediately if imbalance reverses.",
        regimes=["range", "low_vol"],
        time_windows=["midday"],
        asset_class=["nq", "es"],
        atr_preference="low",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:1 to 1:2",
        key_indicators=["dom_imbalance", "spread", "tick_count"],
        strengths="Very high frequency. Small but consistent profits. Low drawdown.",
        weaknesses="Transaction costs eat into profits. Needs very low latency. Not viable on all contracts.",
    ),

    # ── PATTERN STRATEGIES ─────────────────────────────────────────────

    Strategy(
        name="Double Bottom / Double Top",
        category="pattern",
        description="Classic reversal pattern at key levels. Price tests support/resistance twice and reverses. The second test with divergent momentum is the entry trigger.",
        entry_logic="Price tests a key level twice. Second test shows momentum divergence (RSI higher low for double bottom, lower high for double top). Enter on the reversal candle after the second test. Volume should decline on second test.",
        exit_logic="Stop: below the double bottom or above the double top. Target: measured move (neckline to pattern low/high, projected from neckline break).",
        regimes=["range", "trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["support_resistance", "rsi", "volume", "swing_highs_lows"],
        strengths="Well-known pattern with statistical edge. Clear measured move target. Works on all timeframes.",
        weaknesses="Pattern can fail (triple bottom). Requires patience. Not precise on entry timing.",
    ),

    Strategy(
        name="Head and Shoulders",
        category="pattern",
        description="Classic reversal pattern with three peaks — middle peak highest. Neckline break confirms the reversal. One of the most reliable chart patterns.",
        entry_logic="Identify H&S pattern (left shoulder, head, right shoulder). Enter on neckline break with volume confirmation. Right shoulder should show declining volume. RSI divergence at head vs right shoulder confirms.",
        exit_logic="Target: measured move (head to neckline, projected from break point). Stop: above right shoulder.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2 to 1:3",
        key_indicators=["swing_highs_lows", "volume", "rsi", "neckline"],
        strengths="High R:R. Well-documented statistical edge. Clear measured move target.",
        weaknesses="Patterns can fail. Identification is somewhat subjective. Late entries if waiting for neckline break.",
    ),

    Strategy(
        name="Bull/Bear Flag Continuation",
        category="pattern",
        description="After a strong impulse move, price consolidates in a flag (parallel channel). The breakout continues the prior trend. One of the most reliable continuation patterns.",
        entry_logic="Strong impulse move (flagpole) followed by consolidation in a parallel channel (flag). Enter on breakout of the flag in the direction of the pole. Volume should contract during flag and expand on breakout.",
        exit_logic="Target: measured move equal to the flagpole length. Stop: opposite side of the flag channel.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="55-65%",
        risk_reward="1:2 to 1:3",
        key_indicators=["trend_lines", "volume", "flagpole_length"],
        strengths="High probability continuation pattern. Clear measured move target. Tight stop (narrow flag).",
        weaknesses="Flags can morph into reversals. Needs clean flagpole. Subjective pattern boundaries.",
    ),

    Strategy(
        name="Cup and Handle",
        category="pattern",
        description="Rounded bottom (cup) followed by small consolidation (handle). Breakout above handle = strong continuation. Popular on daily charts but works intraday on indices.",
        entry_logic="Identify rounded cup formation over 15-30 bars. Handle forms as small pullback (less than 50% of cup depth). Enter on breakout above the handle with volume surge.",
        exit_logic="Target: depth of the cup projected from the breakout point. Stop: below the handle low.",
        regimes=["trending_up"],
        time_windows=["morning", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:2 to 1:3",
        key_indicators=["volume", "pattern_depth", "handle_retracement"],
        strengths="Very high R:R. Strong continuation signal. Works well on momentum stocks.",
        weaknesses="Takes time to form. Handle can break down. Requires volume confirmation.",
    ),

    # ── SMC / ICT STRATEGIES ───────────────────────────────────────────

    Strategy(
        name="Order Block Retest",
        category="smc",
        description="ICT/SMC: Enter on price returning to a bullish or bearish order block. Order blocks represent institutional accumulation/distribution zones.",
        entry_logic="Identify an order block (last opposing candle before displacement). Wait for price to return to the OB zone. Enter on rejection at the OB (wick rejection, not body close through). Confirm with structure (must be in discount for bullish OB, premium for bearish).",
        exit_logic="Stop: beyond the OB. Target: opposing liquidity pool or imbalance zone. Minimum 1:2 RR.",
        regimes=["trending_up", "trending_down", "range"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2 to 1:3",
        key_indicators=["order_blocks", "displacement", "premium_discount"],
        strengths="Institutional logic. High R:R. Clear stop placement. Works across all markets.",
        weaknesses="Not all OBs hold. Requires structure context. Multiple OBs can overlap.",
    ),

    Strategy(
        name="Liquidity Sweep Reversal",
        category="smc",
        description="ICT/SMC: Enter after price sweeps a swing high/low (taking stops) and reverses back inside the range. Stop hunts by smart money create high-probability reversals.",
        entry_logic="Price sweeps above a swing high (or below swing low) by 1-5 ticks. Immediately reverses and closes back inside the range. Enter on the close-back candle. Volume should spike on the sweep. DOM should show absorption on the reversal.",
        exit_logic="Stop: beyond the sweep extreme. Target: opposing swing or next liquidity pool. Minimum 1:3 RR.",
        regimes=["range", "volatile"],
        time_windows=["open_drive", "morning", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:3",
        key_indicators=["swing_highs_lows", "volume", "dom_imbalance", "delta"],
        strengths="Very high R:R. Based on market maker behavior. Clear invalidation (sweep doesn't reverse).",
        weaknesses="Requires real-time detection. Not all sweeps reverse. Can be part of a larger move.",
    ),

    Strategy(
        name="FVG Fill Entry",
        category="smc",
        description="ICT/SMC: Enter when price returns to fill a Fair Value Gap. FVGs are imbalances that price tends to revisit. High-probability entry with institutional logic.",
        entry_logic="Identify a Fair Value Gap (3-candle imbalance). Wait for price to retrace into the FVG zone. Enter on the first candle that enters the FVG from the correct side (long in bullish FVG, short in bearish). Confirm the FVG is in the correct premium/discount zone.",
        exit_logic="Stop: beyond the FVG zone. Target: next liquidity pool or opposing FVG. Minimum 1:2 RR.",
        regimes=["trending_up", "trending_down", "any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["fvg", "premium_discount", "displacement"],
        strengths="Institutional logic. High-probability fill. Clear entry zone. Works on all timeframes.",
        weaknesses="Not all FVGs fill. Old FVGs lose relevance. Multiple FVGs can overlap.",
    ),

    Strategy(
        name="Change of Character (CHoCH)",
        category="smc",
        description="ICT/SMC: Enter on the first sign of trend reversal — when price breaks the most recent swing in the opposite direction. CHoCH precedes BOS and signals early reversals.",
        entry_logic="In a downtrend, price breaks above the most recent lower high (CHoCH). Wait for price to pull back to the CHoCH level or the nearest OB. Enter on the retest. Confirm with delta shift.",
        exit_logic="Stop: below the CHoCH low. Target: next significant high or FVG fill. Trail at structure.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="50-55%",
        risk_reward="1:2 to 1:3",
        key_indicators=["swing_highs_lows", "order_blocks", "delta", "displacement"],
        strengths="Early reversal detection. High R:R. Based on market structure shift.",
        weaknesses="Not all CHoCH lead to reversals. Can be a correction within trend. Lower win rate.",
    ),

    # ── EVENT-DRIVEN STRATEGIES ────────────────────────────────────────

    Strategy(
        name="Gap and Go",
        category="event",
        description="Trade in the direction of a significant gap when catalyzed by news. Gaps above 0.3% with pre-market volume show institutional conviction.",
        entry_logic="Gap > 0.3% at open with identifiable catalyst (earnings, macro, sector news). Pre-market volume > 1.5x average. Enter on first pullback in gap direction (5-min bar close). VWAP should hold as support/resistance.",
        exit_logic="Target: gap extension (1.5x gap size). Stop: gap fill (return to prior close). Time stop: 45 minutes.",
        regimes=["trending_up", "trending_down", "volatile"],
        time_windows=["open_drive", "morning"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["gap_size", "volume", "vwap", "pre_market_range"],
        strengths="Institutional conviction behind the move. Clear catalyst. High volume = real moves.",
        weaknesses="Not all gaps follow through. Earnings gaps can reverse. Requires pre-market analysis.",
    ),

    Strategy(
        name="Gap Fade",
        category="event",
        description="Fade gaps > 0.5% that show exhaustion in the first 15 minutes. Most gaps fill partially — statistical edge in fading overextended gaps without strong catalysts.",
        entry_logic="Gap > 0.5% at open. No strong catalyst (or old news already priced in). First 15 minutes show reversal pattern (shooting star for gap up, hammer for gap down). Volume declining. Enter on reversal confirmation.",
        exit_logic="Target: gap fill (prior close) or 50% fill. Stop: beyond the opening range extreme. Time stop: 60 minutes.",
        regimes=["range", "volatile"],
        time_windows=["open_drive", "morning"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["gap_size", "volume", "reversal_patterns", "vwap"],
        strengths="Statistical edge — most gaps fill partially. Clear target (prior close). Counter-trend profits.",
        weaknesses="Strong catalyst gaps don't fill. Fighting institutional flow. Requires gap classification.",
    ),

    Strategy(
        name="Earnings Drift",
        category="event",
        description="Post-earnings announcement drift (PEAD) — stocks continue moving in the direction of the earnings surprise for days/weeks. One of the most well-documented anomalies.",
        entry_logic="Company reports earnings that beat/miss consensus by significant margin. Enter in the direction of the surprise on the first pullback after the gap. Wait for the 5-minute bar to close. Confirm with sector ETF movement.",
        exit_logic="Target: hold for 1-5 days (swing). Trail stop at prior day VWAP. Exit on sector reversal or earnings day range break.",
        regimes=["trending_up", "trending_down"],
        time_windows=["open_drive", "morning"],
        asset_class=["nasdaq_stocks", "large_cap"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:2 to 1:5",
        key_indicators=["earnings_surprise", "volume", "sector_etf", "gap_size"],
        strengths="Well-documented academic anomaly. Strong statistical backing. Multi-day hold = less noise.",
        weaknesses="Requires earnings calendar tracking. Individual stock risk. After-hours gaps are volatile.",
    ),

    Strategy(
        name="Afternoon Reversal",
        category="event",
        description="Markets tend to reverse between 1:30-2:30 PM EST as institutions rebalance. Fade the morning trend if it shows exhaustion signals at this time.",
        entry_logic="Morning trend has been strong (1+ ATR move). Between 1:30-2:30 PM EST, look for reversal signals: RSI divergence, delta divergence, shooting star/hammer at VWAP or key level. Enter on reversal confirmation.",
        exit_logic="Target: VWAP or morning's midpoint. Stop: beyond the afternoon extreme. Time stop: 90 minutes (before close).",
        regimes=["trending_up", "trending_down"],
        time_windows=["afternoon"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="50-55%",
        risk_reward="1:1.5",
        key_indicators=["rsi", "delta", "vwap", "time_of_day"],
        strengths="Documented time-of-day effect. Institutions rebalancing creates real flow. Fade overcrowded moves.",
        weaknesses="Strong trend days don't reverse. Less reliable than open strategies. Requires regime awareness.",
    ),

    # ── STATISTICAL / QUANTITATIVE STRATEGIES ──────────────────────────

    Strategy(
        name="Mean Reversion Z-Score",
        category="stat_arb",
        description="Enter when price deviates 2+ standard deviations from a rolling mean. Pure statistical mean reversion with Z-score-based entry and exit.",
        entry_logic="Calculate 20-period rolling mean and standard deviation. Enter long when Z-score < -2 (price is 2SD below mean). Enter short when Z-score > +2. Confirm with declining momentum (RSI extreme).",
        exit_logic="Target: Z-score returns to 0 (mean). Stop: Z-score reaches -3 or +3 (further extension). Time stop: 30 minutes.",
        regimes=["range", "low_vol"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="low",
        volume_preference="normal",
        win_rate_estimate="60-65%",
        risk_reward="1:1 to 1:1.5",
        key_indicators=["z_score", "rolling_mean", "rsi", "standard_deviation"],
        strengths="Pure statistical edge. High win rate. Clear mathematical entry/exit. No subjectivity.",
        weaknesses="Trending markets produce sustained Z-score extremes. Regime dependence. Low R:R.",
    ),

    Strategy(
        name="Pairs / Relative Value",
        category="stat_arb",
        description="Trade the spread between two correlated instruments (NQ vs ES, AAPL vs QQQ, etc.). When the spread deviates from historical norm, trade the convergence.",
        entry_logic="Calculate the rolling spread between two correlated assets. Enter when spread exceeds 2 standard deviations from the mean. Long the underperformer, short the overperformer. Correlation must remain > 0.80.",
        exit_logic="Target: spread returns to mean. Stop: spread widens to 3 standard deviations. Time stop: end of day.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="60-65%",
        risk_reward="1:1.5",
        key_indicators=["spread", "correlation", "z_score", "rolling_mean"],
        strengths="Market-neutral (hedged). Statistical backing. Works in all market conditions.",
        weaknesses="Correlation can break down. Margin requirements. Two positions = double transaction costs.",
    ),

    Strategy(
        name="Intraday Momentum (Gao-Ritter)",
        category="stat_arb",
        description="Academic strategy: the first 30-minute return predicts the last 30-minute return on the same day. Documented by Gao and Ritter (2010) with positive alpha.",
        entry_logic="Calculate the return from open to 10:00 AM. If positive, go long at 3:30 PM EST. If negative, go short. Simple binary signal based on morning return direction.",
        exit_logic="Close at 4:00 PM EST (market close). No stop — holding through close is part of the strategy.",
        regimes=["any"],
        time_windows=["close"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="52-55%",
        risk_reward="1:1",
        key_indicators=["morning_return", "time_of_day"],
        strengths="Academic backing with published research. Simple to implement. No parameters to overfit.",
        weaknesses="Small edge. Transaction costs matter. Regime-dependent. Not all days exhibit the pattern.",
    ),

    Strategy(
        name="Overnight Return Predictor",
        category="stat_arb",
        description="The overnight return (prior close to open) predicts intraday direction. Large positive overnight gaps tend to continue, small gaps tend to revert.",
        entry_logic="Calculate overnight return (gap percentage). If gap > 0.5%, trade in gap direction after first 15-minute confirmation. If gap < 0.2%, fade the gap (trade toward prior close).",
        exit_logic="Gap-continuation: target 1.5x gap extension. Gap-fade: target gap fill. Stop: 1 ATR. Time stop: 2 hours.",
        regimes=["any"],
        time_windows=["open_drive", "morning"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="52-55%",
        risk_reward="1:1.5",
        key_indicators=["gap_size", "overnight_return", "volume"],
        strengths="Simple rule-based. No complex indicators. Documented in academic literature.",
        weaknesses="Small edge. Requires gap classification. Not all gaps are equal (catalyst vs. no catalyst).",
    ),

    # ── EQUITY-SPECIFIC STRATEGIES ─────────────────────────────────────

    Strategy(
        name="Sector Rotation Momentum",
        category="momentum",
        description="Rotate into the strongest sector ETF (XLK, XLE, XLF, etc.) based on relative strength. When tech leads, NQ outperforms. When energy leads, NQ underperforms.",
        entry_logic="Rank sector ETFs by 5-day relative performance vs SPY. Go long the top 2 sectors, short the bottom 2 (or adjust NQ bias accordingly). Use RS ratio > 1.0 as long trigger, < 1.0 as short trigger.",
        exit_logic="Rebalance weekly or when RS ranking changes. Stop: sector drops below 20-day MA.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning", "any"],
        asset_class=["nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["relative_strength", "sector_etfs", "moving_averages"],
        strengths="Diversified across sectors. Captures institutional flows. Adaptable to market conditions.",
        weaknesses="Slower signals (daily/weekly). Not suitable for scalping. Sector correlations can break down.",
    ),

    Strategy(
        name="High-of-Day Breakout (Stocks)",
        category="breakout",
        description="Buy stocks making new highs of the day with volume surge. Simple momentum strategy that catches continuation moves in strong names.",
        entry_logic="Stock makes new high of the day. Volume on the breakout bar is 2x+ the 20-bar average. Stock is above VWAP and rising EMA9. Sector (XLK for tech) is also positive. Enter on the breakout bar close.",
        exit_logic="Target: measured move (morning range projected from breakout). Stop: VWAP or breakout candle low. Trail at EMA9.",
        regimes=["trending_up"],
        time_windows=["morning", "midday"],
        asset_class=["nasdaq_stocks", "large_cap"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="50-55%",
        risk_reward="1:2",
        key_indicators=["hod", "volume", "vwap", "ema9", "sector_etf"],
        strengths="Simple rules. Catches strong momentum. Works well in bull markets.",
        weaknesses="False breakouts in choppy markets. Requires stock screener. Individual stock risk.",
    ),

    Strategy(
        name="Relative Strength Leaders",
        category="momentum",
        description="Buy stocks showing relative strength to the index during pullbacks. When NQ drops 0.5%, stocks that hold flat or go up are showing institutional accumulation.",
        entry_logic="Identify stocks that are green or flat while NQ/SPY is red (or vice versa for shorts). Wait for NQ to stabilize/bounce. Enter the RS leader when NQ bounces. The leader should accelerate on the NQ recovery.",
        exit_logic="Target: outperformance continuation (1.5x NQ recovery). Stop: stock breaks below day's VWAP. Trail at intraday rising support.",
        regimes=["trending_up", "volatile"],
        time_windows=["morning", "midday"],
        asset_class=["nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["relative_strength", "vwap", "sector_performance"],
        strengths="Institutional flow signal. Outperformers tend to continue. Reduces market risk.",
        weaknesses="Requires real-time screening. RS can shift quickly. Individual stock events can override.",
    ),

    Strategy(
        name="Pre-Market Gapper (Stocks)",
        category="event",
        description="Trade stocks gapping 3%+ pre-market on earnings/news. These have high institutional interest and produce reliable opening drive moves.",
        entry_logic="Stock gaps 3%+ pre-market with clear catalyst (earnings beat, upgrade, FDA approval, etc.). Pre-market volume > 500K shares. Wait for first 5-minute candle to close at open. Enter in gap direction if candle is strong (>60% body). Enter counter-gap if first candle reverses with volume.",
        exit_logic="Target: pre-market high (long) or low (short). Stop: VWAP or opening candle extreme. Time stop: 30 minutes.",
        regimes=["volatile", "trending_up", "trending_down"],
        time_windows=["open_drive"],
        asset_class=["nasdaq_stocks", "large_cap"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:2 to 1:3",
        key_indicators=["gap_size", "pre_market_volume", "catalyst", "vwap"],
        strengths="High probability follow-through on catalyst gaps. Clear entry rules. Fast profits.",
        weaknesses="Pre-market analysis required. Not all gaps follow through. High volatility = high risk.",
    ),

    Strategy(
        name="Moving Average Ribbon Trend",
        category="momentum",
        description="Use a ribbon of EMAs (8, 13, 21, 34, 55) to identify and ride trends. When ribbons are stacked and expanding, the trend is strong. Compression signals trend exhaustion.",
        entry_logic="All EMAs stacked in order (8 > 13 > 21 > 34 > 55 for uptrend). Ribbons expanding (diverging). Enter on pullback to the 21 or 34 EMA. Volume should increase on the bounce.",
        exit_logic="Exit when 8 EMA crosses below 21 EMA (trend weakening). Trail at the 34 EMA. Target: continuation to new highs.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="50-55%",
        risk_reward="1:2 to 1:3",
        key_indicators=["ema_ribbon", "ema8", "ema13", "ema21", "ema34", "ema55"],
        strengths="Visual trend identification. Multiple support levels. Works on all timeframes.",
        weaknesses="Lagging indicator. Choppy markets produce tangled ribbons. Late entries.",
    ),

    # ── SWING / MULTI-DAY STRATEGIES ───────────────────────────────────

    Strategy(
        name="Weekly VWAP Bounce",
        category="swing",
        description="Use the weekly VWAP as a swing trading anchor. Price respects weekly VWAP as institutional fair value on a multi-day basis. Bounces off weekly VWAP are high-probability entries.",
        entry_logic="Price pulls back to weekly VWAP in an overall uptrend (above monthly VWAP). Enter on reversal candle at weekly VWAP on the daily chart. Daily RSI should be between 40-60 (not oversold yet).",
        exit_logic="Target: retest of prior swing high. Stop: 1 daily ATR below weekly VWAP. Hold 2-5 days.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2 to 1:3",
        key_indicators=["weekly_vwap", "monthly_vwap", "daily_rsi", "daily_atr"],
        strengths="Institutional anchor level. Multi-day hold reduces noise. High R:R swings.",
        weaknesses="Requires daily chart analysis. Overnight risk. Less actionable intraday.",
    ),

    Strategy(
        name="52-Week High Breakout",
        category="swing",
        description="Buy stocks or indices breaking to new 52-week highs. New highs represent no overhead supply — all holders are in profit, reducing selling pressure.",
        entry_logic="Price closes at a new 52-week high. Volume on the breakout day is 1.5x+ average. RSI is above 60 but below 80 (strong but not exhausted). Enter on the first pullback to the breakout level.",
        exit_logic="Trail stop at 20-day MA. Target: hold for trend continuation (multiple weeks). Exit on 20-day MA break.",
        regimes=["trending_up"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:3 to 1:5",
        key_indicators=["52_week_high", "volume", "rsi", "20_day_ma"],
        strengths="No overhead resistance. Strong momentum confirmation. Multi-week holds capture big moves.",
        weaknesses="Buying at the high feels wrong. Late entries. Pullbacks from highs can be deep.",
    ),

    Strategy(
        name="Trend Exhaustion Counter",
        category="mean_reversion",
        description="Counter-trend entry after 3+ consecutive same-direction bars with declining delta. Extended moves without pullbacks tend to snap back. High risk but high reward.",
        entry_logic="3+ consecutive bars in the same direction. Each bar has declining volume or delta. RSI at extreme (>80 or <20). Enter counter-trend on the first reversal candle. Require key level confluence (S/R, VWAP band, round number).",
        exit_logic="Target: 50% retracement of the extended move. Stop: beyond the extreme. Time stop: 15 minutes.",
        regimes=["trending_up", "trending_down", "volatile"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="high",
        volume_preference="normal",
        win_rate_estimate="45-50%",
        risk_reward="1:2 to 1:3",
        key_indicators=["consecutive_bars", "delta", "rsi", "support_resistance"],
        strengths="Catches snap-back moves. High R:R. Counter-trend profits. Works at session extremes.",
        weaknesses="Fighting the trend. Lower win rate. Requires precise timing. Strong trends don't exhaust.",
    ),

    Strategy(
        name="Keltner Channel Mean Reversion",
        category="mean_reversion",
        description="Trade bounces off Keltner Channel extremes (2x ATR). Price outside KC is statistically unusual and tends to revert to the midline (EMA20).",
        entry_logic="Price closes outside the 2x ATR Keltner Channel. Next bar shows reversal (close back inside the channel). Enter on the reversal bar close. Volume should spike on the extreme and decline on reversal.",
        exit_logic="Target: KC midline (EMA20). Stop: 0.5 ATR beyond the extreme. Time stop: 30 minutes.",
        regimes=["range", "volatile"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["keltner_channels", "atr", "ema20", "volume"],
        strengths="ATR-adaptive bands. Statistical edge at extremes. Clear entry/exit. Adapts to volatility.",
        weaknesses="Trending markets produce serial touches. Needs regime filter. Similar to BB reversal.",
    ),

    Strategy(
        name="Fibonacci Retracement Entry",
        category="pattern",
        description="Enter at key Fibonacci levels (38.2%, 50%, 61.8%) during pullbacks in trending markets. The golden ratio levels represent natural support/resistance.",
        entry_logic="Identify a clear impulse leg. Price retraces to 38.2%, 50%, or 61.8% Fibonacci level. Enter on reversal candle at the level. OTE zone (62-79%) is the highest-probability entry. Volume should decline on pullback.",
        exit_logic="Target: new swing high/low or 161.8% extension. Stop: below 78.6% retracement. Trail at prior Fib level.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="50-55%",
        risk_reward="1:2 to 1:3",
        key_indicators=["fibonacci", "swing_highs_lows", "volume", "trend_direction"],
        strengths="Widely followed levels. Self-fulfilling prophecy. Works on all timeframes and assets.",
        weaknesses="Many possible swing points. Subjective level selection. Not all retracements respect Fib.",
    ),

    Strategy(
        name="Inside Bar Breakout",
        category="breakout",
        description="An inside bar (fully contained within prior bar's range) represents consolidation. The breakout from an inside bar is a compression release with directional conviction.",
        entry_logic="Identify an inside bar (high < prior high AND low > prior low). Enter on the break of the inside bar's high (long) or low (short). Prefer inside bars that form at key levels (S/R, VWAP, EMA). Smaller inside bars (narrower range) produce stronger breakouts.",
        exit_logic="Target: measured move equal to the mother bar's range. Stop: opposite side of the inside bar.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="low",
        win_rate_estimate="50-55%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["inside_bar", "mother_bar_range", "support_resistance"],
        strengths="Clear, objective setup. Works on all timeframes. Tight stop (inside bar range). Frequent setups.",
        weaknesses="False breakouts common. Needs level confluence for higher probability. Direction is a coin flip without context.",
    ),

    Strategy(
        name="Ichimoku Cloud Trend",
        category="momentum",
        description="Trade with the Ichimoku Cloud trend. Price above the cloud is bullish, below is bearish. Tenkan/Kijun cross within the trend gives entry signals.",
        entry_logic="Price above the Kumo cloud (bullish). Tenkan-sen crosses above Kijun-sen (golden cross). Chikou span above price from 26 periods ago. Enter on the cross or on pullback to Kijun-sen.",
        exit_logic="Exit on Tenkan crossing below Kijun. Stop: below the cloud or Kijun-sen. Target: next cloud twist or major resistance.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="50-55%",
        risk_reward="1:2",
        key_indicators=["ichimoku_cloud", "tenkan_sen", "kijun_sen", "chikou_span"],
        strengths="Complete trading system in one indicator. Cloud provides dynamic S/R. Multiple confirmation signals.",
        weaknesses="Complex for beginners. Lagging indicator. Choppy markets produce false signals.",
    ),

    # ═══════════════════════════════════════════════════════════════════
    #  OPTIONS FLOW & GAMMA STRATEGIES
    # ═══════════════════════════════════════════════════════════════════

    Strategy(
        name="0DTE Gamma Squeeze",
        category="options_flow",
        description="When 0DTE call open interest is concentrated at a strike, market makers must delta-hedge by buying futures as price approaches that strike. This creates a self-reinforcing squeeze toward the strike.",
        entry_logic="Identify the strike with highest 0DTE call OI. As NQ approaches from below, go long. The gamma effect accelerates buying as MMs hedge. Conversely for puts — as price drops toward high put OI strikes, MMs sell futures creating acceleration lower.",
        exit_logic="Target: the high-OI strike level. Stop: 1 ATR below entry. Exit if price stalls 5+ points from strike. The effect is strongest in the last 2 hours of trading.",
        regimes=["trending_up", "trending_down", "volatile"],
        time_windows=["afternoon", "close"],
        asset_class=["nq", "es"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["options_oi", "gamma_exposure", "delta_hedging", "strike_levels"],
        strengths="Mechanical force — MMs MUST hedge. Self-reinforcing. Strongest on 0DTE expiry days (Mon/Wed/Fri).",
        weaknesses="Requires options flow data. Gamma flips near the strike. Can reverse violently post-expiry.",
    ),

    Strategy(
        name="Gamma Flip Level",
        category="options_flow",
        description="The gamma flip level is where aggregate dealer gamma changes from positive to negative. Above the flip, MMs dampen moves (selling rallies, buying dips). Below the flip, MMs amplify moves (selling dips, buying rallies).",
        entry_logic="Identify the gamma flip level from options OI data. When price crosses ABOVE the flip, expect mean-reversion (fade extremes). When price crosses BELOW the flip, expect momentum continuation (trend following). Adjust strategy selection based on which side of the flip you're on.",
        exit_logic="Above gamma flip: tight targets, quick profits (1:1 RR). Below gamma flip: wider targets, trail stops (1:2 RR). Exit on re-crossing the flip level.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="55-60%",
        risk_reward="varies",
        key_indicators=["gamma_exposure", "gamma_flip_level", "dealer_positioning"],
        strengths="Changes the character of the market. Explains why some days are mean-reverting vs trending. Institutional-level insight.",
        weaknesses="Requires specialized options data (SpotGamma, etc.). Flip level shifts intraday. Complex to calculate.",
    ),

    Strategy(
        name="Put/Call Ratio Extreme",
        category="options_flow",
        description="Extreme put/call ratios signal crowded sentiment. Very high P/C ratio (>1.2) = excessive fear = bullish contrarian signal. Very low P/C (<0.5) = excessive greed = bearish contrarian signal.",
        entry_logic="Monitor equity put/call ratio. When P/C > 1.2 on a pullback day, look for long entries on reversal signals. When P/C < 0.5 on a rally day, look for short entries. Works best at multi-week extremes.",
        exit_logic="Target: mean reversion in P/C ratio (back toward 0.7-0.9). Stop: new extreme in P/C or new price extreme. Hold 1-5 days.",
        regimes=["volatile", "trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["put_call_ratio", "vix", "sentiment"],
        strengths="Strong contrarian signal at extremes. Well-documented academic anomaly. Works on index and individual stocks.",
        weaknesses="Extremes can persist. Not precise on timing. Better as swing signal than intraday.",
    ),

    Strategy(
        name="Dark Pool Print Reversal",
        category="options_flow",
        description="Large dark pool prints (block trades off-exchange) at key levels signal institutional accumulation or distribution. These prints often precede reversals.",
        entry_logic="Detect large dark pool prints (10x+ normal size) at or near key S/R levels. If a large buy print occurs at support during a selloff, go long. If a large sell print occurs at resistance during a rally, go short. Confirm with subsequent price action.",
        exit_logic="Target: next key level or 1.5 ATR. Stop: beyond the dark pool print level. Time stop: 30 minutes.",
        regimes=["range", "trending_up", "trending_down"],
        time_windows=["morning", "midday", "afternoon"],
        asset_class=["nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["dark_pool_volume", "block_trades", "support_resistance"],
        strengths="Institutional footprint. Off-exchange = intentional positioning. High-conviction signal.",
        weaknesses="Requires dark pool data feed. Not all large prints are directional (hedges). Delayed reporting.",
    ),

    Strategy(
        name="Options Expiration Pin",
        category="options_flow",
        description="On monthly/weekly options expiration (OPEX), price tends to 'pin' to the strike with maximum open interest due to delta hedging decay. Trade toward the max pain strike.",
        entry_logic="On OPEX day, identify the max pain strike (where option holders lose the most). If price is above max pain, expect downward drift. If below, expect upward drift. Enter in the direction of max pain. Effect is strongest in the last 2 hours.",
        exit_logic="Target: max pain strike. Stop: 1 ATR in the opposite direction. Close at 4 PM if not at target.",
        regimes=["range", "any"],
        time_windows=["afternoon", "close"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="low",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["max_pain", "options_oi", "expiration_date"],
        strengths="Documented mechanical effect. Predictable on OPEX days. MMs have financial incentive to pin.",
        weaknesses="Strong catalysts override pinning. Not every OPEX pins. Requires options data.",
    ),

    # ═══════════════════════════════════════════════════════════════════
    #  INTERMARKET STRATEGIES
    # ═══════════════════════════════════════════════════════════════════

    Strategy(
        name="VIX Mean Reversion Trade",
        category="intermarket",
        description="VIX spikes above 25+ are typically short-lived. When VIX spikes and NQ drops, the subsequent VIX crush provides a tailwind for NQ longs. Buy NQ when VIX shows exhaustion.",
        entry_logic="VIX spikes above 25 (or 50%+ above its 20-day MA). Wait for VIX to show a bearish reversal candle (shooting star, engulfing). Enter long NQ when VIX starts declining. NQ and VIX are inversely correlated ~-0.85.",
        exit_logic="Target: NQ recovers 50-75% of the VIX-spike selloff. Stop: VIX makes new high. Hold 1-5 days.",
        regimes=["volatile"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="60-65%",
        risk_reward="1:2 to 1:3",
        key_indicators=["vix", "vix_ma20", "nq_correlation", "volume"],
        strengths="Mean reversion of fear is reliable. VIX spikes are statistically short-lived. High R:R.",
        weaknesses="VIX can stay elevated in bear markets. Timing the peak is difficult. Requires holding through volatility.",
    ),

    Strategy(
        name="Dollar-NQ Inverse Play",
        category="intermarket",
        description="NQ and DXY (US Dollar Index) are inversely correlated. When the dollar weakens, multinational tech earnings improve and NQ tends to rally. Dollar strength pressures NQ.",
        entry_logic="DXY drops below its 20-day MA and shows momentum decline. NQ should be at or above VWAP. Enter long NQ on the DXY breakdown confirmation. For shorts: DXY rallies above 20-day MA = headwind for NQ.",
        exit_logic="Target: NQ moves proportional to DXY move (beta ~1.5x). Stop: DXY reverses direction. Hold 1-5 days.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "nasdaq_stocks"],
        atr_preference="any",
        volume_preference="normal",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["dxy", "dxy_ma20", "nq_correlation", "currency_pairs"],
        strengths="Fundamental macro relationship. Large cap tech has huge international revenue. Documented correlation.",
        weaknesses="Correlation varies over time. Other factors can dominate. Slow-moving signal (daily).",
    ),

    Strategy(
        name="Bond Yield Divergence",
        category="intermarket",
        description="When 10Y Treasury yields rise sharply, growth stocks (NQ-heavy) underperform as future earnings are discounted more heavily. Falling yields = bullish NQ. The yield curve (10Y-2Y) signals recession risk.",
        entry_logic="10Y yield drops 5+ bps in a day while NQ is flat or down — go long NQ (yields leading the move). 10Y yield spikes 10+ bps — go short NQ or reduce long exposure. Inverted yield curve (10Y < 2Y) = elevated recession risk = reduce overall exposure.",
        exit_logic="Target: NQ catches up to yield move (1-3 day lag). Stop: yield reverses direction. Use yield curve as portfolio-level risk adjustment.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["10y_yield", "2y_yield", "yield_curve", "tnx"],
        strengths="Fundamental macro driver. NQ is the most rate-sensitive index. Yield data is free and real-time.",
        weaknesses="Relationship isn't 1:1. QE/QT regimes distort signals. Other factors can override.",
    ),

    Strategy(
        name="Crude Oil NQ Correlation",
        category="intermarket",
        description="Oil spikes often hurt NQ (higher costs, inflation fears) while oil drops help (lower costs, disinflation). The relationship is clearest at extremes.",
        entry_logic="CL (crude oil futures) drops 3%+ in a day — look for long NQ entries (disinflation tailwind). CL spikes 5%+ — look for short NQ or reduce exposure (inflation headwind). Most relevant when oil is the primary macro narrative.",
        exit_logic="Target: NQ adjusts over 1-3 days. Stop: oil move reverses. Position size based on narrative strength.",
        regimes=["volatile"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="high",
        volume_preference="any",
        win_rate_estimate="50-55%",
        risk_reward="1:1.5",
        key_indicators=["cl_futures", "oil_correlation", "inflation_expectations"],
        strengths="Clear macro relationship. Oil is a leading indicator for inflation. Easy to monitor.",
        weaknesses="Correlation varies by macro regime. Supply-side oil moves differ from demand-side. Not always relevant.",
    ),

    Strategy(
        name="Risk-On/Risk-Off Regime Filter",
        category="intermarket",
        description="Monitor a basket of risk indicators (VIX, credit spreads, HYG, DXY, gold) to determine the macro regime. Risk-on favors long NQ, risk-off favors short or sideline.",
        entry_logic="Risk-On signals: VIX < 18, HYG rising, credit spreads narrowing, DXY flat/falling, gold flat. Risk-Off signals: VIX > 22, HYG falling, credit spreads widening, DXY rising, gold rising. Trade NQ in the direction of the regime. Transition periods (mixed signals) = reduce size.",
        exit_logic="Regime-level filter — not a specific trade entry. Use to adjust position size and strategy selection. Full size in clear risk-on, half size in transition, sideline or short-only in risk-off.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="N/A (filter)",
        risk_reward="N/A (filter)",
        key_indicators=["vix", "hyg", "credit_spreads", "dxy", "gold", "risk_score"],
        strengths="Comprehensive macro context. Prevents trading against the macro trend. Portfolio-level risk management.",
        weaknesses="Signals can be mixed. Regime changes aren't instant. Requires multiple data feeds.",
    ),

    # ═══════════════════════════════════════════════════════════════════
    #  SEASONAL / CALENDAR STRATEGIES
    # ═══════════════════════════════════════════════════════════════════

    Strategy(
        name="OPEX Week Gamma Unwind",
        category="seasonal",
        description="The week of monthly options expiration (3rd Friday) sees massive gamma unwind. Dealers who were short gamma buy back hedges, creating directional pressure. OPEX week tends to be volatile with pinning on Friday.",
        entry_logic="During OPEX week (Mon-Thu before 3rd Friday), expect elevated volatility and wider ranges. Monday/Tuesday: trade momentum (gamma unwind creates trends). Wednesday/Thursday: ranges narrow as pin effect starts. Friday: trade toward max pain strike in the afternoon.",
        exit_logic="Adjust daily. Mon-Tue: trail stops on momentum trades. Wed-Fri: tighten targets, play for pin. Exit all by Friday close.",
        regimes=["volatile", "any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["options_expiration", "gamma_exposure", "max_pain", "calendar"],
        strengths="Documented calendar effect. Dealer hedging is mechanical. Predictable volatility pattern.",
        weaknesses="Each OPEX is different (OI distribution varies). Macro events can override. Requires options data.",
    ),

    Strategy(
        name="Month-End Rebalancing Flow",
        category="seasonal",
        description="Pension funds and mutual funds rebalance portfolios at month-end (last 3 trading days). When equities outperformed bonds during the month, funds sell stocks and buy bonds (bearish flow). When bonds outperformed, funds buy stocks (bullish flow).",
        entry_logic="In the last 3 trading days of the month, check if NQ outperformed bonds (TLT) during the month. If NQ > TLT: expect selling pressure (short bias). If TLT > NQ: expect buying pressure (long bias). Enter in the expected flow direction at the open.",
        exit_logic="Target: hold through month-end. Stop: 1.5 ATR. Close all positions by month-end close.",
        regimes=["any"],
        time_windows=["morning", "any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["calendar", "nq_vs_tlt", "rebalancing_flow"],
        strengths="Institutional flow is mechanical (mandated by fund rules). Documented by academic research. Predictable timing.",
        weaknesses="Flow size varies. Other catalysts can dominate. Effect is spread over 3 days, not concentrated.",
    ),

    Strategy(
        name="FOMC Drift (Pre-Announcement)",
        category="seasonal",
        description="Academic research (Lucca & Moench, 2015) shows NQ tends to drift UP in the 24 hours before FOMC announcements. The 'pre-FOMC drift' is one of the most robust calendar anomalies.",
        entry_logic="Go long NQ at the close the day BEFORE the FOMC announcement. Hold through the announcement. The drift averages +0.5% in the 24 hours pre-FOMC. Reduce size if VIX > 30 (extreme fear overrides the drift).",
        exit_logic="Close 30 minutes after the FOMC statement release. Stop: 1.5 ATR from entry. Do NOT hold through the press conference if the initial reaction is strongly negative.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="55-65%",
        risk_reward="1:1.5",
        key_indicators=["fomc_date", "calendar", "vix"],
        strengths="Robust academic backing. Documented for 30+ years. Simple to implement. No parameters to overfit.",
        weaknesses="Not every FOMC has the drift. Hiking cycles reduce the effect. Post-announcement reversal risk.",
    ),

    Strategy(
        name="Turn-of-Month Effect",
        category="seasonal",
        description="Equity returns are disproportionately concentrated in the last day of the month and first 3 days of the next month. This 4-day window captures ~80% of monthly returns historically.",
        entry_logic="Go long NQ at the close on the last trading day of the month. Hold through the first 3 trading days of the new month. The effect is driven by automatic 401(k) contributions, pension fund flows, and institutional rebalancing.",
        exit_logic="Close at the end of the 3rd trading day of the new month. Stop: 2 ATR from entry (wide — this is a multi-day hold). Reduce size in bear markets.",
        regimes=["trending_up", "any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["calendar", "month_end", "401k_flows"],
        strengths="Well-documented anomaly. Mechanical flow (401k contributions). 80%+ of monthly returns in 4 days.",
        weaknesses="Bear markets can override. Not every month works. Overnight gap risk on multi-day hold.",
    ),

    Strategy(
        name="Quad Witching Volatility",
        category="seasonal",
        description="Quarterly expiration of stock options, index options, index futures, and single-stock futures (3rd Friday of Mar/Jun/Sep/Dec). Volume and volatility spike as $3-5T of notional rolls or expires.",
        entry_logic="On quad witching Friday, expect high volume and erratic moves. Morning: trade momentum (heavy rollover creates directional moves). Afternoon: expect mean-reversion as most rolls complete. The last hour often sees massive volume but little net movement.",
        exit_logic="Tighten all stops on quad witching day. Take profits quickly. Exit all positions before the close. Don't hold overnight through quad witching.",
        regimes=["volatile"],
        time_windows=["morning", "afternoon", "close"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="50-55%",
        risk_reward="1:1",
        key_indicators=["quad_witching_date", "volume", "options_expiration"],
        strengths="Predictable volatility event. High volume = tight spreads. Known date in advance.",
        weaknesses="Erratic price action. Whipsaws common. Difficult to predict direction. Best used as filter, not entry signal.",
    ),

    Strategy(
        name="Earnings Season Rotation",
        category="seasonal",
        description="During earnings season (Jan/Apr/Jul/Oct), mega-cap tech reports in a specific order. Rotation between FAANG/Mag7 names creates predictable sector flows that affect NQ.",
        entry_logic="Track earnings calendar for AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA. Pre-earnings: IV crush candidates (sell premium). Post-earnings: trade the drift in the surprise direction. When mega-caps beat: NQ gets lifted. When they miss: NQ drops disproportionately due to index weight.",
        exit_logic="Pre-earnings: close before announcement. Post-earnings: hold for 2-5 day drift. Stop: earnings day range break.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "nasdaq_stocks", "large_cap"],
        atr_preference="high",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["earnings_calendar", "iv_rank", "sector_weight", "surprise_magnitude"],
        strengths="Predictable calendar. Mega-cap moves are NQ moves (top 7 = ~55% of QQQ). Well-documented drift.",
        weaknesses="Individual stock risk. After-hours gaps. Forward guidance matters more than the beat/miss.",
    ),

    # ═══════════════════════════════════════════════════════════════════
    #  NQ-SPECIFIC BEHAVIORAL STRATEGIES
    # ═══════════════════════════════════════════════════════════════════

    Strategy(
        name="Mega-Cap Concentration Play",
        category="nq_specific",
        description="NQ/QQQ is dominated by ~7 stocks (AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA) comprising ~55% of the index. When these names move in unison, NQ amplifies. When they diverge, NQ chops.",
        entry_logic="Monitor the Mag7 in real-time. If 5+ of 7 are green, NQ has strong upward bias — trade long aggressively. If 5+ are red, NQ has strong downward bias — trade short. If split (3-4 each direction), expect range-bound chop — fade extremes or sit out.",
        exit_logic="Regime filter — adjusts strategy selection and size. Full size when Mag7 aligned, half size when mixed, sideline when divergent.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="N/A (filter)",
        risk_reward="N/A (filter)",
        key_indicators=["mag7_alignment", "aapl", "msft", "googl", "amzn", "meta", "nvda", "tsla"],
        strengths="The defining characteristic of NQ. When Mag7 align, NQ trends hard. Clear, objective measure.",
        weaknesses="Requires individual stock monitoring. Composition changes over time. Equal-weight vs cap-weight divergence.",
    ),

    Strategy(
        name="NQ vs ES Relative Strength",
        category="nq_specific",
        description="NQ/ES ratio reveals risk appetite. When NQ outperforms ES, markets are in 'risk-on' growth mode. When ES outperforms NQ, markets are rotating to 'value/safety' mode.",
        entry_logic="Calculate NQ/ES ratio on 5-minute bars. If ratio is rising (NQ outperforming), go long NQ — growth/tech is leading. If ratio is falling (ES outperforming), go short NQ or switch to ES longs — rotation out of tech. Track the ratio's 20-period MA for trend confirmation.",
        exit_logic="Exit NQ longs when ratio breaks below its 20-period MA. Exit NQ shorts when ratio breaks above. Use as a regime filter for all NQ strategies.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["nq_es_ratio", "relative_strength", "sector_rotation"],
        strengths="Real-time regime detection. Captures institutional rotation. Simple to calculate.",
        weaknesses="Ratio can chop in range-bound markets. Mega-cap events distort the signal. Intraday noise.",
    ),

    Strategy(
        name="Tech Sector ETF Lead",
        category="nq_specific",
        description="Semiconductor ETF (SMH/SOXX) often leads NQ by 15-30 minutes. If semis break out before NQ, it's a leading indicator for NQ direction. NVDA alone can move NQ 50+ points.",
        entry_logic="Monitor SMH/SOXX relative to its VWAP. If semis break above VWAP while NQ is still below — go long NQ (semis leading). If semis break down while NQ holds — prepare for NQ breakdown. Confirm with NVDA price action (largest NQ weight).",
        exit_logic="Target: NQ catches up to semi move. Stop: semis reverse direction. Time stop: 30 minutes for the lead to materialize.",
        regimes=["trending_up", "trending_down"],
        time_windows=["morning", "midday"],
        asset_class=["nq", "nasdaq_stocks"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["smh", "soxx", "nvda", "sector_lead", "vwap"],
        strengths="Documented leading relationship. Semis are highest-beta NQ component. NVDA is the clearest single-stock signal.",
        weaknesses="Lead time varies. Individual semi events (earnings, guidance) create noise. Relationship isn't constant.",
    ),

    Strategy(
        name="Growth-to-Value Rotation Detector",
        category="nq_specific",
        description="When money rotates from growth (NQ-heavy) to value (financials, energy, industrials), NQ underperforms. Detect rotation early by monitoring IWM/IWF ratio and XLF/XLK ratio.",
        entry_logic="IWM (small-cap value) outperforming IWF (large-cap growth) = rotation out of NQ. XLF (financials) outperforming XLK (tech) = same signal. When both ratios are rising: reduce NQ long exposure or go short. When both falling: NQ is the place to be.",
        exit_logic="Regime filter — not a specific entry. Adjusts NQ exposure. Full long NQ when growth leads. Reduce/hedge when value leads. Neutral when mixed.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "nasdaq_stocks"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="N/A (filter)",
        risk_reward="N/A (filter)",
        key_indicators=["iwm_iwf_ratio", "xlf_xlk_ratio", "sector_rotation", "growth_value"],
        strengths="Early warning for NQ underperformance. Captures institutional rotation. Multiple confirming ratios.",
        weaknesses="Slow signal (daily). Rotation can be temporary. Macro events override rotation patterns.",
    ),

    # ═══════════════════════════════════════════════════════════════════
    #  MARKET MICROSTRUCTURE STRATEGIES
    # ═══════════════════════════════════════════════════════════════════

    Strategy(
        name="Bid-Ask Spread Expansion",
        category="microstructure",
        description="When the bid-ask spread widens suddenly, it signals liquidity withdrawal — market makers are stepping back. This often precedes a sharp move in the direction of the aggressive side.",
        entry_logic="Detect spread widening to 2x+ normal (MNQ normal = 0.25, wide = 0.50+). If the widening occurs with aggressive selling (ask-side volume dominant), go short. If with aggressive buying (bid-side dominant), go long. Spread widening at key levels is especially significant.",
        exit_logic="Target: 4-8 ticks in the direction of aggression. Stop: spread normalizes without price following through. Time stop: 2 minutes.",
        regimes=["volatile", "any"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["spread", "bid_ask", "aggressor_side", "liquidity"],
        strengths="Pure microstructure edge. Signals liquidity crisis moments. Very fast signals.",
        weaknesses="Requires tick-level data. Very short hold time. Spread widening can be temporary.",
    ),

    Strategy(
        name="Large Lot Detection",
        category="microstructure",
        description="When abnormally large orders (10x+ normal) appear on the tape, it signals institutional activity. The direction and level of these prints matter more than the surrounding noise.",
        entry_logic="Detect trades 10x+ average size on the time & sales. If a large buy appears at or near support, go long. If a large sell appears at or near resistance, go short. Cluster of large lots in one direction is the strongest signal.",
        exit_logic="Target: next key level (S/R, VWAP, round number). Stop: beyond the large lot price. Time stop: 10 minutes.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks"],
        atr_preference="any",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:1.5",
        key_indicators=["trade_size", "time_and_sales", "support_resistance", "institutional_flow"],
        strengths="Direct institutional footprint. Unambiguous signal (large lots are intentional). Works in all conditions.",
        weaknesses="Large lots can be hedges (not directional). Algorithmic slicing hides true size. Requires real-time tape reading.",
    ),

    Strategy(
        name="Order Book Imbalance Scalp",
        category="microstructure",
        description="When the limit order book shows 3:1+ imbalance (bid vs ask depth), price tends to move toward the heavy side as passive liquidity attracts aggressive execution.",
        entry_logic="DOM shows 3:1+ bid-to-ask ratio at top of book — go long (heavy bids will attract buyers). 3:1+ ask-to-bid — go short. Confirm with recent trade direction (is aggressive flow hitting the heavy side?). Best at key price levels.",
        exit_logic="Target: 3-6 ticks. Stop: imbalance reverses (ratio flips). Time stop: 90 seconds. Exit immediately if large lot appears against you.",
        regimes=["range", "any"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="low",
        volume_preference="normal",
        win_rate_estimate="55-65%",
        risk_reward="1:1 to 1:2",
        key_indicators=["dom_imbalance", "bid_depth", "ask_depth", "aggressor_side"],
        strengths="Pure level 2 edge. Very tight stops. Frequent setups. Works in choppy markets.",
        weaknesses="Book can be spoofed. Requires fast execution. Icebergs hide true depth. Very short hold time.",
    ),

    Strategy(
        name="Sweep Detector Entry",
        category="microstructure",
        description="A sweep occurs when a large aggressive order takes out multiple price levels in milliseconds. This signals urgency — someone NEEDS to get filled and is willing to pay up. Trade with the sweep direction.",
        entry_logic="Detect a sweep: multiple price levels consumed in <100ms with volume 5x+ normal. Go in the sweep direction. Sweeps at key S/R levels are the highest probability. Confirm with subsequent tape (does aggressive flow continue?).",
        exit_logic="Target: 6-12 ticks (sweep creates momentum). Stop: price returns to pre-sweep level. Time stop: 5 minutes.",
        regimes=["volatile", "trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es"],
        atr_preference="medium",
        volume_preference="high",
        win_rate_estimate="55-60%",
        risk_reward="1:2",
        key_indicators=["sweep_detection", "aggressor_volume", "price_levels_consumed", "time_and_sales"],
        strengths="Signals genuine urgency. Institutional-grade flow. Momentum follows sweeps. Clear invalidation.",
        weaknesses="Can be stop-hunts (not continuation). Requires tick-by-tick data. Very fast execution needed.",
    ),

    # ═══════════════════════════════════════════════════════════════════
    #  RISK & POSITION MANAGEMENT STRATEGIES
    # ═══════════════════════════════════════════════════════════════════

    Strategy(
        name="Pyramiding (Add to Winners)",
        category="risk_management",
        description="Add to winning positions at predefined levels instead of entering full size at once. Reduces initial risk and increases size only when the trade is proving correct.",
        entry_logic="Enter with 50% of intended position size. Add 25% when trade moves 1R in your favor. Add final 25% when trade moves 2R. Each add must have independent technical justification (new support level, momentum confirmation). Move stop to breakeven on first add.",
        exit_logic="Trail stop at the entry of the most recent add. Target: 3-5R on full position. Never let a pyramided position turn into a loss.",
        regimes=["trending_up", "trending_down"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="medium",
        volume_preference="normal",
        win_rate_estimate="40-50%",
        risk_reward="1:3 to 1:5",
        key_indicators=["profit_target_levels", "trend_confirmation", "support_resistance"],
        strengths="Maximizes winners. Reduces initial risk. Only adds when trade is working. Phenomenal R:R.",
        weaknesses="Lower win rate (many adds get stopped at breakeven). Requires discipline. Overcomplicates simple trades.",
    ),

    Strategy(
        name="Correlation-Based Position Sizing",
        category="risk_management",
        description="Reduce position size when portfolio correlation is high. If you're long NQ and QQQ and AAPL, you're essentially 3x long the same thing. Size each position inversely to its correlation with existing positions.",
        entry_logic="Before entering a new trade, calculate its correlation with existing positions. If correlation > 0.8, reduce the new position by 50%. If adding a hedge (correlation < -0.5), normal or increased size is fine. Track aggregate portfolio delta, not just individual positions.",
        exit_logic="N/A — this is a sizing framework, not an entry/exit signal. Apply to all strategies.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="N/A (sizing)",
        risk_reward="N/A (sizing)",
        key_indicators=["correlation_matrix", "portfolio_delta", "position_count"],
        strengths="Prevents concentrated risk. Professional risk management. Reduces drawdowns.",
        weaknesses="Requires real-time correlation data. Over-diversification reduces returns. Complex to implement.",
    ),

    Strategy(
        name="Volatility-Adjusted Stop",
        category="risk_management",
        description="Set stop distances based on ATR rather than fixed ticks. In volatile markets, wider stops prevent premature stop-outs. In quiet markets, tighter stops reduce risk.",
        entry_logic="For any trade, set stop = 1.5-2x ATR(14) on the 5-minute timeframe. In low-vol regimes (ATR < 5 on MNQ), use 1.5x ATR. In high-vol regimes (ATR > 15), use 2x ATR. Adjust target proportionally to maintain R:R ratio.",
        exit_logic="Stop: 1.5-2x ATR from entry. Target: 1.5-3x the stop distance. Trail stop at 1x ATR once in profit. Never use fixed-tick stops — always ATR-adjusted.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="N/A (risk management)",
        risk_reward="1:1.5 to 1:2",
        key_indicators=["atr", "volatility_regime", "stop_distance"],
        strengths="Adapts to market conditions. Prevents stop-outs in volatile markets. Professional standard.",
        weaknesses="Wider stops = larger dollar risk per trade. Must adjust position size to compensate. ATR can lag sudden vol changes.",
    ),

    Strategy(
        name="Time-Based Exit (Time Stop)",
        category="risk_management",
        description="Exit trades that haven't worked within a predefined time window. If the thesis isn't playing out, the edge has decayed. Time stops prevent holding dead trades.",
        entry_logic="N/A — this is an exit framework. For scalps: 3-5 minute time stop. For day trades: 30-45 minute time stop. For swing entries: end-of-day time stop. If the trade is flat (within 0.5R of entry) after the time stop, exit at market.",
        exit_logic="Exit at the time stop regardless of P&L if trade hasn't reached 1R profit. Exception: if trade is within 2 ticks of target AND momentum is with you, extend by 50% of the time stop.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="N/A (exit rule)",
        risk_reward="N/A (exit rule)",
        key_indicators=["hold_time", "time_since_entry", "pnl_vs_target"],
        strengths="Prevents dead money. Frees capital for better setups. Enforces discipline. Reduces opportunity cost.",
        weaknesses="May exit right before the move. Different strategies need different time stops. Requires strategy-specific tuning.",
    ),

    Strategy(
        name="Equity Curve Trading",
        category="risk_management",
        description="Trade your own equity curve — when your system is on a losing streak (below its 10-trade MA), reduce size or pause. When on a winning streak (above MA), increase size. Meta-strategy for managing drawdowns.",
        entry_logic="Track a 10-trade moving average of your equity curve. When equity is above the 10-trade MA: trade full size (system is in sync with market). When below: reduce to 50% size or pause entirely. This prevents giving back profits during system-unfriendly regimes.",
        exit_logic="N/A — this is a portfolio management framework. Resume full size when equity crosses back above its 10-trade MA.",
        regimes=["any"],
        time_windows=["any"],
        asset_class=["nq", "es", "nasdaq_stocks", "large_cap", "any"],
        atr_preference="any",
        volume_preference="any",
        win_rate_estimate="N/A (meta-strategy)",
        risk_reward="N/A (meta-strategy)",
        key_indicators=["equity_curve", "trade_ma", "win_streak", "loss_streak"],
        strengths="Reduces drawdowns dramatically. Data-driven position sizing. Prevents emotional overtrading after losses.",
        weaknesses="Can miss the recovery (going full size after the losing streak ends). Requires sufficient trade history. Small sample sizes are noisy.",
    ),
]


# ═══════════════════════════════════════════════════════════════════════
#  LOADER LOGIC
# ═══════════════════════════════════════════════════════════════════════

def load_strategies(reset: bool = False):
    """Load all strategies into KnowledgeRAG."""
    from core.knowledge_rag import KnowledgeRAG

    rag = KnowledgeRAG()
    if not rag._initialized:
        logger.error("KnowledgeRAG not initialized — is chromadb installed?")
        return 0

    if reset:
        # Clear existing strategies and re-seed rules
        try:
            import chromadb
            client = chromadb.PersistentClient(path=rag._db_path)
            client.delete_collection("trading_knowledge")
            rag._collection = client.create_collection(
                name="trading_knowledge",
                metadata={"hnsw:space": "cosine"},
            )
            rag._seed_knowledge()
            logger.info("[LOADER] Reset collection and re-seeded rules")
        except Exception as e:
            logger.error(f"[LOADER] Reset failed: {e}")
            return 0

    loaded = 0
    skipped = 0
    for s in STRATEGIES:
        sid = f"strat_{s.name.lower().replace(' ', '_').replace('/', '_')[:60]}"
        doc = _build_document(s)
        meta = _build_metadata(s)

        if rag.add_strategy(sid, s.name, doc, meta):
            loaded += 1
        else:
            skipped += 1

    total = rag._collection.count() if rag._collection else 0
    strat_count = rag.get_strategy_count()
    logger.info(f"[LOADER] Loaded {loaded} new strategies ({skipped} already existed). "
                f"Total entries: {total} ({strat_count} strategies)")
    return loaded


def list_strategies():
    """List all loaded strategies."""
    from core.knowledge_rag import KnowledgeRAG
    rag = KnowledgeRAG()
    if not rag._initialized:
        print("KnowledgeRAG not initialized")
        return

    try:
        results = rag._collection.get(where={"type": "strategy"})
        if not results or not results["ids"]:
            print("No strategies loaded. Run: python -m tools.strategy_loader")
            return

        print(f"\n{'='*70}")
        print(f"  LOADED STRATEGIES ({len(results['ids'])} total)")
        print(f"{'='*70}\n")

        for meta in sorted(results["metadatas"], key=lambda m: m.get("category", "")):
            cat = meta.get("category", "?")
            name = meta.get("title", "?")
            regimes = meta.get("regimes", "")
            atr = meta.get("atr_preference", "")
            print(f"  [{cat:16s}] {name:40s} regimes={regimes:30s} ATR={atr}")
    except Exception as e:
        print(f"Error: {e}")


def query_strategies(question: str, n: int = 5):
    """Query strategy knowledge base."""
    from core.knowledge_rag import KnowledgeRAG
    rag = KnowledgeRAG()
    results = rag.query_strategies(question, n_results=n)

    if not results:
        print(f"No strategies found for: '{question}'")
        return

    print(f"\n{'='*70}")
    print(f"  STRATEGIES MATCHING: '{question}'")
    print(f"{'='*70}\n")

    for i, r in enumerate(results, 1):
        print(f"  #{i} [{r['category']}] {r['title']} (relevance: {r['relevance']:.3f})")
        print(f"     Regimes: {r['regimes']}")
        print(f"     ATR: {r['atr_preference']}  |  Assets: {r['asset_class']}")
        print(f"     Windows: {r['time_windows']}")
        # Print first 200 chars of content
        content = r["content"]
        desc_start = content.find("Description: ")
        if desc_start >= 0:
            desc = content[desc_start+13:]
            desc_end = desc.find("\n")
            print(f"     {desc[:desc_end][:120]}")
        print()


def show_stats():
    """Show category and regime breakdown."""
    from core.knowledge_rag import KnowledgeRAG
    rag = KnowledgeRAG()
    if not rag._initialized:
        print("KnowledgeRAG not initialized")
        return

    try:
        results = rag._collection.get(where={"type": "strategy"})
        if not results or not results["ids"]:
            print("No strategies loaded.")
            return

        categories = {}
        regimes = {}
        assets = {}
        for meta in results["metadatas"]:
            cat = meta.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
            for r in meta.get("regimes", "").split(","):
                r = r.strip()
                if r:
                    regimes[r] = regimes.get(r, 0) + 1
            for a in meta.get("asset_class", "").split(","):
                a = a.strip()
                if a:
                    assets[a] = assets.get(a, 0) + 1

        rules = rag._collection.count() - len(results["ids"])

        print(f"\n{'='*50}")
        print(f"  STRATEGY KNOWLEDGE BASE STATS")
        print(f"{'='*50}")
        print(f"\n  Total entries: {rag._collection.count()}")
        print(f"  Rules: {rules}")
        print(f"  Strategies: {len(results['ids'])}")

        print(f"\n  BY CATEGORY:")
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            print(f"    {cat:20s}: {count}")

        print(f"\n  BY REGIME:")
        for reg, count in sorted(regimes.items(), key=lambda x: -x[1]):
            print(f"    {reg:20s}: {count}")

        print(f"\n  BY ASSET CLASS:")
        for asset, count in sorted(assets.items(), key=lambda x: -x[1]):
            print(f"    {asset:20s}: {count}")
        print()

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phoenix Strategy Knowledge Loader")
    parser.add_argument("--list", action="store_true", help="List loaded strategies")
    parser.add_argument("--query", type=str, help="Query strategies by description")
    parser.add_argument("--stats", action="store_true", help="Show category/regime breakdown")
    parser.add_argument("--reset", action="store_true", help="Clear and reload all entries")
    parser.add_argument("-n", type=int, default=5, help="Number of results for --query")
    args = parser.parse_args()

    if args.list:
        list_strategies()
    elif args.query:
        query_strategies(args.query, n=args.n)
    elif args.stats:
        show_stats()
    else:
        count = load_strategies(reset=args.reset)
        print(f"\nLoaded {count} strategies. Use --stats or --query to explore.")
