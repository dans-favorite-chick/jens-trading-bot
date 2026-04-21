"""
Phoenix Bot -- Automated Research Bot

Runs overnight strategy discovery by sweeping parameter variations,
analyzing regime-specific performance, and testing strategy combinations.
Produces structured reports for the Session Debriefer and human review.

DESIGN PHILOSOPHY:
  This bot is for OBSERVATION and DISCOVERY only. It presents findings
  and recommendations but NEVER modifies the live bot's configuration.
  The user and the AI debriefer decide what to implement.

Usage:
    python tools/research_bot.py --data C:\\temp\\mnq_historical.csv
    python tools/research_bot.py --data C:\\temp\\mnq_historical.csv --quick
    python tools/research_bot.py --data C:\\temp\\mnq_historical.csv --quick --ai
"""

import argparse
import copy
import itertools
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

# Project imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.backtester import Backtester, read_csv
from config.strategies import STRATEGIES

logger = logging.getLogger("ResearchBot")

# =====================================================================
# Parameter Grids
# =====================================================================

FULL_PARAM_GRID = {
    "bias_momentum": {
        "min_tf_votes": [1, 2, 3],
        "min_momentum": [35, 45, 55],
        "stop_ticks": [8, 12, 15],
    },
    "spring_setup": {
        "min_wick_ticks": [4, 6, 8],
        "stop_multiplier": [1.0, 1.5, 2.0],
        "target_rr": [1.5, 2.0, 2.5],
    },
    "vwap_pullback": {
        "stop_ticks": [6, 8, 10],
        "target_rr": [1.5, 2.0, 2.5],
    },
    "high_precision_only": {
        "min_tf_votes": [3, 4],
        "min_precision": [40, 50, 60],
    },
}

QUICK_PARAM_GRID = {
    "bias_momentum": {
        "min_tf_votes": [2, 3],
        "min_momentum": [45, 55],
        "stop_ticks": [9, 12],
    },
    "spring_setup": {
        "min_wick_ticks": [4, 6],
        "stop_multiplier": [1.5, 2.0],
        "target_rr": [1.5, 2.0],
    },
    "vwap_pullback": {
        "stop_ticks": [6, 8],
        "target_rr": [1.8, 2.5],
    },
    "high_precision_only": {
        "min_tf_votes": [3, 4],
        "min_precision": [50, 60],
    },
}

# Strategy combinations to test
FULL_COMBINATIONS = [
    ["bias_momentum"],
    ["spring_setup"],
    ["vwap_pullback"],
    ["high_precision_only"],
    ["bias_momentum", "spring_setup"],
    ["bias_momentum", "vwap_pullback"],
    ["bias_momentum", "high_precision_only"],
    ["spring_setup", "vwap_pullback"],
    ["bias_momentum", "spring_setup", "vwap_pullback"],
    ["bias_momentum", "spring_setup", "vwap_pullback", "high_precision_only"],
]

QUICK_COMBINATIONS = [
    ["bias_momentum"],
    ["spring_setup"],
    ["bias_momentum", "spring_setup"],
    ["bias_momentum", "spring_setup", "vwap_pullback", "high_precision_only"],
]


# =====================================================================
# Helpers
# =====================================================================

def _expand_grid(param_dict: dict) -> list[dict]:
    """Expand a parameter dict into a list of all combinations.

    Example:
        {"a": [1,2], "b": [3,4]} -> [{"a":1,"b":3}, {"a":1,"b":4}, ...]
    """
    keys = list(param_dict.keys())
    values = list(param_dict.values())
    combos = []
    for combo in itertools.product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


def _extract_result_row(results: dict, params: dict, strategy_name: str) -> dict:
    """Pull key metrics from a backtest result for one strategy variation."""
    summary = results.get("summary", {})
    by_strat = results.get("by_strategy", {}).get(strategy_name, {})
    by_regime = results.get("by_regime", {})

    # Build regime breakdown
    regime_breakdown = {}
    for regime, data in by_regime.items():
        wr = round(data["wins"] / max(1, data["trades"]) * 100, 1)
        regime_breakdown[regime] = {
            "trades": data["trades"],
            "wins": data["wins"],
            "wr": wr,
            "pnl": data["pnl"],
        }

    return {
        "params": params,
        "trades": summary.get("total_trades", 0),
        "wins": summary.get("wins", 0),
        "losses": summary.get("losses", 0),
        "wr": summary.get("win_rate", 0),
        "pnl": summary.get("total_pnl", 0),
        "avg_pnl": summary.get("avg_pnl_per_trade", 0),
        "max_drawdown": summary.get("max_drawdown", 0),
        "profit_factor": summary.get("profit_factor", 0),
        "signals_generated": summary.get("signals_generated", 0),
        "signals_taken": summary.get("signals_taken", 0),
        "by_regime": regime_breakdown,
    }


