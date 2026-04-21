"""
S9 — 4E Adaptive Params (Phase E-H, Wave 2)

Deterministic, safety-validated proposal generator. Consumes S8's learner
output (``logs/ai_learner/pending_recommendations.json``) and emits
human-readable proposal Markdown files — NEVER auto-applies config.

Design rules
------------
1. Hard safety bounds are ENFORCED here (see ``SafetyBounds``). Any
   recommendation that violates a bound is rejected and logged.
2. Accepted recommendations become proposal ``.md`` files — Jennifer (or
   a human reviewer) then runs ``tools/approve_proposal.py`` which
   creates a git branch, applies the change, runs tests, and STOPS.
3. This module never imports ``config.strategies`` at module scope and
   never writes to it.
4. Forbidden files (``config/account_routing.py``, live-trading flags)
   are hard-rejected regardless of other content.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("agents.adaptive_params")

# ─── Safety bounds (hardcoded, non-overridable at runtime) ──────────────

@dataclass(frozen=True)
class SafetyBounds:
    MAX_RISK_PER_TRADE: float = 100.0
    MAX_DAILY_LOSS_CAP: float = 500.0
    MIN_STOP_TICKS: int = 4
    MAX_STOP_TICKS: int = 200
    MAX_SIZE_MULT: float = 3.0
    # Risk gates that can never be disabled
    NEVER_DISABLE: tuple[str, ...] = (
        "max_daily_loss",
        "risk_per_trade",
        "recovery_mode",
        "vix_filter",
        "daily_loss_cap",
        "circuit_breaker",
    )
    # Files that may never be modified by a proposal
    FORBIDDEN_FILES: tuple[str, ...] = (
        "config/account_routing.py",
        "account_routing.py",
    )
    # Params names that flag live-trading. Never proposable.
    FORBIDDEN_PARAMS: tuple[str, ...] = (
        "live_trading",
        "LIVE_TRADING",
        "account_id",
        "broker_account",
    )

BOUNDS = SafetyBounds()

# ─── Paths ──────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEARNER_DIR = _PROJECT_ROOT / "logs" / "ai_learner"
PENDING_FILE = LEARNER_DIR / "pending_recommendations.json"
PROPOSALS_DIR = LEARNER_DIR / "proposals"
REJECTED_LOG = LEARNER_DIR / "rejected.jsonl"


# ─── Validation result ──────────────────────────────────────────────────

@dataclass
class ValidationResult:
    accepted: bool
    reason: str = ""
    recommendation: dict = field(default_factory=dict)


def _is_falsy_disable(current: Any, proposed: Any) -> bool:
    """True if proposed value appears to be disabling a gate that was enabled."""
    truthy_current = bool(current) if current is not None else False
    falsy_proposed = proposed in (False, 0, 0.0, None, "", "off", "disabled")
    return truthy_current and falsy_proposed


def validate_recommendation(rec: dict, bounds: SafetyBounds = BOUNDS) -> ValidationResult:
    """Check one recommendation dict against hard safety bounds.

    Expected keys: strategy, param, current, proposed, rationale, expected_impact.
    """
    required = {"strategy", "param", "current", "proposed"}
    missing = required - set(rec or {})
    if missing:
        return ValidationResult(False, f"missing fields: {sorted(missing)}", rec)

    strategy = str(rec["strategy"])
    param = str(rec["param"])
    proposed = rec["proposed"]
    current = rec["current"]

    # Forbidden params
    if param in bounds.FORBIDDEN_PARAMS:
        return ValidationResult(False, f"forbidden param: {param}", rec)

    # Forbidden files (if caller encoded a file path)
    target_file = str(rec.get("target_file", ""))
    for fp in bounds.FORBIDDEN_FILES:
        if fp and fp in target_file.replace("\\", "/"):
            return ValidationResult(False, f"forbidden file: {target_file}", rec)

    # Never-disable risk gates
    if param in bounds.NEVER_DISABLE and _is_falsy_disable(current, proposed):
        return ValidationResult(False, f"cannot disable risk gate: {param}", rec)

    # Risk per trade cap
    if param == "risk_per_trade":
        try:
            if float(proposed) > bounds.MAX_RISK_PER_TRADE:
                return ValidationResult(
                    False,
                    f"risk_per_trade {proposed} > ${bounds.MAX_RISK_PER_TRADE}",
                    rec,
                )
        except (TypeError, ValueError):
            return ValidationResult(False, f"risk_per_trade not numeric: {proposed}", rec)

    # Daily loss cap
    if param in ("daily_loss_cap", "max_daily_loss"):
        try:
            if float(proposed) > bounds.MAX_DAILY_LOSS_CAP:
                return ValidationResult(
                    False,
                    f"{param} {proposed} > ${bounds.MAX_DAILY_LOSS_CAP}",
                    rec,
                )
        except (TypeError, ValueError):
            return ValidationResult(False, f"{param} not numeric: {proposed}", rec)

    # Stop distances (ticks)
    if "stop" in param and "tick" in param:
        try:
            v = float(proposed)
            if v < bounds.MIN_STOP_TICKS:
                return ValidationResult(
                    False, f"stop {v} ticks < min {bounds.MIN_STOP_TICKS}", rec
                )
            if v > bounds.MAX_STOP_TICKS:
                return ValidationResult(
                    False, f"stop {v} ticks > max {bounds.MAX_STOP_TICKS}", rec
                )
        except (TypeError, ValueError):
            return ValidationResult(False, f"{param} not numeric: {proposed}", rec)

    # Size multiplier
    if "size" in param and ("mult" in param or "multiplier" in param):
        try:
            if float(proposed) > bounds.MAX_SIZE_MULT:
                return ValidationResult(
                    False,
                    f"size multiplier {proposed} > {bounds.MAX_SIZE_MULT}x",
                    rec,
                )
        except (TypeError, ValueError):
            return ValidationResult(False, f"{param} not numeric: {proposed}", rec)

    return ValidationResult(True, "ok", rec)


# ─── Proposal writer ────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("_", s).strip("_").lower() or "unknown"


def make_proposal_id(strategy: str, param: str, now: Optional[datetime] = None) -> str:
    n = now or datetime.now(timezone.utc)
    ts = n.strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{_slug(strategy)}_{_slug(param)}"


def _render_markdown(rec: dict, proposal_id: str) -> str:
    strategy = rec.get("strategy", "?")
    param = rec.get("param", "?")
    current = rec.get("current", "?")
    proposed = rec.get("proposed", "?")
    rationale = rec.get("rationale", "(no rationale provided)")
    impact = rec.get("expected_impact", "(no impact estimate)")

    return f"""# AI Proposal: {proposal_id}

