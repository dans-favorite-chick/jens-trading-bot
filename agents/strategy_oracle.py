"""
Phoenix Strategy Oracle -- Phase 4D successor (replaces agents/historical_learner.py).

Spec: docs/superpowers/specs/2026-05-31-strategy-oracle-design.md
Plan: docs/superpowers/plans/2026-05-31-strategy-oracle-plan.md

Structural invariants enforced by CI:
- No imports from bots/, core/, bridge/, data_feeds/.
- All outputs land under logs/oracle/<mode>/.
- The LLM never computes a number -- only narrates the facts panel.
- Every proposal requires human approval downstream (Phase 4E).

WHAT THIS MODULE DOES
---------------------
1. Pre-flight gates (no LLM): API key, warehouse, output dirs.
2. Regime stability gate (no LLM, mode-aware).
3. Deterministic compute layer builds facts.json (no LLM).
4. Anthropic tool-use loop drives a 5-tool conversation:
       think / fetch_strategy_stats / check_regime / write_finding / propose_change
5. Phase 3 verifier runs over the narrative; rejections are stripped.
6. Outputs: debrief.md, facts.json, audit.jsonl, pending_changes.json.

ALLOWED IMPORTS
---------------
- Standard library only.
- analytics.compute_engine / regime_gate / verifier / prepared_queries
- anthropic (Claude SDK)
- numpy / pandas (transitively)

FORBIDDEN IMPORTS (CI invariant)
--------------------------------
- bots/, core/, bridge/, data_feeds/
- Any module on the live-trading path.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as _dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal, TextIO

from anthropic import Anthropic

from analytics import compute_engine, prepared_queries, regime_gate, verifier

__no_trade_path_imports__ = True

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

WAREHOUSE_PATH = prepared_queries.WAREHOUSE_PATH

# Logs root -- monkey-patched in tests to redirect to tmp.
LOGS_ORACLE_ROOT = Path(r"C:\Trading Project\phoenix_bot\logs\oracle")

# Anthropic model + sampling.
MODEL_ID = "claude-sonnet-4-6"
TEMPERATURE = 0.0
MAX_TOOL_ITERATIONS = 25  # hard ceiling on loop length

# Canonical terminal stop_reason values per the Anthropic messages API.
# Any of these means "no further turn possible" -- the loop must exit
# regardless of whether tool_use blocks are still present, otherwise a
# max_tokens response carrying a partial tool_use will spin until the
# hard ceiling, burning tokens.
TERMINAL_STOP_REASONS = (
    "end_turn", "max_tokens", "stop_sequence", "pause_turn", "refusal",
)

# Training cutoff used to gate the "look-ahead" downgrade note in the
# debrief. Windows ending after this date no longer overlap the model's
# training data, so the note should be suppressed.
LOOKAHEAD_CUTOFF_DATE = "2026-01-31"

# Forbidden module roots -- the CI invariant scanner refuses these.
_FORBIDDEN_ROOTS = ("bots", "core", "bridge", "data_feeds")

Mode = Literal["research", "weekly", "daily"]

MODE_CONFIG: dict[str, dict] = {
    "research": {
        "window_days": 1825,
        "token_budget": 200_000,
        "can_propose": True,
        "skip_regime_gate": False,
    },
    "weekly": {
        "window_days": 7,
        "token_budget": 80_000,
        "can_propose": True,
        "skip_regime_gate": False,
    },
    "daily": {
        "window_days": 1,
        "token_budget": 15_000,
        "can_propose": False,
        "skip_regime_gate": True,
    },
}

# ---------------------------------------------------------------------------
# Tool surface (exactly 5)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "think",
        "description": (
            "Reason about the next step. Required before any state-changing "
            "call. No side effects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Step-by-step internal reasoning.",
                },
            },
            "required": ["reasoning"],
        },
    },
    {
        "name": "fetch_strategy_stats",
        "description": (
            "Return the full pre-computed panel for one strategy from "
            "facts.json. Pure dict lookup -- no recomputation possible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"strategy": {"type": "string"}},
            "required": ["strategy"],
        },
    },
    {
        "name": "check_regime",
        "description": (
            "Return the pre-computed regime stability verdict. Mirrors the "
            "pre-flight check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "write_finding",
        "description": (
            "Append a structured finding to facts.json findings array. "
            "Blocks n<30 and verdict='FAILED'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "Stable unique id, e.g. "
                        "'bias_momentum_post_915_decay_2026-05-31'"
                    ),
                },
                "strategy": {"type": "string"},
                "verdict": {
                    "type": "string",
                    "enum": [
                        "CONFIRMED", "REFUTED", "INCONCLUSIVE",
                        "INSUFFICIENT_DATA",
                    ],
                },
                "confidence": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH"],
                },
                "sample_size": {"type": "integer"},
                "rationale": {
                    "type": "string",
                    "description": (
                        "Plain-English explanation citing facts.json values."
                    ),
                },
                "supporting_metrics": {
                    "type": "object",
                    "description": "Key metrics referenced.",
                },
                "expires_after_days": {"type": "integer", "default": 30},
            },
            "required": [
                "id", "strategy", "verdict", "confidence",
                "sample_size", "rationale",
            ],
        },
    },
    {
        "name": "propose_change",
        "description": (
            "Stage a parameter proposal in pending_changes.json. Requires "
            "confidence in {MEDIUM, HIGH} and a finding_id. REJECTED in "
            "daily mode."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["LONG", "SHORT", "BOTH"],
                },
                "parameter_name": {"type": "string"},
                "current_value": {},
                "proposed_value": {},
                "rationale": {"type": "string"},
                "confidence": {
                    "type": "string",
                    "enum": ["MEDIUM", "HIGH"],
                },
                "sample_size": {"type": "integer"},
                "finding_id": {"type": "string"},
                "expected_improvement": {"type": "string"},
            },
            "required": [
                "strategy", "direction", "parameter_name", "current_value",
                "proposed_value", "rationale", "confidence", "sample_size",
                "finding_id",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Run-context dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _RunCtx:
    """State threaded through the tool dispatcher.

    Holds the mutable facts panel (write_finding appends), the audit handle,
    the staged proposal list, and identifying metadata so each audit row
    can be reconstructed.
    """
    mode: str
    facts: dict
    audit_fh: TextIO | None
    run_date: str
    pending_proposals: list[dict]


# ---------------------------------------------------------------------------
# CI invariant scanner
# ---------------------------------------------------------------------------

def _scan_source_for_forbidden_imports(source: str) -> None:
    """Raise RuntimeError if `source` imports any forbidden trade-path root.

    Walks the AST of the supplied text looking for ``import`` and ``from ...``
    statements that reference a module whose first dotted component is one
    of `_FORBIDDEN_ROOTS`.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".", 1)[0]
                if root in _FORBIDDEN_ROOTS:
                    raise RuntimeError(
                        f"Forbidden trade-path import detected: {alias.name!r}"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".", 1)[0]
            if root in _FORBIDDEN_ROOTS:
                raise RuntimeError(
                    f"Forbidden trade-path import detected: {mod!r}"
                )


