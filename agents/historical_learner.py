"""
Phoenix Bot — S8 Historical Learner (Phase E-H, weekly)

Reads the last N=14 days of history JSONL logs + trade_memory.json, computes
per-strategy aggregates (WR by regime, PF by hour bucket, confluence
effectiveness, best/worst hour), asks Claude for 3-7 testable hypotheses
with concrete config changes, and writes:

  - logs/ai_learner/weekly_YYYY-MM-DD.md   (human-readable report)
  - logs/ai_learner/pending_recommendations.json
        (structured input for S9 adaptive tuner)

Hypothesis schema (each recommendation):
    {
      "strategy": str,
      "param": str,
      "current": Any,
      "proposed": Any,
      "rationale": str,
      "expected_impact": str,
    }

Non-blocking: any failure (missing data, Claude down, malformed response)
returns gracefully with a best-effort report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from agents.base_agent import AIClient, BaseAgent
from agents import config as agent_config

logger = logging.getLogger("agents.historical_learner")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = _PROJECT_ROOT / "logs" / "history"
TRADE_MEMORY_PATH = _PROJECT_ROOT / "logs" / "trade_memory.json"
LEARNER_OUT_DIR = _PROJECT_ROOT / "logs" / "ai_learner"
PROMPT_PATH = Path(__file__).parent / "prompts" / "learner.md"

DEFAULT_DAYS = 14
REQUIRED_FIELDS = ("strategy", "param", "current", "proposed",
                   "rationale", "expected_impact")


# ─── Loading ────────────────────────────────────────────────────────────

def load_trade_memory(path: Path = TRADE_MEMORY_PATH) -> list[dict]:
    """Return trade records from trade_memory.json. [] on any failure."""
    try:
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # some builds wrap list under "trades"
            data = data.get("trades", [])
        return list(data) if isinstance(data, list) else []
    except Exception as e:
        logger.warning("load_trade_memory failed: %s", e)
        return []


def load_history_events(
    history_dir: Path = HISTORY_DIR,
    days: int = DEFAULT_DAYS,
    today: Optional[date] = None,
) -> list[dict]:
    """Load all JSONL events for last `days` days. Never raises."""
    if today is None:
        today = date.today()
    events: list[dict] = []
    for i in range(days):
        d = today - timedelta(days=i)
        # Accept both `{date}_prod.jsonl` / `_lab.jsonl` and `_sim.jsonl`
        for suffix in ("prod", "lab", "sim"):
            path = history_dir / f"{d}_{suffix}.jsonl"
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning("read %s failed: %s", path, e)
    return events


# ─── Aggregation ────────────────────────────────────────────────────────

def _trade_hour_ct(t: dict) -> int:
    """Return hour-of-day (CT) for a trade, or -1 if undeterminable.

    CT = UTC - 6 (ignores DST drift — good enough for hourly buckets).
    Accepts either epoch float in `entry_time` or ISO string in `ts`.
    """
    et = t.get("entry_time")
    if isinstance(et, (int, float)) and et > 0:
        try:
            dt = datetime.fromtimestamp(float(et), tz=timezone.utc)
            return ((dt.hour - 6) % 24)
        except Exception:
            pass
    ts = t.get("ts") or t.get("entry_ts")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return ((dt.astimezone(timezone.utc).hour - 6) % 24)
        except Exception:
            pass
    return -1


def _profit_factor(pnls: Iterable[float]) -> float:
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss <= 0:
        return float("inf") if gross_win > 0 else 0.0
    return round(gross_win / gross_loss, 3)


def _win_rate(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return round(100.0 * wins / len(pnls), 2)


def compute_aggregates(trades: list[dict]) -> dict:
    """Compute per-strategy aggregates over the trade list.

    Returns a JSON-serializable dict suitable for the prompt.
    """
    if not trades:
        return {"total_trades": 0, "strategies": {}, "global": {}}

    by_strat: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        s = t.get("strategy") or "unknown"
        by_strat[s].append(t)

    def _pnl(t: dict) -> float:
        v = t.get("pnl_dollars")
        if v is None:
            v = t.get("pnl_ticks")
        try:
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0

    strategies: dict[str, dict] = {}
    all_pnls: list[float] = []
    for s, ts in by_strat.items():
        pnls = [_pnl(t) for t in ts]
        all_pnls.extend(pnls)

        # WR by regime
        by_regime: dict[str, list[float]] = defaultdict(list)
        for t, p in zip(ts, pnls):
            r = t.get("regime") or (t.get("market_snapshot") or {}).get("regime") or "UNKNOWN"
            by_regime[r].append(p)
        regime_stats = {
            r: {"n": len(v), "wr": _win_rate(v), "pf": _profit_factor(v),
                "pnl": round(sum(v), 2)}
            for r, v in by_regime.items()
        }

        # PF / WR by hour-of-day CT
        by_hour: dict[int, list[float]] = defaultdict(list)
        for t, p in zip(ts, pnls):
            h = _trade_hour_ct(t)
            by_hour[h].append(p)
        hour_stats = {
            str(h): {"n": len(v), "wr": _win_rate(v), "pf": _profit_factor(v),
                     "pnl": round(sum(v), 2)}
            for h, v in sorted(by_hour.items())
        }

        # Best / worst hour by PF (require n>=2)
        scored_hours = [
            (h, st) for h, st in hour_stats.items() if st["n"] >= 2
        ]
        best_hour = max(scored_hours, key=lambda x: x[1]["pf"], default=(None, None))
        worst_hour = min(scored_hours, key=lambda x: x[1]["pf"], default=(None, None))

        # Confluence effectiveness — map confluence-count to WR
        by_conf_count: dict[int, list[float]] = defaultdict(list)
        for t, p in zip(ts, pnls):
            confs = t.get("confluences") or []
            if not isinstance(confs, list):
                confs = []
            by_conf_count[len(confs)].append(p)
        confluence_stats = {
            str(c): {"n": len(v), "wr": _win_rate(v), "pf": _profit_factor(v)}
            for c, v in sorted(by_conf_count.items())
        }

        strategies[s] = {
            "n_trades": len(ts),
            "wr_overall": _win_rate(pnls),
            "pf_overall": _profit_factor(pnls),
            "pnl_total": round(sum(pnls), 2),
            "avg_win": round(
                sum(p for p in pnls if p > 0) / max(1, sum(1 for p in pnls if p > 0)), 2
            ),
            "avg_loss": round(
                sum(p for p in pnls if p < 0) / max(1, sum(1 for p in pnls if p < 0)), 2
            ),
            "by_regime": regime_stats,
            "by_hour_ct": hour_stats,
            "best_hour_ct": {"hour": best_hour[0], "stats": best_hour[1]},
            "worst_hour_ct": {"hour": worst_hour[0], "stats": worst_hour[1]},
            "by_confluence_count": confluence_stats,
        }

    return {
        "total_trades": len(trades),
        "global": {
            "wr": _win_rate(all_pnls),
            "pf": _profit_factor(all_pnls),
            "pnl": round(sum(all_pnls), 2),
        },
        "strategies": strategies,
    }


def summarize_history_events(events: list[dict]) -> dict:
    """Skim history events for regime exposure and signal counts."""
    regimes: dict[str, int] = defaultdict(int)
    signals = skipped = blocked = 0
    for e in events:
        et = e.get("event")
        if et == "bar":
            r = e.get("regime")
            if r:
                regimes[r] += 1
        elif et == "eval":
            for s in e.get("strategies", []) or []:
                r = s.get("result", "")
                if r == "SIGNAL":
                    signals += 1
                elif r == "SKIP":
                    skipped += 1
                elif r == "BLOCKED":
                    blocked += 1
    return {
        "regime_bar_counts": dict(regimes),
        "signals_generated": signals,
        "signals_skipped": skipped,
        "signals_blocked": blocked,
        "total_events": len(events),
    }


# ─── Recommendation validation ──────────────────────────────────────────

def _validate_recommendations(raw: Any) -> list[dict]:
    """Extract & validate the recommendations list. Returns [] on failure."""
    if isinstance(raw, dict):
        recs = raw.get("recommendations")
    else:
        recs = raw
    if not isinstance(recs, list):
        return []
    out: list[dict] = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        if not all(k in r for k in REQUIRED_FIELDS):
            continue
        out.append({k: r[k] for k in REQUIRED_FIELDS})
    # 3-7 target but we accept whatever schema-valid count came back
    return out


# ─── Prompt building ────────────────────────────────────────────────────

def _load_prompt_template() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are Phoenix's weekly strategy analyst. Produce 3-7 JSON "
            "recommendations with fields strategy, param, current, "
            "proposed, rationale, expected_impact."
        )


def build_prompt(aggregates: dict, history_summary: dict,
                 days: int, window_end: date) -> str:
    window_start = window_end - timedelta(days=days - 1)
    body = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "days": days,
        "aggregates": aggregates,
        "history_summary": history_summary,
    }
    return (
        f"# Weekly aggregates — {window_start} to {window_end} ({days}d)\n\n"
        "```json\n" + json.dumps(body, indent=2, default=str) + "\n```\n\n"
        "Return ONLY the JSON block described in the system prompt. "
        "Produce 3 to 7 recommendations."
    )


# ─── Markdown report ────────────────────────────────────────────────────

def render_markdown(aggregates: dict, history_summary: dict,
                    recommendations: list[dict],
                    window_start: date, window_end: date,
                    raw_ai_text: Optional[str]) -> str:
    lines = [
        f"# Phoenix Weekly Learner — {window_end.isoformat()}",
        "",
        f"Window: **{window_start} -> {window_end}** "
        f"({(window_end - window_start).days + 1} days)",
        f"Total trades: **{aggregates.get('total_trades', 0)}**",
        "",
        "## Global",
        f"- WR: {aggregates.get('global', {}).get('wr', 0)}%",
        f"- PF: {aggregates.get('global', {}).get('pf', 0)}",
        f"- PnL: ${aggregates.get('global', {}).get('pnl', 0)}",
        "",
        "## Signals (from history events)",
        f"- Generated: {history_summary.get('signals_generated', 0)}",
        f"- Skipped:   {history_summary.get('signals_skipped', 0)}",
        f"- Blocked:   {history_summary.get('signals_blocked', 0)}",
        "",
        "## Per-strategy",
    ]
    for name, st in aggregates.get("strategies", {}).items():
        lines.append(f"### {name}")
        lines.append(
            f"- n={st['n_trades']}  WR={st['wr_overall']}%  "
            f"PF={st['pf_overall']}  PnL=${st['pnl_total']}"
        )
        bh = st.get("best_hour_ct", {}).get("hour")
        wh = st.get("worst_hour_ct", {}).get("hour")
        if bh is not None:
            lines.append(f"- Best hour (CT): {bh}  Worst: {wh}")
        lines.append("")

    lines.append("## Recommendations")
    if recommendations:
        for i, r in enumerate(recommendations, 1):
            lines.append(
                f"### {i}. {r['strategy']} — `{r['param']}`: "
                f"{r['current']} -> {r['proposed']}"
            )
            lines.append(f"- **Rationale:** {r['rationale']}")
            lines.append(f"- **Expected impact:** {r['expected_impact']}")
            lines.append("")
    else:
        lines.append("_No recommendations produced (AI unavailable or "
                     "response invalid). See raw output below._\n")
        if raw_ai_text:
            lines.append("```")
            lines.append(raw_ai_text[:4000])
            lines.append("```")

    return "\n".join(lines) + "\n"


# ─── Agent ──────────────────────────────────────────────────────────────

@dataclass
class LearnerResult:
    md_path: Path
    json_path: Path
    recommendations: list[dict]
    aggregates: dict


class HistoricalLearnerAgent(BaseAgent):
    """Weekly Claude-powered aggregate learner."""

    name = "historical_learner"

    def __init__(
        self,
        client: Optional[AIClient] = None,
        *,
        days: int = DEFAULT_DAYS,
        history_dir: Path = HISTORY_DIR,
        trade_memory_path: Path = TRADE_MEMORY_PATH,
        out_dir: Path = LEARNER_OUT_DIR,
    ) -> None:
        super().__init__(client=client)
        self.days = days
        self.history_dir = Path(history_dir)
        self.trade_memory_path = Path(trade_memory_path)
        self.out_dir = Path(out_dir)

    async def run(self, ctx: Any = None) -> LearnerResult:
        today = (ctx or {}).get("today") if isinstance(ctx, dict) else None
        if today is None:
            today = date.today()
        window_end = today
        window_start = today - timedelta(days=self.days - 1)

        # 1. Load
        trades_all = load_trade_memory(self.trade_memory_path)
        trades = _filter_trades_in_window(trades_all, window_start, window_end)
        events = load_history_events(self.history_dir, days=self.days,
                                     today=today)

        # 2. Aggregate
        aggregates = compute_aggregates(trades)
        history_summary = summarize_history_events(events)

        # 3. Ask Claude
        system = _load_prompt_template()
        prompt = build_prompt(aggregates, history_summary, self.days, window_end)

        raw_text: Optional[str] = None
        if agent_config.have_claude():
            raw_text = await self.safe_call(
                lambda: self.client.ask_claude(
                    prompt, system=system, default=None,
                    max_tokens=2048, temperature=0.2,
                ),
                default=None,
                what="ask_claude",
            )

        parsed = AIClient.parse_json(raw_text) if raw_text else None
        recommendations = _validate_recommendations(parsed) if parsed else []

        # 4. Write outputs
        self.out_dir.mkdir(parents=True, exist_ok=True)
        md_path = self.out_dir / f"weekly_{window_end.isoformat()}.md"
        json_path = self.out_dir / "pending_recommendations.json"

        md = render_markdown(aggregates, history_summary, recommendations,
                             window_start, window_end, raw_text)
        md_path.write_text(md, encoding="utf-8")

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "days": self.days,
            "n_trades": aggregates.get("total_trades", 0),
            "recommendations": recommendations,
        }
        json_path.write_text(json.dumps(payload, indent=2, default=str),
                             encoding="utf-8")

        self.log.info(
            "learner wrote %s + %s (%d recs)",
            md_path.name, json_path.name, len(recommendations),
        )
        return LearnerResult(
            md_path=md_path, json_path=json_path,
            recommendations=recommendations, aggregates=aggregates,
        )


# ─── Helpers ────────────────────────────────────────────────────────────

def _filter_trades_in_window(trades: list[dict], start: date, end: date) -> list[dict]:
    """Keep only trades whose entry falls in [start, end] (inclusive)."""
    out = []
    for t in trades:
        et = t.get("entry_time")
        ts_str = t.get("ts") or t.get("entry_ts")
        d: Optional[date] = None
        if isinstance(et, (int, float)) and et > 0:
            try:
                d = datetime.fromtimestamp(float(et), tz=timezone.utc).date()
            except Exception:
                d = None
        if d is None and isinstance(ts_str, str):
            try:
                d = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).date()
            except Exception:
                d = None
        if d is None:
            # Undated trades — include (can't prove they're out of window)
            out.append(t)
            continue
        if start <= d <= end:
            out.append(t)
    return out


async def run_weekly_learner(
    *,
    days: int = DEFAULT_DAYS,
    today: Optional[date] = None,
    client: Optional[AIClient] = None,
) -> LearnerResult:
    """Convenience wrapper for CLI / scheduler."""
    agent = HistoricalLearnerAgent(client=client, days=days)
    ctx = {"today": today} if today else {}
    return await agent.run(ctx)


__all__ = [
    "HistoricalLearnerAgent",
    "LearnerResult",
    "compute_aggregates",
    "summarize_history_events",
    "load_trade_memory",
    "load_history_events",
    "render_markdown",
    "run_weekly_learner",
    "REQUIRED_FIELDS",
]
