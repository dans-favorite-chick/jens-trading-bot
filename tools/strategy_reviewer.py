"""
Phoenix Bot — Multi-Persona Strategy Reviewer

Three AI personas review each generated strategy before it goes live:
  1. Risk Auditor — checks for unsafe conditions, missing stops,
     position sizing errors, look-ahead bias
  2. Edge Skeptic — challenges the hypothesis, identifies failure modes,
     asks "why would this edge persist?"
  3. Integration Checker — verifies the code uses correct market_data keys,
     follows BaseStrategy interface, handles missing data gracefully

Pipeline:
  strategy_factory.py -> strategy_reviewer.py -> approved/ or rejected/

Majority vote (2/3) required to approve a strategy.
"""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

logger = logging.getLogger("StrategyReviewer")

VALID_MARKET_KEYS = [
    "price", "bid", "ask", "vwap", "cvd", "ema9", "ema21", "atr_1m", "atr_5m",
    "tf_bias", "tf_votes_bullish", "tf_votes_bearish",
    "bar_delta", "bar_buy_vol", "bar_sell_vol",
    "dom_bid_stack", "dom_ask_stack", "dom_imbalance", "dom_bid_heavy", "dom_ask_heavy",
    "rsi", "rsi_divergence", "htf_patterns",
    "smc_structure", "smc_recent",
    "hmm_regime", "hmm_confidence",
    "candlestick_patterns", "candlestick_confluence",
    "cot", "intermarket", "calendar_risk",
]

SIGNAL_CONSTRUCTOR = (
    "Signal(direction, stop_ticks, target_rr, confidence, "
    "entry_score, strategy, reason, confluences)"
)

# ---------------------------------------------------------------------------
# Persona system prompts
# ---------------------------------------------------------------------------

RISK_AUDITOR_SYSTEM = """You are a senior risk auditor reviewing algorithmic trading strategies for MNQ (Micro Nasdaq-100) futures.

Your job is to find problems that could lose money or blow up an account. You are deliberately conservative.

Check for ALL of the following:
1. Missing or inadequate stop losses — every strategy MUST have a stop_ticks > 0.
2. Position sizing errors — confidence or entry_score outside valid ranges.
3. Look-ahead bias — using future data, referencing bars that haven't formed yet,
   or indexing bars_5m/bars_1m at positions that imply future knowledge.
4. Missing safety checks for null/NaN data — market dict values can be None or NaN.
   The code must use .get() with safe defaults or explicit None checks.
5. Conditions that fire too frequently (overtrading) — e.g., conditions that are almost
   always true, or missing enough confluence filters.
6. Unrealistic assumptions about fills or spreads — no slippage consideration,
   extremely tight stops (< 4 ticks), or targets that assume perfect execution.

Respond with ONLY valid JSON (no markdown fences):
{
    "approved": true or false,
    "score": 0.0 to 1.0 (1.0 = no risk issues found),
    "feedback": "detailed reasoning about what you found",
    "critical_issues": ["list of dealbreaker issues, empty if none"]
}"""

EDGE_SKEPTIC_SYSTEM = """You are a skeptical quantitative researcher reviewing a proposed MNQ futures trading strategy.

Your role is to challenge the strategy's edge. Assume the strategy does NOT work until proven otherwise.

Evaluate:
1. "Why would this edge persist? Who is on the other side of this trade?"
   If you cannot identify a clear counterparty or behavioral reason, be skeptical.
2. Identify specific failure modes — what market conditions would break this strategy?
   (e.g., low volume, high spread, news events, trend reversals, range-bound markets)
3. Check for overfitting to recent data — is the logic too specific to certain price levels
   or too many parameters tuned to recent conditions?
4. Question the confluence logic — does combining these signals create a real edge,
   or is it just adding noise to noise?
5. Rate the hypothesis strength on a 1-10 scale and explain why.

Respond with ONLY valid JSON (no markdown fences):
{
    "approved": true or false,
    "score": 0.0 to 1.0 (1.0 = strong persistent edge),
    "feedback": "detailed reasoning including hypothesis_strength (1-10), failure modes, and edge assessment",
    "critical_issues": ["list of dealbreaker issues, empty if none"]
}"""

