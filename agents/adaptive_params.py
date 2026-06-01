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
        import importlib
        telegram_notifier = importlib.import_module("core.telegram_notifier")
        telegram_notifier.send_sync(
            f"[AI] {count} new proposals pending review. "
            f"Run: python tools/list_proposals.py"
        )
    except Exception as e:
        logger.warning("telegram notify failed: %s", e)


# ════════════════════════════════════════════════════════════════════
# Phase 3 — Warehouse-driven Claude advisor (added 2026-05-31)
# ════════════════════════════════════════════════════════════════════
#
# Distinct from the S9 pipeline above. Reads per-strategy stats from the
# DuckDB warehouse, asks Claude for parameter recommendations, and writes a
# Markdown doc to docs/param_recommendations/. Runs each recommendation
# through validate_recommendation() (re-using SafetyBounds) before writing,
# so a Claude proposal that violates a hard bound is rejected the same way
# an S9 proposal is.
#
# Hard rules (per spec):
#   - Reads warehouse ONLY (data/warehouse/phoenix.duckdb). No trade_memory.json.
#   - Never writes config/strategies.py. Output is docs/param_recommendations/ only.
#   - 30s timeout, graceful degradation (returns [] on API failure).
#   - Exits clearly if ANTHROPIC_API_KEY is unset.

WAREHOUSE_DB    = _PROJECT_ROOT / "data" / "warehouse" / "phoenix.duckdb"
ADVISOR_OUT_DIR = _PROJECT_ROOT / "docs" / "param_recommendations"
CLAUDE_TIMEOUT_S = 30.0
CLAUDE_MODEL     = "claude-haiku-4-5-20251001"

_ADVISOR_SYSTEM_PROMPT = """You are a quantitative trading strategy parameter advisor for the Phoenix MNQ futures bot.

You will receive: (1) a strategy's current parameter values from config/strategies.py and (2) a backtest-stats summary read from the DuckDB analytics warehouse (per-regime / per-time-of-day breakdowns, win rate, profit factor, MAE/MFE behavior).

Your job is to propose 0-5 parameter changes that the operator should consider. Each proposal must include:
  - param: the EXACT parameter name as it appears in config/strategies.py (e.g., "stop_atr_mult", "min_confluence", "target_rr")
  - current: the current value
  - proposed: the recommended value
  - rationale: a 1-2 sentence WHY grounded in the stats provided
  - expected_impact: a 1-sentence projection of what changes (e.g., "fewer signals but higher PF in HIGH_VOLATILITY regime")

Hard rules:
  - Reply with ONLY a JSON array (no prose, no markdown fences). Empty array [] is valid if no change is warranted.
  - Never propose changes to: live_trading, LIVE_TRADING, account_id, broker_account, max_daily_loss, risk_per_trade, recovery_mode, vix_filter, daily_loss_cap, circuit_breaker, validated, enabled.
  - Stop ticks must stay in [4, 200]. Size multipliers <= 3.0. risk_per_trade <= $100.
  - If stats show <30 trades, return [] (insufficient evidence).
  - Be conservative: small adjustments preferred over large ones; cite the specific stat.
"""


def _load_current_params(strategy: str) -> dict:
    """Read current parameters for `strategy` from config/strategies.py.

    Reading config is allowed; writing is not. Import is inside the function
    so the existing S9 path remains importable without touching strategies.
    """
    try:
        from config.strategies import STRATEGIES
        return dict(STRATEGIES.get(strategy, {}))
    except Exception as exc:
        logger.warning("could not load config.strategies for %s: %s", strategy, exc)
        return {}


