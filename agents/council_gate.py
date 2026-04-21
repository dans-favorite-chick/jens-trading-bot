"""
Phoenix Bot — Phase 4A: Council Gate

7 AI "council members" vote on the morning session bias at session open.
Each voter analyzes the market from a different perspective, then an
orchestrator aggregates the votes into a final bias call.

Dashboard displays: "Council: BULLISH 6/7" (or BEARISH / NEUTRAL)

Runs:
  - Once at session open (OPEN_MOMENTUM regime start)
  - Re-runs if a major regime shift occurs mid-session

Design:
  - Each voter runs concurrently (asyncio.gather) for speed
  - Each voter has a 5s timeout — missing votes don't block
  - Orchestrator needs 4/7 agreement for a directional bias
  - If <4 agree, bias = NEUTRAL
  - Uses Claude (can swap voters to Gemini Flash later for speed)

Non-blocking: timeout/error → vote counts as ABSTAIN
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from agents.ai_client import ask, ask_gemini, extract_json

logger = logging.getLogger("CouncilGate")

# ─── Configuration ──────────────────────────────────────────────────

VOTER_TIMEOUT_S = 5.0              # Per-voter timeout
COUNCIL_TIMEOUT_S = 15.0           # Total council timeout
QUORUM = 5                         # Votes needed for directional bias (out of 8)
VOTER_MODEL = "gemini-2.5-flash"             # Fast model for voters
ORCHESTRATOR_MODEL = "gemini-2.5-flash"      # Synthesis model


# ─── Data Types ─────────────────────────────────────────────────────

@dataclass
class Vote:
    """A single council member's vote."""
    voter: str           # Voter name/role
    bias: str            # "BULLISH" | "BEARISH" | "NEUTRAL" | "ABSTAIN"
    confidence: float    # 0-100
    reasoning: str       # Brief explanation
    latency_ms: float    # How long this vote took


@dataclass
class CouncilResult:
    """Aggregated council decision."""
    bias: str                   # Final bias: BULLISH | BEARISH | NEUTRAL
    vote_count: str             # e.g., "5/7 BULLISH"
    bullish_votes: int
    bearish_votes: int
    neutral_votes: int
    abstain_votes: int
    votes: list[Vote]           # Individual votes
    summary: str                # Orchestrator's synthesis
    total_latency_ms: float
    timestamp: str


# ─── The 7 Voters ──────────────────────────────────────────────────
# Each voter has a unique "lens" through which they evaluate the market.
# This diversity reduces groupthink and catches angles a single prompt would miss.

VOTER_CONFIGS = [
    {
        "name": "Trend Analyst",
        "system": "You analyze price trends using EMA crossovers, higher highs/lows, and multi-timeframe alignment. Focus purely on trend structure.",
    },
    {
        "name": "Order Flow Reader",
        "system": "You analyze order flow: CVD (cumulative volume delta), per-bar delta, and DOM depth imbalance. You read buying/selling pressure.",
    },
    {
        "name": "VWAP Strategist",
        "system": "You analyze price relative to VWAP — above VWAP = bullish, below = bearish. You also consider VWAP as support/resistance and mean reversion setups.",
    },
    {
        "name": "Volatility Analyst",
        "system": "You analyze ATR levels across timeframes to gauge volatility regime. High ATR = trending, low ATR = choppy. You assess whether current volatility favors directional trades.",
    },
    {
        "name": "Session Context Expert",
        "system": "You specialize in time-of-day patterns for MNQ futures. You know that 8:30-10:00 CST is the highest-edge window, lunch hours are choppy, and institutional repositioning happens late afternoon.",
    },
    {
        "name": "Risk Sentiment Gauge",
        "system": "You evaluate the bot's recent trade performance and risk state. Consecutive losses, recovery mode, and win rates inform whether the bot should be aggressive or defensive.",
    },
    {
        "name": "Contrarian Devil's Advocate",
        "system": "You deliberately look for reasons the majority might be WRONG. You challenge the obvious thesis by looking for divergences, exhaustion signals, and traps. If everyone is bullish, you check for bearish flags.",
    },
    {
        "name": "Gamma Flow Analyst",
        "system": """You are a Menthor Q GEX specialist. You interpret options market structure to predict how dealer hedging flows will affect intraday price action.

Your framework:
- GEX Regime: POSITIVE GEX = dealers suppress volatility (fade extremes, mean-revert). NEGATIVE GEX = dealers amplify moves (follow momentum hard, no fading).
- HVL (High Vol Level): The most important number. Price ABOVE HVL = positive gamma zone (stable, bullish bias from dealer flows). Price BELOW HVL = negative gamma zone (volatile, chaotic, dealers amplify every move).
- DEX (Dealer Delta): Negative GEX + Negative DEX = structurally bearish (dealers amplifying AND selling). Negative GEX + Positive DEX = squeeze risk (dealers forced to buy).
- GEX Levels 1-3: Key support/resistance where dealer gamma exposure is highest. Price tends to stall or reverse at these levels.
- Call Wall / Put Wall: Major options strikes. Call wall = ceiling. Put wall = floor. Breakdown of put wall = gamma cascade lower.
- Vanna flow BEARISH = rising VIX forces dealer selling → bearish amplification. Vanna flow BULLISH = VIX falling → dealer buying.
- Charm flow BULLISH = time decay pushes dealers to buy. BEARISH = time decay pushes dealers to sell (especially near OPEX).
- CTAs max short + negative GEX = potential gamma squeeze setup (explosive LONG opportunity).
- Post-OPEX week: gamma stabilizers expired, expect 1.5x normal moves. Widen all stops.

Vote BULLISH if: positive GEX + price above HVL + vanna/charm bullish + CTA covering
Vote BEARISH if: negative GEX + price below HVL + vanna bearish + put wall breaking
Vote NEUTRAL if: GEX data is UNKNOWN/not filled, or mixed signals""",
    },
]


