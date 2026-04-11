"""
Phoenix Bot — Phase 4B: Pre-Trade Filter

Fast AI sanity check before every trade entry.
Called by base_bot.py AFTER a strategy generates a signal but BEFORE
the order is placed.

Design constraints:
  - 3-second hard timeout → defaults to CLEAR (never blocks a trade)
  - Non-blocking: AI failure = CLEAR
  - Returns: CLEAR | CAUTION | SIT_OUT
  - CAUTION reduces position size by 50% but still enters
  - SIT_OUT skips this trade entirely

Uses Claude (fast model) for now. Can swap to Gemini Flash later
for even lower latency.

Integration point in base_bot.py:
    signal = strategy.evaluate(market, bars_5m, bars_1m, session_info)
    if signal:
        verdict = await pretrade_filter.check(signal, market, recent_trades)
        if verdict.action == "SIT_OUT":
            continue  # Skip this trade
        if verdict.action == "CAUTION":
            risk_dollars *= 0.5  # Reduce size
        # ... proceed with entry
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from agents.ai_client import ask_gemini, extract_json

logger = logging.getLogger("PreTradeFilter")

# Hard timeout — AI must respond in 3 seconds or we default to CLEAR
FILTER_TIMEOUT_S = 3.0

# Default model — Gemini Flash for low latency
DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass
class FilterVerdict:
    """Result of the pre-trade AI filter."""
    action: str          # "CLEAR" | "CAUTION" | "SIT_OUT"
    reason: str          # Why this verdict was given
    confidence: float    # 0-100 how confident the AI is
    latency_ms: float    # How long the check took
    source: str          # "ai" or "default" (if timeout/error)


# ─── Prompt Builder ─────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a fast pre-trade risk filter for an MNQ futures trading bot.
You receive a trade signal with market context and must decide in ONE response:

- CLEAR: Trade looks good, proceed normally
- CAUTION: Something is slightly off — enter with reduced size (50%)
- SIT_OUT: Conditions are unfavorable — skip this trade entirely

You MUST respond with ONLY a JSON object (no markdown, no explanation outside the JSON):
{
    "action": "CLEAR" | "CAUTION" | "SIT_OUT",
    "reason": "Brief 1-sentence explanation",
    "confidence": 0-100
}

Key things to watch for:
- Is the signal fighting the dominant trend/regime?
- Has the bot been on a losing streak? (consecutive losses suggest regime mismatch)
- Is volatility (ATR) abnormally high or low for this strategy?
- Are order flow signals (CVD, DOM) confirming or diverging from the signal direction?
- Is the time of day appropriate for this strategy in this regime?
- Was a similar setup just stopped out recently? (revenge trade pattern)

Be decisive. Lean toward CLEAR unless something is clearly wrong.
Speed matters more than perfection — this runs on every signal."""


def build_filter_prompt(
    signal: dict,
    market: dict,
    recent_trades: list[dict],
    regime: str,
) -> str:
    """Build a compact prompt with signal + context."""

    # Summarize recent trade outcomes (last 5)
    recent_summary = []
    for t in recent_trades[-5:]:
        recent_summary.append({
            "strategy": t.get("strategy", ""),
            "direction": t.get("direction", ""),
            "result": t.get("result", ""),
            "pnl": t.get("pnl_dollars", 0),
            "exit_reason": t.get("exit_reason", ""),
        })

    prompt = f"""## Trade Signal to Evaluate

Direction: {signal.get('direction', '')}
Strategy: {signal.get('strategy', '')}
Reason: {signal.get('reason', '')}
Confluences: {json.dumps(signal.get('confluences', []))}
Confidence: {signal.get('confidence', 0)}
Entry Score: {signal.get('entry_score', 0)}
Stop: {signal.get('stop_ticks', 0)} ticks
Target RR: {signal.get('target_rr', 0)}

## Current Market
Regime: {regime}
Price: {market.get('price', 0)}
VWAP: {market.get('vwap', 0)}
EMA9: {market.get('ema9', 0)} | EMA21: {market.get('ema21', 0)}
ATR 5m: {market.get('atr_5m', 0)}
CVD: {market.get('cvd', 0)}
Bar Delta: {market.get('bar_delta', 0)}
DOM Imbalance: {market.get('dom_imbalance', 0.5)} (bid_heavy={market.get('dom_bid_heavy', False)}, ask_heavy={market.get('dom_ask_heavy', False)})
TF Bias: bullish={market.get('tf_votes_bullish', 0)}/4 bearish={market.get('tf_votes_bearish', 0)}/4

## Recent Trades (last 5)
{json.dumps(recent_summary, indent=1)}

Respond with JSON only: {{"action": "...", "reason": "...", "confidence": N}}"""

    return prompt


