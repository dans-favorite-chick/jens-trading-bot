"""
Phoenix Bot — Phase 4 AI Agents

All AI agents for the learning system:
  4A — Council Gate:      7-voter bias consensus at session open
  4B — Pre-Trade Filter:  Fast AI sanity check before every entry
  4C — Session Debriefer: End-of-day coaching debrief via Claude
  4D — Historical Learner: (Phase 2 — not yet built)
  4E — Adaptive Params:    (Phase 2 — not yet built)

Usage in base_bot.py:

    from agents import council_gate, pretrade_filter, session_debriefer

    # At session open:
    council_result = await council_gate.run_council(market, recent_trades)

    # Before each trade:
    verdict = await pretrade_filter.check(signal, market, recent_trades, regime)

    # At session close:
    await session_debriefer.run_debrief(bot_name="prod")
"""

from agents import council_gate
from agents import pretrade_filter
from agents import session_debriefer
from agents import ai_client

__all__ = [
    "council_gate",
    "pretrade_filter",
    "session_debriefer",
    "ai_client",
]
