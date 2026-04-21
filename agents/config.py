"""
Phoenix Bot — Agents Config (S4 infra)

Central configuration for AI agent infrastructure (Phase E-H, sub-streams
H-4A..4E). Defines model identifiers, timeout/retry defaults, log paths,
and env-var keys. Never crashes on missing keys — instead sets the
module-level ``DEGRADED`` flag that agents can check.

Environment variables read:
  - GOOGLE_API_KEY  (or GEMINI_API_KEY as fallback) — Google Gemini
  - ANTHROPIC_API_KEY                               — Claude

This module does NOT replace ``agents.ai_client``; it is the lightweight
config surface that ``agents.base_agent`` (and H-4A..4E agents) sits on.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("agents.config")

# B42: self-load .env with override=True so this module's key reads work
# even when importer didn't call load_dotenv first. Host OS may have
# ANTHROPIC_API_KEY="" set (e.g. Claude Code OAuth shim) which without
# override would silently preempt the real key from .env.
try:
    from dotenv import load_dotenv as _load_dotenv
    _PROJECT_ROOT_FOR_ENV = Path(__file__).resolve().parent.parent
    _load_dotenv(_PROJECT_ROOT_FOR_ENV / ".env", override=True)
except ImportError:
    pass

# ─── Model identifiers ───────────────────────────────────────────────────

MODEL_GEMINI_FLASH = os.environ.get("AGENT_MODEL_GEMINI_FLASH", "gemini-2.5-flash")
MODEL_GEMINI_PRO   = os.environ.get("AGENT_MODEL_GEMINI_PRO",   "gemini-2.5-pro")
MODEL_CLAUDE_SONNET = os.environ.get(
    "AGENT_MODEL_CLAUDE_SONNET", "claude-sonnet-4-5-20250929"
)

# ─── Timeout / retry defaults ────────────────────────────────────────────

DEFAULT_TIMEOUT_S      = float(os.environ.get("AGENT_TIMEOUT_S", "10.0"))
DEFAULT_MAX_ATTEMPTS   = int(os.environ.get("AGENT_MAX_ATTEMPTS", "3"))
DEFAULT_BACKOFF_INITIAL_S = float(os.environ.get("AGENT_BACKOFF_INITIAL_S", "1.0"))
DEFAULT_BACKOFF_FACTOR = float(os.environ.get("AGENT_BACKOFF_FACTOR", "2.0"))
DEFAULT_MAX_TOKENS     = int(os.environ.get("AGENT_MAX_TOKENS", "1024"))
DEFAULT_TEMPERATURE    = float(os.environ.get("AGENT_TEMPERATURE", "0.2"))

# ─── Log paths ───────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.environ.get("AGENT_LOG_DIR", _PROJECT_ROOT / "logs" / "agents"))


def daily_log_path(date_str: str | None = None) -> Path:
    """Return the path for today's (or given date's) agent-call log file."""
    from datetime import datetime
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    return LOG_DIR / f"{date_str}_agent_calls.jsonl"


# ─── API keys + degraded-mode flag ───────────────────────────────────────

GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

_MISSING_KEYS: list[str] = []
if not GOOGLE_API_KEY:
    _MISSING_KEYS.append("GOOGLE_API_KEY")
if not ANTHROPIC_API_KEY:
    _MISSING_KEYS.append("ANTHROPIC_API_KEY")

#: True if ANY required key is missing. Agents should check this and
#: return their ``default`` value rather than making doomed API calls.
DEGRADED: bool = bool(_MISSING_KEYS)

_LOGGED_CRITICAL = False


def _log_degraded_once() -> None:
    """Log CRITICAL exactly once per process when keys are missing."""
    global _LOGGED_CRITICAL
    if _LOGGED_CRITICAL or not DEGRADED:
        return
    _LOGGED_CRITICAL = True
    logger.critical(
        "Agent infra DEGRADED — missing env vars: %s. "
        "Agents will return default values instead of calling LLMs.",
        ", ".join(_MISSING_KEYS),
    )


_log_degraded_once()


def have_gemini() -> bool:
    return bool(GOOGLE_API_KEY)


def have_claude() -> bool:
    return bool(ANTHROPIC_API_KEY)


__all__ = [
    "MODEL_GEMINI_FLASH",
    "MODEL_GEMINI_PRO",
    "MODEL_CLAUDE_SONNET",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BACKOFF_INITIAL_S",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "LOG_DIR",
    "daily_log_path",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEGRADED",
    "have_gemini",
    "have_claude",
]