INTEGRATION_CHECKER_SYSTEM = """You are a code integration reviewer for a Python trading bot (Phoenix Bot).

You verify that AI-generated strategy code will actually run correctly within the system.

Valid market_data keys (accessed via market dict):
{valid_keys}

Signal constructor:
{signal_constructor}

BaseStrategy interface requires: evaluate(self, market: dict, bars_5m: list, bars_1m: list, session_info: dict) -> Signal | None

Check ALL of the following:
1. Only uses valid market_data keys listed above. Flag any key NOT in the list.
2. Follows BaseStrategy interface — evaluate() returns Signal or None, nothing else.
3. Handles missing data with .get() and safe defaults — direct market["key"] access is a bug.
4. No disallowed operations: imports, exec, eval, open, file I/O, system calls.
5. Code is syntactically valid Python and the method body is under 80 lines.
6. Signal constructor used correctly with all required arguments in the right order:
   Signal(direction, stop_ticks, target_rr, confidence, entry_score, strategy, reason, confluences)
   - direction: "LONG" or "SHORT"
   - stop_ticks: int > 0
   - target_rr: float > 0
   - confidence: 0-100
   - entry_score: 0-60
   - strategy: self.name
   - reason: str
   - confluences: list[str]

Respond with ONLY valid JSON (no markdown fences):
{{
    "approved": true or false,
    "score": 0.0 to 1.0 (1.0 = perfect integration),
    "feedback": "detailed list of integration issues or confirmation that code is clean",
    "critical_issues": ["list of dealbreaker issues, empty if none"]
}}""".format(
    valid_keys=", ".join(VALID_MARKET_KEYS),
    signal_constructor=SIGNAL_CONSTRUCTOR,
)

# Persona weights for aggregate score
WEIGHTS = {"risk_auditor": 0.4, "edge_skeptic": 0.3, "integration_checker": 0.3}

# Default failed result for a single persona
_FAIL_RESULT = {
    "approved": False,
    "score": 0.0,
    "feedback": "Persona call failed — defaulting to NOT approved.",
    "critical_issues": ["API call failed; unable to review."],
}