VOTER_PROMPT_TEMPLATE = """Based on the market data below, what is your bias for today's MNQ trading session?

## Market Snapshot
Price: {price}
VWAP: {vwap}
EMA9 (5m): {ema9} | EMA21 (5m): {ema21}
ATR 1m: {atr_1m} | ATR 5m: {atr_5m} | ATR 15m: {atr_15m}
CVD: {cvd}
Last Bar Delta: {bar_delta}
DOM Imbalance: {dom_imbalance} (bid_heavy={dom_bid_heavy}, ask_heavy={dom_ask_heavy})
TF Bias: 1m={tf_1m}, 5m={tf_5m}, 15m={tf_15m}, 60m={tf_60m}
TF Votes: {tf_bullish}/4 bullish, {tf_bearish}/4 bearish
Current Regime: {regime}
Bars Completed: {bars_1m} (1m), {bars_5m} (5m)

## Market Intelligence
VIX: {vix}
News Tier: {news_tier} ({news_summary})
Economic Calendar: {econ_events}
Overnight Range: {overnight_range}

## Macro & Intermarket
DXY (Dollar): {dxy} | 10Y Yield: {bond_yield}
Crypto Fear/Greed: {crypto_fg} | CNN Fear/Greed: {cnn_fg}
Put/Call Ratio: {put_call}
Trump Sentiment: {trump_sentiment}
Reddit/WSB Hot Tickers: {reddit_hot}
Intermarket: {intermarket}
NQ/ES Relative Strength: {nq_es_strength}
Macro: Fed Funds={fed_rate}%, CPI={cpi}% YoY, Unemployment={unemployment}%

## Menthor Q — Options Dealer Flow (GEX Regime)
GEX Regime: {mq_gex_regime} | Net GEX: {mq_net_gex_bn}B
HVL (High Vol Level): {mq_hvl} | Price vs HVL: {mq_price_vs_hvl}
DEX (Dealer Delta Bias): {mq_dex}
GEX Levels: L1={mq_gex_l1} | L2={mq_gex_l2} | L3={mq_gex_l3}
Call Wall (All): {mq_call_wall} | Put Wall (All): {mq_put_wall}
Call Wall (0DTE): {mq_call_0dte} | Put Wall (0DTE): {mq_put_0dte}
Vanna Flow: {mq_vanna} | Charm Flow: {mq_charm}
CTA Positioning: {mq_cta}
MQ Direction Bias: {mq_direction_bias} | Stop Multiplier: {mq_stop_mult}x
MQ Notes: {mq_notes}

## Expert Assessment
{expert_assessment}

## Strategy Performance (backtest + live history)
{strategy_performance}

## Recent Trade Performance
{recent_trades}

Respond with ONLY this JSON:
{{"bias": "BULLISH" | "BEARISH" | "NEUTRAL", "confidence": 0-100, "reasoning": "1-2 sentences"}}"""


# ─── Voter Execution ────────────────────────────────────────────────

