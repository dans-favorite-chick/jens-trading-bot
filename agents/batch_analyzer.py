"""
Phoenix Bot — Claude Batch API Session Analyzer

Runs AFTER market close. Submits the full day's trade log + market context
as a Claude Batch API request (50% cost discount) and gets back structured
JSON analysis with parameter recommendations.

Safety: never auto-applies recommendations. Logs everything. Enforces bounds.

Usage:
    analyzer = BatchAnalyzer()
    batch_id = await analyzer.submit_session(session_data)
    # ... poll later ...
    result = await analyzer.check_batch(batch_id)
    if result:
        analysis = await analyzer.get_results(batch_id)
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("BatchAnalyzer")

# ─── Directories ──────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(_BASE, "..", "logs", "history")
REC_DIR = os.path.join(_BASE, "..", "logs", "ai_recommendations")

# ─── Optional anthropic SDK ──────────────────────────────────────
try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    anthropic = None  # type: ignore
    _HAS_ANTHROPIC = False
    logger.warning("anthropic SDK not installed — BatchAnalyzer will return empty results")

# ─── System Prompt ───────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a professional MNQ futures trading coach analyzing a day's trading "
    "session. Be specific, quantitative, and actionable. Focus on what the data "
    "shows, not general trading advice."
)

RESPONSE_SCHEMA = {
    "date": "YYYY-MM-DD",
    "overall_grade": "A-F letter grade",
    "key_insights": ["list of 3-5 insights"],
    "patterns_identified": [
        {"pattern_type": "behavioral|setup|timing|exit",
         "description": "...", "frequency": 0, "impact_estimate": "..."}
    ],
    "parameter_adjustments": [
        {"parameter_name": "...", "current_value": 0.0,
         "recommended_value": 0.0, "confidence": 0.0, "reasoning": "..."}
    ],
    "behavioral_flags": ["revenge_trading", "fomo_entry", "premature_exit"],
    "regime_assessment": "...",
    "missed_opportunities": ["..."],
    "recommended_focus": "...",
}


# ─── Pydantic Models ────────────────────────────────────────────
class ParameterRecommendation(BaseModel):
    parameter_name: str
    current_value: float
    recommended_value: float
    confidence: float  # 0-1
    reasoning: str


class PatternIdentified(BaseModel):
    pattern_type: str  # 'behavioral', 'setup', 'timing', 'exit'
    description: str
    frequency: int
    impact_estimate: str


class SessionAnalysis(BaseModel):
    date: str
    overall_grade: str
    key_insights: list[str]
    patterns_identified: list[PatternIdentified]
    parameter_adjustments: list[ParameterRecommendation]
    behavioral_flags: list[str]
    regime_assessment: str
    missed_opportunities: list[str]
    recommended_focus: str


# ─── Parameter Safety Guardrails ─────────────────────────────────
class ParameterUpdater:
    """Validates and applies AI-recommended parameter changes with hard bounds."""

    BOUNDS: dict[str, tuple[float, float]] = {
        "min_confluence": (2.0, 5.0),
        "min_momentum_confidence": (40, 90),
        "risk_per_trade": (5, 20),
        "max_daily_loss": (20, 60),
        "stop_ticks": (4, 40),
        "target_rr": (1.0, 3.0),
    }
    MAX_CHANGE_PCT = 0.20   # Max 20% change per session
    MIN_CONFIDENCE = 0.6    # Reject low-confidence recommendations

    def validate(self, rec: ParameterRecommendation) -> tuple[bool, str]:
        """Returns (approved, reason)."""
        # Confidence gate
        if rec.confidence < self.MIN_CONFIDENCE:
            return False, f"Confidence {rec.confidence:.2f} below threshold {self.MIN_CONFIDENCE}"

        # Bounds check
        bounds = self.BOUNDS.get(rec.parameter_name)
        if bounds is None:
            return False, f"Unknown parameter '{rec.parameter_name}' — not in BOUNDS"

        lo, hi = bounds
        if not (lo <= rec.recommended_value <= hi):
            return False, (f"Recommended {rec.recommended_value} outside bounds "
                           f"[{lo}, {hi}] for {rec.parameter_name}")

        # Max change rate
        if rec.current_value != 0:
            change_pct = abs(rec.recommended_value - rec.current_value) / abs(rec.current_value)
            if change_pct > self.MAX_CHANGE_PCT:
                return False, (f"Change of {change_pct:.0%} exceeds max {self.MAX_CHANGE_PCT:.0%} "
                               f"for {rec.parameter_name}")

        return True, "Approved"

    def apply_recommendations(
        self,
        recs: list[ParameterRecommendation],
        current_params: dict,
    ) -> dict:
        """Apply validated recommendations, return new params dict."""
        new_params = dict(current_params)
        for rec in recs:
            approved, reason = self.validate(rec)
            if approved:
                new_params[rec.parameter_name] = rec.recommended_value
                logger.info(f"Applied: {rec.parameter_name} "
                            f"{rec.current_value} -> {rec.recommended_value} ({reason})")
            else:
                logger.warning(f"Rejected: {rec.parameter_name} "
                               f"{rec.current_value} -> {rec.recommended_value} — {reason}")
        return new_params


# ─── Batch Analyzer ─────────────────────────────────────────────
class BatchAnalyzer:
    """Submit post-session analysis via Claude Batch API (50% cost discount)."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.client = None
        if _HAS_ANTHROPIC and self.api_key:
            self.client = anthropic.Anthropic(api_key=self.api_key)
        elif not _HAS_ANTHROPIC:
            logger.warning("anthropic SDK unavailable — batch calls disabled")
        elif not self.api_key:
            logger.warning("ANTHROPIC_API_KEY not set — batch calls disabled")
        os.makedirs(REC_DIR, exist_ok=True)

    # ─── Build prompt from session data ──────────────────────────
    def _build_prompt(self, session_data: dict) -> str:
        d = session_data
        lines = [
            f"# Session Analysis Request — {d.get('date', 'unknown')}",
            "",
            "## Summary Stats",
            f"- Signals generated: {d.get('signals_generated', 0)}",
            f"- Signals taken: {d.get('signals_taken', 0)}",
            f"- Signals filtered: {d.get('signals_filtered', 0)}",
            f"- Regimes seen: {', '.join(d.get('regimes_seen', []))}",
            "",
        ]
        if d.get("market_summary"):
            lines.append("## Market Summary")
            lines.append(json.dumps(d["market_summary"], indent=2, default=str))
            lines.append("")
        if d.get("strategy_performance"):
            lines.append("## Strategy Performance")
            lines.append(json.dumps(d["strategy_performance"], indent=2, default=str))
            lines.append("")
        if d.get("trades"):
            lines.append("## Trades (full records with MAE/MFE)")
            lines.append(json.dumps(d["trades"], indent=2, default=str))
            lines.append("")
        if d.get("council_votes"):
            lines.append("## Council Votes")
            lines.append(json.dumps(d["council_votes"], indent=2, default=str))
            lines.append("")
        if d.get("near_misses"):
            lines.append("## Near Misses")
            lines.append(json.dumps(d["near_misses"], indent=2, default=str))
            lines.append("")

        lines.append("## Requested Output")
        lines.append("Respond with ONLY a JSON object matching this schema:")
        lines.append(json.dumps(RESPONSE_SCHEMA, indent=2))

        return "\n".join(lines)

    # ─── Submit ──────────────────────────────────────────────────
    async def submit_session(self, session_data: dict) -> str:
        """Submit a day's trading data for batch analysis. Returns batch_id."""
        if not self.client:
            logger.warning("No anthropic client — returning empty batch_id")
            return ""

        prompt = self._build_prompt(session_data)
        logger.info(f"Submitting batch for {session_data.get('date', '?')} "
                     f"({len(prompt)} chars)")

        loop = asyncio.get_event_loop()
        batch = await loop.run_in_executor(None, lambda: self.client.messages.batches.create(
            requests=[
                {
                    "custom_id": f"session-{session_data.get('date', 'unknown')}",
                    "params": {
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 4096,
                        "system": SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                }
            ]
        ))

        batch_id = batch.id
        logger.info(f"Batch submitted: {batch_id}")
        return batch_id

    # ─── Poll ────────────────────────────────────────────────────
    async def check_batch(self, batch_id: str) -> Optional[dict]:
        """Poll for batch completion. Returns None if not done."""
        if not self.client or not batch_id:
            return None

        loop = asyncio.get_event_loop()
        batch = await loop.run_in_executor(
            None, lambda: self.client.messages.batches.retrieve(batch_id)
        )

        if batch.processing_status == "ended":
            logger.info(f"Batch {batch_id} complete")
            return {"status": "ended", "batch_id": batch_id}

        logger.debug(f"Batch {batch_id} status: {batch.processing_status}")
        return None

    # ─── Get Results ─────────────────────────────────────────────
    async def get_results(self, batch_id: str) -> SessionAnalysis:
        """Get structured results from completed batch."""
        if not self.client or not batch_id:
            return self._empty_analysis()

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: list(self.client.messages.batches.results(batch_id))
        )

        if not results:
            logger.error(f"Batch {batch_id} returned no results")
            return self._empty_analysis()

        result = results[0]
        if result.result.type == "errored":
            logger.error(f"Batch result errored: {result.result.error}")
            return self._empty_analysis()

        # Extract text from the message response
        text = ""
        for block in result.result.message.content:
            if hasattr(block, "text"):
                text += block.text

        # Parse JSON from response
        analysis = self._parse_analysis(text)

        # Log recommendations
        self._log_recommendations(analysis)

        return analysis

    # ─── History Loader ──────────────────────────────────────────
    def load_session_from_history(self, date_str: str, bot_name: str = "prod") -> dict:
        """Build session_data from JSONL history files."""
        filepath = os.path.join(HISTORY_DIR, f"{date_str}_{bot_name}.jsonl")
        if not os.path.exists(filepath):
            logger.warning(f"No history file: {filepath}")
            return {"date": date_str, "trades": []}

        events = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        trades, regimes, council_votes = [], set(), []
        signals_gen = signals_taken = signals_filtered = 0
        market_summary: dict = {}

        for evt in events:
            etype = evt.get("event", "")
            if etype == "entry":
                trades.append(evt)
                signals_taken += 1
            elif etype == "exit":
                # Attach exit info to matching trade
                trades.append(evt)
            elif etype == "eval":
                for s in evt.get("strategies", []):
                    r = s.get("result", "")
                    if r == "SIGNAL":
                        signals_gen += 1
                    elif r in ("SKIP", "BLOCKED"):
                        signals_filtered += 1
            elif etype == "bar":
                regime = evt.get("regime")
                if regime:
                    regimes.add(regime)
                # Keep last bar's market data as summary
                market_summary = {
                    "atr_5m": evt.get("atr_5m"),
                    "vwap": evt.get("vwap"),
                    "cvd": evt.get("cvd"),
                    "close": evt.get("close"),
                }
            elif etype == "session_summary":
                market_summary["pnl_today"] = evt.get("pnl_today", 0)
                market_summary["win_rate"] = evt.get("win_rate", 0)

        return {
            "date": date_str,
            "trades": trades,
            "signals_generated": signals_gen,
            "signals_taken": signals_taken,
            "signals_filtered": signals_filtered,
            "regimes_seen": list(regimes),
            "market_summary": market_summary,
            "strategy_performance": {},
            "council_votes": council_votes,
            "near_misses": [],
        }

    # ─── Internal Helpers ────────────────────────────────────────
    def _parse_analysis(self, text: str) -> SessionAnalysis:
        """Parse Claude's JSON response into SessionAnalysis."""
        # Strip markdown code fences if present
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]
        if clean.endswith("```"):
            clean = clean.rsplit("```", 1)[0]
        clean = clean.strip()

        # Find JSON object
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            clean = clean[start:end + 1]

        try:
            data = json.loads(clean)
            return SessionAnalysis(**data)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Failed to parse analysis JSON: {e}")
            return self._empty_analysis()

    def _empty_analysis(self) -> SessionAnalysis:
        return SessionAnalysis(
            date=str(date.today()),
            overall_grade="N/A",
            key_insights=["Analysis unavailable"],
            patterns_identified=[],
            parameter_adjustments=[],
            behavioral_flags=[],
            regime_assessment="No data",
            missed_opportunities=[],
            recommended_focus="Ensure batch API connectivity",
        )

    def _log_recommendations(self, analysis: SessionAnalysis):
        """Write analysis to logs/ai_recommendations/ as JSON."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            path = os.path.join(REC_DIR, f"{analysis.date}_{ts}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(analysis.model_dump(), f, indent=2, default=str)
            logger.info(f"Recommendations logged to {path}")
        except Exception as e:
            logger.error(f"Failed to log recommendations: {e}")


# ─── CLI Entry Point ────────────────────────────────────────────
async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Phoenix Bot — Batch Session Analyzer")
    parser.add_argument("--date", type=str, default=str(date.today()),
                        help="Date to analyze (YYYY-MM-DD)")
    parser.add_argument("--bot", type=str, default="prod")
    parser.add_argument("--poll", type=str, default="",
                        help="Poll an existing batch_id instead of submitting")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    analyzer = BatchAnalyzer()

    if args.poll:
        result = await analyzer.check_batch(args.poll)
        if result:
            analysis = await analyzer.get_results(args.poll)
            print(json.dumps(analysis.model_dump(), indent=2))
        else:
            print("Batch not ready yet.")
        return

    session_data = analyzer.load_session_from_history(args.date, args.bot)
    if not session_data.get("trades"):
        print(f"No trades found for {args.date} — nothing to analyze.")
        return

    batch_id = await analyzer.submit_session(session_data)
    if batch_id:
        print(f"Batch submitted: {batch_id}")
        print(f"Poll with: python -m agents.batch_analyzer --poll {batch_id}")
    else:
        print("Batch submission failed.")


if __name__ == "__main__":
    asyncio.run(main())
