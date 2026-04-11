"""
Phoenix Bot — Phase 4C: Session Debriefer

Runs at session close. Reads today's JSONL history log and sends it
to Claude for a coaching-style debrief. Writes output to:
    logs/ai_debrief_YYYY-MM-DD.txt

What it analyzes:
  - Trade entries & exits (P&L, strategy, confluences, timing)
  - Signals that were skipped or blocked (missed opportunities?)
  - Regime transitions and how the bot adapted
  - Risk management decisions (cooloff, recovery mode)
  - Market conditions vs. strategy performance

Output style: warm, constructive coaching — not cold analytics.
Think "trading journal mentor" not "spreadsheet".

Non-blocking: if Claude is unreachable, logs a warning and exits.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, date
from typing import Optional

from agents.ai_client import ask_gemini

logger = logging.getLogger("SessionDebriefer")

# Directories
HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "history")
DEBRIEF_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


# ─── JSONL Reader ───────────────────────────────────────────────────

def load_session_events(target_date: date = None, bot_name: str = "prod") -> list[dict]:
    """
    Load all events from a day's JSONL history file.

    Args:
        target_date: Date to load (default: today)
        bot_name: Bot name suffix in filename (prod, lab)

    Returns:
        List of event dicts, ordered chronologically
    """
    if target_date is None:
        target_date = date.today()

    filename = f"{target_date}_{bot_name}.jsonl"
    filepath = os.path.join(HISTORY_DIR, filename)

    if not os.path.exists(filepath):
        logger.warning(f"No history file found: {filepath}")
        return []

    events = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Bad JSON on line {line_num}: {e}")

    logger.info(f"Loaded {len(events)} events from {filename}")
    return events


# ─── Event Summarizer ──────────────────────────────────────────────

def summarize_session(events: list[dict]) -> dict:
    """
    Crunch the raw events into a structured summary for Claude.
    This keeps the prompt focused and within token limits.
    """
    summary = {
        "total_events": len(events),
        "bars_1m": 0,
        "bars_5m": 0,
        "evals": 0,
        "trades": [],
        "signals_generated": 0,
        "signals_skipped": 0,
        "signals_blocked": 0,
        "regimes_seen": set(),
        "session_summary": None,
        "eval_details": [],     # Last N evals with signal info
        "regime_transitions": [],
    }

    last_regime = None

    for evt in events:
        event_type = evt.get("event", "")

        if event_type == "bar":
            tf = evt.get("timeframe", "")
            if tf == "1m":
                summary["bars_1m"] += 1
            elif tf == "5m":
                summary["bars_5m"] += 1

            # Track regime transitions
            regime = evt.get("regime", "")
            if regime and regime != last_regime:
                summary["regime_transitions"].append({
                    "time": evt.get("ts", ""),
                    "from": last_regime or "START",
                    "to": regime,
                })
                last_regime = regime
            if regime:
                summary["regimes_seen"].add(regime)

        elif event_type == "eval":
            summary["evals"] += 1
            strategies = evt.get("strategies", [])
            best = evt.get("best_signal")

            for strat in strategies:
                result = strat.get("result", "")
                if result == "SIGNAL":
                    summary["signals_generated"] += 1
                elif result == "SKIP":
                    summary["signals_skipped"] += 1
                elif result == "BLOCKED":
                    summary["signals_blocked"] += 1

            # Keep last 50 evals for context (trim for token budget)
            if len(summary["eval_details"]) < 50:
                summary["eval_details"].append({
                    "ts": evt.get("ts", ""),
                    "regime": evt.get("regime", ""),
                    "risk_blocked": evt.get("risk_blocked", False),
                    "best_signal": best,
                    "strategy_results": [
                        {"name": s.get("name", ""), "result": s.get("result", ""),
                         "reason": s.get("reason", "")}
                        for s in strategies
                    ],
                    "price": evt.get("price"),
                    "cvd": evt.get("cvd"),
                    "atr_5m": evt.get("atr_5m"),
                })

        elif event_type == "entry":
            summary["trades"].append({
                "type": "entry",
                "ts": evt.get("ts", ""),
                "direction": evt.get("direction", ""),
                "strategy": evt.get("strategy", ""),
                "reason": evt.get("reason", ""),
                "confluences": evt.get("confluences", []),
                "confidence": evt.get("confidence", 0),
                "entry_score": evt.get("entry_score", 0),
                "price": evt.get("price", 0),
                "stop_price": evt.get("stop_price", 0),
                "target_price": evt.get("target_price", 0),
                "risk_dollars": evt.get("risk_dollars", 0),
                "tier": evt.get("tier", ""),
                "market_vwap": evt.get("market", {}).get("vwap"),
                "market_cvd": evt.get("market", {}).get("cvd"),
                "market_atr": evt.get("market", {}).get("atr_5m"),
                "tf_bias": evt.get("tf_bias"),
            })

        elif event_type == "exit":
            summary["trades"].append({
                "type": "exit",
                "ts": evt.get("ts", ""),
                "direction": evt.get("direction", ""),
                "strategy": evt.get("strategy", ""),
                "entry_price": evt.get("entry_price", 0),
                "exit_price": evt.get("exit_price", 0),
                "pnl_dollars": evt.get("pnl_dollars", 0),
                "pnl_ticks": evt.get("pnl_ticks", 0),
                "exit_reason": evt.get("exit_reason", ""),
                "duration_s": evt.get("duration_s", 0),
                "confluences": evt.get("confluences", []),
            })

        elif event_type == "session_summary":
            summary["session_summary"] = {
                "trade_count": evt.get("trade_count", 0),
                "pnl_today": evt.get("pnl_today", 0),
                "win_rate": evt.get("win_rate", 0),
                "consecutive_losses": evt.get("consecutive_losses", 0),
                "recovery_mode": evt.get("recovery_mode", False),
            }

    # Convert set to list for JSON serialization
    summary["regimes_seen"] = list(summary["regimes_seen"])
    return summary


# ─── Claude Prompt Builder ──────────────────────────────────────────

SYSTEM_PROMPT = """You are Phoenix Bot's trading coach — a warm, insightful mentor who reviews each day's MNQ futures trading session.