def _extract_combo_row(results: dict) -> dict:
    """Pull key metrics from a multi-strategy backtest."""
    summary = results.get("summary", {})
    by_strat = results.get("by_strategy", {})
    by_regime = results.get("by_regime", {})

    per_strategy = {}
    for name, data in by_strat.items():
        per_strategy[name] = {
            "trades": data.get("trades", 0),
            "wr": data.get("win_rate", 0),
            "pnl": data.get("pnl", 0),
        }

    regime_breakdown = {}
    for regime, data in by_regime.items():
        wr = round(data["wins"] / max(1, data["trades"]) * 100, 1)
        regime_breakdown[regime] = {
            "trades": data["trades"],
            "wr": wr,
            "pnl": data["pnl"],
        }

    return {
        "trades": summary.get("total_trades", 0),
        "wins": summary.get("wins", 0),
        "losses": summary.get("losses", 0),
        "wr": summary.get("win_rate", 0),
        "pnl": summary.get("total_pnl", 0),
        "avg_pnl": summary.get("avg_pnl_per_trade", 0),
        "max_drawdown": summary.get("max_drawdown", 0),
        "profit_factor": summary.get("profit_factor", 0),
        "per_strategy": per_strategy,
        "by_regime": regime_breakdown,
    }


def _regime_recommendation(wr: float, pnl: float, trades: int) -> str:
    """Classify a regime's trading recommendation."""
    if trades < 3:
        return "INSUFFICIENT_DATA"
    if wr >= 65 and pnl > 0:
        return "AGGRESSIVE"
    if wr >= 50 and pnl > 0:
        return "NORMAL"
    if wr >= 40 and pnl >= 0:
        return "CAUTIOUS"
    return "RESTRICT"


def _run_single_backtest(bars: list[dict], strategy_names: list[str],
                         param_overrides: Optional[dict] = None) -> dict:
    """Run a single backtest with optional parameter overrides.

    Deep-copies STRATEGIES before modifying, restores after.
    """
    import config.strategies as strat_module

    original_strategies = copy.deepcopy(strat_module.STRATEGIES)
    try:
        if param_overrides:
            for strat_name, params in param_overrides.items():
                if strat_name in strat_module.STRATEGIES:
                    strat_module.STRATEGIES[strat_name].update(params)

        bt = Backtester(strategy_names)
        results = bt.run(bars)
        return results
    finally:
        strat_module.STRATEGIES = original_strategies
        # Re-assign to ensure module-level reference is restored
        import importlib
        # Direct assignment is sufficient since Backtester reads at init time


# =====================================================================
# Phase Runners
# =====================================================================

def run_phase1(bars: list[dict], param_grid: dict) -> dict:
    """Phase 1: Parameter Variation Testing.

    For each strategy, sweep all parameter combinations and record results.
    """
    print("\n" + "=" * 60)
    print("  PHASE 1: Parameter Variation Testing")
    print("=" * 60)

    phase1_results = {}

    for strat_name, grid in param_grid.items():
        combos = _expand_grid(grid)
        print(f"\n  [{strat_name}] Testing {len(combos)} parameter combinations...")

        strat_results = []
        best_pnl = float("-inf")
        best_combo = None

        for i, combo in enumerate(combos):
            # Suppress logging during sweep
            prev_level = logging.root.level
            logging.disable(logging.CRITICAL)
            try:
                results = _run_single_backtest(
                    bars,
                    strategy_names=[strat_name],
                    param_overrides={strat_name: combo},
                )
            finally:
                logging.disable(logging.NOTSET)
                logging.root.setLevel(prev_level)

            row = _extract_result_row(results, combo, strat_name)
            strat_results.append(row)

            # Track best
            if row["pnl"] > best_pnl:
                best_pnl = row["pnl"]
                best_combo = combo

            # Progress
            pct = (i + 1) / len(combos) * 100
            sys.stdout.write(f"\r    Progress: {pct:5.1f}% | "
                             f"Current: {row['trades']}T, {row['wr']}%WR, "
                             f"${row['pnl']:.2f} P&L")
            sys.stdout.flush()

        print(f"\n    Best: {best_combo} -> ${best_pnl:.2f} P&L")
        phase1_results[strat_name] = strat_results

    return phase1_results


