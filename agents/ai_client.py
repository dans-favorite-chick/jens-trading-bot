"""
Phoenix Bot — AI Client Module

Unified interface for AI providers used by all Phase 4 agents.
Primary provider: Google Gemini via google-genai SDK (fast, cost-effective)

All calls are async with timeouts. Failures never block trading.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("AIClient")

# ─── Provider: Google Gemini (google-genai SDK) ───────────────────

_gemini_client = None


def _get_gemini_client():
    """Lazy-init Gemini client."""
    global _gemini_client
    if _gemini_client is None:
        try:
            from google import genai
            api_key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
            if not api_key:
                logger.warning("GEMINI_API_KEY not set — Gemini calls will fail")
            _gemini_client = genai.Client(api_key=api_key)
        except ImportError:
            logger.error("google-genai not installed. Run: pip install google-genai")
            raise
    return _gemini_client


async def ask_gemini(
    prompt: str,
    system: str = "",
    model_name: str = "gemini-2.5-flash",
    temperature: float = 0.2,
    max_tokens: int = 1024,
    timeout_s: float = 5.0,
) -> Optional[str]:
    """
    Send a prompt to Gemini and return the text response.

    Args:
        prompt: The user message / main prompt
        system: System instruction
        model_name: Gemini model to use
        temperature: 0.0 = deterministic, 1.0 = creative
        max_tokens: Max response tokens
        timeout_s: Timeout in seconds

    Returns:
        Response text, or None on failure/timeout
    """
    try:
        client = _get_gemini_client()
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        if system:
            config.system_instruction = system

        # google-genai's generate_content is synchronous, run in executor
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                ),
            ),
            timeout=timeout_s,
        )

        text = response.text
        logger.info(f"[Gemini] {model_name} responded ({len(text)} chars)")
        return text

    except asyncio.TimeoutError:
        logger.warning(f"[Gemini] Timeout after {timeout_s}s")
        return None
    except Exception as e:
        logger.error(f"[Gemini] Error: {e}")
        return None


# ─── Utility: JSON Extraction ───────────────────────────────────────

def extract_json(text: str) -> Optional[dict]:
    """
    Extract a JSON object from AI response text.
    Handles responses wrapped in ```json ... ``` blocks.
    """
    if not text:
        return None

    # Try direct parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try extracting from code block
    import re
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning(f"[AI] Could not extract JSON from response ({len(text)} chars)")
    return None
