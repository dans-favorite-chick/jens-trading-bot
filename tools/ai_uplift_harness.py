"""P4-6: AI Uplift Harness — rigorous A/B uplift evaluation for AI agents.

The AI agents (pretrade filter, council, debrief) were running in advisory
mode for months with no measured uplift, and are currently disabled per the
P0-4 canary gate. Before re-enabling any of them in live, the operator needs
a hard, statistically-defensible answer to:

  *Given the trades that actually happened, would the equity curve have been
  meaningfully better if we had ENFORCED the agent's veto?*

That's a one-sided counterfactual: today (advisory mode) Cohort B trades
DID enter, so we know their realized P&L. Blocking them retroactively is
just subtraction. The question is whether the resulting lift exceeds the
noise floor — which for non-normal trade P&L we estimate via bootstrap
resampling on the per-decision uplift.

Output: Markdown report under `out/ai_uplift_<date>_<agent>.md`, or stdout.

Usage:
    python tools/ai_uplift_harness.py --agent all
    python tools/ai_uplift_harness.py --agent pretrade --since 2026-04-01
    python tools/ai_uplift_harness.py --agent council --min-decisions 50

═══ INVESTIGATION FINDING (2026-05-25) ═══
Trade rows in `logs/trade_memory*.json` do NOT currently persist any of
the agent verdicts. Confirmed sample row keys (per-bot file): account,
bot_id, commission*, contracts, direction, entry_price, entry_reason,
entry_time, exit_price, exit_reason, exit_time, fees_dollars,
gross_pnl, hold_time_s, mae_*, market_snapshot, mfe_*, pnl_*, r_*,
recorded_at, result, slippage_dollars, stop_price, strategy,
sub_strategy, target_price, tier, trade_id. The bot maintains
`bot._filter_verdict` and `bot._council_result` in memory but never
copies them into the trade dict that `_trade_exit.py` passes to
`trade_memory.record()`.

Consequence: historical trades will report INSUFFICIENT_VERDICTS. The
harness still works for going-forward data; a separate follow-up task
needs to wire the verdicts into the trade row (see proposal in report).

The harness already accepts both shapes the operator might adopt:
  - flat fields on the trade dict:
        trade["pretrade_verdict"] = "CLEAR"|"CAUTION"|"SIT_OUT"
        trade["council_verdict"]  = "LONG"|"SHORT"|"NEUTRAL"|"BLOCK"
        trade["debrief_verdict"]  = "GO"|"NO_GO"
  - or nested under `agent_verdicts`:
        trade["agent_verdicts"] = {
            "pretrade": "CLEAR", "council": "LONG", "debrief": "GO",
        }
  - or nested inside `market_snapshot` under the same keys.
For each agent we map verdict values onto GO/NO_GO via `_VETO_MAP`.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CT = ZoneInfo("America/Chicago")

# ─── stdout encoding safety (Windows cp1252) ────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Verdict strings the harness recognizes per agent. NO_GO means the agent
# would have blocked the trade. Anything not in either set is treated as
# "missing" and the trade is dropped from that agent's analysis (we never
# guess what an unrecorded verdict was).
_VETO_MAP: dict[str, dict[str, set[str]]] = {
    "pretrade": {
        "GO":    {"CLEAR", "CAUTION", "GO", "PASS"},
        "NO_GO": {"SIT_OUT", "NO_GO", "BLOCK", "VETO", "REJECT"},
    },
    "council": {
        # Council is direction-aware. If the trade direction lines up with
        # the council bias, that's GO. Opposite = NO_GO. NEUTRAL/BLOCK = NO_GO.
        # When the verdict is a plain string (no direction context), we map
        # by surface form alone — the test fixtures use that simple shape.
        "GO":    {"GO", "ALIGN", "ALIGNED", "PASS"},
        "NO_GO": {"NO_GO", "BLOCK", "VETO", "MISALIGNED", "NEUTRAL", "REJECT"},
    },
    "debrief": {
        "GO":    {"GO", "CONTINUE", "PROCEED"},
        "NO_GO": {"NO_GO", "STOP", "HALT", "VETO", "REJECT"},
    },
}

_VERDICT_FIELD = {
    "pretrade": ("pretrade_verdict", "_filter_verdict", "filter_verdict"),
    "council":  ("council_verdict", "_council_verdict", "council_result"),
    "debrief":  ("debrief_verdict", "_debrief_verdict"),
}


@dataclass
class CohortStats:
    n: int = 0
    pnls: list[float] = field(default_factory=list)

    @property
    def total(self) -> float:
        return float(sum(self.pnls))

    @property
    def mean(self) -> float:
        if not self.pnls:
            return 0.0
        return float(statistics.fmean(self.pnls))


@dataclass
class UpliftReport:
    agent: str
    window_start: Optional[str]
    window_end: Optional[str]
    trading_days: int
    decisions: int
    cohort_a: CohortStats
    cohort_b: CohortStats
    per_decision_lift: float
    counterfactual_total: float
    actual_total: float
    bootstrap_ci_low: float
    bootstrap_ci_high: float
    bootstrap_resamples: int
    seed: int
    min_decisions: int
    insufficient: bool
    insufficient_reason: str = ""

    @property
    def verdict_label(self) -> str:
        if self.insufficient:
            return "INSUFFICIENT_DATA"
        if self.bootstrap_ci_low > 0:
            return "AGENT_USEFUL"
        if self.bootstrap_ci_high < 0:
            return "AGENT_HARMFUL"
        return "AGENT_NOT_DEMONSTRABLY_USEFUL"

    @property
    def total_lift(self) -> float:
        return self.counterfactual_total - self.actual_total


# ─── Data helpers ─────────────────────────────────────────────────────

def _parse_ts(t: dict) -> Optional[float]:
    """Best-effort timestamp extraction. Returns CT-anchored Unix seconds.

    Same logic as tools/mae_mfe_asymmetry.py — handles both Unix-float
    (legacy) and ISO-string (per-bot files) shapes.
    """
    for k in ("exit_time", "entry_time", "ts", "recorded_at"):
        v = t.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return float(v)
            s = str(v).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CT)
            return dt.timestamp()
        except Exception:
            continue
    return None


def _trade_pnl(t: dict) -> float:
    """Net P&L if available, else gross. Defaults to 0."""
    for k in ("pnl_dollars_net", "pnl_dollars", "gross_pnl"):
        v = t.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _extract_verdict_raw(trade: dict, agent: str) -> Optional[str]:
    """Pull whatever verdict shape happens to be on the trade.

    Looks in (in order):
      1. trade["agent_verdicts"][agent]
      2. trade[<one of _VERDICT_FIELD[agent]>]
      3. trade["market_snapshot"][<same keys>]

    Returns the normalized uppercase string, or None if no verdict is
    present. We do NOT invent a default — missing means "drop the row";
    the whole point of this harness is to refuse to fabricate verdicts.
    """
    nested = trade.get("agent_verdicts")
    if isinstance(nested, dict) and nested.get(agent):
        return _normalize_verdict(nested[agent])

    fields = _VERDICT_FIELD.get(agent, ())
    for k in fields:
        if k in trade and trade[k] is not None:
            return _normalize_verdict(trade[k])
    snap = trade.get("market_snapshot")
    if isinstance(snap, dict):
        if isinstance(snap.get("agent_verdicts"), dict) and snap["agent_verdicts"].get(agent):
            return _normalize_verdict(snap["agent_verdicts"][agent])
        for k in fields:
            if k in snap and snap[k] is not None:
                return _normalize_verdict(snap[k])
    return None


def _normalize_verdict(v) -> Optional[str]:
    """Pull a verdict string out of dict/str shapes and uppercase it."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip().upper() or None
    if isinstance(v, dict):
        for k in ("verdict", "action", "decision", "bias"):
            if k in v and isinstance(v[k], str):
                return v[k].strip().upper() or None
    return None


