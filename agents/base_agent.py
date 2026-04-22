"""
Phoenix Bot — Base Agent Infrastructure (S4 infra, Phase E-H)

Provides:
  - ``AIClient`` — async wrapper for Gemini + Claude with hard timeouts,
    exponential-backoff retry, graceful-degradation defaults, JSON
    parsing helper, and per-call JSONL logging.
  - ``BaseAgent`` — subclass contract: ``name``, ``run(ctx)``, with
    built-in call wrapping and default-on-failure semantics.

Design rules:
  - Every LLM call has a hard timeout (default 10s).
  - On any failure (timeout, HTTP, parse, missing key) the caller's
    ``default`` value is returned — these methods NEVER raise.
  - Retries use exponential backoff, max 3 attempts.
  - Structured outputs parsed via :meth:`AIClient.parse_json`.
  - Calls logged one-line-per-call to
    ``logs/agents/YYYY-MM-DD_agent_calls.jsonl`` with timestamp, model,
    latency_ms, token estimates, outcome, error_msg.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agents import config as agent_config

logger = logging.getLogger("agents.base")


# ─── Optional-dep import shims ───────────────────────────────────────────

# B53: prefer modern `google.genai` SDK (legacy `google.generativeai`
# is deprecated and emits FutureWarning on import).
_GENAI_KIND = None  # "new" | "legacy" | None
try:
    from google import genai as _genai_new  # type: ignore
    _GENAI_KIND = "new"
    _HAS_GENAI = True
    _genai = None  # unused in new mode
except Exception:
    _genai_new = None
    try:
        import google.generativeai as _genai  # type: ignore
        _GENAI_KIND = "legacy"
        _HAS_GENAI = True
    except Exception:
        _genai = None
        _HAS_GENAI = False

try:
    import anthropic as _anthropic  # type: ignore
    _HAS_ANTHROPIC = True
except Exception:
    _anthropic = None
    _HAS_ANTHROPIC = False

try:
    import aiohttp as _aiohttp  # type: ignore
    _HAS_AIOHTTP = True
except Exception:
    _aiohttp = None
    _HAS_AIOHTTP = False


# ─── Token estimator (cheap, no tokenizer dep) ──────────────────────────

def estimate_tokens(text: str | None) -> int:
    """Rough token estimate — 1 token ~= 4 chars. Good enough for cost logs."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# ─── Call-log writer ─────────────────────────────────────────────────────

def _write_call_log(entry: dict) -> None:
    """Append one JSON line to today's agent-calls log. Never raises."""
    try:
        path: Path = agent_config.daily_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:  # pragma: no cover
        logger.warning("call-log write failed: %s", e)


# ─── AIClient ───────────────────────────────────────────────────────────

@dataclass
class CallResult:
    text: Optional[str]
    outcome: str  # "success" | "timeout" | "error" | "degraded"
    error_msg: Optional[str]
    latency_ms: float
    model: str


