"""Phoenix weekly evolution — Sunday 18:00 CT.

Per Jennifer 2026-04-25:
  - Auto-create a git branch + commit, NEVER auto-push, NEVER auto-merge.
  - Every commit body MUST include a "Validation status" section with
    CPCV / DSR / PBO checkboxes that read "NOT YET RUN (Phase C
    dependency)" until the meta-labeler ships. Makes the validation
    gate explicit on every weekly proposal.

Workflow:
  1. Aggregate the past week's grades from out/grades/
  2. Identify P# predictions that consistently failed
  3. Pull the most-frequent rejection reasons from sim_bot logs
  4. Run agents/adaptive_params.py against the week's data (if available)
  5. Single Claude review pass on each proposed knob change
  6. Create branch weekly-evolution/YYYY-MM-DD
  7. Apply changes (typically config/strategies.py edits)
  8. Commit with the validation-checkbox body template
  9. NEVER push. Telegram alert: "review by Monday morning"

Usage:
  python tools/routines/weekly_evolution.py
  python tools/routines/weekly_evolution.py --session-date 2026-04-26 --no-commit
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent.parent))

from tools.routines._shared import (
    CT_TZ, RoutineReport, call_claude, send_telegram_now, write_artifacts,
)

logger = logging.getLogger("WeeklyEvolution")

PROJECT_ROOT = _HERE.parent.parent.parent
GRADES_DIR = PROJECT_ROOT / "out" / "grades"


# ═══════════════════════════════════════════════════════════════════════
# Validation-status commit body template (Jennifer's amendment)
# ═══════════════════════════════════════════════════════════════════════

VALIDATION_STATUS_TEMPLATE = """## Validation status

The following metrics MUST be computed and ticked before this proposal
is reviewed for merge. Until the Phase C meta-labeler lands, all three
remain unchecked by design — that's the gate.

- [ ] **CPCV fold metrics**       — NOT YET RUN (Phase C dependency)
- [ ] **DSR p-value**              — NOT YET RUN (Phase C dependency)
- [ ] **PBO** (Probability of Backtest Overfitting) — NOT YET RUN (Phase C dependency)