def _ci_invariant_check() -> None:
    """Run the forbidden-import scan against THIS module's source file."""
    src = Path(__file__).read_text(encoding="utf-8")
    _scan_source_for_forbidden_imports(src)


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _write_audit(fh: TextIO | None, event: dict) -> None:
    """Append one JSON line to the audit handle (no-op when None).

    Flushes immediately so a crash mid-run still leaves the event on disk
    -- the audit is the operator's only record of what the LLM did before
    the crash.
    """
    if fh is None:
        return
    try:
        fh.write(json.dumps(event, default=str) + "\n")
        fh.flush()
    except (TypeError, ValueError, OSError) as e:
        # An unserializable input or disk/IO failure should never crash
        # the orchestrator. Log and continue.
        logger.warning("audit write failed: %s; skipping", e)


def _open_audit(mode: str, today: str, root: Path) -> TextIO:
    out_dir = root / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    # Append mode so same-day re-runs (e.g. operator triggers weekly twice
    # to debug something) don't truncate the prior audit. One audit file
    # per (mode, date) accumulates events from every run that day.
    return open(out_dir / f"{today}_audit.jsonl", "a", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _tool_think(args: dict, _ctx: _RunCtx) -> dict:
    reasoning = args.get("reasoning") or ""
    return {"ok": True, "reasoning_received": bool(reasoning)}


def _tool_fetch_strategy_stats(args: dict, ctx: _RunCtx) -> dict:
    strategy = args.get("strategy")
    panel = ctx.facts.get("strategies", {}).get(strategy)
    if panel is None:
        return {
            "ok": False,
            "error": f"strategy {strategy!r} not found in facts panel",
        }
    return {"ok": True, "strategy": strategy, "panel": panel}


def _tool_check_regime(_args: dict, ctx: _RunCtx) -> dict:
    return {"ok": True, "regime": ctx.facts.get("regime", {})}


_VALID_CONFIDENCES_FINDING = ("LOW", "MEDIUM", "HIGH")
_VALID_VERDICTS = (
    "CONFIRMED", "REFUTED", "INCONCLUSIVE", "INSUFFICIENT_DATA",
)


def _tool_write_finding(args: dict, ctx: _RunCtx) -> dict:
    try:
        n = int(args.get("sample_size", 0))
    except (TypeError, ValueError):
        n = 0
    if n < 30:
        return {
            "ok": False,
            "error": f"sample_size={n} below floor (30 required)",
        }
    # Normalize verdict / confidence so LLM-supplied 'medium' / 'confirmed'
    # don't slip through. FAILED is a legacy reject keyword caught here
    # before the enum check so the operator sees a more specific message.
    verdict = (args.get("verdict") or "").strip().upper()
    if verdict == "FAILED":
        return {
            "ok": False,
            "error": "verdict 'FAILED' is not an allowed finding verdict",
        }
    if verdict not in _VALID_VERDICTS:
        return {
            "ok": False,
            "error": (
                f"verdict={verdict!r} not in "
                f"{sorted(_VALID_VERDICTS)}"
            ),
        }
    confidence = (args.get("confidence") or "").strip().upper()
    if confidence not in _VALID_CONFIDENCES_FINDING:
        return {
            "ok": False,
            "error": (
                f"confidence={confidence!r} not in "
                f"{sorted(_VALID_CONFIDENCES_FINDING)}"
            ),
        }
    finding = {
        "id": args.get("id"),
        "strategy": args.get("strategy"),
        "verdict": verdict,
        "confidence": confidence,
        "sample_size": n,
        "rationale": args.get("rationale", ""),
        "supporting_metrics": args.get("supporting_metrics", {}),
        "expires_after_days": int(args.get("expires_after_days", 30) or 30),
        "recorded_at": ctx.run_date,
        "run_mode": ctx.mode,
    }
    ctx.facts.setdefault("findings", []).append(finding)
    return {"ok": True, "finding_id": finding["id"]}


def _tool_propose_change(args: dict, ctx: _RunCtx) -> dict:
    if ctx.mode == "daily":
        return {
            "ok": False,
            "error": (
                "propose_change is disabled in daily mode; daily produces "
                "findings only"
            ),
        }
    # Normalize confidence to upper-case so LLM variants like 'medium'
    # don't slip through.
    confidence = (args.get("confidence") or "").strip().upper()
    if confidence not in ("MEDIUM", "HIGH"):
        return {
            "ok": False,
            "error": (
                f"confidence={confidence!r} is not in {{MEDIUM, HIGH}}; "
                "proposal rejected"
            ),
        }
    try:
        n = int(args.get("sample_size", 0))
    except (TypeError, ValueError):
        n = 0
    if n < 30:
        return {
            "ok": False,
            "error": f"sample_size={n} below floor (30 required)",
        }
    if not args.get("finding_id"):
        return {
            "ok": False,
            "error": "finding_id is required to link evidence",
        }
    # Spec sec 12d: `current_value` MUST come from the compute layer via
    # AST parse of config/strategies.py -- never from the LLM. Override
    # whatever the model supplied with the real value; if the lookup
    # fails (missing key, malformed config), fall back to the LLM-supplied
    # value and log a warning so the auditor can spot the discrepancy.
    strategy_name = args.get("strategy")
    parameter_name = args.get("parameter_name")
    current_value: Any = args.get("current_value")
    try:
        current_value = prepared_queries.current_param_value(
            strategy_name, parameter_name,
        )
    except (KeyError, FileNotFoundError, ValueError) as e:
        logger.warning(
            "could not AST-parse current value for %s.%s: %s; "
            "using LLM-supplied value",
            strategy_name, parameter_name, e,
        )
    proposal = {
        "proposed_at": ctx.run_date,
        "run_mode": ctx.mode,
        "strategy": strategy_name,
        "direction": args.get("direction"),
        "parameter_name": parameter_name,
        "current_value": current_value,
        "proposed_value": args.get("proposed_value"),
        "rationale": args.get("rationale", ""),
        "confidence": confidence,
        "sample_size": n,
        "finding_id": args.get("finding_id"),
        "expected_improvement": args.get("expected_improvement", ""),
        "metrics": _proposal_metrics_snapshot(ctx, strategy_name),
        "status": "PENDING_HUMAN_REVIEW",
        "approved": False,
        "applied": False,
    }
    ctx.pending_proposals.append(proposal)
    return {"ok": True, "proposal_index": len(ctx.pending_proposals) - 1}


def _proposal_metrics_snapshot(ctx: _RunCtx, strategy: Any) -> dict:
    """Pull a small subset of metrics from the facts panel for traceability."""
    panel = ctx.facts.get("strategies", {}).get(strategy, {})
    metrics = panel.get("metrics", {})
    return {
        "dsr": metrics.get("dsr"),
        "psr": metrics.get("psr"),
        "bhy_p": metrics.get("bhy_p_adjusted"),
        "wfe_ratio": metrics.get("wfe_ratio"),
        "n_trades": metrics.get("n_trades"),
    }


_TOOL_FNS = {
    "think": _tool_think,
    "fetch_strategy_stats": _tool_fetch_strategy_stats,
    "check_regime": _tool_check_regime,
    "write_finding": _tool_write_finding,
    "propose_change": _tool_propose_change,
}


def _dispatch_tool(name: str, args: dict, ctx: _RunCtx) -> dict:
    """Run a tool and append one audit row. Returns the tool result dict."""
    fn = _TOOL_FNS.get(name)
    if fn is None:
        result = {"ok": False, "error": f"unknown tool {name!r}"}
    else:
        try:
            result = fn(args or {}, ctx)
        except Exception as e:  # defensive -- tool fns are small & well-tested
            logger.exception("tool %s raised", name)
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _write_audit(
        ctx.audit_fh,
        {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "mode": ctx.mode,
            "tool": name,
            "input": args,
            "result_ok": bool(result.get("ok")),
            "result_summary": {
                k: v for k, v in result.items() if k != "panel"
            },
        },
    )
    return result


# ---------------------------------------------------------------------------
# Pre-flight & warehouse
# ---------------------------------------------------------------------------

def _run_preflight(mode: str) -> dict:
    """Cheap synchronous gates. Returns ``{ok, reason, warehouse_path}``.

    Does NOT check the API key -- that's handled in `run()` so it can map
    cleanly to `halted_no_api_key`. Does NOT check regime -- that's a
    separate phase.
    """
    if mode not in MODE_CONFIG:
        return {"ok": False, "reason": f"unknown mode {mode!r}"}
    wh = Path(WAREHOUSE_PATH)
    if not wh.exists():
        return {
            "ok": False,
            "reason": f"warehouse not found: {wh}",
            "warehouse_path": str(wh),
        }
    return {"ok": True, "warehouse_path": str(wh)}


def _open_warehouse_conn(path: str):
    """Open a read-only DuckDB connection. Indirection for monkey-patching."""
    return prepared_queries.open_conn(path)


def _check_regime_gate(conn, mode: str) -> dict:
    """Thin wrapper so tests can substitute the regime verdict."""
    return regime_gate.check_regime_stability(conn, mode)


# ---------------------------------------------------------------------------
# Facts builder (deterministic compute layer)
# ---------------------------------------------------------------------------

def _build_facts(conn, mode: str, regime: dict, root: Path) -> dict:
    """Build the immutable facts panel for this run.

    For each strategy with >= 30 friction-applied trades in the window,
    compute the full risk-metric panel via ``compute_engine``. Also loads
    prior findings (last 30 days) and computes delta vs the most recent
    facts.json under ``root``.

    `root` is threaded explicitly so test fixtures and callers can
    redirect the prior-findings/prior-facts lookup without relying on
    the module-level LOGS_ORACLE_ROOT.
    """
    cfg = MODE_CONFIG[mode]
    window = int(cfg["window_days"])

    strategies = prepared_queries.strategies_with_trades(conn, window, min_n=30)

    # Single pass: pull each strategy's trades exactly once and reuse the
    # cached DataFrame for both the effective-N calculation and the
    # per-strategy metric panel. This halves warehouse round-trips and
    # eliminates a TOCTOU window where a concurrent ingest could change
    # the data between the two scans.
    strategy_trades: dict[str, Any] = {}
    for strat in strategies:
        trades_df = prepared_queries.trades_for_strategy(conn, strat, window)
        if trades_df.empty:
            continue
        strategy_trades[strat] = trades_df

    trial_returns: list = []
    for trades_df in strategy_trades.values():
        if "pnl_dollars" in trades_df.columns:
            arr = trades_df["pnl_dollars"].to_numpy(dtype=float)
            trial_returns.append(arr)
    n_eff = compute_engine.compute_effective_n(trial_returns) if trial_returns else 1

    panel: dict[str, dict] = {}
    for strat, trades_df in strategy_trades.items():
        wfa = prepared_queries.wfa_summary_for_strategy(conn, strat)
        panel[strat] = compute_engine.compute_strategy_metrics(
            trades_df, wfa, n_eff,
        )
        # Spec sec 8a-8f: per-strategy splits (hour-of-day, regime,
        # direction, MAE/MFE distribution by direction, confluence lift).
        # Aggregate-only -- no row-level data -- so the token budget for
        # the LLM panel stays bounded. The 5-tool invariant is preserved:
        # fetch_strategy_stats returns the full panel dict, which now
        # includes a "splits" key.
        splits: dict[str, Any] = {}
        try:
            splits["by_hour_ct"] = prepared_queries.panel_by_hour_ct(
                conn, strat, window,
            ).to_dict("records")
            splits["by_regime"] = prepared_queries.panel_by_regime(
                conn, strat, window,
            ).to_dict("records")
            splits["by_direction"] = prepared_queries.panel_by_direction(
                conn, strat, window,
            ).to_dict("records")
            splits["mae_mfe_long"] = prepared_queries.mae_mfe_distribution(
                conn, strat, "LONG", window,
            ).to_dict("records")
            splits["mae_mfe_short"] = prepared_queries.mae_mfe_distribution(
                conn, strat, "SHORT", window,
            ).to_dict("records")
            splits["confluence_lift"] = prepared_queries.confluence_lift(
                conn, strat, window,
            ).to_dict("records")
        except Exception as e:  # noqa: BLE001 -- query failures must not crash facts build
            logger.warning(
                "splits computation failed for strategy %s: %s", strat, e,
            )
            splits = {}
        panel[strat]["splits"] = splits

    today = _today_str()
    window_start = (_dt.date.today() - _dt.timedelta(days=window)).isoformat()
    window_end = today

    facts: dict = {
        "run_mode": mode,
        "run_date": today,
        "window_start": window_start,
        "window_end": window_end,
        "regime": regime,
        "n_trials_effective": int(n_eff),
        "strategies": panel,
        "findings": [],
        "prior_findings_loaded": _load_prior_findings(root),
    }
    prior = _load_prior_facts(mode, root)
    facts["delta_vs_prior"] = compute_engine.compute_delta_vs_prior(
        facts, prior,
    )
    return facts


def _today_str() -> str:
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# Prior findings memory load (spec sec 13)
# ---------------------------------------------------------------------------

def _load_prior_findings(root: Path,
                         max_age_days: int = 30,
                         cap: int = 10) -> list[dict]:
    """Walk ``<root>/*/<date>_facts.json``, gather findings within
    `max_age_days`, drop expired ones, cap at `cap` most recent rows.

    `root` is the oracle logs root; callers (notably `_build_facts` and
    `run`) thread the value through so test fixtures can redirect it
    without monkey-patching the module-level constant.

    Best-effort. Any unparseable file is skipped with a debug log.
    """
    out: list[dict] = []
    if not root.exists():
        return out
    today = _dt.date.today()
    for mode_dir in root.iterdir():
        if not mode_dir.is_dir():
            continue
        for fp in mode_dir.glob("*_facts.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.debug("skipping unreadable facts file %s: %s", fp, e)
                continue
            run_date = data.get("run_date")
            try:
                rd = _dt.date.fromisoformat(run_date) if run_date else None
            except (TypeError, ValueError):
                rd = None
            if rd is None:
                continue
            age = (today - rd).days
            if age > max_age_days:
                continue
            for f in data.get("findings", []) or []:
                expiry = int(f.get("expires_after_days", 30) or 30)
                if age > expiry:
                    continue
                # Record what mode/run the finding came from so the prompt
                # context can disambiguate.
                row = dict(f)
                row.setdefault("source_mode", data.get("run_mode"))
                row.setdefault("source_run_date", run_date)
                out.append(row)
    # Sort by source_run_date descending, cap.
    out.sort(key=lambda r: r.get("source_run_date") or "", reverse=True)
    return out[:cap]


def _load_prior_facts(mode: str, root: Path) -> dict | None:
    """Return the most recent prior facts.json for delta computation.

    Looks first in `<mode>/` then `research/` as a fallback baseline.
    `root` is threaded in so the lookup respects test redirection.
    """
    for sub in (mode, "research"):
        d = root / sub
        if not d.exists():
            continue
        files = sorted(d.glob("*_facts.json"))
        if not files:
            continue
        try:
            return json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("could not read prior facts %s: %s", files[-1], e)
            continue
    return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

_STRUCTURAL_RULES = """\
STRUCTURAL RULES (NON-NEGOTIABLE)
1. You are the narrator, never the calculator. Do NOT invent numbers.
   Every number you cite in the final report MUST appear in facts.json.
   The Phase 3 verifier rejects any number that is not in facts.
2. NEVER write SQL. The five tools are your only data interface.
3. NEVER claim causation about macro events ("the Fed pivoted",
   "CPI surprised", "rally after FOMC"). Conditional associations only.
4. NEVER reference market events near a YYYY-MM date (e.g. "the market
   crashed in 2022-10"). The lookahead scanner rejects these.
5. Sample-size floor n>=30 for every finding. Below that, log nothing.
6. propose_change requires confidence in {MEDIUM, HIGH} and a finding_id
   linking back to a previously written finding. Daily mode rejects all
   proposals.
7. Long and short are SEPARATE channels. Never propose a long parameter
   for a short setup or vice versa.
8. Token budget for this run is __TOKEN_BUDGET__. Plan accordingly.
"""

_CONFIDENCE_RUBRIC = """\
CONFIDENCE TIERS
- INSUFFICIENT: n<30 -- no finding written
- LOW:          30<=n<100 -- finding only, no proposal
- MEDIUM:       100<=n<200 with WFA pass -- proposal eligible
- HIGH:         n>=200 with WFA pass -- proposal eligible
WFA agreement = mean_oos_pf >= 0.6 * mean_is_pf.
"""

_PROPOSAL_GATES = """\
PROPOSAL GATES (ALL must pass before propose_change)
- DSR >= 0.95
- PSR >= 0.90
- BHY-adjusted p <= 0.05
- MinTRL met (n_trades >= compute_min_trl)
- WFA pass
- Regime stable (z_score within +/-1.5)
A proposal failing ANY gate is logged as a finding but NOT staged.
"""

_TOOL_SURFACE = """\
TOOL SURFACE (5 tools exactly)
- think(reasoning)              : reason aloud; required before state changes
- fetch_strategy_stats(strategy): full panel for one strategy
- check_regime()                : current regime stability verdict
- write_finding(...)            : append to findings array (n>=30 enforced)
- propose_change(...)           : stage a parameter proposal (HUMAN review)
"""

SYSTEM_PROMPT_RESEARCH = (
    "You are the Phoenix Strategy Oracle in RESEARCH mode. "
    "You are reviewing 5 years of trade data to (re-)establish ground "
    "truth and propose foundational parameter changes.\n\n"
    + _STRUCTURAL_RULES + "\n"
    + _CONFIDENCE_RUBRIC + "\n"
    + _PROPOSAL_GATES + "\n"
    + _TOOL_SURFACE
)

SYSTEM_PROMPT_WEEKLY = (
    "You are the Phoenix Strategy Oracle in WEEKLY mode. "
    "You are reviewing the trailing 7 days against the 5-year baseline. "
    "Focus on materially-changed metrics flagged in delta_vs_prior.\n\n"
    + _STRUCTURAL_RULES + "\n"
    + _CONFIDENCE_RUBRIC + "\n"
    + _PROPOSAL_GATES + "\n"
    + _TOOL_SURFACE
)

SYSTEM_PROMPT_DAILY = (
    "You are the Phoenix Strategy Oracle in DAILY mode. "
    "This is a light pass over a single day. Output is preliminary and "
    "NOT actionable. propose_change is DISABLED. Confidence ceiling is "
    "LOW. Your job is anomaly surfacing, nothing else.\n\n"
    + _STRUCTURAL_RULES + "\n"
    + _CONFIDENCE_RUBRIC + "\n"
    + _TOOL_SURFACE
)

_PROMPTS_BY_MODE = {
    "research": SYSTEM_PROMPT_RESEARCH,
    "weekly":   SYSTEM_PROMPT_WEEKLY,
    "daily":    SYSTEM_PROMPT_DAILY,
}


def _build_compact_summary(facts: dict) -> str:
    """Render a Markdown one-line-per-strategy summary table."""
    rows = ["| strategy | n | DSR | PSR | gates_pass | top_failed |",
            "|---|---:|---:|---:|:---:|---|"]
    for name, panel in facts.get("strategies", {}).items():
        m = panel.get("metrics", {})
        g = panel.get("gates", {})
        failed = g.get("failed_gates") or []
        top_failed = failed[0] if failed else "-"
        rows.append(
            f"| {name} | {m.get('n_trades', 0)} | "
            f"{_fmt_num(m.get('dsr'))} | {_fmt_num(m.get('psr'))} | "
            f"{'PASS' if g.get('all_pass_for_proposal') else 'FAIL'} | "
            f"{top_failed} |"
        )
    return "\n".join(rows)


def _fmt_num(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _build_initial_user_message(facts: dict) -> str:
    summary = _build_compact_summary(facts)
    prior = facts.get("prior_findings_loaded") or []
    prior_block = "_(no prior findings in last 30 days)_"
    if prior:
        lines = []
        for f in prior:
            lines.append(
                f"- [{f.get('source_run_date')}] {f.get('id')} "
                f"({f.get('strategy')}, {f.get('confidence')}): "
                f"{f.get('rationale', '')[:140]}"
            )
        prior_block = "\n".join(lines)

    delta = facts.get("delta_vs_prior", {})
    delta_block = "baseline run" if delta.get("is_baseline") else json.dumps(
        delta.get("summary", {}), default=str,
    )

    return (
        f"# Phoenix Strategy Oracle -- {facts.get('run_mode')} run\n"
        f"Run date: {facts.get('run_date')}\n"
        f"Window: {facts.get('window_start')} -> {facts.get('window_end')}\n\n"
        f"## Compact Summary (full panels available via fetch_strategy_stats)\n"
        f"{summary}\n\n"
        f"## Prior findings (last 30 days)\n"
        f"{prior_block}\n\n"
        f"## Delta vs prior run\n"
        f"{delta_block}\n\n"
        f"Begin by calling `think` to plan, then fetch panels for the "
        f"strategies that look most interesting. Conclude with a "
        f"narrative summary."
    )


# ---------------------------------------------------------------------------
# Anthropic loop
# ---------------------------------------------------------------------------

def _extract_text_from_blocks(blocks: list[Any]) -> str:
    parts = []
    for b in blocks:
        if getattr(b, "type", None) == "text":
            parts.append(getattr(b, "text", "") or "")
    return "\n".join(p for p in parts if p)


def _extract_tool_uses(blocks: list[Any]) -> list[Any]:
    return [b for b in blocks if getattr(b, "type", None) == "tool_use"]


def _block_to_message_content(b: Any) -> dict:
    """Convert an SDK content block back to a dict for assistant echo."""
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": getattr(b, "text", "")}
    if t == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(b, "id", "tu_unknown"),
            "name": getattr(b, "name", ""),
            "input": getattr(b, "input", {}) or {},
        }
    if t == "thinking":
        return {"type": "thinking", "thinking": getattr(b, "thinking", "")}
    # Unknown block type -- emit a stringified placeholder so the conversation
    # remains well-formed.
    return {"type": "text", "text": ""}


def _run_llm_loop(client: Anthropic,
                   system_prompt: str,
                   user_msg: str,
                   ctx: _RunCtx,
                   token_budget: int) -> tuple[str, int]:
    """Drive the tool-use conversation. Returns (final_narrative, total_tokens).

    The loop is bounded by:
      - MAX_TOOL_ITERATIONS (hard ceiling)
      - `token_budget` (soft cap; once exceeded we ask for a graceful wrap)
    """
    messages: list[dict] = [{"role": "user", "content": user_msg}]
    total_tokens = 0
    final_text = ""
    budget_nudge_sent = False

    for iteration in range(MAX_TOOL_ITERATIONS):
        resp = client.messages.create(
            model=MODEL_ID,
            max_tokens=4096,
            temperature=TEMPERATURE,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            total_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            total_tokens += int(getattr(usage, "output_tokens", 0) or 0)

        blocks = list(getattr(resp, "content", []) or [])
        text = _extract_text_from_blocks(blocks)
        if text:
            final_text = text  # latest text wins

        tool_uses = _extract_tool_uses(blocks)
        stop_reason = getattr(resp, "stop_reason", "")

        # Exit on any canonical terminal stop_reason BEFORE dispatching
        # further tool calls. This prevents a max_tokens-truncated turn
        # (which can still carry partial tool_use blocks) from spinning
        # the loop until MAX_TOOL_ITERATIONS.
        if stop_reason in TERMINAL_STOP_REASONS:
            return final_text, total_tokens
        if not tool_uses:
            # stop_reason was something non-terminal (e.g. "tool_use")
            # but the response carries no tool_use blocks. That's a
            # malformed response shape -- bail out rather than loop.
            logger.warning(
                "LLM returned stop_reason=%r with no tool_uses; exiting loop",
                stop_reason,
            )
            return final_text, total_tokens

        # Echo the assistant content (text + tool_use blocks) into the
        # conversation history, then build a single user message with
        # tool_result blocks for each tool_use this turn.
        messages.append(
            {"role": "assistant",
             "content": [_block_to_message_content(b) for b in blocks]}
        )
        tool_result_blocks: list[dict] = []
        for tu in tool_uses:
            result = _dispatch_tool(
                getattr(tu, "name", ""),
                getattr(tu, "input", {}) or {},
                ctx,
            )
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": getattr(tu, "id", "tu_unknown"),
                "content": json.dumps(result, default=str),
                "is_error": not bool(result.get("ok")),
            })

        # Soft token-budget enforcement: append the nudge as an extra
        # text block INSIDE the tool_result user message (not as a second
        # user message). The Anthropic API requires strict
        # user/assistant alternation -- appending a second consecutive
        # user message returns 400 on the next call.
        if total_tokens >= token_budget and not budget_nudge_sent:
            logger.warning(
                "token budget %d exceeded (used %d); requesting wrap",
                token_budget, total_tokens,
            )
            tool_result_blocks.append({
                "type": "text",
                "text": (
                    "Token budget exceeded. Stop calling tools and "
                    "produce a concise final narrative on your next turn."
                ),
            })
            budget_nudge_sent = True

        messages.append({"role": "user", "content": tool_result_blocks})

    logger.warning("MAX_TOOL_ITERATIONS (%d) reached; returning latest text",
                    MAX_TOOL_ITERATIONS)
    return final_text, total_tokens


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------

def _strip_rejected_findings(facts: dict,
                              rejected_ids: list[str]) -> tuple[dict, int]:
    """Remove rejected findings in-place. Returns (facts, n_removed)."""
    findings = facts.get("findings", []) or []
    keep = [f for f in findings if f.get("id") not in set(rejected_ids)]
    n_removed = len(findings) - len(keep)
    facts["findings"] = keep
    return facts, n_removed


def _apply_downgrades(facts: dict, downgrades: list[dict]) -> None:
    """Mutate findings to reflect Phase 3 verifier confidence downgrades."""
    by_id = {f.get("id"): f for f in facts.get("findings", []) or []}
    for d in downgrades:
        fid = d.get("finding_id")
        if fid in by_id:
            by_id[fid]["confidence_original"] = d.get("old")
            by_id[fid]["confidence"] = d.get("new")
            by_id[fid].setdefault("downgrade_reason", d.get("reason"))


def _render_report_card(facts: dict) -> str:
    n_total = len(facts.get("strategies", {}))
    n_pass = 0
    failed_gate_counts: dict[str, int] = {}
    for panel in facts.get("strategies", {}).values():
        g = panel.get("gates", {})
        if g.get("all_pass_for_proposal"):
            n_pass += 1
        for fg in g.get("failed_gates") or []:
            failed_gate_counts[fg] = failed_gate_counts.get(fg, 0) + 1
    lines = [
        "## Report Card",
        f"- {n_total} strategies analyzed",
        f"- {n_pass} cleared all gates -> proposals",
    ]
    for gate, count in sorted(failed_gate_counts.items(),
                                key=lambda kv: -kv[1]):
        lines.append(f"- {count} strategies failed gate {gate}")
    return "\n".join(lines)


def _render_regime_section(regime: dict) -> str:
    if regime.get("mode_skipped"):
        return "## Regime\nDaily mode -- regime gate intentionally skipped."
    if regime.get("stable"):
        z = regime.get("z_score")
        return (
            f"## Regime\nStable (z={_fmt_num(z)} vs 1.5 threshold). "
            f"Analysis proceeded normally."
        )
    return (
        f"## Regime\nUNSTABLE -- analysis halted. "
        f"Warning: {regime.get('warning')}"
    )


def _render_delta_section(facts: dict) -> str:
    """Render the 'Delta vs Last Run' section per spec sec 12b.

    Lists strategies whose materially_changed flag is set, with the
    DSR/WR/n deltas and any tier flip. Returns an empty string if no
    deltas are present (baseline or unchanged).
    """
    delta = facts.get("delta_vs_prior") or {}
    if delta.get("is_baseline"):
        return ""
    strategies = delta.get("strategies") or {}
    lines: list[str] = []
    for strat, d in sorted(strategies.items()):
        if not d.get("materially_changed"):
            continue
        tier_change = d.get("tier_change")
        marker = f" ({tier_change})" if tier_change else ""
        dsr_d = float(d.get("dsr_delta", 0) or 0)
        wr_d = float(d.get("wr_delta", 0) or 0)
        n_d = int(d.get("n_delta", 0) or 0)
        lines.append(
            f"- {strat}{marker}: DSR d={dsr_d:+.3f}, "
            f"WR d={wr_d * 100:+.1f}%, n d={n_d:+d}"
        )
    if not lines:
        return ""
    return "## Delta vs Last Run\n" + "\n".join(lines) + "\n"


def _render_proposals_section(pending_this_run: list[dict] | None) -> str:
    """Render the 'Proposals' section listing this run's staged proposals.

    Pulls from the in-memory list of proposals staged in THIS specific
    run, not the full pending queue (which can accumulate across runs).
    Returns an empty string if nothing was staged this run.
    """
    if not pending_this_run:
        return ""
    lines = ["## Proposals (review pending_changes.json before approving)"]
    for i, p in enumerate(pending_this_run, 1):
        strat = p.get("strategy")
        param = p.get("parameter_name")
        cur = p.get("current_value")
        proposed = p.get("proposed_value")
        conf = p.get("confidence")
        n = p.get("sample_size")
        why = p.get("rationale", "")
        lines.append(
            f"{i}. {strat}.{param}: {cur!r} -> {proposed!r}"
        )
        lines.append(f"   Confidence: {conf}; sample_size: {n}")
        lines.append(f"   Why: {why}")
    return "\n".join(lines) + "\n"


def _write_debrief(mode: str, today: str, narrative: str,
                    verifier_result: dict | None,
                    facts: dict, regime: dict,
                    root: Path,
                    halted: bool = False,
                    pending_this_run: list[dict] | None = None) -> Path:
    """Assemble logs/oracle/<mode>/<today>_debrief.md."""
    out_dir = root / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}_debrief.md"

    report_card = _render_report_card(facts)
    regime_section = _render_regime_section(regime)
    delta_section = _render_delta_section(facts)
    proposals_section = _render_proposals_section(pending_this_run)

    # Gate the look-ahead note on a date comparison: only render it when
    # the analysis window actually overlaps Claude's training cutoff.
    # Windows that end strictly after the cutoff don't need the
    # confidence downgrade warning.
    lookahead_note: str | None = None
    window_end = facts.get("window_end")
    if window_end:
        try:
            window_end_dt = _dt.date.fromisoformat(window_end)
            cutoff_dt = _dt.date.fromisoformat(LOOKAHEAD_CUTOFF_DATE)
            if window_end_dt <= cutoff_dt:
                lookahead_note = (
                    "## Look-Ahead Note\n"
                    f"Analysis window overlaps Claude's training cutoff "
                    f"(~{LOOKAHEAD_CUTOFF_DATE}). Interpretive findings "
                    f"have confidence downgraded one tier per spec sec 9. "
                    f"Fact transcription unaffected."
                )
        except (TypeError, ValueError):
            # Malformed dates -- skip the note rather than crash.
            lookahead_note = None

    lookahead_block = f"{lookahead_note}\n" if lookahead_note else ""

    if halted:
        body = (
            f"# Phoenix Strategy Oracle -- {mode} Debrief (HALTED)\n\n"
            f"## Status\nThis run halted before producing a narrative.\n\n"
            f"{regime_section}\n\n"
            f"{report_card}\n\n"
            f"{lookahead_block}"
        )
    else:
        rej_note = ""
        if verifier_result is not None:
            n_rej = len(verifier_result.get("rejected_findings", []))
            if n_rej:
                rej_note = (
                    f"\n_Note: {n_rej} finding(s) rejected by Phase 3 "
                    f"verifier; see audit.jsonl for details._\n"
                )
        # Order: narrative -> delta -> proposals -> regime -> report card
        # -> lookahead. The delta + proposals sections are inserted
        # BEFORE the report card per spec sec 12b so the operator sees
        # the actionable diff and staged changes prominently near the top.
        body = (
            f"# Phoenix Strategy Oracle -- {mode} Debrief\n"
            f"## Run date: {today}\n\n"
            f"## Narrative\n{narrative}\n{rej_note}\n"
            f"{delta_section}"
            f"{proposals_section}"
            f"{regime_section}\n\n"
            f"{report_card}\n\n"
            f"{lookahead_block}"
        )

    out_path.write_text(body, encoding="utf-8")
    return out_path


