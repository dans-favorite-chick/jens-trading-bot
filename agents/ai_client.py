"""
Phoenix Bot — AI Client Module (Multi-Provider)

Unified interface for AI providers with tiered routing and automatic fallback.

Providers:
  1. Groq (Llama 3 70B)  — Fastest (~100ms). Pre-trade filter, quick decisions.
  2. Gemini 2.5 Flash     — Fast (~1-3s). Council voters, general advisory.
  3. Grok (xAI)           — Fast (~1-2s). Market analysis, political context.
  4. Ollama (local Llama3) — Free. Bulk analysis, backtesting AI.

Tier routing:
  "instant"   — Groq primary, Gemini fallback  (pre-trade filter)
  "fast"      — Gemini primary, Groq fallback   (council voters)
  "deep"      — Gemini primary, Grok fallback   (session debrief, research)
  "political" — Grok primary, Gemini fallback    (Trump/political context)
  "free"      — Ollama primary, Gemini fallback  (bulk/research, no API cost)

All calls are async with timeouts. Failures never block trading.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger("AIClient")

# ─── Tier Routing Configuration ───────────────────────────────────

AI_TIERS = {
    "instant": {
        "primary": "groq",
        "fallback": "gemini",
        "timeout_s": 2.0,
    },
    "fast": {
        "primary": "gemini",
        "fallback": "groq",
        "timeout_s": 5.0,
    },
    "deep": {
        "primary": "gemini",
        "fallback": "grok",
        "timeout_s": 60.0,
    },
    "political": {
        "primary": "grok",
        "fallback": "gemini",
        "timeout_s": 10.0,
    },
    "free": {
        "primary": "ollama",
        "fallback": "gemini",
        "timeout_s": 30.0,
    },
}

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

        # response.text can be None if the model returns empty/blocked content
        text = response.text or ""
        if not text and response.candidates:
            # Try to extract from candidates directly
            for candidate in response.candidates:
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, "text") and part.text:
                            text = part.text
                            break
                    if text:
                        break
        if not text:
            logger.warning(f"[Gemini] {model_name} returned empty response")
            return None
        logger.info(f"[Gemini] {model_name} responded ({len(text)} chars)")
        return text

    except asyncio.TimeoutError:
        logger.warning(f"[Gemini] Timeout after {timeout_s}s")
        return None
    except Exception as e:
        logger.error(f"[Gemini] Error: {e}")
        return None


# ─── Provider: Groq (OpenAI-compatible) ──────────────────────────

async def ask_groq(
    prompt: str,
    system: str = "",
    model: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout_s: float = 2.0,
) -> Optional[str]:
    """
    Send a prompt to Groq via OpenAI-compatible API. Fastest provider (~100ms).

    Returns:
        Response text, or None on failure/timeout
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.warning("[Groq] GROQ_API_KEY not set")
        return None

    if not model:
        model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

    base_url = "https://api.groq.com/openai/v1"

    return await _openai_compatible_call(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
        provider_name="Groq",
    )


# ─── Provider: Grok / xAI (OpenAI-compatible) ────────────────────

async def ask_grok(
    prompt: str,
    system: str = "",
    model: str = "grok-3-fast",
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout_s: float = 10.0,
) -> Optional[str]:
    """
    Send a prompt to xAI Grok via OpenAI-compatible API.
    Good for market analysis and political/social media context.

    Returns:
        Response text, or None on failure/timeout
    """
    api_key = os.environ.get("GROK_API_KEY", "")
    if not api_key:
        logger.warning("[Grok] GROK_API_KEY not set")
        return None

    base_url = "https://api.x.ai/v1"

    return await _openai_compatible_call(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
        provider_name="Grok",
    )


# ─── Provider: Ollama (local, OpenAI-compatible) ─────────────────

