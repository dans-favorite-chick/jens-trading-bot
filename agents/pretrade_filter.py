"""
Phoenix Bot — S6 / Phase H-4B: Pre-Trade Filter

Single Gemini Flash call before each entry. 3-second hard timeout.
NEVER blocks a trade on AI failure — by default verdict is CLEAR.

Design:
  - Uses S4 infra (``agents.base_agent.AIClient`` / ``BaseAgent``).
  - Hard 3 s timeout. Any timeout / parse failure / exception → CLEAR
    (safe pass-through; see mission S6 spec).
  - Verdict enum: ``CLEAR`` | ``CAUTION`` | ``SIT_OUT``.
  - Per-strategy config key ``ai_filter_mode``:
        "advisory"  — log only, trade always proceeds (default).
        "blocking"  — respect SIT_OUT by skipping the trade.
  - All calls are logged via the base_agent JSONL call log.

Back-compat:
  - Module-level ``check(...)`` function is retained so the existing
    call site in ``bots/base_bot.py`` keeps working. Its return type
    ``FilterVerdict`` exposes the legacy ``action`` field (alias of
    ``verdict``) plus ``reason``, ``confidence``, ``latency_ms``,
    ``source``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from agents import config as agent_config
from agents.base_agent import AIClient, BaseAgent

logger = logging.getLogger("agents.pretrade_filter")


# ─── Constants ───────────────────────────────────────────────────────────

#: Hard timeout budget for the AI call (seconds). Mission spec: 3s.
FILTER_TIMEOUT_S: float = 3.0

#: Valid verdict strings.
_VALID_VERDICTS = ("CLEAR", "CAUTION", "SIT_OUT")

#: Default verdict used on any failure (timeout, parse, exception, missing key).
DEFAULT_VERDICT: str = "CLEAR"

#: Default per-strategy filter mode. "advisory" = log only.
DEFAULT_FILTER_MODE: str = "advisory"


class Verdict(str, Enum):
    CLEAR = "CLEAR"
    CAUTION = "CAUTION"
    SIT_OUT = "SIT_OUT"


# ─── Result dataclass (legacy-compatible) ────────────────────────────────

@dataclass
class FilterVerdict:
    """Pre-trade filter result.

    ``verdict`` is the canonical S6 field. ``action`` is a legacy alias
    kept for compatibility with the existing base_bot integration.
    """
    verdict: str              # "CLEAR" | "CAUTION" | "SIT_OUT"
    reason: str
    confidence: float
    latency_ms: float
    source: str               # "ai" | "default"

    @property
    def action(self) -> str:  # legacy alias
        return self.verdict


# ─── Prompt loading ──────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "pretrade.md"


def _load_system_prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:  # pragma: no cover
        logger.warning("pretrade system prompt missing (%s) — using terse fallback", e)
        return (
            "You are a fast pre-trade risk filter. Respond ONLY with JSON "
            '{"verdict":"CLEAR|CAUTION|SIT_OUT","reason":"...","confidence":0-100}.'
        )


def build_user_prompt(
    signal: dict[str, Any],
    market: dict[str, Any],
    recent_trades: list[dict[str, Any]],
    *,
    regime: str = "UNKNOWN",
    strategy_context: str = "",
) -> str:
    """Build the compact per-call user prompt."""
    recent = [
        {
            "strategy": t.get("strategy", ""),
            "direction": t.get("direction", ""),
            "result": t.get("result", ""),
            "pnl": t.get("pnl_dollars", t.get("pnl", 0)),
            "exit_reason": t.get("exit_reason", ""),
        }
        for t in (recent_trades or [])[-5:]
    ]
    ctx_block = f"\n## Strategy context\n{strategy_context}\n" if strategy_context else ""
    return (
        "## Signal\n"
        f"{json.dumps(signal, default=str)}\n\n"
        "## Market snapshot\n"
        f"regime={regime}\n"
        f"{json.dumps(market, default=str)}\n"
        f"{ctx_block}"
        "\n## Recent trades (last 5, newest last)\n"
        f"{json.dumps(recent, default=str)}\n\n"
        'Respond with ONLY: {"verdict":"CLEAR|CAUTION|SIT_OUT","reason":"...","confidence":0-100}'
    )


# ─── The agent ───────────────────────────────────────────────────────────

class PretradeFilter(BaseAgent):
    """S6 pretrade filter agent. One Gemini Flash call, 3 s hard budget."""

    name = "pretrade_filter"

    def __init__(
        self,
        client: Optional[AIClient] = None,
        *,
        timeout_s: float = FILTER_TIMEOUT_S,
    ) -> None:
        super().__init__(client=client)
        self.timeout_s = timeout_s
        self._system_prompt = _load_system_prompt()

    async def check(
        self,
        signal: dict[str, Any],
        market: dict[str, Any],
        recent_trades: list[dict[str, Any]] | None = None,
        *,
        regime: str = "UNKNOWN",
        strategy_context: str = "",
    ) -> FilterVerdict:
        """Run the filter. NEVER raises. Returns ``FilterVerdict``.

        On any failure (timeout, missing key, parse, exception) returns
        ``DEFAULT_VERDICT`` ("CLEAR") with ``source="default"``.
        """
        start = time.monotonic()

        async def _run() -> Any:
            prompt = build_user_prompt(
                signal, market, recent_trades or [],
                regime=regime, strategy_context=strategy_context,
            )
            return await self.client.ask_gemini(
                prompt,
                system=self._system_prompt,
                default=None,
                timeout_s=self.timeout_s,
                max_tokens=256,
                temperature=0.1,
            )

        try:
            text = await asyncio.wait_for(
                self.safe_call(_run, default=None, what="ask_gemini"),
                timeout=self.timeout_s + 0.5,  # outer belt-and-braces guard
            )
        except (asyncio.TimeoutError, Exception) as e:
            latency = (time.monotonic() - start) * 1000.0
            self.log.warning("[%s] outer guard fired (%s) — default CLEAR", self.name, e)
            return FilterVerdict(
                verdict=DEFAULT_VERDICT,
                reason=f"filter error ({type(e).__name__}) — default CLEAR",
                confidence=0.0,
                latency_ms=latency,
                source="default",
            )

        latency = (time.monotonic() - start) * 1000.0

        if text is None:
            return FilterVerdict(
                verdict=DEFAULT_VERDICT,
                reason="AI unavailable or timed out — default CLEAR",
                confidence=0.0,
                latency_ms=latency,
                source="default",
            )

        parsed = AIClient.parse_json(text, default=None)
        if not isinstance(parsed, dict):
            return FilterVerdict(
                verdict=DEFAULT_VERDICT,
                reason="unparseable AI response — default CLEAR",
                confidence=0.0,
                latency_ms=latency,
                source="default",
            )

        verdict = str(parsed.get("verdict") or parsed.get("action") or "CLEAR").upper()
        if verdict not in _VALID_VERDICTS:
            verdict = DEFAULT_VERDICT
        try:
            confidence = float(parsed.get("confidence", 50))
        except (TypeError, ValueError):
            confidence = 50.0

        reason = str(parsed.get("reason", "")).strip() or "no reason given"

        fv = FilterVerdict(
            verdict=verdict,
            reason=reason,
            confidence=confidence,
            latency_ms=latency,
            source="ai",
        )
        if verdict == "CAUTION":
            self.log.warning("[%s] CAUTION (%.0f%%, %.0fms): %s",
                             self.name, confidence, latency, reason)
        else:
            self.log.info("[%s] %s (%.0f%%, %.0fms): %s",
                          self.name, verdict, confidence, latency, reason)
        return fv


# ─── Config helper ───────────────────────────────────────────────────────

def get_filter_mode(strategy_name: str) -> str:
    """Return the configured ``ai_filter_mode`` for a strategy.

    Defaults to ``"advisory"`` when unset or when config load fails.
    """
    try:
        from config.strategies import STRATEGIES  # local import: avoid cycles
        cfg = STRATEGIES.get(strategy_name, {}) or {}
        mode = str(cfg.get("ai_filter_mode", DEFAULT_FILTER_MODE)).lower()
        if mode not in ("advisory", "blocking"):
            mode = DEFAULT_FILTER_MODE
        return mode
    except Exception:
        return DEFAULT_FILTER_MODE


def should_skip_trade(verdict: FilterVerdict, strategy_name: str) -> bool:
    """Combine verdict + per-strategy mode → boolean skip decision.

    Returns True only when mode=="blocking" AND verdict=="SIT_OUT".
    """
    return (
        get_filter_mode(strategy_name) == "blocking"
        and verdict.verdict == "SIT_OUT"
    )


# ─── Module-level back-compat shim ───────────────────────────────────────

#: Shared singleton — created lazily so tests can monkey-patch.
_DEFAULT_AGENT: Optional[PretradeFilter] = None


def _get_default_agent() -> PretradeFilter:
    global _DEFAULT_AGENT
    if _DEFAULT_AGENT is None:
        _DEFAULT_AGENT = PretradeFilter()
    return _DEFAULT_AGENT


async def check(
    signal: dict[str, Any],
    market: dict[str, Any],
    recent_trades: list[dict[str, Any]] | None = None,
    regime: str = "UNKNOWN",
    model: str | None = None,              # legacy arg — ignored
    strategy_context: str = "",
) -> FilterVerdict:
    """Module-level entrypoint used by ``bots/base_bot.py``.

    Delegates to a shared :class:`PretradeFilter` instance. Back-compat
    with the pre-S6 signature — the ``model`` kwarg is accepted and
    ignored (Gemini Flash is hard-coded).
    """
    agent = _get_default_agent()
    return await agent.check(
        signal, market, recent_trades or [],
        regime=regime, strategy_context=strategy_context,
    )


__all__ = [
    "Verdict",
    "FilterVerdict",
    "PretradeFilter",
    "build_user_prompt",
    "check",
    "get_filter_mode",
    "should_skip_trade",
    "FILTER_TIMEOUT_S",
    "DEFAULT_VERDICT",
    "DEFAULT_FILTER_MODE",
]