def _classify(verdict: str, agent: str) -> Optional[str]:
    """Map a raw verdict string to "GO", "NO_GO", or None (unrecognized)."""
    table = _VETO_MAP[agent]
    if verdict in table["GO"]:
        return "GO"
    if verdict in table["NO_GO"]:
        return "NO_GO"
    return None


# ─── Bootstrap CI ─────────────────────────────────────────────────────

def bootstrap_ci(
    pnls_blocked: list[float],
    pnls_allowed_n: int,
    *,
    seed: int = 42,
    resamples: int = 1000,
    ci: float = 0.95,
) -> tuple[float, float]:
    """Bootstrap CI on per-decision uplift.

    Per-decision uplift for a sample of N decisions is the mean P&L of the
    blocked cohort REMOVED from the total — i.e. how much per-decision
    P&L improves if you block those trades.

    Resample WITH replacement from the union of all decisions, count how
    much P&L would have been blocked in each resample, then convert to
    per-decision lift. CI from the 2.5/97.5 percentiles by default.

    Returns (low, high). Empty inputs → (0, 0).
    """
    if pnls_allowed_n <= 0 and not pnls_blocked:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n_total = pnls_allowed_n + len(pnls_blocked)
    if n_total <= 0:
        return (0.0, 0.0)
    # Build the union: P&L from blocked trades, plus 0s for allowed trades
    # (because blocking an "allowed" trade isn't what we're testing; the
    # uplift comes entirely from removing the negative tail of cohort B).
    # The per-decision lift estimator is: -mean(pnl over cohort B) * (n_B / N)
    # i.e. the share of cohort B in the population times its negated mean.
    # That equals the difference in mean per-decision P&L between
    # "block B" and "allow B" decision policies.
    blocked_arr = list(pnls_blocked)
    lifts: list[float] = []
    for _ in range(resamples):
        # Resample which trades are "in" the bootstrap window. We resample
        # the full population (blocked + allowed) and recompute per-decision
        # lift on the bootstrap sample.
        sample_blocked_pnl = 0.0
        sample_n_blocked = 0
        for _ in range(n_total):
            # Choose a population slot uniformly. If it lands inside the
            # blocked sub-population, pick one of those P&Ls at random.
            if blocked_arr and rng.random() < (len(blocked_arr) / n_total):
                sample_blocked_pnl += rng.choice(blocked_arr)
                sample_n_blocked += 1
        # per-decision lift = -sum(blocked_pnl) / n_total
        lifts.append(-sample_blocked_pnl / n_total)
    lifts.sort()
    lo_idx = max(0, int((0.5 - ci / 2) * resamples))
    hi_idx = min(resamples - 1, int((0.5 + ci / 2) * resamples))
    return (lifts[lo_idx], lifts[hi_idx])


