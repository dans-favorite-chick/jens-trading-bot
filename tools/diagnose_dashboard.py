"""
Phoenix - Dashboard Deep-Dive Diagnostic (READ-ONLY)

Investigates all possible causes for dashboard display bugs:
  - Backend API actually returns correct data?
  - Frontend code calls the right endpoint?
  - Frontend renders the response correctly?
  - Filter/window definitions match operator expectations?
  - "Active strategies" count comes from where?
  - Per-bot strategy enumeration matches reality?

Output: out/dashboard_diagnostic_<today>.md
NEVER modifies state. Pure investigation.

Usage:
    python tools/diagnose_dashboard.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CT = ZoneInfo("America/Chicago")


def _data_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "out").exists() or (cwd / "logs").exists():
        return cwd
    if (ROOT / "logs").exists():
        return ROOT
    return cwd


def safe_curl(url: str, timeout: int = 5) -> dict:
    """curl an endpoint, return parsed JSON or error dict."""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 2
        )
        if result.returncode != 0:
            return {"_error": f"curl exit {result.returncode}",
                    "_stderr": result.stderr[:200]}
        if not result.stdout.strip():
            return {"_error": "empty response"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            return {"_error": f"non-JSON response: {e}",
                    "_first_200": result.stdout[:200]}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


_SKIP_DIRS = ("node_modules", ".git", "__pycache__", "venv", ".venv",
              ".claude", ".pytest_cache", "out", "logs", "data",
              "agents/skills_overlay", ".venv-ml")


def grep_recursive(root: Path, patterns: list[str],
                   extensions: list[str],
                   max_results: int = 200) -> list[tuple]:
    """Find pattern matches in files with given extensions."""
    matches: list[tuple] = []
    compiled = [re.compile(pat, re.IGNORECASE) for pat in patterns]
    for ext in extensions:
        for f in root.rglob(f"*{ext}"):
            if any(skip in str(f) for skip in _SKIP_DIRS):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(content.splitlines(), 1):
                    for rx in compiled:
                        if rx.search(line):
                            matches.append((f, i, line.strip()[:200]))
                            break
                    if len(matches) >= max_results:
                        return matches
            except Exception:
                continue
    return matches


def main():
    data_root = _data_root()
    today = datetime.now(CT).date()
    out = data_root / f"out/dashboard_diagnostic_{today}.md"
    out.parent.mkdir(exist_ok=True)
    L = []
    L.append(f"# Phoenix Dashboard Deep-Dive Diagnostic - {today}")
    L.append("")
    L.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
    L.append("")
    L.append("**THIS IS A READ-ONLY REPORT. NO STATE MODIFIED.**")
    L.append("")

    # ─── 1. Locate the dashboard ────────────────────────────────────────
    L.append("## 1. Where is the dashboard?")
    L.append("")
    candidates: list[Path] = []
    for p in (ROOT / "dashboard").rglob("*"):
        if any(skip in str(p) for skip in _SKIP_DIRS):
            continue
        if p.is_file():
            candidates.append(p)
    L.append(f"Found {len(candidates)} files in dashboard/:")
    L.append("")
    for c in candidates[:40]:
        L.append(f"- `{c.relative_to(ROOT)}` ({c.stat().st_size:,} bytes)")
    L.append("")

    # ─── 2. Backend routes ──────────────────────────────────────────────
    L.append("## 2. Backend API routes")
    L.append("")
    route_matches = grep_recursive(
        ROOT,
        [r"@app\.route", r"@server\.route", r"@router\.(get|post)",
         r"@bp\.route", r"add_url_rule"],
        [".py"], max_results=80,
    )
    if route_matches:
        L.append(f"Found {len(route_matches)} route definitions:")
        L.append("")
        L.append("| File | Line | Definition |")
        L.append("|---|---:|---|")
        for f, line_no, line in route_matches:
            L.append(f"| `{f.relative_to(ROOT)}` | {line_no} | "
                     f"`{line.replace('|', chr(92) + '|')}` |")
        L.append("")
    else:
        L.append("_No Flask/FastAPI route definitions found._")
        L.append("")

    # ─── 3. Frontend fetch calls ────────────────────────────────────────
    L.append("## 3. Frontend fetch calls")
    L.append("")
    fetch_matches = grep_recursive(
        ROOT,
        [r"fetch\s*\(['\"]/api", r"axios\.", r"\$\.ajax", r"\$\.get",
         r"XMLHttpRequest"],
        [".html", ".js", ".jsx", ".ts", ".tsx", ".vue"],
        max_results=80,
    )
    if fetch_matches:
        L.append(f"Found {len(fetch_matches)} frontend fetch calls "
                 f"(showing all):")
        L.append("")
        L.append("| File | Line | Call |")
        L.append("|---|---:|---|")
        for f, line_no, line in fetch_matches:
            L.append(f"| `{f.relative_to(ROOT)}` | {line_no} | "
                     f"`{line[:140].replace('|', chr(92) + '|')}` |")
        L.append("")
    else:
        L.append("_No frontend fetch calls found._")
        L.append("")

    # ─── 4. Frontend rendering of P&L / trades ──────────────────────────
    L.append("## 4. Frontend rendering of trades / P&L")
    L.append("")
    render_matches = grep_recursive(
        ROOT / "dashboard",
        [r"per_strategy", r"per_bot", r"\.pnl",
         r"today.pnl", r"trade_count", r"\.trades"],
        [".html", ".js", ".jsx", ".ts", ".tsx", ".vue"],
        max_results=60,
    )
    if render_matches:
        L.append(f"Found {len(render_matches)} render references:")
        L.append("")
        L.append("| File | Line | Reference |")
        L.append("|---|---:|---|")
        for f, line_no, line in render_matches:
            L.append(f"| `{f.relative_to(ROOT)}` | {line_no} | "
                     f"`{line[:140].replace('|', chr(92) + '|')}` |")
        L.append("")
    else:
        L.append("_No render references found in dashboard/_")
        L.append("")

    # ─── 5. Live API health check ───────────────────────────────────────
    L.append("## 5. Live API health check")
    L.append("")
    endpoints_to_test = [
        "http://localhost:5000/",
        "http://localhost:5000/api/today-pnl",
        "http://localhost:5000/api/today",
        "http://localhost:5000/api/trades",
        "http://localhost:5000/api/strategies",
        "http://localhost:5000/api/all-signals",
        "http://localhost:5000/api/status",
        "http://localhost:5000/api/strategy-performance",
        "http://localhost:5000/api/bot/status",
        "http://localhost:8767/health",
    ]
    L.append("| Endpoint | Status | First 120 chars |")
    L.append("|---|---|---|")
    live_results = {}
    for url in endpoints_to_test:
        result = safe_curl(url)
        live_results[url] = result
        if "_error" in result:
            status = f"FAIL: {result['_error'][:60]}"
            sample = (result.get("_first_200", "")[:80]
                      if "_first_200" in result else "-")
        else:
            status = "OK"
            sample_str = json.dumps(result, default=str)[:120]
            sample = (sample_str.replace("|", "\\|").replace("\n", " "))
        L.append(f"| `{url}` | {status} | `{sample}` |")
    L.append("")

    # ─── 6. Full /api/today-pnl response ────────────────────────────────
    L.append("## 6. Full `/api/today-pnl` response")
    L.append("")
    today_pnl = live_results.get("http://localhost:5000/api/today-pnl", {})
    if "_error" in today_pnl:
        L.append(f"FAIL: {today_pnl['_error']}")
    else:
        L.append("```json")
        L.append(json.dumps(today_pnl, indent=2, default=str)[:3000])
        L.append("```")
    L.append("")

    # ─── 7. Strategies endpoint ─────────────────────────────────────────
    L.append("## 7. `/api/strategies` (or equivalent) response")
    L.append("")
    found_strat_endpoint = False
    for url, result in live_results.items():
        if "/api/strategies" in url and "_error" not in result:
            L.append(f"### `{url}`")
            L.append("```json")
            L.append(json.dumps(result, indent=2, default=str)[:2000])
            L.append("```")
            L.append("")
            found_strat_endpoint = True
            break
    if not found_strat_endpoint:
        L.append("_No /api/strategies endpoint responded with valid JSON._")
        L.append("")

    # /api/status often has strategy info
    status_result = live_results.get("http://localhost:5000/api/status", {})
    if "_error" not in status_result:
        L.append("### `/api/status` (often holds strategy state)")
        L.append("")
        # Show top-level keys + extract strategy-related sub-trees
        L.append("Top-level keys:")
        for k in list(status_result.keys())[:30]:
            v = status_result[k]
            if isinstance(v, (dict, list)):
                L.append(f"- `{k}`: {type(v).__name__}({len(v)})")
            else:
                L.append(f"- `{k}`: `{str(v)[:60]}`")
        L.append("")

    # ─── 8. 'Active strategies' / strategy-list source ──────────────────
    L.append("## 8. 'Active strategies' / strategy-list source code")
    L.append("")
    strat_list_matches = grep_recursive(
        ROOT,
        [r"validated.*True", r"only_validated", r"loaded_strategies",
         r"active_strategies"],
        [".py"], max_results=40,
    )
    if strat_list_matches:
        L.append("References to validated filtering / strategy list:")
        L.append("")
        L.append("| File | Line | Reference |")
        L.append("|---|---:|---|")
        for f, line_no, line in strat_list_matches:
            L.append(f"| `{f.relative_to(ROOT)}` | {line_no} | "
                     f"`{line[:140].replace('|', chr(92) + '|')}` |")
        L.append("")

    # ─── 9. config/strategies.py validated flags ────────────────────────
    L.append("## 9. Which strategies are `validated=True` in config?")
    L.append("")
    try:
        from config.strategies import STRATEGIES
        validated_count = 0
        unvalidated_count = 0
        L.append("| Strategy | validated | enabled (if present) |")
        L.append("|---|---|---|")
        for name, cfg in STRATEGIES.items():
            v = cfg.get("validated", "-")
            e = cfg.get("enabled", "-")
            if v is True:
                validated_count += 1
            else:
                unvalidated_count += 1
            L.append(f"| `{name}` | `{v}` | `{e}` |")
        L.append("")
        L.append(f"**Summary: {validated_count} validated, "
                 f"{unvalidated_count} unvalidated.**")
    except Exception as e:
        L.append(f"WARN: Could not import STRATEGIES: {e}")
    L.append("")

    # ─── 10. Per-bot loaded strategies ──────────────────────────────────
    L.append("## 10. What strategies did each bot actually load?")
    L.append("")
    for log_name, log_path in (
        ("sim_bot", data_root / "logs" / "sim_bot_stdout.log"),
        ("prod_bot", data_root / "logs" / "prod_bot_stdout.log"),
    ):
        L.append(f"### `{log_name}`")
        L.append("")
        if not log_path.exists():
            L.append("_log not found_")
            L.append("")
            continue
        loaded: set[str] = set()
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.search(r"Loaded strategy:\s*(\S+)", line)
                    if m:
                        loaded.add(m.group(1))
        except Exception:
            pass
        L.append(f"Loaded strategies (from log greps): **{len(loaded)}**")
        for s in sorted(loaded):
            L.append(f"- `{s}`")
        L.append("")

    # ─── 11. trade_memory today ─────────────────────────────────────────
    L.append("## 11. `trade_memory.json` today's trades")
    L.append("")
    tm_file = data_root / "logs" / "trade_memory.json"
    if tm_file.exists():
        trades = json.loads(tm_file.read_text(encoding="utf-8"))
        if isinstance(trades, dict):
            trades = trades.get("trades", [])
        today_calendar = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            for k in ("exit_ts_ct", "exit_time", "ts", "recorded_at"):
                v = t.get(k)
                if v is None:
                    continue
                try:
                    if isinstance(v, (int, float)):
                        ts = datetime.fromtimestamp(v, CT)
                    else:
                        ts = datetime.fromisoformat(
                            str(v).replace("Z", "+00:00")
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=CT)
                        ts = ts.astimezone(CT)
                    if ts.date() == today:
                        today_calendar.append(t)
                    break
                except Exception:
                    pass
        L.append(f"Trades dated today (calendar day): "
                 f"**{len(today_calendar)}**")
        L.append(f"Total P&L: "
                 f"**${sum(t.get('pnl_dollars', 0) for t in today_calendar):+.2f}**")
        if today_calendar:
            by_strat = Counter(t.get("strategy") for t in today_calendar)
            L.append("")
            L.append("Per strategy:")
            for s, c in by_strat.most_common():
                L.append(f"- `{s}`: {c} trades")
    L.append("")

    # ─── 12. Diagnosis matrix ───────────────────────────────────────────
    L.append("## 12. Diagnosis matrix")
    L.append("")
    L.append("Cross-reference the above to identify the root cause:")
    L.append("")
    L.append("| Symptom | Possible cause | Where to fix |")
    L.append("|---|---|---|")
    L.append("| Dashboard shows $0 + no trades | Frontend hits wrong endpoint | "
             "Section 3 fetch URL |")
    L.append("| Dashboard shows $0 + no trades | Frontend reads wrong field "
             "in response | Section 4 render code |")
    L.append("| Dashboard shows $0 + no trades | Date filter excludes today's "
             "trades | Section 6 timezone/window logic |")
    L.append("| Dashboard shows only 2 strategies | UI hardcodes prod-only "
             "filter | Section 8 / Section 9 |")
    L.append("| Dashboard shows only 2 strategies | UI fetches an endpoint "
             "that returns prod-only | Section 7 |")
    L.append("| Dashboard shows only 2 strategies | Operator expects sim+prod, "
             "UI shows prod only | UX fix: per-bot panels |")
    L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\nWrote {out}")
    print("Read this report carefully BEFORE writing any fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