async def _run_voter(config: dict, market: dict, recent_trades_str: str) -> Vote:
    """Run a single council voter."""
    start = time.time()
    name = config["name"]

    try:
        tf_bias = market.get("tf_bias", {})
        intel = market.get("intel", {})
        strat_perf = market.get("strategy_performance", {})
        strat_perf_str = json.dumps(strat_perf, indent=1, default=str)[:500] if strat_perf else "No history yet"

        # Extract nested intel data safely
        news_data = intel.get("news", intel) if isinstance(intel.get("news"), dict) else intel
        cal_data = intel.get("calendar", {})
        ctx_data = intel.get("market_context", {})
        trump_data = intel.get("trump", {})
        reddit_data = intel.get("reddit", {})
        fred_data = intel.get("fred", {})
        crypto_data = intel.get("crypto_fear_greed", {})
        cnn_data = intel.get("cnn_fear_greed", {})
        dxy_data = intel.get("dxy", {})
        bond_data = intel.get("bond_yields", {})
        pc_data = intel.get("put_call", {})
        im_data = intel.get("intermarket", {})
        nq_es_data = intel.get("nq_es_relative_strength", {})

        # Build expert assessment if available
        expert_str = "N/A"
        try:
            from agents.expert_knowledge import interpret_market_conditions
            expert_str = interpret_market_conditions(intel)
        except Exception:
            pass

        # Extract Menthor Q data from market snapshot
        mq_data = market.get("menthorq", {})
        mq_price = market.get("price", 0)
        mq_hvl = mq_data.get("hvl", 0.0)
        mq_price_vs_hvl = (
            f"ABOVE HVL ({mq_price - mq_hvl:+.2f})" if mq_hvl > 0 and mq_price > mq_hvl
            else f"BELOW HVL ({mq_price - mq_hvl:+.2f})" if mq_hvl > 0 and mq_price <= mq_hvl
            else "UNKNOWN (HVL not filled)"
        )
        mq_gex_regime = mq_data.get("gex_regime", "UNKNOWN")
        mq_net_gex_bn = mq_data.get("net_gex_bn", 0.0)
        mq_dex = mq_data.get("dex", "UNKNOWN")
        mq_gex_l1 = mq_data.get("gex_level_1", 0.0) or "N/A"
        mq_gex_l2 = mq_data.get("gex_level_2", 0.0) or "N/A"
        mq_gex_l3 = mq_data.get("gex_level_3", 0.0) or "N/A"
        mq_call_wall = mq_data.get("call_resistance_all", 0.0) or "N/A"
        mq_put_wall = mq_data.get("put_support_all", 0.0) or "N/A"
        mq_call_0dte = mq_data.get("call_resistance_0dte", 0.0) or "N/A"
        mq_put_0dte = mq_data.get("put_support_0dte", 0.0) or "N/A"
        mq_vanna = mq_data.get("vanna", "NEUTRAL")
        mq_charm = mq_data.get("charm", "NEUTRAL")
        mq_cta = mq_data.get("cta_positioning", "NEUTRAL")
        mq_direction_bias = mq_data.get("direction_bias", "NEUTRAL")
        mq_stop_mult = mq_data.get("stop_multiplier", 1.0)
        mq_notes = mq_data.get("notes", "MQ data not yet filled for today")

        # Reddit hot tickers
        reddit_hot = ", ".join(
            [t["ticker"] for t in reddit_data.get("nq_relevant", [])[:5]]
        ) or "N/A"

        # Intermarket summary
        im_summary = "N/A"
        if im_data.get("risk_on") is not None:
            im_summary = "RISK-ON" if im_data.get("risk_on") else (
                "RISK-OFF" if im_data.get("risk_off") else "MIXED"
            )

        prompt = VOTER_PROMPT_TEMPLATE.format(
            price=market.get("price", 0),
            vwap=market.get("vwap", 0),
            ema9=market.get("ema9", 0),
            ema21=market.get("ema21", 0),
            atr_1m=market.get("atr_1m", 0),
            atr_5m=market.get("atr_5m", 0),
            atr_15m=market.get("atr_15m", 0),
            cvd=market.get("cvd", 0),
            bar_delta=market.get("bar_delta", 0),
            dom_imbalance=market.get("dom_imbalance", 0.5),
            dom_bid_heavy=market.get("dom_bid_heavy", False),
            dom_ask_heavy=market.get("dom_ask_heavy", False),
            tf_1m=tf_bias.get("1m", "NEUTRAL"),
            tf_5m=tf_bias.get("5m", "NEUTRAL"),
            tf_15m=tf_bias.get("15m", "NEUTRAL"),
            tf_60m=tf_bias.get("60m", "NEUTRAL"),
            tf_bullish=market.get("tf_votes_bullish", 0),
            tf_bearish=market.get("tf_votes_bearish", 0),
            regime=market.get("regime", "UNKNOWN"),
            bars_1m=market.get("bars_1m", 0),
            bars_5m=market.get("bars_5m", 0),
            vix=intel.get("vix", {}).get("vix", "N/A"),
            news_tier=news_data.get("highest_tier", "N/A"),
            news_summary=news_data.get("summary", "No news")[:100],
            econ_events=cal_data.get("next_event", "No upcoming events"),
            overnight_range=ctx_data.get("overnight_range", "N/A"),
            dxy=f"{dxy_data.get('price', 'N/A')} ({dxy_data.get('trend', '?')})",
            bond_yield=f"{bond_data.get('yield_10y', 'N/A')}% ({bond_data.get('trend', '?')})",
            crypto_fg=f"{crypto_data.get('score', 'N/A')} ({crypto_data.get('classification', '?')})",
            cnn_fg=f"{cnn_data.get('score', 'N/A')} ({cnn_data.get('rating', '?')})",
            put_call=f"{pc_data.get('ratio', 'N/A')} ({pc_data.get('signal', '?')})",
            trump_sentiment=f"{trump_data.get('score', 0):.2f} keywords={trump_data.get('market_keywords', [])}",
            reddit_hot=reddit_hot,
            intermarket=im_summary,
            nq_es_strength=(
                f"{nq_es_data.get('signal', 'N/A')} "
                f"(NQ {nq_es_data.get('nq_change_30m', 0):+.3f}% vs ES {nq_es_data.get('es_change_30m', 0):+.3f}%, "
                f"RS={nq_es_data.get('relative_strength', 0):+.3f}%, "
                f"spread {nq_es_data.get('spread_trend', 'N/A')})"
            ),
            fed_rate=fred_data.get("fed_funds_rate", "N/A"),
            cpi=fred_data.get("cpi_yoy", "N/A"),
            unemployment=fred_data.get("unemployment", "N/A"),
            expert_assessment=expert_str,
            strategy_performance=strat_perf_str,
            recent_trades=recent_trades_str,
            mq_gex_regime=mq_gex_regime,
            mq_net_gex_bn=mq_net_gex_bn,
            mq_hvl=mq_hvl if mq_hvl else "N/A",
            mq_price_vs_hvl=mq_price_vs_hvl,
            mq_dex=mq_dex,
            mq_gex_l1=mq_gex_l1,
            mq_gex_l2=mq_gex_l2,
            mq_gex_l3=mq_gex_l3,
            mq_call_wall=mq_call_wall,
            mq_put_wall=mq_put_wall,
            mq_call_0dte=mq_call_0dte,
            mq_put_0dte=mq_put_0dte,
            mq_vanna=mq_vanna,
            mq_charm=mq_charm,
            mq_cta=mq_cta,
            mq_direction_bias=mq_direction_bias,
            mq_stop_mult=mq_stop_mult,
            mq_notes=mq_notes,
        )

        response = await ask(
            prompt=prompt,
            system=config["system"],
            tier="fast",
            max_tokens=200,
            temperature=0.2,
        )

        latency = (time.time() - start) * 1000

        if response is None:
            return Vote(voter=name, bias="ABSTAIN", confidence=0,
                        reasoning="Timeout/no response", latency_ms=latency)

        parsed = extract_json(response)
        if parsed is None:
            return Vote(voter=name, bias="ABSTAIN", confidence=0,
                        reasoning="Unparseable response", latency_ms=latency)

        bias = parsed.get("bias", "NEUTRAL").upper()
        if bias not in ("BULLISH", "BEARISH", "NEUTRAL"):
            bias = "NEUTRAL"

        return Vote(
            voter=name,
            bias=bias,
            confidence=float(parsed.get("confidence", 50)),
            reasoning=parsed.get("reasoning", "No reasoning given"),
            latency_ms=latency,
        )

    except Exception as e:
        latency = (time.time() - start) * 1000
        logger.warning(f"[Council] Voter '{name}' error: {e}")
        return Vote(voter=name, bias="ABSTAIN", confidence=0,
                    reasoning=f"Error: {str(e)[:60]}", latency_ms=latency)