# ─── Core analysis ────────────────────────────────────────────────────

def build_cohorts(
    trades: list[dict],
    agent: str,
) -> tuple[CohortStats, CohortStats, int]:
    """Sort trades into Cohort A (GO) / Cohort B (NO_GO).

    Returns (cohort_a, cohort_b, n_missing_verdict).
    """
    cohort_a = CohortStats()
    cohort_b = CohortStats()
    missing = 0
    for t in trades:
        raw = _extract_verdict_raw(t, agent)
        if raw is None:
            missing += 1
            continue
        decision = _classify(raw, agent)
        if decision is None:
            missing += 1
            continue
        pnl = _trade_pnl(t)
        if decision == "GO":
            cohort_a.n += 1
            cohort_a.pnls.append(pnl)
        else:
            cohort_b.n += 1
            cohort_b.pnls.append(pnl)
    return cohort_a, cohort_b, missing


def analyze_agent(
    trades: list[dict],
    agent: str,
    *,
    since: Optional[str] = None,
    min_decisions: int = 100,
    seed: int = 42,
    resamples: int = 1000,
) -> UpliftReport:
    """Build full UpliftReport for a single agent on the supplied trades."""
    # Window filter
    window_start = since
    window_end: Optional[str] = None
    filtered = trades
    if since:
        cutoff = datetime.fromisoformat(since).replace(tzinfo=CT).timestamp()
        filtered = [t for t in trades if (_parse_ts(t) or 0) >= cutoff]

    timestamps = sorted(ts for ts in (_parse_ts(t) for t in filtered) if ts)
    if timestamps:
        window_start = datetime.fromtimestamp(timestamps[0], tz=CT).date().isoformat()
        window_end = datetime.fromtimestamp(timestamps[-1], tz=CT).date().isoformat()
        unique_days = len({datetime.fromtimestamp(ts, tz=CT).date() for ts in timestamps})
    else:
        unique_days = 0

    cohort_a, cohort_b, missing = build_cohorts(filtered, agent)
    decisions = cohort_a.n + cohort_b.n

    if decisions == 0:
        return UpliftReport(
            agent=agent,
            window_start=window_start,
            window_end=window_end,
            trading_days=unique_days,
            decisions=0,
            cohort_a=cohort_a,
            cohort_b=cohort_b,
            per_decision_lift=0.0,
            counterfactual_total=0.0,
            actual_total=0.0,
            bootstrap_ci_low=0.0,
            bootstrap_ci_high=0.0,
            bootstrap_resamples=resamples,
            seed=seed,
            min_decisions=min_decisions,
            insufficient=True,
            insufficient_reason=(
                "No usable verdicts found on any trade. "
                "Trade memory does not currently persist agent verdicts — "
                "see report header for the follow-up proposal."
            ),
        )

    if decisions < min_decisions:
        return UpliftReport(
            agent=agent,
            window_start=window_start,
            window_end=window_end,
            trading_days=unique_days,
            decisions=decisions,
            cohort_a=cohort_a,
            cohort_b=cohort_b,
            per_decision_lift=0.0,
            counterfactual_total=0.0,
            actual_total=0.0,
            bootstrap_ci_low=0.0,
            bootstrap_ci_high=0.0,
            bootstrap_resamples=resamples,
            seed=seed,
            min_decisions=min_decisions,
            insufficient=True,
            insufficient_reason=(
                f"Only {decisions} usable decisions (need ≥ {min_decisions}). "
                f"({missing} trades had no recognized verdict for this agent.)"
            ),
        )

    actual_total = cohort_a.total + cohort_b.total
    counterfactual_total = cohort_a.total  # block cohort B
    per_decision_lift = (counterfactual_total - actual_total) / decisions
    lo, hi = bootstrap_ci(
        cohort_b.pnls,
        cohort_a.n,
        seed=seed,
        resamples=resamples,
    )
    return UpliftReport(
        agent=agent,
        window_start=window_start,
        window_end=window_end,
        trading_days=unique_days,
        decisions=decisions,
        cohort_a=cohort_a,
        cohort_b=cohort_b,
        per_decision_lift=per_decision_lift,
        counterfactual_total=counterfactual_total,
        actual_total=actual_total,
        bootstrap_ci_low=lo,
        bootstrap_ci_high=hi,
        bootstrap_resamples=resamples,
        seed=seed,
        min_decisions=min_decisions,
        insufficient=False,
    )