async def ask_ollama(
    prompt: str,
    system: str = "",
    model: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout_s: float = 30.0,
) -> Optional[str]:
    """
    Send a prompt to local Ollama via OpenAI-compatible API. Free, no API cost.

    Returns:
        Response text, or None on failure/timeout
    """
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    if not model:
        model = os.environ.get("OLLAMA_MODEL", "llama3")

    return await _openai_compatible_call(
        base_url=base_url,
        api_key="ollama",  # Ollama doesn't need a real key but the header is required
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
        provider_name="Ollama",
    )


# ─── Shared OpenAI-Compatible Call ────────────────────────────────

async def _openai_compatible_call(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    system: str,
    max_tokens: int,
    temperature: float,
    timeout_s: float,
    provider_name: str,
) -> Optional[str]:
    """
    Shared implementation for all OpenAI-compatible providers (Groq, Grok, Ollama).
    Runs the synchronous requests.post in an executor to stay async.

    Returns:
        Response text, or None on failure/timeout
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _do_request():
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    try:
        loop = asyncio.get_event_loop()
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _do_request),
            timeout=timeout_s + 1.0,  # Slightly longer than requests timeout
        )
        logger.info(f"[{provider_name}] {model} responded ({len(text)} chars)")
        return text

    except asyncio.TimeoutError:
        logger.warning(f"[{provider_name}] Timeout after {timeout_s}s")
        return None
    except requests.exceptions.ConnectionError:
        logger.warning(f"[{provider_name}] Connection refused (is {provider_name} running?)")
        return None
    except Exception as e:
        logger.error(f"[{provider_name}] Error: {e}")
        return None


# ─── Provider Dispatch Map ────────────────────────────────────────

_PROVIDER_FUNCS = {
    "groq": ask_groq,
    "gemini": ask_gemini,
    "grok": ask_grok,
    "ollama": ask_ollama,
}

# Map provider names to their parameter name for the model argument
# (Gemini uses model_name, the others use model)
_PROVIDER_MODEL_DEFAULTS = {
    "groq": "",                # Uses GROQ_MODEL env or llama3-70b-8192
    "gemini": "gemini-2.5-flash",
    "grok": "grok-3-fast",
    "ollama": "",              # Uses OLLAMA_MODEL env or llama3
}


async def _call_provider(
    provider: str,
    prompt: str,
    system: str,
    max_tokens: int,
    temperature: float,
    timeout_s: float,
) -> Optional[str]:
    """Call a specific provider by name."""
    func = _PROVIDER_FUNCS.get(provider)
    if func is None:
        logger.error(f"[AI] Unknown provider: {provider}")
        return None

    if provider == "gemini":
        return await func(
            prompt=prompt,
            system=system,
            model_name=_PROVIDER_MODEL_DEFAULTS["gemini"],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
        )
    else:
        return await func(
            prompt=prompt,
            system=system,
            model=_PROVIDER_MODEL_DEFAULTS[provider],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
        )


# ─── Universal ask() with Tiered Routing ─────────────────────────

async def ask(
    prompt: str,
    system: str = "",
    tier: str = "fast",
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> Optional[str]:
    """
    Universal AI call with tiered routing + automatic fallback.

    Tries primary provider for the tier. If it fails/timeouts,
    falls back to the secondary provider. Never blocks trading.

    Args:
        prompt: The user message / main prompt
        system: System instruction
        tier: Routing tier — "instant", "fast", "deep", "political", "free"
        max_tokens: Max response tokens
        temperature: 0.0 = deterministic, 1.0 = creative

    Returns:
        Response text, or None if all providers fail
    """
    tier_config = AI_TIERS.get(tier)
    if tier_config is None:
        logger.warning(f"[AI] Unknown tier '{tier}', falling back to 'fast'")
        tier_config = AI_TIERS["fast"]

    primary = tier_config["primary"]
    fallback = tier_config["fallback"]
    timeout_s = tier_config["timeout_s"]

    start = time.time()

    # Try primary provider
    logger.debug(f"[AI] Tier={tier} → trying {primary} (timeout={timeout_s}s)")
    result = await _call_provider(
        provider=primary,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
    )

    if result is not None:
        elapsed = (time.time() - start) * 1000
        logger.info(f"[AI] Tier={tier} {primary} succeeded in {elapsed:.0f}ms")
        return result

    # Primary failed — try fallback
    logger.warning(f"[AI] Tier={tier} {primary} failed, falling back to {fallback}")
    result = await _call_provider(
        provider=fallback,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
    )

    if result is not None:
        elapsed = (time.time() - start) * 1000
        logger.info(f"[AI] Tier={tier} {fallback} (fallback) succeeded in {elapsed:.0f}ms")
        return result

    elapsed = (time.time() - start) * 1000
    logger.error(f"[AI] Tier={tier} ALL providers failed after {elapsed:.0f}ms")
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


# ─── Health Check ────────────────────────────────────────────────

async def health_check_all() -> dict:
    """
    Quick health check of all configured providers.
    Returns a dict of provider -> {status, latency_ms, model, response_preview}.
    """
    results = {}
    test_prompt = "Reply with exactly: OK"
    test_system = "You are a health check bot. Reply with only the word OK."

    providers = {
        "groq": ("instant", "Groq (Llama 3 70B)"),
        "gemini": ("fast", "Gemini 2.5 Flash"),
        "grok": ("political", "Grok (xAI)"),
        "ollama": ("free", "Ollama (local Llama 3)"),
    }

    for name, (tier, label) in providers.items():
        start = time.time()
        try:
            result = await _call_provider(
                provider=name,
                prompt=test_prompt,
                system=test_system,
                max_tokens=10,
                temperature=0.0,
                timeout_s=10.0,
            )
            elapsed = (time.time() - start) * 1000
            results[name] = {
                "label": label,
                "status": "OK" if result else "NO_RESPONSE",
                "latency_ms": round(elapsed, 0),
                "model": _PROVIDER_MODEL_DEFAULTS.get(name, "?"),
                "response_preview": (result or "")[:50],
            }
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            results[name] = {
                "label": label,
                "status": f"ERROR: {str(e)[:60]}",
                "latency_ms": round(elapsed, 0),
                "model": _PROVIDER_MODEL_DEFAULTS.get(name, "?"),
                "response_preview": "",
            }

    return results


# ─── Standalone Test ─────────────────────────────────────────────

async def _test():
    """Health check all providers."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    print("\n" + "=" * 60)
    print("  PHOENIX BOT — AI Provider Health Check")
    print("=" * 60)

    results = await health_check_all()

    for name, info in results.items():
        status_icon = "PASS" if info["status"] == "OK" else "FAIL"
        print(f"\n  [{status_icon}] {info['label']}")
        print(f"        Status:  {info['status']}")
        print(f"        Latency: {info['latency_ms']:.0f}ms")
        print(f"        Model:   {info['model']}")
        if info["response_preview"]:
            print(f"        Preview: {info['response_preview']}")

    # Test tiered routing
    print(f"\n{'-' * 60}")
    print("  Testing tiered ask() routing...")
    print(f"{'-' * 60}")

    for tier_name in AI_TIERS:
        tier_cfg = AI_TIERS[tier_name]
        start = time.time()
        result = await ask(
            prompt="What is 2+2? Reply with just the number.",
            system="Reply concisely.",
            tier=tier_name,
            max_tokens=10,
            temperature=0.0,
        )
        elapsed = (time.time() - start) * 1000
        status = "OK" if result else "FAIL"
        preview = (result or "no response").strip()[:30]
        print(f"  [{status}] tier={tier_name:10s} "
              f"({tier_cfg['primary']}->{tier_cfg['fallback']}) "
              f"{elapsed:6.0f}ms — {preview}")

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    asyncio.run(_test())