def run_phase2(bars: list[dict], phase1_results: dict) -> dict:
    """Phase 2: Regime-Specific Analysis.

    Find the best overall config, then break down regime performance.
    """
    print("\n" + "=" * 60)
    print("  PHASE 2: Regime-Specific Analysis")
    print("=" * 60)

    # Find best overall config across all strategies
    best_overall = None
    best_pnl = float("-inf")
    best_strat_name = None

    for strat_name, results_list in phase1_results.items():
        for row in results_list:
            if row["trades"] >= 3 and row["pnl"] > best_pnl:
                best_pnl = row["pnl"]
                best_overall = row
                best_strat_name = strat_name

    if not best_overall:
        print("  No viable configurations found in Phase 1.")
        return {"best_config": None, "regime_scorecard": {}}

    print(f"\n  Best overall: {best_strat_name} with {best_overall['params']}")
    print(f"  -> {best_overall['trades']} trades, {best_overall['wr']}% WR, "
          f"${best_overall['pnl']:.2f} P&L")

    # Build regime scorecard from best config's regime breakdown
    regime_scorecard = {}
    for regime, data in best_overall.get("by_regime", {}).items():
        wr = data.get("wr", 0)
        pnl = data.get("pnl", 0)
        trades = data.get("trades", 0)
        wins = data.get("wins", 0)
        rec = _regime_recommendation(wr, pnl, trades)
        regime_scorecard[regime] = {
            "trades": trades,
            "wins": wins,
            "wr": wr,
            "pnl": pnl,
            "recommendation": rec,
        }

    # Print scorecard
    print(f"\n  Regime Scorecard:")
    for regime, sc in sorted(regime_scorecard.items()):
        print(f"    {regime:25s} | {sc['trades']:3d}T | {sc['wr']:5.1f}%WR | "
              f"${sc['pnl']:8.2f} | {sc['recommendation']}")

    # Find best config PER regime (scan all phase1 results)
    best_per_regime = {}
    for strat_name, results_list in phase1_results.items():
        for row in results_list:
            for regime, data in row.get("by_regime", {}).items():
                pnl = data.get("pnl", 0)
                trades = data.get("trades", 0)
                if trades < 2:
                    continue
                key = regime
                if key not in best_per_regime or pnl > best_per_regime[key]["pnl"]:
                    best_per_regime[key] = {
                        "strategy": strat_name,
                        "params": row["params"],
                        "trades": trades,
                        "wr": data.get("wr", 0),
                        "pnl": pnl,
                    }

    print(f"\n  Best Config Per Regime:")
    for regime, info in sorted(best_per_regime.items()):
        print(f"    {regime:25s} | {info['strategy']:20s} | "
              f"{info['params']} | ${info['pnl']:.2f}")

    return {
        "best_config": {
            "strategy": best_strat_name,
            "params": best_overall["params"],
            "trades": best_overall["trades"],
            "wr": best_overall["wr"],
            "pnl": best_overall["pnl"],
            "profit_factor": best_overall["profit_factor"],
        },
        "regime_scorecard": regime_scorecard,
        "best_per_regime": best_per_regime,
    }


def run_phase3(bars: list[dict], combinations: list[list[str]]) -> list[dict]:
    """Phase 3: Strategy Combination Testing.

    Test running multiple strategies simultaneously.
    """
    print("\n" + "=" * 60)
    print("  PHASE 3: Strategy Combination Testing")
    print("=" * 60)

    combo_results = []

    for i, strat_list in enumerate(combinations):
        label = " + ".join(strat_list)
        sys.stdout.write(f"\r  Testing: {label:60s}")
        sys.stdout.flush()

        # Suppress logging
        prev_level = logging.root.level
        logging.disable(logging.CRITICAL)
        try:
            results = _run_single_backtest(bars, strategy_names=strat_list)
        finally:
            logging.disable(logging.NOTSET)
            logging.root.setLevel(prev_level)

        row = _extract_combo_row(results)
        row["strategies"] = strat_list
        row["label"] = label
        combo_results.append(row)

    # Sort by P&L
    combo_results.sort(key=lambda x: x["pnl"], reverse=True)

    print(f"\n\n  Combination Rankings:")
    for i, row in enumerate(combo_results):
        marker = " <-- BEST" if i == 0 else ""
        print(f"    {i+1}. {row['label']:50s} | {row['trades']:3d}T | "
              f"{row['wr']:5.1f}%WR | ${row['pnl']:8.2f} PF={row['profit_factor']}{marker}")

    return combo_results