- **Strategy:** `{strategy}`
- **Param:** `{param}`
- **Current:** `{current!r}`
- **Proposed:** `{proposed!r}`
- **Generated:** {datetime.now(timezone.utc).isoformat()}
- **Status:** PENDING_APPROVAL

## Reasoning

{rationale}

## Expected Impact

{impact}

## Machine-Readable Change

```json
{json.dumps({"strategy": strategy, "param": param, "current": current, "proposed": proposed}, indent=2)}
```

## Rollback Instructions

If this change is merged and later proves harmful:

1. `git log --oneline config/strategies.py` — find the commit for proposal `{proposal_id}`.
2. `git revert <commit-sha>` — revert on `main` (or your deployment branch).
3. Restart the affected bot process so it re-reads `config/strategies.py`.
4. Verify by inspecting `STRATEGIES["{strategy}"]["{param}"]` in a Python REPL.

## Approval

Run: `python tools/approve_proposal.py {proposal_id}`

This will create branch `ai-proposal/{proposal_id}`, apply the change,
run the test suite, and STOP. Jennifer merges manually.
"""


def _append_rejected(rec: dict, reason: str) -> None:
    REJECTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "recommendation": rec,
    }
    try:
        with open(REJECTED_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:  # pragma: no cover
        logger.warning("rejected-log write failed: %s", e)


def write_proposal(rec: dict, *, proposals_dir: Optional[Path] = None,
                   proposal_id: Optional[str] = None) -> Path:
    """Render and persist a proposal MD. Returns the file path."""
    pid = proposal_id or make_proposal_id(rec.get("strategy", "?"), rec.get("param", "?"))
    dest_dir = proposals_dir or PROPOSALS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"proposal_{pid}.md"
    path.write_text(_render_markdown(rec, pid), encoding="utf-8")
    return path


# ─── Orchestration ──────────────────────────────────────────────────────

@dataclass
class ProcessResult:
    accepted: list[Path] = field(default_factory=list)
    rejected: list[tuple[dict, str]] = field(default_factory=list)


def process_pending(
    *,
    pending_file: Optional[Path] = None,
    proposals_dir: Optional[Path] = None,
    rejected_log: Optional[Path] = None,
) -> ProcessResult:
    """Read pending recommendations, validate each, and write outputs."""
    pf = pending_file or PENDING_FILE
    pd = proposals_dir or PROPOSALS_DIR
    rl = rejected_log or REJECTED_LOG

    result = ProcessResult()
    if not pf.exists():
        logger.info("no pending recommendations at %s", pf)
        return result

    try:
        recs = json.loads(pf.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("failed to parse %s: %s", pf, e)
        return result

    if not isinstance(recs, list):
        logger.warning("pending recommendations not a list")
        return result

    for rec in recs:
        if not isinstance(rec, dict):
            result.rejected.append(({"raw": rec}, "not a dict"))
            continue
        v = validate_recommendation(rec)
        if not v.accepted:
            # Inline rejected-log write using provided path
            rl.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(rl, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "reason": v.reason,
                        "recommendation": rec,
                    }, default=str) + "\n")
            except Exception as e:  # pragma: no cover
                logger.warning("rejected-log write failed: %s", e)
            result.rejected.append((rec, v.reason))
            continue
        path = write_proposal(rec, proposals_dir=pd)
        result.accepted.append(path)

    _notify_new_proposals(len(result.accepted))
    return result


def _notify_new_proposals(count: int) -> None:
    """Send a Telegram alert when N>0 new proposals were written.

    Silent no-op if TELEGRAM_BOT_TOKEN / TELEGRAM_TOKEN is unset. Never raises.
    """
    if count <= 0:
        return
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")):
        return
    try:
        from core import telegram_notifier
        telegram_notifier.send_sync(
            f"[AI] {count} new proposals pending review. "
            f"Run: python tools/list_proposals.py"
        )
    except Exception as e:
        logger.warning("telegram notify failed: %s", e)


__all__ = [
    "BOUNDS",
    "SafetyBounds",
    "ValidationResult",
    "ProcessResult",
    "validate_recommendation",
    "make_proposal_id",
    "write_proposal",
    "process_pending",
    "PENDING_FILE",
    "PROPOSALS_DIR",
    "REJECTED_LOG",
]