# ─── Main Filter Function ──────────────────────────────────────────

async def check(
    signal: dict,
    market: dict,
    recent_trades: list[dict],
    regime: str = "UNKNOWN",
    model: str = DEFAULT_MODEL,
) -> FilterVerdict:
    """
    Run pre-trade AI filter on a signal.

    Args:
        signal: The trade signal dict (direction, strategy, confluences, etc.)
        market: Current market snapshot from tick_aggregator
        recent_trades: Recent trade history (from TradeMemory or PositionManager)
        regime: Current market regime string
        model: AI model to use

    Returns:
        FilterVerdict with action, reason, confidence, and latency
    """
    start = time.time()

    try:
        prompt = build_filter_prompt(signal, market, recent_trades, regime)

        response = await ask_gemini(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            model_name=model,
            max_tokens=256,       # Keep response short
            temperature=0.1,      # Deterministic
            timeout_s=FILTER_TIMEOUT_S,
        )

        latency = (time.time() - start) * 1000

        if response is None:
            logger.info(f"[Filter] No response in {latency:.0f}ms — defaulting to CLEAR")
            return FilterVerdict(
                action="CLEAR",
                reason="AI timeout/error — defaulting to safe pass-through",
                confidence=0,
                latency_ms=latency,
                source="default",
            )

        # Parse response
        parsed = extract_json(response)
        if parsed is None:
            logger.warning(f"[Filter] Could not parse response — defaulting to CLEAR")
            return FilterVerdict(
                action="CLEAR",
                reason=f"Unparseable AI response — defaulting to pass-through",
                confidence=0,
                latency_ms=latency,
                source="default",
            )

        action = parsed.get("action", "CLEAR").upper()
        if action not in ("CLEAR", "CAUTION", "SIT_OUT"):
            action = "CLEAR"

        verdict = FilterVerdict(
            action=action,
            reason=parsed.get("reason", "No reason given"),
            confidence=float(parsed.get("confidence", 50)),
            latency_ms=latency,
            source="ai",
        )

        logger.info(f"[Filter] {verdict.action} ({verdict.confidence:.0f}% conf, "
                     f"{verdict.latency_ms:.0f}ms): {verdict.reason}")
        return verdict

    except Exception as e:
        latency = (time.time() - start) * 1000
        logger.error(f"[Filter] Exception: {e} — defaulting to CLEAR")
        return FilterVerdict(
            action="CLEAR",
            reason=f"Filter error: {str(e)[:80]}",
            confidence=0,
            latency_ms=latency,
            source="default",
        )


# ─── Standalone Test ────────────────────────────────────────────────

async def _test():
    """Quick test with fake data."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    fake_signal = {
        "direction": "LONG",
        "strategy": "bias_momentum",
        "reason": "EMA9 > EMA21, 3 TF bullish, CVD rising",
        "confluences": ["ema_bullish", "tf_bias_3+", "cvd_positive", "above_vwap"],
        "confidence": 72,
        "entry_score": 45,
        "stop_ticks": 9,
        "target_rr": 2.0,
    }

    fake_market = {
        "price": 18527.50,
        "vwap": 18520.00,
        "ema9": 18525.00,
        "ema21": 18518.00,
        "atr_5m": 12.5,
        "cvd": 340.0,
        "bar_delta": 85.0,
        "dom_imbalance": 0.58,
        "dom_bid_heavy": False,
        "dom_ask_heavy": False,
        "tf_votes_bullish": 3,
        "tf_votes_bearish": 1,
    }

    fake_recent = [
        {"strategy": "bias_momentum", "direction": "LONG", "result": "WIN",
         "pnl_dollars": 12.50, "exit_reason": "target_hit"},
        {"strategy": "spring_setup", "direction": "SHORT", "result": "LOSS",
         "pnl_dollars": -8.00, "exit_reason": "stop_loss"},
    ]

    verdict = await check(fake_signal, fake_market, fake_recent, regime="OPEN_MOMENTUM")
    print(f"\nVerdict: {verdict.action}")
    print(f"Reason: {verdict.reason}")
    print(f"Confidence: {verdict.confidence}%")
    print(f"Latency: {verdict.latency_ms:.0f}ms")
    print(f"Source: {verdict.source}")


if __name__ == "__main__":
    asyncio.run(_test())
