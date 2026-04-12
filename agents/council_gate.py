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

from agents.ai_client import ask_gemini, extract_json

logger = logging.getLogger("CouncilGate")

# ─── Configuration ──────────────────────────────────────────────────

VOTER_TIMEOUT_S = 5.0              # Per-voter timeout
COUNCIL_TIMEOUT_S = 15.0           # Total council timeout
QUORUM = 4                         # Votes needed for directional bias (out of 7)
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
            vix=intel.get("vix", "N/A"),
            news_tier=intel.get("highest_tier", "N/A"),
            news_summary=intel.get("summary", "No news data")[:100],
            econ_events=intel.get("next_event", "No upcoming events"),
            overnight_range=intel.get("overnight_range", "N/A"),
            strategy_performance=strat_perf_str,
            recent_trades=recent_trades_str,
        )

        response = await ask_gemini(
            prompt=prompt,
            system=config["system"],
            model_name=VOTER_MODEL,
            max_tokens=200,
            temperature=0.2,
            timeout_s=VOTER_TIMEOUT_S,
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

    prompt = f"""The Phoenix Bot council of 7 AI analysts just voted on today's MNQ session bias.

## Votes
{vote_summary}

## Market Context
Price: {market.get('price', 0)} | VWAP: {market.get('vwap', 0)}
CVD: {market.get('cvd', 0)} | ATR 5m: {market.get('atr_5m', 0)}
Regime: {market.get('regime', 'UNKNOWN')}

Write a 2-3 sentence synthesis of the council's consensus (or disagreement).
Highlight any notable dissent, especially from the Contrarian.
Be concise — this goes on the dashboard."""

    system = ("You are the chief strategist synthesizing your council's votes. "
              "Be concise, clear, and decisive. Dashboard space is limited.")

    result = await ask_gemini(
        prompt=prompt,
        system=system,
        model_name=ORCHESTRATOR_MODEL,
        max_tokens=300,
        temperature=0.3,
        timeout_s=10.0,
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
    if bullish >= QUORUM:
        bias = "BULLISH"
        vote_str = f"{bullish}/7 BULLISH"
    elif bearish >= QUORUM:
        bias = "BEARISH"
        vote_str = f"{bearish}/7 BEARISH"
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
