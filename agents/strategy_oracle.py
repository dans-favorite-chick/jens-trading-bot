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
    """Append one JSON line to the audit handle (no-op when None)."""
    if fh is None:
        return
    try:
        fh.write(json.dumps(event, default=str) + "\n")
    except (TypeError, ValueError) as e:
        # An unserializable input should never crash the orchestrator.
        logger.warning("audit write failed: %s; skipping", e)


def _open_audit(mode: str, today: str, root: Path) -> TextIO:
    out_dir = root / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    return open(out_dir / f"{today}_audit.jsonl", "w", encoding="utf-8")


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
    verdict = args.get("verdict", "")
    if verdict == "FAILED":
        return {
            "ok": False,
            "error": "verdict 'FAILED' is not an allowed finding verdict",
        }
    finding = {
        "id": args.get("id"),
        "strategy": args.get("strategy"),
        "verdict": verdict,
        "confidence": args.get("confidence"),
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
    confidence = args.get("confidence", "")
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
    proposal = {
        "proposed_at": ctx.run_date,
        "run_mode": ctx.mode,
        "strategy": args.get("strategy"),
        "direction": args.get("direction"),
        "parameter_name": args.get("parameter_name"),
        "current_value": args.get("current_value"),
        "proposed_value": args.get("proposed_value"),
        "rationale": args.get("rationale", ""),
        "confidence": confidence,
        "sample_size": n,
        "finding_id": args.get("finding_id"),
        "expected_improvement": args.get("expected_improvement", ""),
        "metrics": _proposal_metrics_snapshot(ctx, args.get("strategy")),
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

def _build_facts(conn, mode: str, regime: dict) -> dict:
    """Build the immutable facts panel for this run.

    For each strategy with >= 30 friction-applied trades in the window,
    compute the full risk-metric panel via ``compute_engine``. Also loads
    prior findings (last 30 days) and computes delta vs the most recent
    facts.json.
    """
    cfg = MODE_CONFIG[mode]
    window = int(cfg["window_days"])

    strategies = prepared_queries.strategies_with_trades(conn, window, min_n=30)
    panel: dict[str, dict] = {}
    trial_returns: list = []
    for strat in strategies:
        trades_df = prepared_queries.trades_for_strategy(conn, strat, window)
        if trades_df.empty:
            continue
        if "pnl_dollars" in trades_df.columns:
            arr = trades_df["pnl_dollars"].to_numpy(dtype=float)
            trial_returns.append(arr)
    n_eff = compute_engine.compute_effective_n(trial_returns) if trial_returns else 1

    for strat in strategies:
        trades_df = prepared_queries.trades_for_strategy(conn, strat, window)
        if trades_df.empty:
            continue
        wfa = prepared_queries.wfa_summary_for_strategy(conn, strat)
        panel[strat] = compute_engine.compute_strategy_metrics(
            trades_df, wfa, n_eff,
        )

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
        "prior_findings_loaded": _load_prior_findings(),
    }
    prior = _load_prior_facts(mode)
    facts["delta_vs_prior"] = compute_engine.compute_delta_vs_prior(
        facts, prior,
    )
    return facts


def _today_str() -> str:
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# Prior findings memory load (spec sec 13)
# ---------------------------------------------------------------------------

def _load_prior_findings(max_age_days: int = 30,
                         cap: int = 10) -> list[dict]:
    """Walk ``logs/oracle/*/<date>_facts.json``, gather findings within
    `max_age_days`, drop expired ones, cap at `cap` most recent rows.

    Best-effort. Any unparseable file is skipped with a debug log.
    """
    out: list[dict] = []
    root = LOGS_ORACLE_ROOT
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


def _load_prior_facts(mode: str) -> dict | None:
    """Return the most recent prior facts.json for delta computation.

    Looks first in `<mode>/` then `research/` as a fallback baseline.
    """
    root = LOGS_ORACLE_ROOT
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

        if not tool_uses or stop_reason == "end_turn":
            # No more tool calls. We're done.
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
        messages.append({"role": "user", "content": tool_result_blocks})

        # Soft token-budget enforcement: nudge the LLM to wrap up.
        if total_tokens >= token_budget:
            logger.warning(
                "token budget %d exceeded (used %d); requesting wrap",
                token_budget, total_tokens,
            )
            messages.append({
                "role": "user",
                "content": (
                    "Token budget exceeded. Stop calling tools and produce a "
                    "concise final narrative now."
                ),
            })

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


def _write_debrief(mode: str, today: str, narrative: str,
                    verifier_result: dict | None,
                    facts: dict, regime: dict,
                    root: Path,
                    halted: bool = False) -> Path:
    """Assemble logs/oracle/<mode>/<today>_debrief.md."""
    out_dir = root / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}_debrief.md"

    n_proposals = sum(
        1 for f in facts.get("findings", []) or []
    )  # number of findings; proposals tracked separately
    report_card = _render_report_card(facts)
    regime_section = _render_regime_section(regime)

    lookahead_note = (
        "## Look-Ahead Note\n"
        "Analysis window overlaps Claude's training cutoff. "
        "Interpretive findings downgraded one tier per spec sec 9. "
        "Fact transcription unaffected."
    )

    if halted:
        body = (
            f"# Phoenix Strategy Oracle -- {mode} Debrief (HALTED)\n\n"
            f"## Status\nThis run halted before producing a narrative.\n\n"
            f"{regime_section}\n\n"
            f"{report_card}\n\n"
            f"{lookahead_note}\n"
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
        body = (
            f"# Phoenix Strategy Oracle -- {mode} Debrief\n"
            f"## Run date: {today}\n\n"
            f"## Narrative\n{narrative}\n{rej_note}\n"
            f"{regime_section}\n\n"
            f"{report_card}\n\n"
            f"{lookahead_note}\n"
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
    tmp = out_path.with_suffix(".json.tmp")
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
        return {
            "status": "halted_preflight_failure",
            "mode": mode,
            "reason": f"unknown mode {mode!r}",
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
            facts = _build_facts(conn, mode, regime)
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
        vresult = verifier.verify_report(
            facts=facts,
            narrative_md=narrative,
            findings=list(facts.get("findings", []) or []),
            lookahead_active=True,
        )
        # Strip rejected findings + apply downgrades.
        facts, n_removed = _strip_rejected_findings(
            facts, vresult.get("rejected_findings", []),
        )
        _apply_downgrades(facts, vresult.get("downgrades", []))

        # Phase 5: write outputs.
        facts_path = _write_facts(mode, today, facts, root)
        debrief_path = _write_debrief(
            mode, today, narrative=narrative,
            verifier_result=vresult, facts=facts, regime=regime,
            root=root, halted=False,
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
        return {
            "status": "halted_preflight_failure",
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
    "__no_trade_path_imports__",
]
