"""Approve an AI proposal — branch, apply, test, STOP (S9).

Usage: python tools/approve_proposal.py <proposal_id> [--dry-run]

Steps:
  1. Load proposal_<id>.md from logs/ai_learner/proposals/.
  2. Extract the machine-readable change block.
  3. Re-validate against SafetyBounds (belt + suspenders).
  4. Create git branch ai-proposal/<id> off current HEAD.
  5. Apply the change to config/strategies.py.
  6. Run `pytest --tb=no -q`.
  7. Print a summary. NEVER merges.

--dry-run: perform validation + preview the edited file contents but
writes nothing to disk and creates no branch. Used by tests.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.adaptive_params import (
    PROPOSALS_DIR,
    validate_recommendation,
)

STRATEGIES_FILE = _ROOT / "config" / "strategies.py"

_JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]*?)```", re.MULTILINE)


class ApprovalError(RuntimeError):
    pass


def extract_change(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ApprovalError(f"No ```json block in {md_path.name}")
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        raise ApprovalError(f"Bad JSON in {md_path.name}: {e}")
    for k in ("strategy", "param", "current", "proposed"):
        if k not in data:
            raise ApprovalError(f"Missing field '{k}' in proposal JSON")
    return data


def _format_literal(v: Any) -> str:
    return repr(v)


def apply_change_to_source(source: str, change: dict) -> str:
    """Return modified source text. Uses AST to locate STRATEGIES[strategy][param].

    Conservative: we do a targeted regex replace on the value in the
    dict literal for the matching strategy section. If we cannot
    unambiguously locate the key, we raise — this is SAFER than risking
    corruption.
    """
    strategy = change["strategy"]
    param = change["param"]
    proposed = change["proposed"]

    # Parse to confirm the key exists under STRATEGIES or STRATEGY_DEFAULTS.
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ApprovalError(f"strategies.py doesn't parse: {e}")

    found_container = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in (
                    "STRATEGIES", "STRATEGY_DEFAULTS"
                ):
                    # is strategy key + param key present?
                    if tgt.id == "STRATEGIES":
                        # Dict of strategy -> dict
                        if isinstance(node.value, ast.Dict):
                            for k, v in zip(node.value.keys, node.value.values):
                                if (isinstance(k, ast.Constant) and k.value == strategy
                                        and isinstance(v, ast.Dict)):
                                    for ik, iv in zip(v.keys, v.values):
                                        if isinstance(ik, ast.Constant) and ik.value == param:
                                            found_container = ("STRATEGIES", strategy)
                                            break
                    else:  # STRATEGY_DEFAULTS — allow strategy="" or "global"
                        if isinstance(node.value, ast.Dict):
                            for ik, iv in zip(node.value.keys, node.value.values):
                                if isinstance(ik, ast.Constant) and ik.value == param:
                                    found_container = ("STRATEGY_DEFAULTS", None)
                                    break

    if not found_container:
        raise ApprovalError(
            f"Could not locate {strategy}.{param} in STRATEGIES/STRATEGY_DEFAULTS"
        )

    # Targeted regex replacement within the strategy block (STRATEGIES case)
    # or directly (STRATEGY_DEFAULTS case).
    if found_container[0] == "STRATEGIES":
        # Find the opening of the strategy's section, then replace first
        # occurrence of "param": <value>, inside it (simple, but scoped).
        # Match: "<strategy>": { ... "param": VALUE,
        strat_re = re.compile(
            r'("' + re.escape(strategy) + r'"\s*:\s*\{)'
        )
        m = strat_re.search(source)
        if not m:
            raise ApprovalError(f"strategy opener for {strategy} not found in source")
        start = m.end()
        # Find end of this dict block by brace counting.
        depth = 1
        i = start
        while i < len(source) and depth > 0:
            c = source[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        block_end = i  # exclusive
        block = source[start:block_end]
        param_re = re.compile(
            r'("' + re.escape(param) + r'"\s*:\s*)([^,\n\}]+)'
        )
        new_block, n = param_re.subn(
            lambda mm: mm.group(1) + _format_literal(proposed), block, count=1
        )
        if n == 0:
            raise ApprovalError(f"param {param} not found in {strategy} block")
        return source[:start] + new_block + source[block_end:]
    else:
        # STRATEGY_DEFAULTS top-level param
        defaults_re = re.compile(r'(STRATEGY_DEFAULTS\s*=\s*\{)')
        m = defaults_re.search(source)
        if not m:
            raise ApprovalError("STRATEGY_DEFAULTS opener not found")
        start = m.end()
        depth = 1
        i = start
        while i < len(source) and depth > 0:
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
            i += 1
        block_end = i
        block = source[start:block_end]
        param_re = re.compile(
            r'("' + re.escape(param) + r'"\s*:\s*)([^,\n\}]+)'
        )
        new_block, n = param_re.subn(
            lambda mm: mm.group(1) + _format_literal(proposed), block, count=1
        )
        if n == 0:
            raise ApprovalError(f"param {param} not found in STRATEGY_DEFAULTS")
        return source[:start] + new_block + source[block_end:]


def approve(proposal_id: str, *, dry_run: bool = False,
            proposals_dir: Path | None = None,
            strategies_file: Path | None = None) -> dict:
    pd = proposals_dir or PROPOSALS_DIR
    sf = strategies_file or STRATEGIES_FILE
    md = pd / f"proposal_{proposal_id}.md"
    if not md.exists():
        raise ApprovalError(f"Proposal not found: {md}")

    change = extract_change(md)

    # Belt + suspenders: re-validate.
    v = validate_recommendation({
        "strategy": change["strategy"],
        "param": change["param"],
        "current": change["current"],
        "proposed": change["proposed"],
    })
    if not v.accepted:
        raise ApprovalError(f"Re-validation failed: {v.reason}")

    original = sf.read_text(encoding="utf-8")
    new_source = apply_change_to_source(original, change)

    # Sanity: new source must still parse.
    try:
        ast.parse(new_source)
    except SyntaxError as e:
        raise ApprovalError(f"Edited source has syntax error: {e}")

    summary: dict = {
        "proposal_id": proposal_id,
        "strategy": change["strategy"],
        "param": change["param"],
        "current": change["current"],
        "proposed": change["proposed"],
        "dry_run": dry_run,
        "branch": None,
        "tests": None,
        "applied": False,
    }

    if dry_run:
        summary["preview_bytes"] = len(new_source)
        return summary

    branch = f"ai-proposal/{proposal_id}"
    try:
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=_ROOT, check=True, capture_output=True,
        )
        summary["branch"] = branch
    except subprocess.CalledProcessError as e:
        raise ApprovalError(f"git branch failed: {e.stderr.decode(errors='ignore')}")

    sf.write_text(new_source, encoding="utf-8")
    summary["applied"] = True

    # Run tests
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=no", "-q"],
        cwd=_ROOT, capture_output=True, text=True,
    )
    summary["tests"] = {
        "returncode": r.returncode,
        "stdout_tail": "\n".join(r.stdout.splitlines()[-20:]),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Approve an AI proposal")
    ap.add_argument("proposal_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        summary = approve(args.proposal_id, dry_run=args.dry_run)
    except ApprovalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print("=" * 60)
    print(f"Proposal: {summary['proposal_id']}")
    print(f"  {summary['strategy']}.{summary['param']}: "
          f"{summary['current']!r} -> {summary['proposed']!r}")
    if summary["dry_run"]:
        print("  [DRY RUN] no changes written")
        return 0
    print(f"  Branch : {summary['branch']}")
    t = summary["tests"] or {}
    print(f"  Tests  : returncode={t.get('returncode')}")
    print("  ---- pytest tail ----")
    print(t.get("stdout_tail", ""))
    print("=" * 60)
    print("NEXT: review the diff, merge manually if good:")
    print(f"  git diff main...{summary['branch']}")
    print(f"  git checkout main && git merge --no-ff {summary['branch']}")
    return 0 if (t.get("returncode") == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