def _warehouse_stats(strategy: str, *,
                     db_path: Optional[Path] = None,
                     min_session_date: Optional[str] = None,
                     include_gross: bool = False) -> dict:
    """Read per-strategy stats from the warehouse. Returns {} on any failure.

    Cross-era P&L caveat (spec section 10): by default this filters to runs
    with ``friction_applied = TRUE`` to keep gross legacy-era pnl_dollars from
    polluting the headline numbers. Pass ``include_gross=True`` to opt in to
    the unfiltered pool (the headline ``friction_applied_mix`` diagnostic
    always returns the full unfiltered split so callers can see the
    population they're operating on).

    Output dict keys:
      n_trades, net_pnl, win_rate, profit_factor, avg_mae_ticks, avg_mfe_ticks,
      by_regime: [{regime, n, net, win_rate}, ...],
      by_tod:    [{tod_bucket, n, net, win_rate}, ...],
      friction_applied_mix: {true: n, false: n} for the FILTERED population,
      friction_applied_mix_all: same split BEFORE the filter applied,
      friction_filter: "friction_net_only" | "all_eras",
      run_count: number of distinct runs feeding the stats,
      date_range: [min_session_date, max_session_date].
    """
    db_path = db_path or WAREHOUSE_DB
    if not db_path.exists():
        logger.warning("warehouse DB not found at %s", db_path)
        return {}
    try:
        import duckdb
    except ImportError:
        logger.warning("duckdb not installed; skipping warehouse stats")
        return {}

    sdf_filter = ""
    params_strat: list = [strategy]
    if min_session_date:
        sdf_filter = " AND t.session_date >= ?"
        params_strat.append(min_session_date)

    # Friction filter: default = friction_net only. With include_gross=True
    # the filter clause is empty (caller takes spec section 10 responsibility).
    friction_clause = "" if include_gross else " AND r.friction_applied = TRUE"
    friction_label  = "all_eras" if include_gross else "friction_net_only"

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        # Pre-filter diagnostic: full friction split BEFORE applying the filter.
        # Always returned so the caller can prove the filter did what they expect.
        fa_mix_all = con.execute(f"""
            SELECT r.friction_applied, COUNT(*) AS n
            FROM trades_ct t JOIN runs r ON t.run_id = r.run_id
            WHERE t.strategy = ?{sdf_filter}
            GROUP BY r.friction_applied
        """, params_strat).fetchall()

        head = con.execute(f"""
            SELECT COUNT(*)                                          AS n,
                   SUM(t.pnl_dollars)                                AS net,
                   AVG(CASE WHEN t.pnl_dollars > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   SUM(CASE WHEN t.pnl_dollars > 0 THEN t.pnl_dollars ELSE 0 END) /
                       NULLIF(-SUM(CASE WHEN t.pnl_dollars < 0 THEN t.pnl_dollars ELSE 0 END), 0) AS pf,
                   AVG(t.mae_ticks)                                  AS avg_mae,
                   AVG(t.mfe_ticks)                                  AS avg_mfe,
                   MIN(t.session_date)                               AS min_d,
                   MAX(t.session_date)                               AS max_d,
                   COUNT(DISTINCT t.run_id)                          AS n_runs
            FROM trades_ct t JOIN runs r ON t.run_id = r.run_id
            WHERE t.strategy = ?{sdf_filter}{friction_clause}
        """, params_strat).fetchone()
        if not head or head[0] == 0:
            return {
                "n_trades": 0,
                "friction_filter": friction_label,
                "friction_applied_mix_all": {str(r[0]): int(r[1]) for r in fa_mix_all},
            }

        by_regime = con.execute(f"""
            SELECT t.regime, COUNT(*) AS n,
                   SUM(t.pnl_dollars) AS net,
                   AVG(CASE WHEN t.pnl_dollars > 0 THEN 1.0 ELSE 0.0 END) AS wr
            FROM trades_ct t JOIN runs r ON t.run_id = r.run_id
            WHERE t.strategy = ?{sdf_filter}{friction_clause}
            GROUP BY t.regime ORDER BY net DESC NULLS LAST
        """, params_strat).fetchall()

        by_tod = con.execute(f"""
            SELECT t.tod_bucket, COUNT(*) AS n,
                   SUM(t.pnl_dollars) AS net,
                   AVG(CASE WHEN t.pnl_dollars > 0 THEN 1.0 ELSE 0.0 END) AS wr
            FROM trades_ct t JOIN runs r ON t.run_id = r.run_id
            WHERE t.strategy = ?{sdf_filter}{friction_clause}
            GROUP BY t.tod_bucket ORDER BY net DESC NULLS LAST
        """, params_strat).fetchall()

        fa_mix_filtered = con.execute(f"""
            SELECT r.friction_applied, COUNT(*) AS n
            FROM trades_ct t JOIN runs r ON t.run_id = r.run_id
            WHERE t.strategy = ?{sdf_filter}{friction_clause}
            GROUP BY r.friction_applied
        """, params_strat).fetchall()
    finally:
        con.close()

    return {
        "n_trades":                 int(head[0] or 0),
        "net_pnl":                  float(head[1] or 0.0),
        "win_rate":                 float(head[2] or 0.0),
        "profit_factor":            float(head[3]) if head[3] is not None else None,
        "avg_mae_ticks":            float(head[4]) if head[4] is not None else None,
        "avg_mfe_ticks":            float(head[5]) if head[5] is not None else None,
        "date_range":               [str(head[6]) if head[6] else None,
                                     str(head[7]) if head[7] else None],
        "run_count":                int(head[8] or 0),
        "friction_filter":          friction_label,
        "by_regime":                [{"regime": r[0], "n": int(r[1]),
                                      "net": float(r[2] or 0), "win_rate": float(r[3] or 0)}
                                     for r in by_regime],
        "by_tod":                   [{"tod_bucket": r[0], "n": int(r[1]),
                                      "net": float(r[2] or 0), "win_rate": float(r[3] or 0)}
                                     for r in by_tod],
        "friction_applied_mix":     {str(r[0]): int(r[1]) for r in fa_mix_filtered},
        "friction_applied_mix_all": {str(r[0]): int(r[1]) for r in fa_mix_all},
    }