Your role:
- Analyze what happened today with the bot's automated trades
- Identify what went well (celebrate wins!)
- Spot patterns in losses or missed opportunities
- Give specific, actionable coaching for tomorrow
- Be encouraging but honest — Jennifer is learning and building this system

Context about the system:
- Phoenix Bot trades MNQ (Micro Nasdaq 100 Futures), $0.50/tick
- It runs during NY session (8:30-10:00 AM CST primary window)
- Strategies: bias_momentum, spring_setup, vwap_pullback, high_precision_only, tick_scalp
- Market regimes shift through the day (OPEN_MOMENTUM is the best window)
- Risk: $2,000 account, $45-50/day max loss, dynamic sizing by entry quality tier
- The bot uses confluences (multiple confirming signals) to filter entries

Write your debrief as a coaching journal entry. Use sections like:
1. Session Overview (quick stats + vibe)
2. Trade-by-Trade Review (what worked, what didn't, and why)
3. Patterns & Observations (recurring themes)
4. Strategy Performance Notes (which strategies earned their keep)
5. Tomorrow's Focus (1-3 specific things to watch)

Keep it conversational and warm — this is a coaching session, not an audit."""


def build_debrief_prompt(summary: dict, target_date: date) -> str:
    """Build the user prompt with today's session data."""
    # Pair entries with their exits for a complete trade picture
    trades_narrative = []
    entries_pending = {}

    for item in summary["trades"]:
        if item["type"] == "entry":
            key = f"{item['direction']}_{item['strategy']}_{item['ts'][:16]}"
            entries_pending[key] = item
            trades_narrative.append(item)
        elif item["type"] == "exit":
            trades_narrative.append(item)

    prompt = f"""# Trading Session Debrief Request — {target_date.strftime('%A, %B %d, %Y')}

## Session Stats
- Bars processed: {summary['bars_1m']} (1m), {summary['bars_5m']} (5m)
- Strategy evaluations: {summary['evals']}
- Signals generated: {summary['signals_generated']}
- Signals skipped: {summary['signals_skipped']}
- Signals blocked by risk: {summary['signals_blocked']}
- Regimes seen: {', '.join(summary['regimes_seen']) if summary['regimes_seen'] else 'None recorded'}
"""

    if summary["session_summary"]:
        ss = summary["session_summary"]
        prompt += f"""
## End-of-Day Summary
- Total trades: {ss['trade_count']}
- Day P&L: ${ss['pnl_today']:.2f}
- Win rate: {ss['win_rate']:.0f}%
- Consecutive losses at close: {ss['consecutive_losses']}
- Recovery mode triggered: {'Yes' if ss['recovery_mode'] else 'No'}
"""

    if summary["regime_transitions"]:
        prompt += "\n## Regime Transitions\n"
        for rt in summary["regime_transitions"]:
            prompt += f"- {rt['time'][:19]}: {rt['from']} -> {rt['to']}\n"

    if trades_narrative:
        prompt += "\n## Trades (Chronological)\n"
        prompt += json.dumps(trades_narrative, indent=2, default=str)
    else:
        prompt += "\n## Trades\nNo trades were taken today.\n"

    # Include a sample of eval details for missed opportunity analysis
    if summary["eval_details"]:
        # Only include evals where something interesting happened
        interesting_evals = [
            e for e in summary["eval_details"]
            if e.get("best_signal") or e.get("risk_blocked")
            or any(s.get("result") in ("SIGNAL", "BLOCKED") for s in e.get("strategy_results", []))
        ]
        if interesting_evals:
            prompt += "\n## Notable Strategy Evaluations (signals, blocks, near-misses)\n"
            prompt += json.dumps(interesting_evals[:20], indent=2, default=str)

    prompt += """

Please write today's coaching debrief. Remember:
- Be warm and encouraging — celebrate what went right
- Be specific about what to improve (not vague)
- If no trades happened, analyze WHY and whether that was the right call
- Look for patterns across the signals and market conditions
"""

    return prompt


# ─── Main Debrief Runner ───────────────────────────────────────────

async def run_debrief(
    target_date: date = None,
    bot_name: str = "prod",
    model: str = "gemini-2.0-flash",
) -> Optional[str]:
    """
    Run the full session debrief pipeline.

    Args:
        target_date: Date to debrief (default: today)
        bot_name: Which bot's logs to read (prod, lab)
        model: Claude model to use

    Returns:
        Path to the debrief file, or None if failed
    """
    if target_date is None:
        target_date = date.today()

    logger.info(f"Starting session debrief for {target_date} ({bot_name})")

    # 1. Load events
    events = load_session_events(target_date, bot_name)
    if not events:
        logger.warning(f"No events found for {target_date} — skipping debrief")
        return None

    # 2. Summarize
    summary = summarize_session(events)
    logger.info(f"Summary: {summary['total_events']} events, "
                f"{len(summary['trades'])} trade records, "
                f"{summary['signals_generated']} signals")

    # 3. Build prompt and call Claude
    prompt = build_debrief_prompt(summary, target_date)
    logger.info(f"Prompt built ({len(prompt)} chars), calling Claude...")

    debrief_text = await ask_gemini(
        prompt=prompt,
        system=SYSTEM_PROMPT,
        model_name=model,
        max_tokens=4096,
        temperature=0.4,  # Slightly creative for coaching tone
        timeout_s=90.0,   # Debrief can take a moment
    )

    if not debrief_text:
        logger.error("Claude returned no response for debrief")
        return None

    # 4. Write debrief file
    os.makedirs(DEBRIEF_DIR, exist_ok=True)
    debrief_path = os.path.join(DEBRIEF_DIR, f"ai_debrief_{target_date}.txt")

    header = (
        f"{'=' * 60}\n"
        f"  PHOENIX BOT — SESSION DEBRIEF\n"
        f"  Date: {target_date.strftime('%A, %B %d, %Y')}\n"
        f"  Bot: {bot_name}\n"
        f"  Model: {model}\n"
        f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'=' * 60}\n\n"
    )

    with open(debrief_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(debrief_text)
        f.write(f"\n\n{'=' * 60}\n")
        f.write(f"  Session Stats: {summary['total_events']} events | "
                f"{len(summary['trades'])} trades | "
                f"{summary['signals_generated']} signals\n")
        f.write(f"{'=' * 60}\n")

    logger.info(f"Debrief written to {debrief_path} ({len(debrief_text)} chars)")
    return debrief_path


# ─── CLI Entry Point ───────────────────────────────────────────────

async def main():
    """Run debrief from command line."""
    import argparse

    parser = argparse.ArgumentParser(description="Phoenix Bot Session Debriefer")
    parser.add_argument("--date", type=str, default=None,
                        help="Date to debrief (YYYY-MM-DD, default: today)")
    parser.add_argument("--bot", type=str, default="prod",
                        help="Bot name (prod or lab, default: prod)")
    parser.add_argument("--model", type=str, default="gemini-2.0-flash",
                        help="Gemini model to use")
    args = parser.parse_args()

    # Configure logging for CLI
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    # Load .env if available
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    target = date.fromisoformat(args.date) if args.date else None
    path = await run_debrief(target_date=target, bot_name=args.bot, model=args.model)

    if path:
        print(f"\nDebrief saved to: {path}")
        print(f"\nPreview:\n{'─' * 40}")
        with open(path, "r") as f:
            print(f.read()[:2000])
            if os.path.getsize(path) > 2000:
                print(f"\n... ({os.path.getsize(path)} chars total)")
    else:
        print("\nNo debrief generated (no data or AI failure)")


if __name__ == "__main__":
    asyncio.run(main())