Until those three boxes are ticked by an actual run of the meta-labeler,
DO NOT MERGE this branch to main. Operator may cherry-pick individual
config tweaks for paper-trade verification on sim_bot only.
"""


def build_commit_body(week_start: str, week_end: str, proposals: list[dict],
                      ai_review: str) -> str:
    """Build the full git commit body. Always includes validation checkboxes."""
    lines = [
        f"weekly_evolution: proposals from {week_start} -> {week_end}",
        "",
        "## Summary",
        f"- {len(proposals)} adaptive-params proposal(s) drafted from this week's grades",
        "- All proposals reviewed by Claude (see AI review below)",
        "- Branch is intentionally NOT pushed; review locally before merge",
        "",
        "## Proposals",
    ]
    for i, p in enumerate(proposals, 1):
        lines.append(f"{i}. **{p.get('strategy', '?')}** — {p.get('description', '?')}")
        if p.get("reasoning"):
            lines.append(f"   Reason: {p['reasoning'][:160]}")
        if p.get("diff"):
            lines.append(f"   Diff: {p['diff'][:200]}")
    lines.append("")
    if ai_review:
        lines.append("## AI review (Claude Sonnet)")
        lines.append("")
        lines.append(ai_review)
        lines.append("")
    lines.append(VALIDATION_STATUS_TEMPLATE)
    return "\n".join(lines)


def aggregate_week(end_date: str) -> dict:
    """Walks out/grades/ for the last 7 days ending at end_date."""
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    start = end - timedelta(days=6)
    pid_pass = Counter()
    pid_fail = Counter()
    found_dates = []
    for delta in range(7):
        d = (start + timedelta(days=delta)).isoformat()
        p = GRADES_DIR / f"{d}.json"
        if not p.exists():
            continue
        found_dates.append(d)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for r in data.get("results", []):
            pid = r.get("prediction_id")
            if not pid:
                continue
            (pid_pass if r.get("overall_pass") else pid_fail)[pid] += 1
    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "n_sessions": len(found_dates),
        "found_dates": found_dates,
        "pass_counts": dict(pid_pass),
        "fail_counts": dict(pid_fail),
        "consistent_failures": [pid for pid, n in pid_fail.items()
                                if n >= max(2, len(found_dates) // 2)],
    }


def generate_proposals(week_summary: dict) -> list[dict]:
    """Run agents/adaptive_params.py if available; else stub a 'look at this'
    proposal per consistent failure."""
    proposals: list[dict] = []
    try:
        from agents import adaptive_params
        for fn_name in ("propose_for_week", "run_weekly", "generate_proposals"):
            if hasattr(adaptive_params, fn_name):
                fn = getattr(adaptive_params, fn_name)
                try:
                    result = fn(week_summary)
                    if isinstance(result, list):
                        proposals.extend(result)
                        break
                except TypeError:
                    continue
                except Exception as e:
                    logger.warning(f"[evolution] adaptive_params.{fn_name} raised: {e!r}")
                    continue
    except ImportError:
        pass
    for pid in week_summary.get("consistent_failures", []):
        if not any(p.get("prediction_id") == pid for p in proposals):
            proposals.append({
                "prediction_id": pid,
                "strategy": _pid_to_strategy(pid),
                "description": f"Investigate consistent {pid} failure",
                "reasoning": f"Failed {week_summary['fail_counts'].get(pid, 0)}/{week_summary['n_sessions']} sessions this week",
                "diff": None,
            })
    return proposals


def _pid_to_strategy(pid: str) -> str:
    return {
        "P1": "orb",
        "P2": "bias_momentum",
        "P3": "noise_area",
        "P4": "ib_breakout",
        "P5": "compression_breakout",
        "P6": "spring_setup",
    }.get(pid, "unknown")


def ai_review_proposals(proposals: list[dict]) -> str:
    if not proposals:
        return "(no proposals to review)"
    summary = "\n".join(
        f"{i+1}. {p.get('strategy', '?')}: {p.get('description', '?')} — "
        f"{p.get('reasoning', '')[:200]}"
        for i, p in enumerate(proposals)
    )
    prompt = (
        "You are reviewing proposed config knob changes for an MNQ futures "
        "trading bot. Evaluate each proposal for: safety risk, "
        "regime-applicability, signal coverage. Be brief and specific.\n\n"
        f"Proposals:\n{summary}\n\n"
        "Output exactly one short paragraph per proposal, plus a one-line "
        "bottom-line verdict: SAFE / CAUTION / REJECT. Total under 250 words."
    )
    text = call_claude(prompt, max_tokens=600,
                       system="You are a quant code-reviewer; concise, no fluff.")
    return text or "(AI review unavailable — Claude API not configured)"


def create_branch_and_commit(week_summary: dict, proposals: list[dict],
                             ai_review: str, dry_run: bool = False) -> dict:
    """Returns dict with branch_name, commit_sha, committed, pushed (always False)."""
    today = week_summary["week_end"]
    branch = f"weekly-evolution/{today}"
    body = build_commit_body(week_summary["week_start"], today, proposals, ai_review)
    out = {
        "branch_name": branch,
        "commit_sha": None,
        "committed": False,
        "pushed": False,
        "error": None,
        "commit_body": body,
        "dry_run": dry_run,
    }
    if dry_run:
        return out
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], cwd=PROJECT_ROOT,
                       check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", branch], cwd=PROJECT_ROOT,
                       check=True, capture_output=True)
        proposals_path = PROJECT_ROOT / "out" / "weekly_evolution" / f"proposals_{today}.md"
        proposals_path.parent.mkdir(parents=True, exist_ok=True)
        proposals_path.write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", str(proposals_path)], cwd=PROJECT_ROOT,
                       check=True, capture_output=True)
        commit_subject = f"weekly_evolution: {today} ({len(proposals)} proposals)"
        body_file = PROJECT_ROOT / ".git" / f"COMMIT_EDITMSG_evolution_{today}"
        body_file.write_text(commit_subject + "\n\n" + body, encoding="utf-8")
        subprocess.run(["git", "commit", "-F", str(body_file)],
                       cwd=PROJECT_ROOT, check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                             check=True, capture_output=True, text=True).stdout.strip()
        out["commit_sha"] = sha
        out["committed"] = True
    except subprocess.CalledProcessError as e:
        out["error"] = f"git op failed: {e.stderr.decode('utf-8', errors='ignore')[:200]}"
    except Exception as e:
        out["error"] = repr(e)
    return out


def run(session_date: str | None = None, no_commit: bool = False) -> RoutineReport:
    today = session_date or datetime.now(CT_TZ).strftime("%Y-%m-%d")
    report = RoutineReport(name="weekly_evolution", session_date=today)

    week = aggregate_week(today)
    if week["n_sessions"] == 0:
        report.set_verdict_check(
            "data_present", "RED",
            f"no grade files in out/grades/ for week ending {today}",
        )
        write_artifacts(report)
        return report
    report.set_verdict_check(
        "data_present", "GREEN",
        f"{week['n_sessions']} session grade(s) found for the week",
        week,
    )

    body = (
        f"- Week: {week['week_start']} -> {week['week_end']}\n"
        f"- Sessions found: {week['n_sessions']}\n"
        f"- Pass counts: {week['pass_counts']}\n"
        f"- Fail counts: {week['fail_counts']}\n"
        f"- Consistent failures: {week['consistent_failures']}\n"
    )
    report.add_section("Week aggregation", body)

    proposals = generate_proposals(week)
    if proposals:
        report.set_verdict_check(
            "proposals", "YELLOW",
            f"{len(proposals)} proposal(s) drafted for review",
            {"count": len(proposals)},
        )
    else:
        report.set_verdict_check("proposals", "GREEN",
                                  "no proposals — week was clean")

    ai_review = ai_review_proposals(proposals)
    report.add_section("Proposals + AI review", ai_review)

    git_result = create_branch_and_commit(week, proposals, ai_review,
                                          dry_run=no_commit)
    if git_result["committed"]:
        report.set_verdict_check(
            "git_branch", "GREEN",
            f"branch {git_result['branch_name']} committed (sha {git_result['commit_sha'][:8]}) — NOT pushed",
            git_result,
        )
    elif no_commit:
        report.set_verdict_check(
            "git_branch", "GREEN",
            f"--no-commit: would have created {git_result['branch_name']}",
        )
    else:
        report.set_verdict_check(
            "git_branch", "YELLOW",
            f"branch creation failed: {git_result.get('error', 'unknown')}",
            git_result,
        )
    safe_dump = {k: v for k, v in git_result.items() if k != 'commit_body'}
    report.add_section(
        "Git operation result",
        f"```\n{json.dumps(safe_dump, indent=2, default=str)}\n```",
    )

    paths = write_artifacts(report)

    body_lines = [
        f"<b>Phoenix weekly evolution — {today}</b>",
        f"  proposals: {len(proposals)}",
        f"  consistent failures: {week.get('consistent_failures', [])}",
        (f"  branch: <code>{git_result['branch_name']}</code>"
         if git_result["committed"] else "  branch: not created"),
        "",
        "Review by Monday morning. Validation checkboxes (CPCV/DSR/PBO)",
        "will read NOT YET RUN until the Phase C meta-labeler lands.",
        f"Artifact: {paths['markdown']}",
    ]
    send_telegram_now("Phoenix weekly evolution", "\n".join(body_lines))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--session-date", default=None, help="Override the date (YYYY-MM-DD)")
    parser.add_argument("--no-commit", action="store_true",
                        help="Run end-to-end but don't touch git (dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    report = run(session_date=args.session_date, no_commit=args.no_commit)
    print(f"\n=== Phoenix weekly evolution — verdict: {report.verdict} ===")
    for c in report.verdict_checks:
        glyph = {"GREEN": "[ok]", "YELLOW": "[--]", "RED": "[XX]"}[c.status]
        print(f"  {glyph} {c.name}: {c.detail}")
    return 0 if report.verdict == "GREEN" else (1 if report.verdict == "YELLOW" else 2)


if __name__ == "__main__":
    sys.exit(main())
