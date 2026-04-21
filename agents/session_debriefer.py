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

from agents.ai_client import ask, ask_gemini

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

SYSTEM_PROMPT = """You are Phoenix Bot's trading coach — a warm, insightful mentor who reviews each day's MNQ futures trading session. You think like a professional algo trader with deep market microstructure knowledge AND options gamma flow expertise.

Your role:
- Analyze what happened today with the bot's automated trades
- Identify what went well (celebrate wins!)
- Spot patterns in losses or missed opportunities
- Cross-reference trades against intermarket signals (DXY, yields, VIX, crypto)
- Evaluate whether the bot respected regime boundaries
- Evaluate trades against Menthor Q GEX/options flow data when available
- Give specific, actionable coaching for tomorrow
- Be encouraging but honest — Jen is learning and building this system

Context about the system:
- Phoenix Bot trades MNQ (Micro Nasdaq 100 Futures), $0.50/tick, 0.25 tick size
- It runs during NY session (8:30-10:00 AM CST primary window)
- Strategies: bias_momentum, spring_setup, vwap_pullback, high_precision_only
- 8 market regimes: OPEN_MOMENTUM is the best window (backtest: 100% WR in MID_MORNING)
- PREMARKET_DRIFT historically bleeds money (37.5% WR) — should be cautious there
- Risk: $500-$1500 account, $45/day max loss, dynamic sizing by entry quality tier
- The bot uses confluences (multiple confirming signals) to filter entries

Expert analysis framework:
- Was DXY moving inverse to NQ? (DXY up = NQ bearish)
- Were bond yields spiking? (yields up = growth stocks down = NQ down)
- Was crypto fear/greed aligned with equity sentiment?
- Did Trump post anything market-moving? (tariffs = instant NQ reaction)
- Were there economic releases the bot should have avoided?
- Was the put/call ratio extreme? (contrarian signals)
- Did institutional dark pool flow align with bot's direction?

━━━ MENTHOR Q GEX ANALYSIS — evaluate every session against this ━━━
GEX (Gamma Exposure) determines HOW dealers amplify or suppress price moves:

POSITIVE GEX days: Dealers suppress volatility → mean-revert, fade extremes, tighter stops.
  - Were LONGs taken at/above GEX support levels? Good.
  - Were shorts faded near call resistance? Good.
  - Losing trades that pushed through GEX levels = bot should have exited earlier.

NEGATIVE GEX days: Dealers AMPLIFY moves → follow momentum, wider stops, bigger targets.
  - LONG signals below HVL (High Vol Level) = high risk. Bot should have sat out most longs.
  - SHORT signals below HVL = maximum power. Did the bot capture these?
  - Put wall break = gamma cascade lower. Was the bot short? If not, missed opportunity.
  - LONG above HVL in negative GEX = acceptable with 1.5x stops. Did stops hold?

HVL (High Vol Level): Most important level of the day.
  - Price vs HVL at each trade time = the single biggest filter question
  - HVL reclaim = strong LONG setup (regime shift to positive gamma)
  - HVL loss = strong SHORT setup (regime shift to negative gamma)

Vanna/Charm evaluation:
  - Bearish vanna + bot taking longs = working against dealer selling cascades
  - OPEX week: Were stops wide enough for 1.5x normal moves?

CTA Model: If CTAs were max short and bot was long into a squeeze, that's a great trade.
If CTAs were max short and bot was also short, good alignment.

For each trade in the review, ask:
  1. Was the GEX regime known (positive/negative)?
  2. Was price above or below HVL at entry time?
  3. Did the trade direction align with gamma amplification direction?
  4. Were stops appropriately sized for the GEX regime?
  5. Was the exit near a key GEX level (call/put wall, GEX L1/L2)?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Write your debrief as a coaching journal entry. Use sections like:
1. Session Overview (quick stats + market context)
2. Trade-by-Trade Review (what worked, what didn't, intermarket context + GEX alignment)
3. Menthor Q Analysis (GEX regime, HVL relationship, how dealer flows shaped today)
4. Patterns & Observations (recurring themes, regime analysis)
5. Strategy Performance Notes (which strategies earned their keep)
6. Intermarket Analysis (what the macro picture said vs. what the bot did)
7. Tomorrow's Focus (1-3 specific things to watch, upcoming MQ levels, economic events)

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
    model: str = "gemini-2.5-flash",
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

    debrief_text = await ask(
        prompt=prompt,
        system=SYSTEM_PROMPT,
        tier="deep",          # Complex analysis — Gemini primary, Grok fallback
        max_tokens=4096,
        temperature=0.4,      # Slightly creative for coaching tone
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
    parser.add_argument("--model", type=str, default="gemini-2.5-flash",
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


# ═══════════════════════════════════════════════════════════════════════
# S7 — 4C Session Debriefer (BaseAgent + Claude Sonnet)
# ═══════════════════════════════════════════════════════════════════════
#
# Post-flatten (16:00 CT, pre-17:00 globex reopen) review. Reads today's
# logs/history/YYYY-MM-DD_sim.jsonl + logs/trade_memory.json, calls
# Claude via AIClient, writes logs/ai_debrief/YYYY-MM-DD.md with the
# five canonical sections (Summary / Wins / Losses / Patterns /
# Questions for Tomorrow). Optional Telegram dispatch default-on.
# ═══════════════════════════════════════════════════════════════════════

from pathlib import Path as _Path
from agents.base_agent import AIClient, BaseAgent
from agents import config as _agent_config

_PROMPTS_DIR = _Path(__file__).resolve().parent / "prompts"
_DEBRIEF_PROMPT_PATH = _PROMPTS_DIR / "debrief.md"
_DEBRIEF_MD_DIR = _Path(__file__).resolve().parent.parent / "logs" / "ai_debrief"
_HISTORY_DIR = _Path(__file__).resolve().parent.parent / "logs" / "history"
_TRADE_MEMORY_PATH = _Path(__file__).resolve().parent.parent / "logs" / "trade_memory.json"

_REQUIRED_SECTIONS = (
    "## Summary",
    "## Wins",
    "## Losses",
    "## Patterns",
    "## Questions for Tomorrow",
)


def _load_jsonl(path: _Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _load_trade_memory(path: _Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_payload(events: list[dict], trade_memory: dict, target_date: date) -> dict:
    """Structured input payload for the Claude prompt."""
    summary = summarize_session(events)
    # Per-strategy P&L + confluence worked/failed tallies
    per_strategy: dict[str, dict] = {}
    confluences_worked: dict[str, int] = {}
    confluences_failed: dict[str, int] = {}
    for t in summary["trades"]:
        if t["type"] != "exit":
            continue
        strat = t.get("strategy") or "unknown"
        ps = per_strategy.setdefault(strat, {"n": 0, "pnl": 0.0, "wins": 0})
        ps["n"] += 1
        pnl = float(t.get("pnl_dollars") or 0)
        ps["pnl"] += pnl
        if pnl > 0:
            ps["wins"] += 1
            for c in t.get("confluences") or []:
                confluences_worked[c] = confluences_worked.get(c, 0) + 1
        else:
            for c in t.get("confluences") or []:
                confluences_failed[c] = confluences_failed.get(c, 0) + 1

    return {
        "date": target_date.isoformat(),
        "stats": {
            "total_events": summary["total_events"],
            "evals": summary["evals"],
            "signals_generated": summary["signals_generated"],
            "signals_skipped": summary["signals_skipped"],
            "signals_blocked": summary["signals_blocked"],
        },
        "session_summary": summary["session_summary"],
        "regime_distribution": summary["regimes_seen"],
        "regime_transitions": summary["regime_transitions"],
        "trades": summary["trades"],
        "per_strategy_pnl": per_strategy,
        "confluences_worked": confluences_worked,
        "confluences_failed": confluences_failed,
        "trade_memory_tail": (trade_memory.get("trades", [])[-20:]
                              if isinstance(trade_memory, dict) else []),
    }


def _fallback_markdown(payload: dict, reason: str) -> str:
    """Deterministic fallback when Claude is unavailable. Includes all
    five required sections so downstream consumers never crash."""
    ss = payload.get("session_summary") or {}
    lines = [
        f"# Phoenix Session Debrief — {payload['date']}",
        "",
        f"_AI unavailable ({reason}); deterministic fallback emitted._",
        "",
        "## Summary",
        f"- Trades: {ss.get('trade_count', 0)}  |  P&L: ${ss.get('pnl_today', 0):.2f}  "
        f"|  Win rate: {ss.get('win_rate', 0):.0f}%",
        f"- Signals generated/skipped/blocked: "
        f"{payload['stats']['signals_generated']}/"
        f"{payload['stats']['signals_skipped']}/"
        f"{payload['stats']['signals_blocked']}",
        f"- Regimes seen: {', '.join(payload['regime_distribution']) or 'none'}",
        "",
        "## Wins",
    ]
    wins = [t for t in payload["trades"]
            if t["type"] == "exit" and float(t.get("pnl_dollars") or 0) > 0]
    if wins:
        for t in wins:
            lines.append(f"- {t.get('ts','')[:16]} {t.get('strategy','?')} "
                         f"+${float(t.get('pnl_dollars',0)):.2f}")
    else:
        lines.append("- No winning exits recorded.")
    lines.append("")
    lines.append("## Losses")
    losses = [t for t in payload["trades"]
              if t["type"] == "exit" and float(t.get("pnl_dollars") or 0) <= 0]
    if losses:
        for t in losses:
            lines.append(f"- {t.get('ts','')[:16]} {t.get('strategy','?')} "
                         f"${float(t.get('pnl_dollars',0)):.2f} "
                         f"({t.get('exit_reason','')})")
    else:
        lines.append("- No losing exits recorded.")
    lines.append("")
    lines.append("## Patterns")
    for strat, ps in (payload.get("per_strategy_pnl") or {}).items():
        lines.append(f"- {strat}: {ps['n']} trades, "
                     f"{ps['wins']} wins, ${ps['pnl']:.2f} net")
    if not payload.get("per_strategy_pnl"):
        lines.append("- No per-strategy activity to report.")
    lines.append("")
    lines.append("## Questions for Tomorrow")
    lines.append("- Was today's regime distribution typical? Should filters tighten?")
    lines.append("- Any strategies silent all day that should have fired?")
    lines.append("")
    return "\n".join(lines)


def _ensure_all_sections(md: str, payload: dict, reason: str) -> str:
    """Guarantee all five required sections exist in output."""
    if all(s in md for s in _REQUIRED_SECTIONS):
        return md
    # Missing sections — fall back to deterministic template but keep
    # the AI text at the top for context.
    fb = _fallback_markdown(payload, f"incomplete-sections:{reason}")
    return md.rstrip() + "\n\n---\n\n" + fb


class SessionDebriefer(BaseAgent):
    """S7 — 4C post-flatten session review. Claude-powered, safe-by-default."""

    name = "session_debriefer"

    def __init__(
        self,
        client: Optional[AIClient] = None,
        *,
        history_dir: _Path = _HISTORY_DIR,
        debrief_dir: _Path = _DEBRIEF_MD_DIR,
        trade_memory_path: _Path = _TRADE_MEMORY_PATH,
        prompt_path: _Path = _DEBRIEF_PROMPT_PATH,
    ) -> None:
        super().__init__(client=client)
        self.history_dir = _Path(history_dir)
        self.debrief_dir = _Path(debrief_dir)
        self.trade_memory_path = _Path(trade_memory_path)
        self.prompt_path = _Path(prompt_path)

    def _history_file(self, target_date: date, bot_name: str) -> _Path:
        return self.history_dir / f"{target_date}_{bot_name}.jsonl"

    async def run(
        self,
        ctx: Any = None,
        *,
        target_date: Optional[date] = None,
        bot_name: str = "sim",
        dispatch_telegram: bool = True,
    ) -> Optional[str]:
        """Generate today's debrief. Returns the output file path or None."""
        if target_date is None:
            target_date = date.today()

        hist_path = self._history_file(target_date, bot_name)
        events = _load_jsonl(hist_path)
        if not events:
            self.log.warning("no history at %s — skipping debrief", hist_path)
            return None

        trade_memory = _load_trade_memory(self.trade_memory_path)
        payload = _build_payload(events, trade_memory, target_date)

        try:
            system_prompt = self.prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            system_prompt = "You are Phoenix Bot's session coach."
            self.log.warning("prompt file read failed (%s)", e)

        user_prompt = (
            f"# Session Data — {target_date.isoformat()}\n\n"
            f"```json\n{json.dumps(payload, indent=2, default=str)}\n```\n\n"
            f"Produce the five-section Markdown debrief now."
        )

        async def _call() -> Optional[str]:
            return await self.client.ask_claude(
                user_prompt,
                system=system_prompt,
                model=_agent_config.MODEL_CLAUDE_SONNET,
                default=None,
                max_tokens=3000,
                temperature=0.4,
            )

        ai_text = await self.safe_call(_call, default=None, what="claude_debrief")

        if ai_text:
            md = _ensure_all_sections(ai_text, payload, reason="ok")
            reason = "success"
        else:
            md = _fallback_markdown(payload, reason="claude-returned-none")
            reason = "fallback"

        self.debrief_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.debrief_dir / f"{target_date}.md"
        header = (
            f"<!-- Phoenix Bot Session Debrief | {target_date} | "
            f"bot={bot_name} | source={reason} | "
            f"generated={datetime.now().isoformat(timespec='seconds')} -->\n\n"
        )
        out_path.write_text(header + md, encoding="utf-8")
        self.log.info("debrief written to %s (%d chars, %s)",
                      out_path, len(md), reason)

        if dispatch_telegram:
            self._maybe_send_telegram(md, target_date)

        return str(out_path)

    @staticmethod
    def _maybe_send_telegram(md: str, target_date: date) -> None:
        """Fire-and-forget Telegram dispatch. Silent if unavailable."""
        if not (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")):
            return
        try:
            from core import telegram_notifier  # type: ignore
        except Exception:
            return
        try:
            # Trim to Telegram 4096-char limit with safety margin.
            body = md if len(md) < 3800 else md[:3800] + "\n\n…(truncated)"
            text = f"Phoenix Debrief {target_date}\n\n{body}"
            send = getattr(telegram_notifier, "send_sync", None)
            if callable(send):
                send(text, parse_mode="")
        except Exception:
            pass


async def run_session_debrief(
    target_date: Optional[date] = None,
    bot_name: str = "sim",
    client: Optional[AIClient] = None,
    dispatch_telegram: bool = True,
) -> Optional[str]:
    """Convenience entry point for the scheduled-task hook."""
    agent = SessionDebriefer(client=client)
    return await agent.run(
        target_date=target_date,
        bot_name=bot_name,
        dispatch_telegram=dispatch_telegram,
    )