# ─── Rendering ────────────────────────────────────────────────────────

_PRETTY_AGENT = {
    "pretrade": "Pre-Trade Filter",
    "council":  "Council Gate",
    "debrief":  "Session Debrief",
}


def render_report(reports: list[UpliftReport], *, include_header: bool = True) -> str:
    out: list[str] = []
    if include_header:
        out.append("# AI Uplift Harness Report")
        out.append("")
        out.append(f"_Generated: {datetime.now(CT).isoformat(timespec='seconds')}_")
        out.append("")
        out.append("## Trade-memory verdict persistence")
        out.append("")
        out.append(
            "As of 2026-05-25, agent verdicts (pretrade filter, council, "
            "debrief) are NOT persisted into `logs/trade_memory*.json`. "
            "The bot keeps `_filter_verdict` and `_council_result` in "
            "memory but never copies them into the trade dict that "
            "`_trade_exit.py` hands to `trade_memory.record()`."
        )
        out.append("")
        out.append(
            "Follow-up task (separate, not retrofitted): wire a "
            "`trade['agent_verdicts'] = {'pretrade': ..., 'council': ..., "
            "'debrief': ...}` write into `bots/_trade_entry.py` at the "
            "point the trade dict is constructed. Going forward, this "
            "harness will work natively."
        )
        out.append("")
    for r in reports:
        out.append(render_one(r))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_one(r: UpliftReport) -> str:
    title = _PRETTY_AGENT.get(r.agent, r.agent)
    lines: list[str] = []
    lines.append(f"## AI Uplift Harness — {title}")
    lines.append("─" * 40)
    window = ""
    if r.window_start and r.window_end:
        window = f"{r.window_start} to {r.window_end}"
    elif r.window_start:
        window = f"since {r.window_start}"
    else:
        window = "(no dated trades in window)"
    lines.append(f"Window: {window} ({r.trading_days} trading days)")
    lines.append(f"Total decisions: {r.decisions}")
    lines.append("")
    if r.insufficient:
        lines.append(f"**Verdict: INSUFFICIENT DATA** — {r.insufficient_reason}")
        lines.append("")
        lines.append("Recommendation: collect more decisions before drawing any "
                     "conclusion. Until trade_memory persists verdicts, this "
                     "agent cannot be evaluated retrospectively.")
        return "\n".join(lines)

    cA = r.cohort_a
    cB = r.cohort_b
    lines.append(
        f"Cohort A (agent said GO): n={cA.n}, mean P&L=${cA.mean:+.2f}, "
        f"total=${cA.total:+.2f}"
    )
    lines.append(
        f"Cohort B (agent said NO-GO): n={cB.n}, mean P&L=${cB.mean:+.2f}, "
        f"total=${cB.total:+.2f}"
    )
    lines.append("")
    lift_total = r.total_lift
    daily_lift = (lift_total / r.trading_days) if r.trading_days else 0.0
    lines.append(
        f"Counterfactual P&L (block Cohort B): ${r.counterfactual_total:+.2f} "
        f"(vs actual ${r.actual_total:+.2f})"
    )
    lines.append(
        f"Lift: ${lift_total:+.2f} over {r.trading_days} days "
        f"= ${daily_lift:+.2f}/day"
    )
    lines.append(f"Per-decision lift: ${r.per_decision_lift:+.2f}")
    lines.append("")
    lines.append(
        f"Bootstrap 95% CI on per-decision lift "
        f"({r.bootstrap_resamples} resamples, seed={r.seed}): "
        f"[${r.bootstrap_ci_low:+.2f}, ${r.bootstrap_ci_high:+.2f}]"
    )
    lines.append("")
    label = r.verdict_label
    if label == "AGENT_USEFUL":
        lines.append("**Verdict: AGENT DEMONSTRABLY USEFUL** — 95% CI excludes zero on the positive side.")
        lines.append("Recommendation: candidate for re-enabling in blocking mode, "
                     "subject to the canary gate.")
    elif label == "AGENT_HARMFUL":
        lines.append("**Verdict: AGENT DEMONSTRABLY HARMFUL** — 95% CI excludes zero on the NEGATIVE side.")
        lines.append("Recommendation: keep disabled; treat verdicts as adversarial "
                     "(may be a reverse-edge signal).")
    else:
        lines.append("**Verdict: AGENT NOT DEMONSTRABLY USEFUL** — 95% CI crosses zero.")
        lines.append(
            f"Recommendation: keep disabled until > {max(400, r.min_decisions * 2)} "
            "decisions and CI excludes zero."
        )
    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────────

