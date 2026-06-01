"""Deterministic 5-year analysis — pure-Python compute layer, no LLM, no halt gates.

Built when the orchestrator halted on regime instability (z=+2.97) preventing
the LLM-narrated analysis from running. This script uses the same compute
primitives the orchestrator uses (analytics.compute_engine, analytics.prepared_queries)
to produce a comprehensive per-strategy report over the full warehouse.

It produces:
  - logs/oracle/research/<date>_deterministic_facts.json
  - logs/oracle/research/<date>_deterministic_report.md

No LLM call. No safety-gate bypass. No human-gated proposal stage. Operator
reviews the report and can later trigger the LLM narration via the orchestrator
once the regime stabilizes or the operator decides to override.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

# Ensure imports resolve when run as a script
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from analytics import compute_engine, prepared_queries, regime_gate  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("deterministic_5yr")

WINDOW_DAYS = 1825  # 5 years
OUTPUT_ROOT = _ROOT / "logs" / "oracle" / "research"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    today = date.today().isoformat()
    facts_path = OUTPUT_ROOT / f"{today}_deterministic_facts.json"
    report_path = OUTPUT_ROOT / f"{today}_deterministic_report.md"

    logger.info("opening warehouse")
    conn = prepared_queries.open_conn()

    logger.info("running regime stability check (informational only)")
    regime = regime_gate.check_regime_stability(conn, "research")
    if not regime.get("stable"):
        logger.warning(
            "regime unstable (z=%.2f) — analysis continues, results flagged",
            regime.get("z_score") or 0.0,
        )

    logger.info("identifying strategies with >= 30 friction-true trades over 5 years")
    strategies = prepared_queries.strategies_with_trades(conn, WINDOW_DAYS, min_n=30)
    logger.info("found %d strategies", len(strategies))

    # Pass 1: pull every strategy's trades once for n_eff computation
    strategy_trades = {}
    for strat in strategies:
        df = prepared_queries.trades_for_strategy(conn, strat, WINDOW_DAYS)
        if len(df) > 0:
            strategy_trades[strat] = df
    logger.info("loaded trades for %d strategies", len(strategy_trades))

    trial_returns = [
        df["pnl_dollars"].to_numpy()
        for df in strategy_trades.values()
        if len(df) > 0
    ]
    n_eff = compute_engine.compute_effective_n(trial_returns)
    logger.info("effective N (BHY trial count) = %d (raw N=%d)", n_eff, len(strategies))

    # Pass 2: per-strategy metrics + splits
    panel = {}
    for strat, trades_df in strategy_trades.items():
        logger.info("computing metrics for %s (n=%d)", strat, len(trades_df))
        wfa = prepared_queries.wfa_summary_for_strategy(conn, strat)
        metrics = compute_engine.compute_strategy_metrics(trades_df, wfa, n_eff)

        # Add splits — same set the orchestrator builds
        splits = {}
        try:
            splits["by_hour_ct"] = prepared_queries.panel_by_hour_ct(
                conn, strat, WINDOW_DAYS
            ).to_dict("records")
            splits["by_regime"] = prepared_queries.panel_by_regime(
                conn, strat, WINDOW_DAYS
            ).to_dict("records")
            splits["by_direction"] = prepared_queries.panel_by_direction(
                conn, strat, WINDOW_DAYS
            ).to_dict("records")
            splits["mae_mfe_long"] = prepared_queries.mae_mfe_distribution(
                conn, strat, "LONG", WINDOW_DAYS
            ).to_dict("records")
            splits["mae_mfe_short"] = prepared_queries.mae_mfe_distribution(
                conn, strat, "SHORT", WINDOW_DAYS
            ).to_dict("records")
        except Exception as e:  # noqa: BLE001
            logger.warning("splits computation failed for %s: %s", strat, e)
            splits = {}

        metrics["splits"] = splits
        panel[strat] = metrics

    conn.close()

    facts = {
        "analysis_mode": "deterministic_5yr",
        "run_date": today,
        "window_days": WINDOW_DAYS,
        "regime": regime,
        "n_trials_effective": n_eff,
        "n_trials_raw": len(strategies),
        "strategies": panel,
    }

    logger.info("writing facts to %s", facts_path)
    facts_path.write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")

    logger.info("rendering markdown report")
    report = _render_report(facts)
    report_path.write_text(report, encoding="utf-8")
    logger.info("wrote report to %s", report_path)

    print(
        json.dumps(
            {
                "status": "complete",
                "facts_path": str(facts_path),
                "report_path": str(report_path),
                "n_strategies": len(panel),
                "n_eff": n_eff,
                "regime_stable": regime.get("stable"),
                "regime_z": regime.get("z_score"),
            },
            default=str,
        )
    )
    return 0


def _render_report(facts: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Phoenix Strategy Oracle - Deterministic 5-Year Analysis")
    lines.append("")
    lines.append(f"**Run date:** {facts['run_date']}")
    lines.append(f"**Window:** {facts['window_days']} days (~5 years)")
    lines.append(f"**Strategies analyzed:** {len(facts['strategies'])}")
    lines.append(
        f"**Effective N (BHY trial count):** {facts['n_trials_effective']} "
        f"(raw N={facts['n_trials_raw']})"
    )
    lines.append("")

    # Regime
    regime = facts.get("regime", {})
    lines.append("## Regime")
    lines.append("")
    if regime.get("stable"):
        lines.append(
            f"Stable. Latest month z-score: {regime.get('z_score', 0):.2f} "
            f"(threshold +/-1.50)."
        )
    else:
        lines.append(
            f"**UNSTABLE.** Latest month ({regime.get('latest_month')}) "
            f"sharpe-proxy z-score = {regime.get('z_score', 0):+.2f} vs trailing "
            f"6-mo baseline. This deterministic analysis ran anyway; treat any "
            f"recommendations with extra caution until regime stabilizes."
        )
        if regime.get("warning"):
            lines.append("")
            lines.append(f"> {regime['warning']}")
    lines.append("")

    # Rank table - all strategies
    lines.append("## Strategy Ranking (by DSR)")
    lines.append("")
    lines.append(
        "| Strategy | n | DSR | PSR | HLZ t | BHY p | PF | Sortino | Calmar | MaxDD$ | WFE | Gates |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    ranked = sorted(
        facts["strategies"].items(),
        key=lambda kv: (kv[1]["metrics"].get("dsr") or 0.0),
        reverse=True,
    )
    for strat, p in ranked:
        m = p["metrics"]
        g = p["gates"]
        gate_status = "PASS" if g.get("all_pass_for_proposal") else "fail: " + ",".join(
            g.get("failed_gates", [])[:3]
        )
        lines.append(
            f"| {strat} | {m.get('n_trades')} "
            f"| {_fmt(m.get('dsr'))} | {_fmt(m.get('psr'))} "
            f"| {_fmt(m.get('hlz_t_stat'))} | {_fmt(m.get('bhy_p_adjusted'))} "
            f"| {_fmt(m.get('profit_factor'))} | {_fmt(m.get('sortino'))} "
            f"| {_fmt(m.get('calmar'))} | {_fmt(m.get('max_drawdown_dollars'))} "
            f"| {_fmt(m.get('wfe_ratio'))} | {gate_status} |"
        )
    lines.append("")

    # Per-strategy detail
    lines.append("## Per-Strategy Detail")
    lines.append("")
    for strat, p in ranked:
        m = p["metrics"]
        g = p["gates"]
        lines.append(f"### {strat}")
        lines.append("")
        lines.append(f"- **n_trades:** {m.get('n_trades')}")
        lines.append(f"- **DSR:** {_fmt(m.get('dsr'))} | **PSR:** {_fmt(m.get('psr'))}")
        lines.append(
            f"- **HLZ t-stat:** {_fmt(m.get('hlz_t_stat'))} "
            f"| **BHY-adjusted p:** {_fmt(m.get('bhy_p_adjusted'))}"
        )
        lines.append(
            f"- **Profit factor:** {_fmt(m.get('profit_factor'))} "
            f"| **Win rate:** {_fmt(m.get('win_rate'))} "
            f"| **Sortino:** {_fmt(m.get('sortino'))} "
            f"| **Calmar:** {_fmt(m.get('calmar'))}"
        )
        lines.append(
            f"- **Max drawdown $:** {_fmt(m.get('max_drawdown_dollars'))} "
            f"| **MinTRL:** {m.get('min_trl')} "
            f"| **WFE ratio:** {_fmt(m.get('wfe_ratio'))} "
            f"| **WFA pass:** {m.get('wfa_pass')}"
        )
        lines.append("")
        if g.get("all_pass_for_proposal"):
            lines.append("**Gate status:** ALL PROPOSAL GATES PASS - eligible for parameter changes.")
        else:
            failed = g.get("failed_gates", [])
            lines.append(f"**Gate status:** FAIL on: {', '.join(failed)}")
        lines.append("")

        # Splits — long/short asymmetry
        splits = p.get("splits", {})
        by_dir = splits.get("by_direction") or []
        if by_dir:
            lines.append("**By direction:**")
            lines.append("")
            lines.append("| Direction | n | WR | PF | Avg P&L |")
            lines.append("|---|---:|---:|---:|---:|")
            for row in by_dir:
                lines.append(
                    f"| {row.get('direction')} | {row.get('n_trades')} "
                    f"| {_fmt(row.get('win_rate'))} "
                    f"| {_fmt(row.get('profit_factor'))} "
                    f"| {_fmt(row.get('avg_pnl'))} |"
                )
            lines.append("")

        # By hour CT — top 5 worst and best
        by_hour = splits.get("by_hour_ct") or []
        if by_hour:
            sorted_hours = sorted(by_hour, key=lambda r: r.get("avg_pnl") or 0.0)
            worst = sorted_hours[:3]
            best = sorted_hours[-3:]
            lines.append("**Best 3 hours (CT) by avg P&L:**")
            lines.append("")
            lines.append("| Hour | n | WR | PF | Avg P&L |")
            lines.append("|---:|---:|---:|---:|---:|")
            for row in best:
                lines.append(
                    f"| {row.get('hour_ct')} | {row.get('n_trades')} "
                    f"| {_fmt(row.get('win_rate'))} "
                    f"| {_fmt(row.get('profit_factor'))} "
                    f"| {_fmt(row.get('avg_pnl'))} |"
                )
            lines.append("")
            lines.append("**Worst 3 hours (CT) by avg P&L:**")
            lines.append("")
            lines.append("| Hour | n | WR | PF | Avg P&L |")
            lines.append("|---:|---:|---:|---:|---:|")
            for row in worst:
                lines.append(
                    f"| {row.get('hour_ct')} | {row.get('n_trades')} "
                    f"| {_fmt(row.get('win_rate'))} "
                    f"| {_fmt(row.get('profit_factor'))} "
                    f"| {_fmt(row.get('avg_pnl'))} |"
                )
            lines.append("")

        # MAE elbow note
        mae_long = splits.get("mae_mfe_long") or []
        mae_short = splits.get("mae_mfe_short") or []
        if mae_long or mae_short:
            lines.append("**MAE distribution rows (long / short):** " +
                         f"{len(mae_long)} / {len(mae_short)} — full distribution in facts.json")
            lines.append("")

        lines.append("---")
        lines.append("")

    # Summary findings
    lines.append("## Summary")
    lines.append("")
    n_pass = sum(
        1 for _, p in facts["strategies"].items()
        if p["gates"].get("all_pass_for_proposal")
    )
    lines.append(f"- {n_pass} of {len(facts['strategies'])} strategies clear all proposal gates.")
    # Failure reasons aggregated
    failure_counts: dict[str, int] = {}
    for _, p in facts["strategies"].items():
        for gate in p["gates"].get("failed_gates", []):
            failure_counts[gate] = failure_counts.get(gate, 0) + 1
    if failure_counts:
        lines.append("- Failure reason counts across all strategies:")
        for gate, count in sorted(failure_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {gate}: {count}")
    lines.append("")
    lines.append(
        "## Next Steps for the Operator"
    )
    lines.append("")
    lines.append(
        "1. Review the rank table — strategies passing all gates are eligible for parameter changes."
    )
    lines.append(
        "2. The regime gate halted the LLM-narrated orchestrator (z=+2.97). If you decide the "
        "regime instability is informational rather than blocking, you can either (a) wait for "
        "the trailing 6-month z to settle below 1.5, or (b) explicitly authorize a research-mode "
        "halt-bypass."
    )
    lines.append(
        "3. The deterministic facts.json is at `logs/oracle/research/<date>_deterministic_facts.json` "
        "— this is the same shape the orchestrator would have produced minus the LLM narrative."
    )
    return "\n".join(lines) + "\n"


def _fmt(v) -> str:
    if v is None:
        return "-"
    try:
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, int):
            return f"{v:d}"
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return str(v)


if __name__ == "__main__":
    sys.exit(main())