def _build_user_prompt(strategy: str, stats: dict, current_params: dict) -> str:
    """Compact per-call prompt for the Claude advisor."""
    return (
        f"## Strategy\n{strategy}\n\n"
        f"## Current parameters (from config/strategies.py)\n"
        f"{json.dumps(current_params, indent=2, default=str)}\n\n"
        f"## Warehouse backtest stats\n"
        f"{json.dumps(stats, indent=2, default=str)}\n\n"
        f"Return your recommendations as a JSON array now."
    )


def request_claude_recommendations(strategy: str, stats: dict,
                                   current_params: dict, *,
                                   timeout_s: float = CLAUDE_TIMEOUT_S,
                                   model: str = CLAUDE_MODEL) -> tuple[list[dict], str]:
    """Call Claude for parameter recommendations.

    Returns a tuple ``(recommendations, status)`` where status is one of:
      - ``"ok"``                  — Claude returned a parseable list (may be
                                    empty if Claude judged no changes warranted)
      - ``"no_api_key"``          — ANTHROPIC_API_KEY missing
      - ``"insufficient_trades"`` — n_trades < 30 gate fired
      - ``"sdk_missing"``         — anthropic SDK import failure
      - ``"api_error:<ClassName>"``— Claude SDK raised an exception
      - ``"parse_error"``         — Claude returned non-list or unparseable JSON
      - ``"non_dict_items"``      — list returned but no items survived the
                                    isinstance(r, dict) filter
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set; returning []")
        return [], "no_api_key"
    if stats.get("n_trades", 0) < 30:
        logger.info("only %d trades for %s; skipping API call",
                    stats.get("n_trades", 0), strategy)
        return [], "insufficient_trades"
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; returning []")
        return [], "sdk_missing"
    try:
        client = anthropic.Anthropic(timeout=timeout_s)
        # Prompt caching intentionally not enabled: the system prompt is ~400
        # tokens, below Haiku 4.5's ~1024-token minimum for cache_control to
        # fire. Re-evaluate if/when the prompt grows or we batch many strategies
        # per run (would amortize a per-call refit).
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            system=_ADVISOR_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": _build_user_prompt(strategy, stats, current_params),
            }],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        text = text.strip()
        # Strip stray markdown fence if the model emitted one
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        recs = json.loads(text)
        if not isinstance(recs, list):
            logger.warning("Claude returned non-list payload; treating as []")
            return [], "parse_error"
        # Ensure each rec carries the strategy field (Claude may omit it).
        for r in recs:
            if isinstance(r, dict):
                r.setdefault("strategy", strategy)
        filtered = [r for r in recs if isinstance(r, dict)]
        # If the raw list was non-empty but every item was non-dict, that is a
        # distinct failure mode from Claude legitimately returning [].
        if recs and not filtered:
            return [], "non_dict_items"
        return filtered, "ok"
    except Exception as exc:
        logger.warning("Claude advisor call failed for %s: %s", strategy, exc)
        return [], f"api_error:{type(exc).__name__}"


_STATUS_EXPLANATIONS: dict[str, str] = {
    "ok":                   "",
    "no_api_key":           "ANTHROPIC_API_KEY not set; doc shows stats only",
    "insufficient_trades":  "fewer than 30 trades after filtering; advisor abstained",
    "sdk_missing":          "anthropic SDK not installed",
    "parse_error":          "Claude returned non-JSON or non-list payload",
    "non_dict_items":       "Claude returned a list but items weren't dicts",
}


def _status_line(status: str) -> str:
    """Render the **Claude status:** preamble bullet."""
    if status == "ok":
        return f"- **Claude status:** `{status}`"
    # api_error:* has a dynamic class name; look up by prefix.
    if status.startswith("api_error:"):
        explanation = "Anthropic SDK raised; recommendations may be incomplete"
    else:
        explanation = _STATUS_EXPLANATIONS.get(status, "unknown status")
    return f"- **Claude status:** `{status}` ⚠ ({explanation})"


def _render_advisor_markdown(strategy: str, stats: dict, current_params: dict,
                             accepted: list[dict], rejected: list[tuple[dict, str]],
                             *, generated_at: Optional[datetime] = None,
                             claude_status: str = "ok") -> str:
    gen = generated_at or datetime.now(timezone.utc)
    fmix_all = stats.get("friction_applied_mix_all", {})
    fmix_f   = stats.get("friction_applied_mix", {})
    era      = stats.get("friction_filter", "?")
    pf_val   = stats.get('profit_factor')
    pf_str   = f"{pf_val:.3f}" if isinstance(pf_val, (int, float)) else "n/a"
    lines: list[str] = [
        f"# Adaptive Params Recommendations — `{strategy}`",
        "",
        f"- **Generated:** {gen.isoformat()}",
        f"- **Source:** DuckDB warehouse (`data/warehouse/phoenix.duckdb`) — Claude `{CLAUDE_MODEL}` advisor",
        f"- **Era filter:** `{era}`  "
        f"(filtered split: {fmix_f}, unfiltered split: {fmix_all})",
        _status_line(claude_status),
        f"- **n_trades:** {stats.get('n_trades', 0)}  "
        f"net=${stats.get('net_pnl', 0):.2f}  "
        f"win_rate={stats.get('win_rate', 0)*100:.1f}%  "
        f"PF={pf_str}",
        f"- **Date range:** {stats.get('date_range', [None, None])}",
        "",
        "> **Operator-action required.** This module never writes "
        "`config/strategies.py`. Review the proposals below; if accepted, "
        "manually edit `config/strategies.py` and restart the bot.",
        "",
        "## Accepted proposals",
        "",
    ]
    if not accepted:
        lines.append("_None._")
    else:
        for i, rec in enumerate(accepted, 1):
            lines += [
                f"### {i}. `{rec.get('param', '?')}`",
                "",
                f"- **Current:** `{rec.get('current', '?')!r}`",
                f"- **Proposed:** `{rec.get('proposed', '?')!r}`",
                f"- **Rationale:** {rec.get('rationale', '(n/a)')}",
                f"- **Expected impact:** {rec.get('expected_impact', '(n/a)')}",
                "",
            ]
    lines += ["## Rejected proposals (failed safety bounds)", ""]
    if not rejected:
        lines.append("_None._")
    else:
        for rec, reason in rejected:
            lines.append(f"- `{rec.get('param', '?')}` → `{rec.get('proposed', '?')!r}` "
                         f"— **{reason}**")
    lines += [
        "",
        "## Warehouse stats snapshot (input to Claude)",
        "",
        "```json",
        json.dumps(stats, indent=2, default=str),
        "```",
        "",
    ]
    return "\n".join(lines)


def write_advisor_recommendation_doc(
    strategy: str, stats: dict, current_params: dict,
    recommendations: list[dict], *,
    out_dir: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
    claude_status: str = "ok",
) -> Path:
    """Validate each recommendation, render Markdown, write under out_dir.

    Filename pattern: ``<YYYY-MM-DD>_<strategy>.md``. Overwrites within the
    same day so re-running for the same strategy yields a fresh doc.

    ``claude_status`` is forwarded straight to ``_render_advisor_markdown`` so
    the preamble shows whether the advisor call succeeded or what went wrong.
    """
    out_dir = out_dir or ADVISOR_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    gen = generated_at or datetime.now(timezone.utc)

    accepted: list[dict] = []
    rejected: list[tuple[dict, str]] = []
    for rec in recommendations:
        if not isinstance(rec, dict):
            rejected.append(({"raw": rec}, "not a dict"))
            continue
        rec.setdefault("strategy", strategy)
        v = validate_recommendation(rec)
        if v.accepted:
            accepted.append(rec)
        else:
            rejected.append((rec, v.reason))

    md = _render_advisor_markdown(strategy, stats, current_params,
                                   accepted, rejected, generated_at=gen,
                                   claude_status=claude_status)
    # Filename includes UTC time so same-day re-runs accumulate rather than
    # silently overwriting (the operator may be mid-review of an earlier doc).
    fname = f"{gen.strftime('%Y-%m-%dT%H-%M-%SZ')}_{_slug(strategy)}.md"
    path = out_dir / fname
    path.write_text(md, encoding="utf-8")
    return path


def _suggest_strategy_name(strategy: str) -> str:
    """Return a 'did you mean: X' fragment, or empty string if no close match."""
    try:
        import difflib
        from config.strategies import STRATEGIES
        matches = difflib.get_close_matches(strategy, list(STRATEGIES.keys()),
                                            n=3, cutoff=0.6)
        if matches:
            return f" (did you mean: {', '.join(matches)}?)"
    except Exception:
        pass
    return ""


class UnknownStrategyError(SystemExit):
    """Strategy name is not present in STRATEGIES and has zero warehouse trades."""
    pass


def run_warehouse_advisor(strategy: str, *,
                          db_path: Optional[Path] = None,
                          out_dir: Optional[Path] = None,
                          timeout_s: float = CLAUDE_TIMEOUT_S,
                          min_session_date: Optional[str] = None,
                          include_gross: bool = False) -> Path:
    """Top-level Phase 3 entry point. Returns the path of the written doc.

    Raises ``UnknownStrategyError`` (a SystemExit subclass) when the name is
    not present in ``config/strategies.STRATEGIES`` AND has zero rows in the
    warehouse. A *known* strategy with zero warehouse rows still gets a doc
    (its absence is itself a signal worth recording).

    Even if the API key is missing or the call fails, a doc IS written that
    records the warehouse stats and an empty proposals section. The operator
    can read it and act manually.
    """
    stats = _warehouse_stats(strategy, db_path=db_path,
                             min_session_date=min_session_date,
                             include_gross=include_gross)
    current_params = _load_current_params(strategy)

    # Fail-loud: typo guard. If the strategy is unknown to config AND has no
    # warehouse trades, an empty doc would be misleading. Exit 2 with a hint.
    if not current_params and stats.get("n_trades", 0) == 0:
        hint = _suggest_strategy_name(strategy)
        raise UnknownStrategyError(
            f"unknown strategy: {strategy!r}{hint}\n"
            f"       Strategy is absent from config/strategies.STRATEGIES AND "
            f"has zero rows in the warehouse trades table."
        )

    if stats.get("n_trades", 0) == 0:
        logger.info("strategy=%s known to config but has no warehouse trades; "
                    "writing empty doc", strategy)
        recs: list[dict] = []
        claude_status = "insufficient_trades"
    else:
        recs, claude_status = request_claude_recommendations(
            strategy, stats, current_params, timeout_s=timeout_s,
        )
    return write_advisor_recommendation_doc(
        strategy, stats, current_params, recs, out_dir=out_dir,
        claude_status=claude_status,
    )


def main_advisor(argv=None) -> int:
    """CLI entry: ``python -m agents.adaptive_params analyze --strategy <name>``.

    Exits 0 on success (even if no proposals were generated), 2 if
    ANTHROPIC_API_KEY is required but missing AND the operator did not pass
    --skip-api-check.
    """
    import argparse
    ap = argparse.ArgumentParser(
        prog="agents.adaptive_params",
        description="Phase 3 warehouse-driven Claude advisor for strategy params.",
    )
    ap.add_argument("--strategy", required=True, help="Strategy key to analyze")
    ap.add_argument("--db", default=None, help="Override warehouse DB path")
    ap.add_argument("--out-dir", default=None,
                    help=f"Override output dir (default: {ADVISOR_OUT_DIR})")
    ap.add_argument("--timeout", type=float, default=CLAUDE_TIMEOUT_S,
                    help="Claude API timeout in seconds (default: 30)")
    ap.add_argument("--since", default=None,
                    help="Filter warehouse stats to session_date >= this ISO date")
    ap.add_argument("--include-gross", action="store_true",
                    help="Include legacy friction-GROSS rows in the stats pool "
                         "(default: friction-net only; see warehouse spec section 10).")
    ap.add_argument("--allow-no-api-key", action="store_true",
                    help="Run even without ANTHROPIC_API_KEY (writes stats-only doc)")
    args = ap.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY") and not args.allow_no_api_key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set in the environment.\n"
            "       Set it (e.g., via .env) and re-run, OR pass "
            "--allow-no-api-key to write a stats-only doc with no Claude call.",
            file=__import__("sys").stderr,
        )
        return 2

    db_path = Path(args.db) if args.db else None
    out_dir = Path(args.out_dir) if args.out_dir else None
    try:
        path = run_warehouse_advisor(
            args.strategy, db_path=db_path, out_dir=out_dir,
            timeout_s=args.timeout, min_session_date=args.since,
            include_gross=args.include_gross,
        )
    except UnknownStrategyError as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 2
    print(f"wrote: {path}")
    return 0


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
    # Phase 3 (warehouse advisor):
    "WAREHOUSE_DB",
    "ADVISOR_OUT_DIR",
    "CLAUDE_MODEL",
    "CLAUDE_TIMEOUT_S",
    "request_claude_recommendations",
    "write_advisor_recommendation_doc",
    "run_warehouse_advisor",
    "main_advisor",
]


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main_advisor())