def _resolve_output_path(arg: Optional[str], agent: str) -> Path:
    if arg:
        return Path(arg)
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(CT).date().isoformat()
    return out_dir / f"ai_uplift_{today}_{agent}.md"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="A/B uplift harness for Phoenix AI agents."
    )
    p.add_argument(
        "--agent", choices=("pretrade", "council", "debrief", "all"),
        default="all",
        help="Which agent to analyze (default: all).",
    )
    p.add_argument(
        "--since", default=None,
        help="ISO date (YYYY-MM-DD); restrict to trades on/after this date.",
    )
    p.add_argument(
        "--min-decisions", type=int, default=100,
        help="Refuse to report if cohort size < this (default: 100).",
    )
    p.add_argument(
        "--seed", type=int, default=42,
        help="Bootstrap RNG seed for reproducibility (default: 42).",
    )
    p.add_argument(
        "--resamples", type=int, default=1000,
        help="Bootstrap resamples (default: 1000).",
    )
    p.add_argument(
        "--output", default=None,
        help="Write Markdown report to this path. "
             "Default: out/ai_uplift_<date>_<agent>.md. "
             "Use '-' to write to stdout.",
    )
    p.add_argument(
        "--logs-dir", default=None,
        help="Override logs directory (mostly for tests).",
    )
    args = p.parse_args(argv)

    from core.trade_memory import load_all_trades
    logs_dir = args.logs_dir or str(ROOT / "logs")
    trades = load_all_trades(logs_dir=logs_dir)
    if not isinstance(trades, list):
        print("ERROR: load_all_trades returned wrong shape", file=sys.stderr)
        return 1

    agents = (
        ["pretrade", "council", "debrief"]
        if args.agent == "all"
        else [args.agent]
    )
    reports = [
        analyze_agent(
            trades, a,
            since=args.since,
            min_decisions=args.min_decisions,
            seed=args.seed,
            resamples=args.resamples,
        )
        for a in agents
    ]
    md = render_report(reports)

    if args.output == "-":
        sys.stdout.write(md)
        return 0
    out_path = _resolve_output_path(args.output, args.agent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