def _write_facts(mode: str, today: str, facts: dict, root: Path) -> Path:
    out_dir = root / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}_facts.json"
    out_path.write_text(json.dumps(facts, indent=2, default=str),
                         encoding="utf-8")
    return out_path


def _save_pending_proposals(new_proposals: list[dict],
                              root: Path) -> Path:
    """Append the new proposals to the shared queue at logs/oracle/pending_changes.json.

    Atomic: read existing -> append -> write to .tmp -> replace.
    """
    out_path = root / "pending_changes.json"
    root.mkdir(parents=True, exist_ok=True)
    existing: dict = {"pending": []}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if "pending" not in existing or not isinstance(existing["pending"], list):
                existing = {"pending": []}
        except (OSError, json.JSONDecodeError):
            existing = {"pending": []}
    existing["pending"].extend(new_proposals)
    # Per-process unique tmp filename so two concurrent oracle runs can't
    # stomp on each other's .tmp file mid-rename. The atomic os.replace
    # still serializes the visible state, but each writer now stages its
    # work in a private location first.
    tmp = out_path.with_name(
        f".pending_changes.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    )
    tmp.write_text(json.dumps(existing, indent=2, default=str),
                    encoding="utf-8")
    os.replace(tmp, out_path)
    return out_path


def _save_research_baseline(facts: dict, root: Path) -> Path:
    out_dir = root / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "baseline_facts.json"
    out_path.write_text(json.dumps(facts, indent=2, default=str),
                         encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(mode: Mode, save_baseline: bool = True,
        client: Anthropic | None = None) -> dict:
    """Main entry. Runs the 4-phase Oracle pipeline.

    Returns a dict with the four output paths plus run-summary counts.
    """
    if mode not in MODE_CONFIG:
        # Use the same full 11-key shape every other halt branch returns
        # so downstream consumers can treat the result dict uniformly.
        return {
            "status": "halted_preflight_failure",
            "mode": mode,
            "reason": f"unknown mode {mode!r}",
            "facts_path": None,
            "debrief_path": None,
            "audit_path": None,
            "pending_changes_path": None,
            "n_findings": 0,
            "n_proposals_staged": 0,
            "n_findings_rejected_by_verifier": 0,
            "regime": {},
            "verifier_result": None,
        }

    # Pre-flight #1: API key.
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {
            "status": "halted_no_api_key",
            "mode": mode,
            "facts_path": None,
            "debrief_path": None,
            "audit_path": None,
            "pending_changes_path": None,
            "n_findings": 0,
            "n_proposals_staged": 0,
            "n_findings_rejected_by_verifier": 0,
            "regime": {},
            "verifier_result": None,
        }

    # Pre-flight #2: warehouse + dirs.
    pre = _run_preflight(mode)
    if not pre.get("ok"):
        return {
            "status": "halted_preflight_failure",
            "mode": mode,
            "facts_path": None,
            "debrief_path": None,
            "audit_path": None,
            "pending_changes_path": None,
            "n_findings": 0,
            "n_proposals_staged": 0,
            "n_findings_rejected_by_verifier": 0,
            "regime": {},
            "verifier_result": None,
            "reason": pre.get("reason"),
        }

    today = _today_str()
    root = LOGS_ORACLE_ROOT
    (root / mode).mkdir(parents=True, exist_ok=True)
    audit_fh = _open_audit(mode, today, root)

    try:
        conn = _open_warehouse_conn(pre["warehouse_path"])
        try:
            regime = _check_regime_gate(conn, mode)
            # Phase 2: deterministic compute layer.
            facts = _build_facts(conn, mode, regime, root)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        # Halt on unstable regime (weekly/research only -- daily skips).
        if (not regime.get("stable")) and not regime.get("mode_skipped"):
            facts_path = _write_facts(mode, today, facts, root)
            debrief_path = _write_debrief(
                mode, today, narrative="(halted on regime instability)",
                verifier_result=None, facts=facts, regime=regime,
                root=root, halted=True,
            )
            audit_fh.close()
            return {
                "status": "halted_regime_unstable",
                "mode": mode,
                "facts_path": str(facts_path),
                "debrief_path": str(debrief_path),
                "audit_path": str(root / mode / f"{today}_audit.jsonl"),
                "pending_changes_path": str(root / "pending_changes.json"),
                "n_findings": 0,
                "n_proposals_staged": 0,
                "n_findings_rejected_by_verifier": 0,
                "regime": regime,
                "verifier_result": None,
            }

        # Phase 3: LLM tool-use loop.
        ctx = _RunCtx(
            mode=mode,
            facts=facts,
            audit_fh=audit_fh,
            run_date=today,
            pending_proposals=[],
        )
        if client is None:
            client = Anthropic()
        system_prompt = _PROMPTS_BY_MODE[mode].replace(
            "__TOKEN_BUDGET__",
            f"{MODE_CONFIG[mode]['token_budget']:,}",
        )
        user_msg = _build_initial_user_message(facts)
        narrative, tokens_used = _run_llm_loop(
            client, system_prompt, user_msg, ctx,
            token_budget=int(MODE_CONFIG[mode]["token_budget"]),
        )

        # Phase 4: verifier.
        # Compute lookahead_active from window vs cutoff so the per-finding
        # downgrade only fires when the analysis window actually overlaps
        # Claude's training data. Without this, runs whose window ends
        # AFTER the cutoff still see surviving INTERPRETATION findings
        # downgraded one tier with no explanation in the debrief (the
        # debrief note IS conditional, but the downgrade logic was not).
        try:
            window_end_dt = _dt.date.fromisoformat(facts.get("window_end", ""))
            cutoff_dt = _dt.date.fromisoformat(LOOKAHEAD_CUTOFF_DATE)
            lookahead_active = window_end_dt <= cutoff_dt
        except (TypeError, ValueError):
            lookahead_active = True  # safe fallback
        vresult = verifier.verify_report(
            facts=facts,
            narrative_md=narrative,
            findings=list(facts.get("findings", []) or []),
            lookahead_active=lookahead_active,
        )
        # Strip rejected findings + apply downgrades.
        facts, n_removed = _strip_rejected_findings(
            facts, vresult.get("rejected_findings", []),
        )
        _apply_downgrades(facts, vresult.get("downgrades", []))

        # A proposal whose linked finding was rejected has lost its
        # evidentiary basis -- drop it before persisting to
        # pending_changes.json so an operator never sees an
        # orphan-evidence proposal.
        rejected_ids = set(vresult.get("rejected_findings", []) or [])
        if rejected_ids:
            filtered_proposals = [
                p for p in ctx.pending_proposals
                if p.get("finding_id") not in rejected_ids
            ]
            n_proposals_stripped = (
                len(ctx.pending_proposals) - len(filtered_proposals)
            )
            if n_proposals_stripped > 0:
                logger.info(
                    "stripped %d proposals linked to rejected findings",
                    n_proposals_stripped,
                )
            ctx.pending_proposals = filtered_proposals

        # Phase 5: write outputs.
        facts_path = _write_facts(mode, today, facts, root)
        debrief_path = _write_debrief(
            mode, today, narrative=narrative,
            verifier_result=vresult, facts=facts, regime=regime,
            root=root, halted=False,
            pending_this_run=list(ctx.pending_proposals),
        )
        pending_path = _save_pending_proposals(ctx.pending_proposals, root)
        if mode == "research" and save_baseline:
            _save_research_baseline(facts, root)

        audit_fh.flush()
        audit_fh.close()

        return {
            "status": "complete",
            "mode": mode,
            "facts_path": str(facts_path),
            "debrief_path": str(debrief_path),
            "audit_path": str(root / mode / f"{today}_audit.jsonl"),
            "pending_changes_path": str(pending_path),
            "n_findings": len(facts.get("findings", []) or []),
            "n_proposals_staged": len(ctx.pending_proposals),
            "n_findings_rejected_by_verifier": n_removed,
            "regime": regime,
            "verifier_result": vresult,
            "tokens_used": tokens_used,
        }
    except Exception as e:  # pragma: no cover -- defensive
        logger.exception("strategy_oracle.run crashed in %s mode", mode)
        try:
            audit_fh.close()
        except Exception:  # noqa: BLE001
            pass
        # An unhandled exception inside the post-preflight pipeline is a
        # runtime error, NOT a preflight failure. Distinguish the two so
        # operators can tell whether the system never started vs. crashed
        # mid-flight.
        return {
            "status": "halted_runtime_error",
            "mode": mode,
            "reason": f"{type(e).__name__}: {e}",
            "facts_path": None,
            "debrief_path": None,
            "audit_path": None,
            "pending_changes_path": None,
            "n_findings": 0,
            "n_proposals_staged": 0,
            "n_findings_rejected_by_verifier": 0,
            "regime": {},
            "verifier_result": None,
        }


__all__ = [
    "run",
    "Mode",
    "MODE_CONFIG",
    "TOOLS",
    "SYSTEM_PROMPT_RESEARCH",
    "SYSTEM_PROMPT_WEEKLY",
    "SYSTEM_PROMPT_DAILY",
    "WAREHOUSE_PATH",
    "LOGS_ORACLE_ROOT",
    "MODEL_ID",
    "TERMINAL_STOP_REASONS",
    "LOOKAHEAD_CUTOFF_DATE",
    "__no_trade_path_imports__",
]