class StrategyReviewer:
    """Multi-persona AI strategy reviewer. Requires ANTHROPIC_API_KEY."""

    def __init__(self):
        self._api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._last_result: dict | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review(self, strategy_code: str, hypothesis: dict) -> dict:
        """Run all three reviewer personas on a strategy.

        Args:
            strategy_code: Full Python source of the strategy.
            hypothesis: The original hypothesis dict from the factory.

        Returns:
            Dict with approved, votes, scores, feedback, aggregate_score,
            critical_issues, and reviewed_at.
        """
        personas = {
            "risk_auditor": (self._review_risk, strategy_code, hypothesis),
            "edge_skeptic": (self._review_edge, strategy_code, hypothesis),
            "integration_checker": (self._review_integration, strategy_code, hypothesis),
        }

        results: dict[str, dict] = {}

        # Run personas in parallel
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(fn, code, hyp): name
                for name, (fn, code, hyp) in personas.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    logger.error("[REVIEWER] %s raised: %s", name, exc)
                    results[name] = dict(_FAIL_RESULT)

        # Tally votes
        votes = {name: r["approved"] for name, r in results.items()}
        scores = {name: r["score"] for name, r in results.items()}
        feedback = {name: r["feedback"] for name, r in results.items()}
        approvals = sum(1 for v in votes.values() if v)
        approved = approvals >= 2

        # Collect all critical issues
        all_issues: list[str] = []
        for name, r in results.items():
            for issue in r.get("critical_issues", []):
                all_issues.append(f"[{name}] {issue}")

        # If any critical issues exist, force rejection
        if all_issues:
            approved = False

        aggregate = sum(scores[n] * WEIGHTS[n] for n in WEIGHTS)

        for name in results:
            logger.info(
                "[REVIEWER] %s: %s (score=%.2f)",
                name,
                "APPROVED" if votes[name] else "REJECTED",
                scores[name],
            )
        logger.info(
            "[REVIEWER] Final: %s (aggregate=%.2f, votes=%d/3)",
            "APPROVED" if approved else "REJECTED",
            aggregate,
            approvals,
        )

        self._last_result = {
            "approved": approved,
            "votes": votes,
            "scores": scores,
            "feedback": feedback,
            "aggregate_score": round(aggregate, 4),
            "critical_issues": all_issues,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._last_result

    def to_dict(self) -> dict:
        """Serialize last review result for dashboard consumption."""
        if self._last_result is None:
            return {"available": True, "last_review": None}
        return {"available": True, "last_review": self._last_result}

    # ------------------------------------------------------------------
    # Claude API call
    # ------------------------------------------------------------------

    def _call_claude(self, system: str, user: str, max_tokens: int = 1500) -> str:
        """Call Claude API with system + user prompt. SDK first, urllib fallback."""
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self._api_key)
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return response.content[0].text
        except ImportError:
            import urllib.request

            data = json.dumps({
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            resp = urllib.request.urlopen(req, timeout=60)
            result = json.loads(resp.read().decode())
            return result["content"][0]["text"]

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_review_json(text: str) -> dict:
        """Extract JSON from Claude response, handling markdown fences and extras."""
        # Strip markdown code fences if present
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*", "", text)

        # Find outermost { ... }
        start = text.find("{")
        if start == -1:
            raise ValueError("No JSON object found in response")

        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        raw = text[start:end]
        parsed = json.loads(raw)

        # Normalize fields
        return {
            "approved": bool(parsed.get("approved", False)),
            "score": float(min(1.0, max(0.0, parsed.get("score", 0.0)))),
            "feedback": str(parsed.get("feedback", "")),
            "critical_issues": list(parsed.get("critical_issues", [])),
        }

    # ------------------------------------------------------------------
    # Individual persona reviews
    # ------------------------------------------------------------------

    def _review_risk(self, code: str, hypothesis: dict) -> dict:
        """Risk Auditor persona."""
        user_prompt = (
            "Review this MNQ trading strategy for risk issues.\n\n"
            f"HYPOTHESIS:\n{json.dumps(hypothesis, indent=2)}\n\n"
            f"STRATEGY CODE:\n```python\n{code}\n```"
        )
        try:
            raw = self._call_claude(RISK_AUDITOR_SYSTEM, user_prompt)
            return self._parse_review_json(raw)
        except Exception as exc:
            logger.error("[REVIEWER] risk_auditor parse/call failed: %s", exc)
            return dict(_FAIL_RESULT)

    def _review_edge(self, code: str, hypothesis: dict) -> dict:
        """Edge Skeptic persona."""
        user_prompt = (
            "Challenge this MNQ trading strategy's edge.\n\n"
            f"HYPOTHESIS:\n{json.dumps(hypothesis, indent=2)}\n\n"
            f"STRATEGY CODE:\n```python\n{code}\n```"
        )
        try:
            raw = self._call_claude(EDGE_SKEPTIC_SYSTEM, user_prompt)
            return self._parse_review_json(raw)
        except Exception as exc:
            logger.error("[REVIEWER] edge_skeptic parse/call failed: %s", exc)
            return dict(_FAIL_RESULT)

    def _review_integration(self, code: str, hypothesis: dict) -> dict:
        """Integration Checker persona."""
        user_prompt = (
            "Verify this MNQ trading strategy integrates correctly with Phoenix Bot.\n\n"
            f"HYPOTHESIS:\n{json.dumps(hypothesis, indent=2)}\n\n"
            f"STRATEGY CODE:\n```python\n{code}\n```"
        )
        try:
            raw = self._call_claude(INTEGRATION_CHECKER_SYSTEM, user_prompt)
            return self._parse_review_json(raw)
        except Exception as exc:
            logger.error("[REVIEWER] integration_checker parse/call failed: %s", exc)
            return dict(_FAIL_RESULT)
