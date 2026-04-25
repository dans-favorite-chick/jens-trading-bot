"""
Phoenix Bot - Open-Prediction Grading Harness

Runs the six P1-P6 graders against a session's sim_bot log and emits
JSON / Markdown / HTML reports plus a one-line summary into
logs/grading_summary.log. Designed to run as a Windows Scheduled Task
at 16:00 CT Mon-Fri.

Exit codes:
  0 — every prediction passed
  1 — at least one failed
  2 — grader/parser error (the run aborted before all graders completed)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import date as date_cls, datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.graders import GradeResult
from tools.graders.orb_or_too_wide import OrbOrTooWideGrader
from tools.graders.bias_vwap_gate import BiasVwapGateGrader
from tools.graders.noise_cadence_spam import NoiseCadenceSpamGrader
from tools.graders.ib_warmup import IbWarmupGrader
from tools.graders.compression_squeeze import CompressionSqueezeGrader
from tools.graders.spring_silence import SpringSilenceGrader
from tools.log_parsers.sim_bot_log import parse_sim_bot_log


PHOENIX_ROOT = Path(__file__).resolve().parent.parent
GRADES_DIR = PHOENIX_ROOT / "out" / "grades"
BASELINE_DIR = PHOENIX_ROOT / "out" / "baselines"
SUMMARY_LOG = PHOENIX_ROOT / "logs" / "grading_summary.log"
TEMPLATE_HTML = PHOENIX_ROOT / "tools" / "grade_report.html.j2"

GRADERS = [
    OrbOrTooWideGrader(),
    BiasVwapGateGrader(),
    NoiseCadenceSpamGrader(),
    IbWarmupGrader(),
    CompressionSqueezeGrader(),
    SpringSilenceGrader(),
]


def _load_baseline() -> dict:
    """Load the squeeze + other baselines. Tolerant if file missing."""
    p = BASELINE_DIR / "squeeze_baseline.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_md(date_iso: str, results: list[GradeResult]) -> str:
    lines = [f"# Phoenix Open-Prediction Grade — {date_iso}", ""]
    overall_pass = sum(1 for r in results if r.overall_pass)
    lines.append(f"**Score: {overall_pass}/{len(results)} predictions passed.**")
    lines.append("")
    lines.append("| ID | Label | Quant | Threshold | Qual | Overall |")
    lines.append("|----|-------|-------|-----------|------|---------|")
    for r in results:
        q_val = (f"{r.quant_value*100:.2f}%" if r.quant_units == "%"
                 else f"{r.quant_value*100:.1f}% drop" if r.quant_units == "% drop"
                 else f"{r.quant_value:.1f}{r.quant_units}" if r.quant_units == "minutes"
                 else f"{r.quant_value}")
        q_thr = (f"{r.quant_threshold*100:.0f}%" if r.quant_units == "%"
                 else f"{r.quant_threshold*100:.0f}% drop" if r.quant_units == "% drop"
                 else f"{r.quant_threshold}{r.quant_units}" if r.quant_units == "minutes"
                 else f"{r.quant_threshold}")
        lines.append(
            f"| {r.prediction_id} | {r.label} | {q_val} | {q_thr} | "
            f"{'✅' if r.qual_pass else '❌'} | {r.emoji()} |"
        )
    lines.append("")
    lines.append("## Details")
    for r in results:
        lines.append(f"### {r.prediction_id} — {r.label} {r.emoji()}")
        lines.append(f"- **Quantitative:** {'PASS' if r.quant_pass else 'FAIL'}")
        lines.append(f"- **Qualitative:** {r.qual_observation}")
        if r.detail:
            lines.append("- Detail:")
            for k, v in r.detail.items():
                lines.append(f"  - `{k}`: `{v}`")
        if r.notes:
            lines.append(f"- Notes: ```{r.notes[:500]}```")
        lines.append("")
    return "\n".join(lines)


def _build_html(date_iso: str, results: list[GradeResult]) -> str:
    """Build report HTML using a tiny inline template — no jinja dep needed
    so this script runs from any venv. The template file is also written
    to disk for human reference / future jinja-aware tooling."""
    overall_pass = sum(1 for r in results if r.overall_pass)
    rows = []
    for r in results:
        cls = "pass" if r.overall_pass else "fail"
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{r.prediction_id}</td><td>{r.label}</td>"
            f"<td>{r.quant_value:.4f} {r.quant_units}</td>"
            f"<td>{r.quant_threshold} {r.quant_units}</td>"
            f"<td>{'PASS' if r.quant_pass else 'FAIL'}</td>"
            f"<td>{'PASS' if r.qual_pass else 'FAIL'}</td>"
            f"<td>{r.emoji()}</td>"
            f"</tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Phoenix Grade {date_iso}</title>
<style>
body{{font-family:Segoe UI,Helvetica,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;}}
h1{{font-size:24px;}}
table{{border-collapse:collapse;width:100%;}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #ddd;}}
th{{background:#f6f8fa;}}
tr.pass td{{background:#f1f8e9;}}
tr.fail td{{background:#ffebee;}}
.score{{font-size:18px;margin:12px 0 24px;}}
</style></head><body>
<h1>Phoenix Open-Prediction Grade — {date_iso}</h1>
<div class='score'>Score: <b>{overall_pass}/{len(results)}</b> predictions passed</div>
<table><thead><tr>
<th>ID</th><th>Label</th><th>Quant</th><th>Threshold</th><th>Q-Pass</th><th>Qual</th><th>Overall</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""


def _toast(message: str) -> None:
    """Best-effort Windows toast. Silent failure if win10toast missing."""
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast("Phoenix Grade", message, duration=5, threaded=True)
    except Exception:
        pass


def _append_summary(date_iso: str, results: list[GradeResult]) -> None:
    SUMMARY_LOG.parent.mkdir(parents=True, exist_ok=True)
    overall = sum(1 for r in results if r.overall_pass)
    line = (f"{date_iso}  {overall}/{len(results)} pass  "
            f"[{', '.join(r.prediction_id + ('+' if r.overall_pass else '-') for r in results)}]\n")
    with SUMMARY_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def _write_template_file() -> None:
    """Write the documented template path even though we render inline.
    Operator can edit this file to customize the HTML; we'll honor it
    in a future iteration."""
    if not TEMPLATE_HTML.exists():
        TEMPLATE_HTML.parent.mkdir(parents=True, exist_ok=True)
        TEMPLATE_HTML.write_text(
            "{# Placeholder Jinja template — current renderer uses inline HTML #}\n"
            "{# TODO: switch grade_open_predictions.py to use this when jinja is added to deps #}\n",
            encoding="utf-8",
        )


def run_grading(log_path: Path, session_date: date_cls,
                emit_json: bool, emit_md: bool, emit_html: bool,
                notify: bool) -> tuple[int, list[GradeResult]]:
    """Run all graders against `log_path`. Returns (exit_code, results)."""
    if not log_path.exists():
        return 2, []

    # Filter events to the session date so re-runs don't pollute
    since = datetime.combine(session_date, datetime.min.time())
    until = datetime.combine(session_date, datetime.max.time())
    events = [e for e in parse_sim_bot_log(log_path, since=since, until=until)]

    baseline = _load_baseline()

    results: list[GradeResult] = []
    grader_error = False
    for g in GRADERS:
        try:
            r = g._safe_grade(events, baseline)
        except Exception as e:
            grader_error = True
            r = GradeResult(prediction_id=g.prediction_id, label=g.label,
                            quant_pass=False, quant_value=0.0,
                            quant_threshold=g.quant_threshold,
                            quant_units=g.quant_units,
                            qual_pass=False,
                            qual_observation=f"harness error: {e!r}",
                            overall_pass=False)
        results.append(r)

    GRADES_DIR.mkdir(parents=True, exist_ok=True)
    date_iso = session_date.isoformat()
    if emit_json:
        out = GRADES_DIR / f"{date_iso}.json"
        out.write_text(json.dumps({
            "session_date": date_iso,
            "host": socket.gethostname(),
            "log_path": str(log_path),
            "results": [r.to_dict() for r in results],
        }, indent=2, default=str), encoding="utf-8")
    if emit_md:
        (GRADES_DIR / f"{date_iso}.md").write_text(_build_md(date_iso, results), encoding="utf-8")
    if emit_html:
        (GRADES_DIR / f"{date_iso}.html").write_text(_build_html(date_iso, results), encoding="utf-8")

    _append_summary(date_iso, results)

    overall_pass = sum(1 for r in results if r.overall_pass)
    if notify:
        _toast(f"{overall_pass}/{len(results)} predictions passed")

    if grader_error:
        return 2, results
    return (0 if all(r.overall_pass for r in results) else 1), results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--log", type=str, default=str(PHOENIX_ROOT / "logs" / "sim_bot_stdout.log"),
                   help="Path to sim_bot log (default: logs/sim_bot_stdout.log)")
    p.add_argument("--session-date", type=str, default="today",
                   help="Session date as YYYY-MM-DD or 'today' (default: today)")
    p.add_argument("--emit-json", action="store_true")
    p.add_argument("--emit-md", action="store_true")
    p.add_argument("--emit-html", action="store_true")
    p.add_argument("--notify", action="store_true",
                   help="Best-effort Windows toast on completion")
    args = p.parse_args()

    if args.session_date == "today":
        session_date = date_cls.today()
    else:
        session_date = date_cls.fromisoformat(args.session_date)

    _write_template_file()

    exit_code, results = run_grading(
        log_path=Path(args.log),
        session_date=session_date,
        emit_json=args.emit_json or not (args.emit_md or args.emit_html),
        emit_md=args.emit_md,
        emit_html=args.emit_html,
        notify=args.notify,
    )

    overall = sum(1 for r in results if r.overall_pass)
    # Detect Windows-cp1252 stdout (default for older Powershell hosts) and
    # use ASCII glyphs to avoid UnicodeEncodeError on emojis. UTF-8 stdouts
    # (Linux, modern Powershell with chcp 65001) get the rich emoji output.
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    use_ascii = enc not in ("utf-8", "utf8")
    pass_glyph = "[ok]" if use_ascii else "\u2705"   # green check
    fail_glyph = "[--]" if use_ascii else "\u274c"   # red cross
    sep = "--" if use_ascii else "\u2014"
    print(f"\n=== Phoenix Open-Prediction Grade {sep} {session_date} ===")
    print(f"Score: {overall}/{len(results)} predictions passed")
    for r in results:
        glyph = pass_glyph if r.overall_pass else fail_glyph
        print(f"  {glyph} {r.prediction_id} {r.label}: "
              f"quant={r.quant_value:.4f}/{r.quant_threshold} "
              f"({'PASS' if r.quant_pass else 'FAIL'}); "
              f"qual={'PASS' if r.qual_pass else 'FAIL'} {sep} {r.qual_observation}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