# ─── Council Orchestrator ──────────────────────────────────────────

def _tally_votes(votes: list[Vote]) -> tuple[int, int, int, int]:
    """Count votes by bias. Returns (bullish, bearish, neutral, abstain)."""
    b = sum(1 for v in votes if v.bias == "BULLISH")
    s = sum(1 for v in votes if v.bias == "BEARISH")
    n = sum(1 for v in votes if v.bias == "NEUTRAL")
    a = sum(1 for v in votes if v.bias == "ABSTAIN")
    return b, s, n, a


async def _orchestrator_synthesis(votes: list[Vote], market: dict) -> str:
    """
    Have the orchestrator (deeper model) synthesize the council's votes
    into a brief summary with rationale.
    """
    vote_summary = "\n".join([
        f"- {v.voter}: {v.bias} ({v.confidence}% conf) — {v.reasoning}"
        for v in votes
    ])

    mq_snap = market.get("menthorq", {})
    mq_summary = (
        f"GEX: {mq_snap.get('gex_regime', 'UNKNOWN')} | "
        f"HVL: {mq_snap.get('hvl', 'N/A')} | "
        f"MQ Bias: {mq_snap.get('direction_bias', 'NEUTRAL')}"
    ) if mq_snap else "MQ data not available"

    prompt = f"""The Phoenix Bot council of {len(VOTER_CONFIGS)} AI analysts just voted on today's MNQ session bias.

## Votes
{vote_summary}

## Market Context
Price: {market.get('price', 0)} | VWAP: {market.get('vwap', 0)}
CVD: {market.get('cvd', 0)} | ATR 5m: {market.get('atr_5m', 0)}
Regime: {market.get('regime', 'UNKNOWN')}
Menthor Q: {mq_summary}

Write a 2-3 sentence synthesis of the council's consensus (or disagreement).
Highlight any notable dissent, especially from the Contrarian or Gamma Flow Analyst.
Be concise — this goes on the dashboard."""

    system = ("You are the chief strategist synthesizing your council's votes. "
              "Be concise, clear, and decisive. Dashboard space is limited.")

    result = await ask(
        prompt=prompt,
        system=system,
        tier="deep",
        max_tokens=300,
        temperature=0.3,
    )

    return result or "Council vote complete. See individual votes for details."