class AIClient:
    """Async wrapper for Gemini + Claude with timeout, retry, logging.

    All public methods return the caller's ``default`` on any failure —
    they never raise. Retries are applied at the ``ask_*`` level.
    """

    def __init__(
        self,
        *,
        timeout_s: float = agent_config.DEFAULT_TIMEOUT_S,
        max_attempts: int = agent_config.DEFAULT_MAX_ATTEMPTS,
        backoff_initial_s: float = agent_config.DEFAULT_BACKOFF_INITIAL_S,
        backoff_factor: float = agent_config.DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.backoff_initial_s = backoff_initial_s
        self.backoff_factor = backoff_factor

    # ---- Gemini -------------------------------------------------------

    async def _gemini_once(
        self,
        prompt: str,
        *,
        system: str,
        model: str,
        timeout_s: float,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Single Gemini call. Raises on failure (caller handles retry)."""
        if not agent_config.have_gemini():
            raise RuntimeError("GOOGLE_API_KEY not set")

        if _HAS_GENAI:
            if _GENAI_KIND == "new":
                def _call() -> str:
                    client = _genai_new.Client(api_key=agent_config.GOOGLE_API_KEY)
                    # New SDK uses config object; system_instruction goes in config.
                    from google.genai import types as _genai_types  # type: ignore
                    cfg = _genai_types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        system_instruction=system or None,
                    )
                    resp = client.models.generate_content(
                        model=model, contents=prompt, config=cfg,
                    )
                    return getattr(resp, "text", "") or ""
            else:
                def _call() -> str:
                    _genai.configure(api_key=agent_config.GOOGLE_API_KEY)
                    m = _genai.GenerativeModel(
                        model_name=model,
                        system_instruction=system or None,
                    )
                    resp = m.generate_content(
                        prompt,
                        generation_config={
                            "temperature": temperature,
                            "max_output_tokens": max_tokens,
                        },
                    )
                    return getattr(resp, "text", "") or ""

            loop = asyncio.get_event_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, _call), timeout=timeout_s
            )

        # REST fallback
        if not _HAS_AIOHTTP:
            raise RuntimeError("Neither google-generativeai nor aiohttp available")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={agent_config.GOOGLE_API_KEY}"
        )
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        timeout = _aiohttp.ClientTimeout(total=timeout_s)
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload) as r:
                r.raise_for_status()
                data = await r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Gemini REST parse failed: {e}; body={data}")

    # ---- Claude -------------------------------------------------------

    async def _claude_once(
        self,
        prompt: str,
        *,
        system: str,
        model: str,
        timeout_s: float,
        max_tokens: int,
        temperature: float,
    ) -> str:
        if not agent_config.have_claude():
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        if _HAS_ANTHROPIC:
            def _call() -> str:
                client = _anthropic.Anthropic(api_key=agent_config.ANTHROPIC_API_KEY)
                msg = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system or "",
                    messages=[{"role": "user", "content": prompt}],
                )
                # Claude returns a list of content blocks
                parts = []
                for block in getattr(msg, "content", []) or []:
                    t = getattr(block, "text", None)
                    if t:
                        parts.append(t)
                return "".join(parts)

            loop = asyncio.get_event_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, _call), timeout=timeout_s
            )

        # REST fallback
        if not _HAS_AIOHTTP:
            raise RuntimeError("Neither anthropic nor aiohttp available")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": agent_config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system or "",
            "messages": [{"role": "user", "content": prompt}],
        }
        timeout = _aiohttp.ClientTimeout(total=timeout_s)
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=headers, json=payload) as r:
                r.raise_for_status()
                data = await r.json()
        try:
            return "".join(
                blk.get("text", "") for blk in data.get("content", []) or []
            )
        except Exception as e:
            raise RuntimeError(f"Claude REST parse failed: {e}; body={data}")

    # ---- Generic retry wrapper ---------------------------------------

    async def _with_retry(
        self,
        func: Callable[[], Awaitable[str]],
        *,
        model: str,
        prompt_for_log: str,
    ) -> CallResult:
        attempt = 0
        backoff = self.backoff_initial_s
        last_err: Optional[str] = None
        start = time.monotonic()

        while attempt < self.max_attempts:
            attempt += 1
            try:
                text = await func()
                latency_ms = (time.monotonic() - start) * 1000.0
                result = CallResult(
                    text=text, outcome="success", error_msg=None,
                    latency_ms=latency_ms, model=model,
                )
                self._log_call(result, prompt_for_log, attempts=attempt)
                return result
            except asyncio.TimeoutError:
                last_err = "timeout"
                outcome_if_final = "timeout"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"[:500]
                outcome_if_final = "error"

            if attempt >= self.max_attempts:
                latency_ms = (time.monotonic() - start) * 1000.0
                result = CallResult(
                    text=None, outcome=outcome_if_final, error_msg=last_err,
                    latency_ms=latency_ms, model=model,
                )
                self._log_call(result, prompt_for_log, attempts=attempt)
                return result

            await asyncio.sleep(backoff)
            backoff *= self.backoff_factor

        # unreachable
        latency_ms = (time.monotonic() - start) * 1000.0
        result = CallResult(
            text=None, outcome="error", error_msg=last_err or "unknown",
            latency_ms=latency_ms, model=model,
        )
        self._log_call(result, prompt_for_log, attempts=attempt)
        return result

    def _log_call(self, result: CallResult, prompt: str, *, attempts: int) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "model": result.model,
            "latency_ms": round(result.latency_ms, 2),
            "input_tokens_est": estimate_tokens(prompt),
            "output_tokens_est": estimate_tokens(result.text),
            "outcome": result.outcome,
            "error_msg": result.error_msg,
            "attempts": attempts,
        }
        _write_call_log(entry)

    # ---- Public API --------------------------------------------------

    async def ask_gemini(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str = agent_config.MODEL_GEMINI_FLASH,
        default: Any = None,
        timeout_s: Optional[float] = None,
        max_tokens: int = agent_config.DEFAULT_MAX_TOKENS,
        temperature: float = agent_config.DEFAULT_TEMPERATURE,
    ) -> Any:
        """Call Gemini. Return text on success, ``default`` on any failure."""
        if not agent_config.have_gemini():
            self._log_call(
                CallResult(None, "degraded", "GOOGLE_API_KEY missing", 0.0, model),
                prompt, attempts=0,
            )
            return default

        t = timeout_s if timeout_s is not None else self.timeout_s

        async def _run() -> str:
            return await self._gemini_once(
                prompt, system=system, model=model, timeout_s=t,
                max_tokens=max_tokens, temperature=temperature,
            )

        res = await self._with_retry(_run, model=model, prompt_for_log=prompt)
        return res.text if res.outcome == "success" else default

    async def ask_claude(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str = agent_config.MODEL_CLAUDE_SONNET,
        default: Any = None,
        timeout_s: Optional[float] = None,
        max_tokens: int = agent_config.DEFAULT_MAX_TOKENS,
        temperature: float = agent_config.DEFAULT_TEMPERATURE,
    ) -> Any:
        """Call Claude. Return text on success, ``default`` on any failure."""
        if not agent_config.have_claude():
            self._log_call(
                CallResult(None, "degraded", "ANTHROPIC_API_KEY missing", 0.0, model),
                prompt, attempts=0,
            )
            return default

        t = timeout_s if timeout_s is not None else self.timeout_s

        async def _run() -> str:
            return await self._claude_once(
                prompt, system=system, model=model, timeout_s=t,
                max_tokens=max_tokens, temperature=temperature,
            )

        res = await self._with_retry(_run, model=model, prompt_for_log=prompt)
        return res.text if res.outcome == "success" else default

    # ---- JSON helper -------------------------------------------------

    @staticmethod
    def parse_json(text: str | None, *, default: Any = None) -> Any:
        """Robust JSON extractor. Returns ``default`` on any parse failure.

        Handles: direct JSON, fenced ```json blocks, and first balanced
        {...} substring.
        """
        if not text:
            return default
        t = text.strip()
        try:
            return json.loads(t)
        except Exception:
            pass
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except Exception:
                pass
        start = t.find("{")
        end = t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except Exception:
                pass
        return default


# ─── BaseAgent ───────────────────────────────────────────────────────────

class BaseAgent:
    """Contract for all S5-S9 agents.

    Subclasses set :attr:`name` and implement :meth:`run`. The base
    class provides :meth:`safe_call` for wrapping arbitrary async
    callables with default-on-failure semantics and a consistent log
    prefix.
    """

    #: Agent identifier (subclasses MUST override). Used in log lines.
    name: str = "base"

    def __init__(self, client: Optional[AIClient] = None) -> None:
        self.client = client or AIClient()
        self.log = logging.getLogger(f"agents.{self.name}")

    async def run(self, ctx: Any) -> Any:  # pragma: no cover - abstract
        """Execute the agent. Subclasses override.

        Should never raise — on failure return a sensible default.
        """
        raise NotImplementedError

    async def safe_call(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        *,
        default: Any = None,
        what: str = "call",
    ) -> Any:
        """Run an async callable with try/except + logging.

        ``coro_factory`` is a zero-arg callable returning a coroutine so
        we can wrap the whole thing — including coroutine construction
        — in the safety net.
        """
        try:
            return await coro_factory()
        except Exception as e:
            self.log.warning("[%s] %s failed (%s) — returning default",
                             self.name, what, e)
            return default


__all__ = [
    "AIClient",
    "BaseAgent",
    "CallResult",
    "estimate_tokens",
]
