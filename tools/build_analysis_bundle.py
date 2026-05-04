"""
Phoenix Bot - Analysis Bundle Generator
Builds out/analysis_package_{today}.md for external trade analysis.

Honest extraction only - schema discovery first, no field-name assumptions.
"""
from __future__ import annotations
import argparse
import json
import sys
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parent.parent  # phoenix_bot/
OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def now_ct() -> datetime:
    return datetime.now(CT)


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CT)
        return dt.astimezone(CT)
    except Exception:
        return None


def safe_read_jsonl(path: Path, limit=None):
    """Yield (lineno, dict). Skip malformed silently; count stored on attribute."""
    bad = 0
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except Exception:
                bad += 1
                continue
            if limit and i >= limit:
                break
    safe_read_jsonl.last_bad = bad  # type: ignore[attr-defined]


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def section(title):
    return f"\n## {title}\n"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="Days back to include")
    args = ap.parse_args()

    today = now_ct().date()
    cutoff = now_ct() - timedelta(days=args.days)
    out_md = OUT_DIR / f"analysis_package_{today}.md"
    out_trades = OUT_DIR / f"trades_raw_{today}.jsonl"
    notes: list[str] = []
    lines: list[str] = []

    lines.append(f"# Phoenix Analysis Bundle - {today}")
    lines.append(f"_Window: last {args.days} days (since {cutoff.date()} CT)_")
    lines.append(f"_Generated: {now_ct().isoformat(timespec='seconds')}_")

    # ---- 0. Environment ---------------------------------------------------
    lines.append(section("0. Environment"))
    lines.append(f"- Project root: `{ROOT}`")
    lines.append(f"- Python: `{sys.version.split()[0]}`")
    try:
        import subprocess
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        lines.append(f"- Git branch: `{branch}` @ `{commit}`")
    except Exception as e:
        lines.append(f"- Git info unavailable: {e}")

    # ---- 1. Configs (verbatim) -------------------------------------------
    lines.append(section("1. Configs (verbatim)"))
    for relpath in ("config/strategies.py",
                    "config/account_routing.py",
                    "config/settings.py"):
        p = ROOT / relpath
        lines.append(f"### `{relpath}`")
        if not p.exists():
            lines.append("_MISSING_\n")
            notes.append(f"{relpath} not found")
            continue
        lines.append(f"```python\n{p.read_text(encoding='utf-8', errors='replace')}\n```\n")

    # ---- 2. System context (verbatim) -------------------------------------
    lines.append(section("2. System Context (verbatim)"))
    for relpath in ("CLAUDE.md",
                    "memory/context/CURRENT_STATE.md",
                    "BUILD_MAP.md"):
        p = ROOT / relpath
        lines.append(f"### `{relpath}`")
        if not p.exists():
            lines.append("_MISSING_\n")
            notes.append(f"{relpath} not found")
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if len(txt) > 30_000:
            lines.append(f"```markdown\n{txt[:30_000]}\n... [truncated, full file is {len(txt):,} chars]\n```\n")
            notes.append(f"{relpath} truncated to 30KB in bundle")
        else:
            lines.append(f"```markdown\n{txt}\n```\n")

    # ---- 3. Strategies dir listing ---------------------------------------
    lines.append(section("3. Strategy Files"))
    sdir = ROOT / "strategies"
    if sdir.exists():
        for f in sorted(sdir.glob("*.py")):
            with f.open(encoding="utf-8", errors="replace") as fh:
                line_count = sum(1 for _ in fh)
            lines.append(f"- `strategies/{f.name}` - {line_count:,} lines, {human_bytes(f.stat().st_size)}")
    else:
        lines.append("_MISSING_")
        notes.append("strategies/ dir not found")

    # ---- 4. File schema discovery ----------------------------------------
    lines.append(section("4. File Schema Discovery"))

    file_groups = {
        "history_sim":   list((ROOT / "logs/history").glob("*_sim.jsonl"))   if (ROOT / "logs/history").exists() else [],
        "history_prod":  list((ROOT / "logs/history").glob("*_prod.jsonl"))  if (ROOT / "logs/history").exists() else [],
        "agent_calls":   list((ROOT / "logs/agent_calls").glob("*.jsonl"))   if (ROOT / "logs/agent_calls").exists() else [],
        "audit_log":     [ROOT / "memory/audit_log.jsonl"],
        "disconnect":    [ROOT / "logs/disconnect_forensics.jsonl"],
        "finnhub_news":  [ROOT / "logs/finnhub_news.jsonl"],
        "grades":        list((ROOT / "out/grades").glob("*.json"))          if (ROOT / "out/grades").exists() else [],
        "incidents":     list((ROOT / "logs/incidents").glob("incident_*.txt")) if (ROOT / "logs/incidents").exists() else [],
    }
    stdout_logs = {
        "sim_stdout":  ROOT / "logs/sim_bot_stdout.log",
        "prod_stdout": ROOT / "logs/prod_bot_stdout.log",
    }

    for group, files in file_groups.items():
        files = [f for f in files if f.exists()]
        total_size = sum(f.stat().st_size for f in files)
        lines.append(f"\n### `{group}` - {len(files)} files, {human_bytes(total_size)}")
        if not files:
            lines.append("_no files present_")
            continue
        dated = [re.match(r"(\d{4}-\d{2}-\d{2})", f.name) for f in files]
        dates = sorted({m.group(1) for m in dated if m})
        if dates:
            lines.append(f"- Date range from filenames: {dates[0]} -> {dates[-1]}")
        if group != "incidents":
            recent = max(files, key=lambda f: f.stat().st_mtime)
            keys = Counter()
            event_types = Counter()
            sample_lines: list[str] = []
            for _, obj in safe_read_jsonl(recent, limit=2000):
                if isinstance(obj, dict):
                    keys.update(obj.keys())
                    if "event" in obj:
                        event_types[obj["event"]] += 1
                if len(sample_lines) < 3:
                    sample_lines.append(json.dumps(obj)[:400])
            lines.append(f"- Most recent: `{recent.name}` (read up to 2000 lines)")
            lines.append("- Top-level keys (count): " +
                         ", ".join(f"`{k}`={v}" for k, v in keys.most_common(20)))
            if event_types:
                lines.append("- `event` types: " +
                             ", ".join(f"`{k}`={v}" for k, v in event_types.most_common()))
            lines.append("- 3 sample lines:\n```json\n" + "\n".join(sample_lines) + "\n```")

    for name, p in stdout_logs.items():
        lines.append(f"\n### `{name}` - `{p.relative_to(ROOT)}`")
        if not p.exists():
            lines.append("_MISSING_")
            continue
        lines.append(
            f"- Size: {human_bytes(p.stat().st_size)}, "
            f"mtime: {datetime.fromtimestamp(p.stat().st_mtime, CT).isoformat(timespec='seconds')}"
        )
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="replace").splitlines()[-5:]
        lines.append("- Tail 5 lines:\n```\n" + "\n".join(tail) + "\n```")

    # ---- 5. Grep signature verification ----------------------------------
    lines.append(section("5. Grep Signature Verification"))
    sigs = [
        (r"\[INTENT", "INTENT"),
        (r"\[FILLED", "FILLED"),
        (r"\[EXIT", "EXIT"),
        (r"\[SIM:[^\]]+\] SIGNAL", "SIGNAL"),
        (r"\[SIM:[^\]]+\] REJECTED", "REJECTED"),
        (r"\[HALT", "HALT"),
        (r"\[ACCOUNT_ROUTING\]", "ACCOUNT_ROUTING"),
        (r"\[SESSION\+GAMMA\]", "SESSION+GAMMA"),
    ]
    for stdout_name, stdout_path in stdout_logs.items():
        lines.append(f"\n### `{stdout_name}`")
        if not stdout_path.exists():
            lines.append("_MISSING_")
            continue
        sig_counts = {label: 0 for _, label in sigs}
        compiled = [(re.compile(rx), label) for rx, label in sigs]
        with stdout_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                for rx, label in compiled:
                    if rx.search(line):
                        sig_counts[label] += 1
                        break
        lines.append("| Signature | Match Count |")
        lines.append("|-----------|-------------|")
        for _, label in sigs:
            lines.append(f"| `{label}` | {sig_counts[label]:,} |")
        for _, label in sigs:
            if sig_counts[label] == 0:
                kw_lines: list[str] = []
                needle = label.replace("+", "").replace("_", "").upper()
                with stdout_path.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if needle in line.replace("+", "").replace("_", "").upper():
                            kw_lines.append(line.rstrip())
                            if len(kw_lines) >= 3:
                                break
                lines.append(f"\n_Zero matches for `{label}`. Keyword search samples:_")
                if kw_lines:
                    lines.append("```\n" + "\n".join(kw_lines) + "\n```")
                else:
                    lines.append("_no keyword matches found either_")
                notes.append(f"{stdout_name}: signature `{label}` has zero matches")

    # ---- 6. Filled trades extraction (JSONL history) ---------------------
    lines.append(section("6. Filled Trades"))
    trades: list[dict] = []
    orphan_entries = 0
    orphan_exits = 0
    for source, files in [("sim", file_groups["history_sim"]),
                          ("prod", file_groups["history_prod"])]:
        open_by_key: dict = {}
        for f in sorted(files):
            for _, obj in safe_read_jsonl(f):
                if not isinstance(obj, dict):
                    continue
                ts = parse_ts(obj.get("ts"))
                if ts and ts < cutoff:
                    continue
                ev = obj.get("event")
                if ev == "entry":
                    key = (obj.get("bot"), obj.get("strategy"))
                    if key in open_by_key:
                        orphan_entries += 1
                    open_by_key[key] = obj
                elif ev == "exit":
                    key = (obj.get("bot"), obj.get("strategy"))
                    entry = open_by_key.pop(key, None)
                    if not entry:
                        orphan_exits += 1
                        continue
                    e_ts = parse_ts(entry.get("ts"))
                    x_ts = parse_ts(obj.get("ts"))
                    market = entry.get("market") or {}
                    trades.append({
                        "source":            source,
                        "bot":               entry.get("bot"),
                        "strategy":          entry.get("strategy"),
                        "direction":         entry.get("direction"),
                        "tier":              entry.get("tier"),
                        "account": (
                            entry.get("account")
                            or market.get("account")
                            or (entry.get("metadata") or {}).get("account")
                        ),
                        "entry_ts_ct":       e_ts.isoformat(timespec="seconds") if e_ts else None,
                        "entry_price":       entry.get("price"),
                        "stop_price":        entry.get("stop_price"),
                        "target_price":      entry.get("target_price"),
                        "exit_ts_ct":        x_ts.isoformat(timespec="seconds") if x_ts else None,
                        "exit_price":        obj.get("exit_price"),
                        "exit_reason":       obj.get("exit_reason"),
                        "contracts":         entry.get("contracts"),
                        "pnl_dollars":       obj.get("pnl_dollars"),
                        "pnl_ticks":         obj.get("pnl_ticks"),
                        "duration_s":        obj.get("duration_s"),
                        "regime_at_entry":   market.get("regime") or entry.get("regime"),
                        "day_type_at_entry": market.get("day_type") or entry.get("day_type"),
                        "confluences":       entry.get("confluences"),
                        "tf_bias":           entry.get("tf_bias"),
                    })
        orphan_entries += len(open_by_key)

    lines.append(f"- Total filled trades extracted: **{len(trades):,}**")
    lines.append(f"- Orphan entries (no matched exit): {orphan_entries}")
    lines.append(f"- Orphan exits (no matched entry): {orphan_exits}")

    truncated = False
    if len(trades) > 2000:
        truncated = True
        trades_for_bundle = trades[-2000:]
        notes.append(f"Trades truncated to last 2000 in bundle (full count: {len(trades):,})")
    else:
        trades_for_bundle = trades

    lines.append("\n```jsonl")
    for t in trades_for_bundle:
        lines.append(json.dumps(t, default=str))
    lines.append("```")

    # ---- 7. Rejection aggregation (stdout logs) --------------------------
    lines.append(section("7. Rejection Aggregation"))
    rej_re = re.compile(r"\[SIM:([^\]]+)\] REJECTED:?\s*(.+)")
    rej_counts: Counter = Counter()
    total_rej = 0
    for _, p in stdout_logs.items():
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = rej_re.search(line)
                if m:
                    strat = m.group(1).strip()
                    reason = m.group(2).strip()[:120]
                    rej_counts[(strat, reason)] += 1
                    total_rej += 1
    lines.append(f"- Total rejections counted: **{total_rej:,}**")
    if rej_counts:
        lines.append("\n| Strategy | Reason | Count |")
        lines.append("|----------|--------|-------|")
        for (strat, reason), count in rej_counts.most_common(50):
            reason_esc = reason.replace("|", r"\|")
            lines.append(f"| `{strat}` | {reason_esc} | {count:,} |")
    else:
        lines.append("_No `[SIM:...] REJECTED` lines found - verify log format._")
        notes.append("Rejection aggregation found nothing - log format may differ")

    # ---- 8. Halts --------------------------------------------------------
    lines.append(section("8. Halt Events"))
    halt_re = re.compile(r"\[HALT[: ]([^\]]+)\][: ]?(.*)")
    halts: list[dict] = []
    for stdout_name, p in stdout_logs.items():
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = halt_re.search(line)
                if m:
                    halts.append({"source": stdout_name, "line": line.rstrip()})
    lines.append(f"- Total halt events: **{len(halts)}**")
    if halts:
        lines.append("```")
        for h in halts[-100:]:
            lines.append(f"[{h['source']}] {h['line']}")
        lines.append("```")
    else:
        lines.append("_None found._")

    # ---- 9. PhoenixGrading cross-check data -------------------------------
    lines.append(section("9. PhoenixGrading Cross-Check Data"))
    grades_files = file_groups["grades"]
    if not grades_files:
        lines.append("_No `out/grades/*.json` files present._")
        notes.append("PhoenixGrading data unavailable - cross-check impossible")
    else:
        grade_summary: dict = {}
        for gf in sorted(grades_files):
            try:
                data = json.loads(gf.read_text(encoding="utf-8"))
                grade_summary[gf.stem] = data
            except Exception as e:
                grade_summary[gf.stem] = {"error": str(e)}
                notes.append(f"Failed to parse {gf.name}: {e}")
        lines.append(f"- {len(grade_summary)} grade file(s) loaded")
        lines.append("```json")
        lines.append(json.dumps(grade_summary, indent=2, default=str)[:50_000])
        lines.append("```")

    # ---- 10. Notes & anomalies -------------------------------------------
    lines.append(section("10. Notes & Anomalies"))
    if notes:
        for n in notes:
            lines.append(f"- {n}")
    else:
        lines.append("_None._")

    # ---- write bundle -----------------------------------------------------
    out_md.write_text("\n".join(lines), encoding="utf-8")
    bundle_size = out_md.stat().st_size

    raw_trades_written = False
    if bundle_size > 500_000 and trades:
        with out_trades.open("w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, default=str) + "\n")
        raw_trades_written = True

    # ---- exec summary -----------------------------------------------------
    print("\n" + "=" * 60)
    print("EXEC SUMMARY")
    print("=" * 60)
    print(f"Bundle:        {out_md}")
    print(f"Bundle size:   {human_bytes(bundle_size)}")
    print(f"Days window:   {args.days}")
    print(f"Trades found:  {len(trades):,}  (orphan entries={orphan_entries}, orphan exits={orphan_exits})")
    print(f"Rejections:    {total_rej:,}")
    print(f"Halts:         {len(halts)}")
    print(f"Anomalies:     {len(notes)}")
    if raw_trades_written:
        print(f"Raw trades:    {out_trades} ({human_bytes(out_trades.stat().st_size)}) - also attach this to chat")
    if truncated:
        print(f"NOTE: Trades truncated to last 2000 in bundle. Full set in {out_trades.name}.")
    print("=" * 60)


if __name__ == "__main__":
    main()