# ─── Main Council Function ─────────────────────────────────────────

async def run_council(
    market: dict,
    recent_trades: list[dict] = None,
) -> CouncilResult:
    """
    Run the full council vote.

    Args:
        market: Current market snapshot from tick_aggregator
        recent_trades: Recent trades from TradeMemory (last 10-20)

    Returns:
        CouncilResult with final bias, all votes, and synthesis
    """
    start = time.time()
    logger.info("[Council] Convening 7-member council for session bias vote...")

    # Format recent trades for the prompt
    if recent_trades:
        recent_str = json.dumps([
            {"strategy": t.get("strategy", ""), "direction": t.get("direction", ""),
             "result": t.get("result", ""), "pnl": t.get("pnl_dollars", 0)}
            for t in recent_trades[-10:]
        ], indent=1)
    else:
        recent_str = "No recent trades available."

    # Run all 7 voters concurrently
    try:
        votes = await asyncio.wait_for(
            asyncio.gather(*[
                _run_voter(config, market, recent_str)
                for config in VOTER_CONFIGS
            ]),
            timeout=COUNCIL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("[Council] Overall timeout — using partial results")
        votes = []

    # Tally
    bullish, bearish, neutral, abstain = _tally_votes(votes)

    # Determine bias (need QUORUM for directional)
    total_voters = len(VOTER_CONFIGS)
    if bullish >= QUORUM:
        bias = "BULLISH"
        vote_str = f"{bullish}/{total_voters} BULLISH"
    elif bearish >= QUORUM:
        bias = "BEARISH"
        vote_str = f"{bearish}/{total_voters} BEARISH"
    else:
        bias = "NEUTRAL"
        vote_str = f"{bullish}B/{bearish}S/{neutral}N/{abstain}A"

    # Log individual votes
    for v in votes:
        logger.info(f"  [{v.voter}] {v.bias} ({v.confidence:.0f}%) "
                     f"in {v.latency_ms:.0f}ms — {v.reasoning[:60]}")

    logger.info(f"[Council] Result: {bias} ({vote_str})")

    # Run orchestrator synthesis (non-blocking — if it fails, we still have the vote)
    try:
        summary = await _orchestrator_synthesis(votes, market)
    except Exception as e:
        logger.warning(f"[Council] Orchestrator error: {e}")
        summary = f"Council voted {vote_str}. Orchestrator unavailable."

    total_latency = (time.time() - start) * 1000

    result = CouncilResult(
        bias=bias,
        vote_count=vote_str,
        bullish_votes=bullish,
        bearish_votes=bearish,
        neutral_votes=neutral,
        abstain_votes=abstain,
        votes=list(votes),
        summary=summary,
        total_latency_ms=total_latency,
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    logger.info(f"[Council] Complete in {total_latency:.0f}ms — {bias} ({vote_str})")
    return result


def council_to_dict(result: CouncilResult) -> dict:
    """Serialize CouncilResult for dashboard display."""
    return {
        "bias": result.bias,
        "vote_count": result.vote_count,
        "bullish": result.bullish_votes,
        "bearish": result.bearish_votes,
        "neutral": result.neutral_votes,
        "abstain": result.abstain_votes,
        "summary": result.summary,
        "latency_ms": round(result.total_latency_ms, 0),
        "timestamp": result.timestamp,
        "votes": [
            {
                "voter": v.voter,
                "bias": v.bias,
                "confidence": v.confidence,
                "reasoning": v.reasoning,
                "latency_ms": round(v.latency_ms, 0),
            }
            for v in result.votes
        ],
    }


# ─── Standalone Test ────────────────────────────────────────────────

async def _test():
    """Quick test with fake market data."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    fake_market = {
        "price": 18527.50,
        "vwap": 18520.00,
        "ema9": 18525.00,
        "ema21": 18518.00,
        "atr_1m": 3.5,
        "atr_5m": 12.5,
        "atr_15m": 22.0,
        "cvd": 340.0,
        "bar_delta": 85.0,
        "dom_imbalance": 0.58,
        "dom_bid_heavy": False,
        "dom_ask_heavy": False,
        "tf_bias": {"1m": "BULLISH", "5m": "BULLISH", "15m": "NEUTRAL", "60m": "BULLISH"},
        "tf_votes_bullish": 3,
        "tf_votes_bearish": 0,
        "bars_1m": 30,
        "bars_5m": 6,
        "regime": "OPEN_MOMENTUM",
    }

    fake_trades = [
        {"strategy": "bias_momentum", "direction": "LONG", "result": "WIN", "pnl_dollars": 15.0},
        {"strategy": "spring_setup", "direction": "SHORT", "result": "LOSS", "pnl_dollars": -8.0},
        {"strategy": "bias_momentum", "direction": "LONG", "result": "WIN", "pnl_dollars": 12.5},
    ]

    result = await run_council(fake_market, fake_trades)

    print(f"\n{'=' * 50}")
    print(f"  COUNCIL RESULT: {result.bias} ({result.vote_count})")
    print(f"{'=' * 50}")
    print(f"\nSynthesis: {result.summary}")
    print(f"\nIndividual votes:")
    for v in result.votes:
        print(f"  {v.voter:25s} | {v.bias:8s} | {v.confidence:3.0f}% | {v.latency_ms:5.0f}ms | {v.reasoning[:50]}")
    print(f"\nTotal time: {result.total_latency_ms:.0f}ms")


if __name__ == "__main__":
    asyncio.run(_test())


# ═════════════════════════════════════════════════════════════════════
# S5 — 4A Council Gate (spec-compliant, built on agents.base_agent)
# ═════════════════════════════════════════════════════════════════════
#
# Spec:
#   - 7 voting personas (trend-follower, mean-reverter, vol-watcher,
#     gamma-reader, intermarket-analyst, session-historian, contrarian)
#   - Voters use Gemini Flash @ temp 0.3 (JSON-mode) via AIClient
#   - Orchestrator uses Gemini Pro → {"verdict","score","summary"}
#   - Runs at 8:30 AM CT session open + on regime shifts
#   - Writes logs/council/YYYY-MM-DD.json
#   - get_current_bias() returns last vote dict (verdict + timestamp)
#   - All AI calls wrapped in safe_call → NEUTRAL default on timeout/error
#   - Tie-break 3-3-1 → NEUTRAL
#
# Coexists with the legacy run_council/council_to_dict API above, which
# S6/S7 bots still consume. The new CouncilGate class is the S5 surface.

from pathlib import Path as _Path
from datetime import datetime as _dt, timezone as _tz

try:
    from agents.base_agent import AIClient as _AIClient, BaseAgent as _BaseAgent
    from agents import config as _agent_cfg
    _HAS_BASE_AGENT = True
except Exception:  # pragma: no cover - defensive
    _AIClient = None  # type: ignore
    _BaseAgent = object  # type: ignore
    _agent_cfg = None  # type: ignore
    _HAS_BASE_AGENT = False


# ─── Spec personas ──────────────────────────────────────────────────

COUNCIL_PERSONAS: list[dict] = [
    {
        "name": "trend-follower",
        "lens": "Weight EMA crossovers, higher-highs/higher-lows, and multi-timeframe trend alignment. Bullish if uptrend intact; bearish if downtrend.",
    },
    {
        "name": "mean-reverter",
        "lens": "Look for overextension vs VWAP / Keltner / RSI extremes. Bullish when oversold and snap-back probable; bearish when overbought.",
    },
    {
        "name": "vol-watcher",
        "lens": "Read ATR regime and VIX. Expanding vol + directional break = follow; compressed vol = fade; spiking VIX from low = risk-off bearish.",
    },
    {
        "name": "gamma-reader",
        "lens": "Interpret Menthor Q GEX / HVL / dealer flow. Positive GEX above HVL = stable bullish; negative GEX below HVL = amplified moves (direction = sign of DEX).",
    },
    {
        "name": "intermarket-analyst",
        "lens": "Cross-asset: DXY, 10Y yields, ES vs NQ relative strength, crypto risk-on/off. Rising yields + strong DXY = bearish equities; NQ outperforming ES = bullish tech.",
    },
    {
        "name": "session-historian",
        "lens": "Compare today's open to analogous setups from the recent trade log. If the pattern historically resolves up, vote BULLISH, etc.",
    },
    {
        "name": "contrarian",
        "lens": "Deliberately challenge the obvious read. If sentiment/positioning is one-sided, fade it. Hunt for exhaustion and traps.",
    },
]

_VOTER_PROMPT_PATH = _Path(__file__).parent / "prompts" / "council_voter.md"
_ORCH_PROMPT_PATH  = _Path(__file__).parent / "prompts" / "council_orchestrator.md"


def _load_prompt(p: _Path, fallback: str) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return fallback


_VOTER_PROMPT_FALLBACK = (
    "You are the {persona_name} on Phoenix Bot's council.\n"
    "Lens: {persona_lens}\n\nMarket:\n{market_json}\n\n"
    "Respond ONLY with JSON: "
    '{{"vote": "BULLISH"|"BEARISH"|"NEUTRAL", "rationale": "<=1 sentence"}}'
)
_ORCH_PROMPT_FALLBACK = (
    "You are the Chief Strategist. Synthesize these 7 votes. Need >=4/7 "
    "for directional verdict; ties or 3-3-1 = NEUTRAL.\n\nVotes:\n{votes_json}\n\n"
    'Respond ONLY with JSON: {{"verdict": "BULLISH"|"BEARISH"|"NEUTRAL", '
    '"score": "N/7", "summary": "<1 sentence>"}}'
)


# ─── Logs path ──────────────────────────────────────────────────────

_PROJECT_ROOT = _Path(__file__).resolve().parent.parent
COUNCIL_LOG_DIR = _Path(os.environ.get("COUNCIL_LOG_DIR", _PROJECT_ROOT / "logs" / "council")) if False else _PROJECT_ROOT / "logs" / "council"

import os as _os  # noqa: E402 — kept local to avoid top-of-file shuffle
COUNCIL_LOG_DIR = _Path(_os.environ.get("COUNCIL_LOG_DIR", str(_PROJECT_ROOT / "logs" / "council")))


def _council_log_path(date_str: Optional[str] = None) -> _Path:
    if date_str is None:
        date_str = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    return COUNCIL_LOG_DIR / f"{date_str}.json"


# ─── Module-level current-bias state ────────────────────────────────

_CURRENT_BIAS: dict = {
    "verdict": "NEUTRAL",
    "score": "0/7",
    "summary": "No council vote yet.",
    "timestamp": None,
}


def get_current_bias() -> dict:
    """Return the last council verdict. Bots consult this as optional filter."""
    return dict(_CURRENT_BIAS)


def _set_current_bias(verdict: str, score: str, summary: str) -> None:
    global _CURRENT_BIAS
    _CURRENT_BIAS = {
        "verdict": verdict,
        "score": score,
        "summary": summary,
        "timestamp": _dt.now(_tz.utc).isoformat(),
    }


# ─── Tally + tie-break ──────────────────────────────────────────────

def _tally_s5(votes: list[dict]) -> tuple[int, int, int]:
    b = sum(1 for v in votes if v.get("vote") == "BULLISH")
    s = sum(1 for v in votes if v.get("vote") == "BEARISH")
    n = sum(1 for v in votes if v.get("vote") == "NEUTRAL")
    return b, s, n


def _deterministic_verdict(votes: list[dict]) -> tuple[str, str]:
    """Local tie-break: >=4/7 wins; otherwise NEUTRAL (incl. 3-3-1)."""
    b, s, n = _tally_s5(votes)
    if b >= 4 and b > s:
        return "BULLISH", f"{b}/7"
    if s >= 4 and s > b:
        return "BEARISH", f"{s}/7"
    majority = max(b, s, n)
    return "NEUTRAL", f"{majority}/7"


# ─── CouncilGate agent ──────────────────────────────────────────────

class CouncilGate(_BaseAgent):  # type: ignore[misc]
    """S5 — 7-voter Gemini Flash council + Gemini Pro orchestrator.

    Usage:
        gate = CouncilGate()
        result = await gate.run({"market": {...}, "trigger": "session_open"})
        bias = get_current_bias()
    """

    name = "council-gate"

    VOTER_TEMPERATURE = 0.3
    VOTER_TIMEOUT_S = 5.0
    ORCH_TIMEOUT_S = 8.0

    def __init__(self, client=None) -> None:
        if not _HAS_BASE_AGENT:
            raise RuntimeError("agents.base_agent not available")
        super().__init__(client=client)
        self.personas = COUNCIL_PERSONAS
        self._voter_prompt = _load_prompt(_VOTER_PROMPT_PATH, _VOTER_PROMPT_FALLBACK)
        self._orch_prompt = _load_prompt(_ORCH_PROMPT_PATH, _ORCH_PROMPT_FALLBACK)

    # ---- Voter -----------------------------------------------------

    async def _vote_one(self, persona: dict, market: dict) -> dict:
        default_vote = {
            "voter": persona["name"],
            "vote": "NEUTRAL",
            "rationale": "default (timeout or error)",
        }

        prompt = self._voter_prompt.format(
            persona_name=persona["name"],
            persona_lens=persona["lens"],
            market_json=json.dumps(market, default=str)[:2000],
        )

        async def _call():
            return await self.client.ask_gemini(
                prompt,
                system=f"You are the {persona['name']} voter. Reply with JSON only.",
                model=_agent_cfg.MODEL_GEMINI_FLASH,
                default=None,
                timeout_s=self.VOTER_TIMEOUT_S,
                temperature=self.VOTER_TEMPERATURE,
                max_tokens=200,
            )

        text = await self.safe_call(_call, default=None, what=f"voter:{persona['name']}")
        if text is None:
            return default_vote

        parsed = _AIClient.parse_json(text, default=None)
        if not isinstance(parsed, dict):
            return default_vote

        vote = str(parsed.get("vote", "NEUTRAL")).upper()
        if vote not in ("BULLISH", "BEARISH", "NEUTRAL"):
            vote = "NEUTRAL"
        return {
            "voter": persona["name"],
            "vote": vote,
            "rationale": str(parsed.get("rationale", ""))[:240],
        }

    # ---- Orchestrator ---------------------------------------------

    async def _orchestrate(self, votes: list[dict]) -> dict:
        # Always compute the deterministic verdict as the default/fallback.
        det_verdict, det_score = _deterministic_verdict(votes)
        default_result = {
            "verdict": det_verdict,
            "score": det_score,
            "summary": f"Council deterministic tally: {det_verdict} ({det_score}).",
        }

        prompt = self._orch_prompt.format(votes_json=json.dumps(votes, indent=2))

        async def _call():
            return await self.client.ask_gemini(
                prompt,
                system="You are the Chief Strategist. Reply with JSON only.",
                model=_agent_cfg.MODEL_GEMINI_PRO,
                default=None,
                timeout_s=self.ORCH_TIMEOUT_S,
                temperature=0.2,
                max_tokens=300,
            )

        text = await self.safe_call(_call, default=None, what="orchestrator")
        if text is None:
            return default_result

        parsed = _AIClient.parse_json(text, default=None)
        if not isinstance(parsed, dict):
            return default_result

        verdict = str(parsed.get("verdict", det_verdict)).upper()
        if verdict not in ("BULLISH", "BEARISH", "NEUTRAL"):
            verdict = det_verdict
        score = str(parsed.get("score", det_score))
        summary = str(parsed.get("summary", default_result["summary"]))[:400]

        # Trust-but-verify: if orchestrator contradicts deterministic tie
        # outcome (3-3-1 or no 4/7 majority), force NEUTRAL per spec.
        b, s, n = _tally_s5(votes)
        if verdict == "BULLISH" and not (b >= 4 and b > s):
            verdict, score = det_verdict, det_score
        elif verdict == "BEARISH" and not (s >= 4 and s > b):
            verdict, score = det_verdict, det_score

        return {"verdict": verdict, "score": score, "summary": summary}

    # ---- Log writer -----------------------------------------------

    def _write_log(self, payload: dict) -> _Path:
        COUNCIL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        p = _council_log_path()
        # Append today's entry to a JSON list for multiple intraday runs.
        existing: list = []
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    existing = data
                elif isinstance(data, dict):
                    existing = [data]
            except Exception:
                existing = []
        existing.append(payload)
        p.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
        return p

    # ---- Public entry ---------------------------------------------

    async def run(self, ctx: Any) -> dict:  # type: ignore[override]
        """Run the council.

        ``ctx`` should be ``{"market": {...}, "trigger": "session_open"|"regime_shift"}``.
        Returns a dict with verdict/score/summary/votes/timestamp/log_path.
        Never raises.
        """
        market = (ctx or {}).get("market", {}) if isinstance(ctx, dict) else {}
        trigger = (ctx or {}).get("trigger", "session_open") if isinstance(ctx, dict) else "session_open"

        votes = await asyncio.gather(
            *[self._vote_one(p, market) for p in self.personas],
            return_exceptions=False,
        )
        votes = list(votes)

        orch = await self._orchestrate(votes)

        payload = {
            "trigger": trigger,
            "timestamp": _dt.now(_tz.utc).isoformat(),
            "verdict": orch["verdict"],
            "score": orch["score"],
            "summary": orch["summary"],
            "votes": votes,
        }

        try:
            log_path = self._write_log(payload)
            payload["log_path"] = str(log_path)
        except Exception as e:
            self.log.warning("[%s] log write failed: %s", self.name, e)
            payload["log_path"] = None

        _set_current_bias(orch["verdict"], orch["score"], orch["summary"])
        return payload


__all__ = [
    # Legacy S5 surface (consumed by bots/base_bot.py)
    "run_council",
    "council_to_dict",
    "CouncilResult",
    "Vote",
    # S5 spec surface
    "CouncilGate",
    "COUNCIL_PERSONAS",
    "get_current_bias",
]