def run_phase4(report_data: dict, output_dir: str) -> tuple[str, str]:
    """Phase 4: Report Generation.

    Write JSON and text reports.
    """
    print("\n" + "=" * 60)
    print("  PHASE 4: Report Generation")
    print("=" * 60)

    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    date_str = datetime.now().strftime("%Y-%m-%d")

    json_path = os.path.join(output_dir, f"research_{ts}.json")
    txt_path = os.path.join(output_dir, f"research_{date_str}.txt")

    # Generate recommendations
    recommendations = _generate_recommendations(report_data)
    report_data["recommendations"] = recommendations

    # Generate AI analysis prompt
    report_data["ai_summary_prompt"] = _build_ai_prompt(report_data)

    # Write JSON
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"  JSON report: {json_path}")

    # Write human-readable text
    txt_content = _build_text_report(report_data)
    with open(txt_path, "w") as f:
        f.write(txt_content)
    print(f"  Text report: {txt_path}")

    return json_path, txt_path


def run_phase5(report_data: dict, json_path: str) -> Optional[str]:
    """Phase 5: AI Analysis (optional, requires GEMINI_API_KEY)."""
    print("\n" + "=" * 60)
    print("  PHASE 5: AI Analysis (Gemini)")
    print("=" * 60)

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  GEMINI_API_KEY not set. Skipping AI analysis.")
        print("  Set it with: set GEMINI_API_KEY=your_key_here")
        return None

    try:
        import aiohttp
        import asyncio
    except ImportError:
        print("  aiohttp not installed. Skipping AI analysis.")
        return None

    prompt = report_data.get("ai_summary_prompt", "")
    if not prompt:
        print("  No AI prompt generated. Skipping.")
        return None

    print("  Sending results to Gemini for analysis...")

    async def _query_gemini():
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 2000,
            },
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}?key={api_key}",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"  Gemini API error {resp.status}: {text[:200]}")
                    return None
                data = await resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "")
        return None

    try:
        ai_response = asyncio.run(_query_gemini())
    except Exception as e:
        print(f"  Gemini query failed: {e}")
        return None

    if ai_response:
        print(f"\n  AI Analysis received ({len(ai_response)} chars)")
        # Append to JSON report
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
            data["ai_analysis"] = ai_response
            with open(json_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
            print("  AI analysis appended to JSON report.")
        except Exception as e:
            print(f"  Could not update JSON: {e}")

        # Print summary
        print(f"\n  --- Gemini Analysis ---")
        for line in ai_response.split("\n")[:20]:
            print(f"  {line}")
        if ai_response.count("\n") > 20:
            print(f"  ... ({ai_response.count(chr(10)) - 20} more lines)")
        print(f"  --- End ---")

        return ai_response

    print("  No response from Gemini.")
    return None


# =====================================================================
# Report Builders
# =====================================================================

def _generate_recommendations(report_data: dict) -> list[str]:
    """Generate actionable recommendations from research results."""
    recs = []

    # Phase 2: Regime recommendations
    scorecard = report_data.get("phase2_regime_analysis", {}).get("regime_scorecard", {})
    for regime, sc in scorecard.items():
        if sc["recommendation"] == "AGGRESSIVE" and sc["trades"] >= 3:
            recs.append(
                f"Increase {regime} trading: {sc['wr']}% WR, "
                f"+${sc['pnl']:.2f} in backtest -- lower confluence gate"
            )
        elif sc["recommendation"] == "RESTRICT":
            recs.append(
                f"Restrict {regime}: {sc['wr']}% WR, "
                f"${sc['pnl']:.2f} P&L -- raise confluence or reduce size"
            )

    # Phase 1: Best params vs current
    for strat_name, results_list in report_data.get("phase1_results", {}).items():
        if not results_list:
            continue
        current = STRATEGIES.get(strat_name, {})
        best = max(results_list, key=lambda x: x["pnl"] if x["trades"] >= 3 else float("-inf"))
        if best["trades"] < 3:
            continue

        # Compare with current defaults
        changed_params = []
        for k, v in best["params"].items():
            current_val = current.get(k)
            if current_val is not None and current_val != v:
                changed_params.append(f"{k}: {current_val} -> {v}")
        if changed_params:
            recs.append(
                f"Consider {strat_name} param changes: {', '.join(changed_params)} "
                f"(backtest: {best['trades']}T, {best['wr']}%WR, ${best['pnl']:.2f})"
            )

    # Phase 3: Best combination
    combos = report_data.get("phase3_combinations", [])
    if combos:
        best_combo = combos[0]  # Already sorted by P&L
        recs.append(
            f"Best strategy combination: {best_combo['label']} "
            f"({best_combo['trades']}T, {best_combo['wr']}%WR, ${best_combo['pnl']:.2f})"
        )

    if not recs:
        recs.append("No significant findings -- current config appears reasonable.")

    return recs


def _build_ai_prompt(report_data: dict) -> str:
    """Build a prompt for Gemini to analyze the research results."""
    p1 = report_data.get("phase1_results", {})
    p2 = report_data.get("phase2_regime_analysis", {})
    p3 = report_data.get("phase3_combinations", [])
    recs = report_data.get("recommendations", [])

    # Summarize top results per strategy
    p1_summary = {}
    for strat, results_list in p1.items():
        sorted_results = sorted(results_list, key=lambda x: x["pnl"], reverse=True)
        p1_summary[strat] = sorted_results[:3]  # Top 3

    prompt = f"""You are an expert algorithmic trading researcher analyzing MNQ (Micro E-mini Nasdaq-100) futures backtest results.

DATA SUMMARY:
- Instrument: MNQ (Micro Nasdaq futures), $0.50/tick, $2/point
- Data: {report_data.get('bars_tested', 0)} 1-minute bars, Jan-Apr 2026
- Strategy styles: momentum following, spring/reversal, VWAP pullback, high-precision confluence
- Single contract, max risk $20/trade, daily stop $45

TOP PARAMETER RESULTS PER STRATEGY:
{json.dumps(p1_summary, indent=2, default=str)}

REGIME SCORECARD:
{json.dumps(p2.get('regime_scorecard', {}), indent=2, default=str)}

BEST CONFIG PER REGIME:
{json.dumps(p2.get('best_per_regime', {}), indent=2, default=str)}

TOP STRATEGY COMBINATIONS (sorted by P&L):
{json.dumps(p3[:5], indent=2, default=str)}

AUTO-GENERATED RECOMMENDATIONS:
{json.dumps(recs, indent=2)}

QUESTIONS:
1. What parameter changes would you recommend and why?
2. Which regimes should we be more/less aggressive in?
3. Are there any red flags in the data (overfitting, insufficient samples, survivorship bias)?
4. What additional tests would you suggest?
5. Provide a concrete action plan: what 2-3 changes should be implemented first?

Be specific with numbers. Reference the actual data. Keep your response under 500 words.
"""
    return prompt


def _build_text_report(report_data: dict) -> str:
    """Build a human-readable text report."""
    lines = []
    ts = report_data.get("timestamp", "unknown")
    bars = report_data.get("bars_tested", 0)
    elapsed = report_data.get("total_elapsed_s", 0)

    lines.append("=" * 70)
    lines.append("  PHOENIX BOT -- RESEARCH REPORT")
    lines.append(f"  Generated: {ts}")
    lines.append(f"  Data: {report_data.get('data_file', 'unknown')}")
    lines.append(f"  Bars tested: {bars:,}")
    lines.append(f"  Total runtime: {elapsed:.1f}s")
    lines.append("=" * 70)

    # Phase 1
    lines.append("\n" + "-" * 70)
    lines.append("  PHASE 1: Parameter Variation Results")
    lines.append("-" * 70)
    for strat_name, results_list in report_data.get("phase1_results", {}).items():
        lines.append(f"\n  Strategy: {strat_name}")
        lines.append(f"  {'Params':<45s} | {'Trades':>6s} | {'WR%':>5s} | {'P&L':>10s} | {'PF':>5s} | {'MaxDD':>8s}")
        lines.append(f"  {'-'*45}-+-{'-'*6}-+-{'-'*5}-+-{'-'*10}-+-{'-'*5}-+-{'-'*8}")
        sorted_results = sorted(results_list, key=lambda x: x["pnl"], reverse=True)
        for row in sorted_results:
            params_str = ", ".join(f"{k}={v}" for k, v in row["params"].items())
            lines.append(
                f"  {params_str:<45s} | {row['trades']:>6d} | {row['wr']:>5.1f} | "
                f"${row['pnl']:>9.2f} | {row['profit_factor']:>5.2f} | ${row['max_drawdown']:>7.2f}"
            )

    # Phase 2
    lines.append("\n" + "-" * 70)
    lines.append("  PHASE 2: Regime Scorecard")
    lines.append("-" * 70)
    p2 = report_data.get("phase2_regime_analysis", {})
    best = p2.get("best_config", {})
    if best:
        lines.append(f"\n  Best overall: {best.get('strategy', '?')} with {best.get('params', {})}")
        lines.append(f"  -> {best.get('trades', 0)} trades, {best.get('wr', 0)}% WR, "
                      f"${best.get('pnl', 0):.2f} P&L, PF={best.get('profit_factor', 0)}")

    scorecard = p2.get("regime_scorecard", {})
    if scorecard:
        lines.append(f"\n  {'Regime':<25s} | {'Trades':>6s} | {'WR%':>5s} | {'P&L':>10s} | {'Action':<20s}")
        lines.append(f"  {'-'*25}-+-{'-'*6}-+-{'-'*5}-+-{'-'*10}-+-{'-'*20}")
        for regime, sc in sorted(scorecard.items()):
            lines.append(
                f"  {regime:<25s} | {sc['trades']:>6d} | {sc['wr']:>5.1f} | "
                f"${sc['pnl']:>9.2f} | {sc['recommendation']:<20s}"
            )

    # Phase 3
    lines.append("\n" + "-" * 70)
    lines.append("  PHASE 3: Strategy Combinations")
    lines.append("-" * 70)
    combos = report_data.get("phase3_combinations", [])
    if combos:
        lines.append(f"\n  {'Rank':>4s}  {'Combination':<50s} | {'Trades':>6s} | {'WR%':>5s} | {'P&L':>10s} | {'PF':>5s}")
        lines.append(f"  {'-'*4}  {'-'*50}-+-{'-'*6}-+-{'-'*5}-+-{'-'*10}-+-{'-'*5}")
        for i, row in enumerate(combos):
            lines.append(
                f"  {i+1:>4d}  {row['label']:<50s} | {row['trades']:>6d} | {row['wr']:>5.1f} | "
                f"${row['pnl']:>9.2f} | {row['profit_factor']:>5.2f}"
            )

    # Recommendations
    lines.append("\n" + "-" * 70)
    lines.append("  RECOMMENDATIONS")
    lines.append("-" * 70)
    for i, rec in enumerate(report_data.get("recommendations", []), 1):
        lines.append(f"  {i}. {rec}")

    # AI analysis
    ai = report_data.get("ai_analysis")
    if ai:
        lines.append("\n" + "-" * 70)
        lines.append("  AI ANALYSIS (Gemini)")
        lines.append("-" * 70)
        for line in ai.split("\n"):
            lines.append(f"  {line}")

    lines.append("\n" + "=" * 70)
    lines.append("  END OF REPORT")
    lines.append("  NOTE: This is observation-only. No live config was modified.")
    lines.append("=" * 70 + "\n")

    return "\n".join(lines)


# =====================================================================
# Main
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Phoenix Bot Research Bot -- Overnight strategy discovery"
    )
    parser.add_argument(
        "--data", required=True,
        help="Path to historical CSV data (e.g. C:\\temp\\mnq_historical.csv)"
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Quick mode: fewer parameter variations for fast iteration"
    )
    parser.add_argument(
        "--ai", action="store_true",
        help="Enable Phase 5: send results to Gemini for AI analysis"
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Output directory (default: logs/research/)"
    )
    args = parser.parse_args()

    # Configure logging -- minimal during sweeps
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    logging.getLogger("ResearchBot").setLevel(logging.INFO)

    mode = "QUICK" if args.quick else "FULL"
    param_grid = QUICK_PARAM_GRID if args.quick else FULL_PARAM_GRID
    combinations = QUICK_COMBINATIONS if args.quick else FULL_COMBINATIONS

    # Count total runs
    total_variations = sum(
        len(_expand_grid(grid)) for grid in param_grid.values()
    )
    total_runs = total_variations + len(combinations)

    print("\n" + "=" * 60)
    print("  PHOENIX BOT -- RESEARCH BOT")
    print(f"  Mode: {mode}")
    print(f"  Data: {args.data}")
    print(f"  Parameter variations: {total_variations}")
    print(f"  Combination tests: {len(combinations)}")
    print(f"  Total backtest runs: {total_runs}")
    print(f"  AI analysis: {'Yes' if args.ai else 'No'}")
    print("=" * 60)

    # Load data ONCE
    print("\n  Loading historical data...")
    # Temporarily enable logging for data load
    logging.disable(logging.NOTSET)
    logging.getLogger("Backtester").setLevel(logging.INFO)
    bars = read_csv(args.data)
    logging.getLogger("Backtester").setLevel(logging.WARNING)

    if not bars:
        print("  ERROR: No bars loaded. Check CSV path.")
        sys.exit(1)

    print(f"  Loaded {len(bars):,} bars")
    print(f"  Date range: {bars[0]['timestamp'][:10]} to {bars[-1]['timestamp'][:10]}")

    total_start = time.time()

    # Phase 1: Parameter sweeps
    phase1_start = time.time()
    phase1_results = run_phase1(bars, param_grid)
    phase1_elapsed = time.time() - phase1_start
    print(f"\n  Phase 1 complete: {phase1_elapsed:.1f}s")

    # Phase 2: Regime analysis
    phase2_start = time.time()
    phase2_results = run_phase2(bars, phase1_results)
    phase2_elapsed = time.time() - phase2_start
    print(f"\n  Phase 2 complete: {phase2_elapsed:.1f}s")

    # Phase 3: Combination testing
    phase3_start = time.time()
    phase3_results = run_phase3(bars, combinations)
    phase3_elapsed = time.time() - phase3_start
    print(f"\n  Phase 3 complete: {phase3_elapsed:.1f}s")

    total_elapsed = time.time() - total_start

    # Build report data
    report_data = {
        "timestamp": datetime.now().isoformat(),
        "data_file": args.data,
        "bars_tested": len(bars),
        "date_range": {
            "start": bars[0]["timestamp"][:10],
            "end": bars[-1]["timestamp"][:10],
        },
        "mode": mode,
        "total_backtest_runs": total_runs,
        "total_elapsed_s": round(total_elapsed, 1),
        "phase_timings": {
            "phase1_s": round(phase1_elapsed, 1),
            "phase2_s": round(phase2_elapsed, 1),
            "phase3_s": round(phase3_elapsed, 1),
        },
        "phase1_results": phase1_results,
        "phase2_regime_analysis": phase2_results,
        "phase3_combinations": phase3_results,
    }

    # Phase 4: Report generation
    output_dir = args.output_dir or os.path.join(
        os.path.dirname(__file__), "..", "logs", "research"
    )
    json_path, txt_path = run_phase4(report_data, output_dir)

    # Phase 5: AI analysis (optional)
    if args.ai:
        ai_response = run_phase5(report_data, json_path)
        if ai_response:
            # Re-write text report with AI analysis included
            report_data["ai_analysis"] = ai_response
            txt_content = _build_text_report(report_data)
            with open(txt_path, "w") as f:
                f.write(txt_content)

    # Final summary
    print("\n" + "=" * 60)
    print("  RESEARCH COMPLETE")
    print("=" * 60)
    print(f"  Total runtime: {total_elapsed:.1f}s")
    print(f"  Backtest runs: {total_runs}")
    print(f"  JSON report:   {json_path}")
    print(f"  Text report:   {txt_path}")
    print(f"\n  RECOMMENDATIONS:")
    for i, rec in enumerate(report_data.get("recommendations", []), 1):
        print(f"    {i}. {rec}")
    print(f"\n  NOTE: No live config was modified. Review and apply manually.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
