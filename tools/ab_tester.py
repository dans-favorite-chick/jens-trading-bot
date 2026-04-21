"""
Phoenix Bot -- A/B Parameter Testing

Runs two configurations (A = current, B = experimental) against the
same historical data and compares results. The winner gets recommended
for promotion.

DESIGN PHILOSOPHY:
  This is the self-evolution engine. It never modifies live config --
  it tests, compares, and recommends. The user decides what to apply.

Usage:
    python tools/ab_tester.py --data C:\\temp\\mnq_historical.csv
    python tools/ab_tester.py --data C:\\temp\\mnq_historical.csv --ai
"""

import argparse
import copy
import json
import logging
import os
import sys
import time
from datetime import datetime

# Project imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.backtester import Backtester, read_csv
from config.strategies import STRATEGIES

logger = logging.getLogger("ABTester")

# =====================================================================
# Scoring Weights
# =====================================================================
# How we pick a winner: normalize each metric 0-1, weighted sum
METRIC_WEIGHTS = {
    "total_pnl": 0.30,
    "win_rate": 0.20,
    "profit_factor": 0.25,
    "max_drawdown": 0.15,   # Lower is better (inverted)
    "avg_pnl_per_trade": 0.10,
}


# =====================================================================
# Helpers
# =====================================================================

def _run_backtest(bars: list[dict], strategy_names: list[str],
                  param_overrides: dict = None) -> dict:
    """Run a single backtest with optional parameter overrides.

    Deep-copies STRATEGIES before modifying, restores after.
    Suppresses logging during the run for clean output.
    """
    import config.strategies as strat_module

    original_strategies = copy.deepcopy(strat_module.STRATEGIES)
    try:
        if param_overrides:
            for strat_name, params in param_overrides.items():
                if strat_name in strat_module.STRATEGIES:
                    strat_module.STRATEGIES[strat_name].update(params)

        # Suppress logging during backtest
        prev_level = logging.root.level
        logging.disable(logging.CRITICAL)
        try:
            bt = Backtester(strategy_names)
            results = bt.run(bars)
        finally:
            logging.disable(logging.NOTSET)
            logging.root.setLevel(prev_level)

        return results
    finally:
        strat_module.STRATEGIES = original_strategies


def _extract_metrics(results: dict) -> dict:
    """Pull key metrics from a backtest result."""
    summary = results.get("summary", {})
    return {
        "total_trades": summary.get("total_trades", 0),
        "wins": summary.get("wins", 0),
        "losses": summary.get("losses", 0),
        "win_rate": summary.get("win_rate", 0),
        "total_pnl": summary.get("total_pnl", 0),
        "avg_pnl_per_trade": summary.get("avg_pnl_per_trade", 0),
        "max_drawdown": summary.get("max_drawdown", 0),
        "profit_factor": summary.get("profit_factor", 0),
        "signals_generated": summary.get("signals_generated", 0),
        "signals_taken": summary.get("signals_taken", 0),
    }


def _normalize(val: float, min_val: float, max_val: float) -> float:
    """Normalize a value to 0-1 range."""
    if max_val == min_val:
        return 0.5
    return (val - min_val) / (max_val - min_val)


def _score_config(metrics: dict, all_metrics: list[dict]) -> float:
    """Score a config against all tested configs using weighted normalization."""
    score = 0.0

    for metric_name, weight in METRIC_WEIGHTS.items():
        values = [m.get(metric_name, 0) for m in all_metrics]
        min_v = min(values)
        max_v = max(values)
        val = metrics.get(metric_name, 0)

        if metric_name == "max_drawdown":
            # Lower drawdown is better -- invert
            normalized = 1.0 - _normalize(val, min_v, max_v)
        else:
            normalized = _normalize(val, min_v, max_v)

        score += normalized * weight

    return round(score, 4)


def _compare_two(metrics_a: dict, metrics_b: dict) -> dict:
    """Compare two sets of metrics and determine a winner."""
    all_metrics = [metrics_a, metrics_b]
    score_a = _score_config(metrics_a, all_metrics)
    score_b = _score_config(metrics_b, all_metrics)

    # Determine winner
    diff = abs(score_a - score_b)
    if diff < 0.02:
        winner = "TIE"
        confidence = 0.0
    elif score_a > score_b:
        winner = "A"
        confidence = diff
    else:
        winner = "B"
        confidence = diff

    # Build comparison
    comparison = {
        "pnl_diff": round(metrics_b["total_pnl"] - metrics_a["total_pnl"], 2),
        "wr_diff": round(metrics_b["win_rate"] - metrics_a["win_rate"], 1),
        "pf_diff": round(metrics_b["profit_factor"] - metrics_a["profit_factor"], 2),
        "dd_diff": round(metrics_b["max_drawdown"] - metrics_a["max_drawdown"], 2),
        "trades_diff": metrics_b["total_trades"] - metrics_a["total_trades"],
    }

    # Reason
    reasons = []
    if comparison["pnl_diff"] > 0:
        reasons.append(f"B has +${comparison['pnl_diff']:.2f} more P&L")
    elif comparison["pnl_diff"] < 0:
        reasons.append(f"A has +${-comparison['pnl_diff']:.2f} more P&L")
    if comparison["wr_diff"] > 0:
        reasons.append(f"B has +{comparison['wr_diff']}% higher WR")
    elif comparison["wr_diff"] < 0:
        reasons.append(f"A has +{-comparison['wr_diff']}% higher WR")

    winner_reason = "; ".join(reasons[:3]) if reasons else "Configs are equivalent"

    # Recommendation
    if winner == "A":
        recommendation = "Keep current config (A). No changes needed."
    elif winner == "B":
        recommendation = "Consider adopting experimental config (B)."
    else:
        recommendation = "No significant difference. Keep current config."

    return {
        "score_a": score_a,
        "score_b": score_b,
        "winner": winner,
        "winner_reason": winner_reason,
        "confidence": round(confidence, 4),
        "recommendation": recommendation,
        "comparison": comparison,
    }


# =====================================================================
# ABTester Class
# =====================================================================

class ABTester:
    def __init__(self, data_path: str):
        self.data_path = data_path
        self.bars = read_csv(data_path)
        if not self.bars:
            raise ValueError(f"No bars loaded from {data_path}")

    def run_comparison(self, config_a: dict, config_b: dict,
                       strategy: str = "bias_momentum") -> dict:
        """
        Run both configs through the backtester and compare.

        Args:
            config_a: Parameter overrides for config A (current)
            config_b: Parameter overrides for config B (experimental)
            strategy: Strategy name to test

        Returns: {
            config_a_results, config_b_results, winner, winner_reason,
            confidence, recommendation, comparison
        }
        """
        print(f"\n  Running A: {config_a}")
        results_a = _run_backtest(self.bars, [strategy], {strategy: config_a})
        metrics_a = _extract_metrics(results_a)

        print(f"  Running B: {config_b}")
        results_b = _run_backtest(self.bars, [strategy], {strategy: config_b})
        metrics_b = _extract_metrics(results_b)

        verdict = _compare_two(metrics_a, metrics_b)

        return {
            "strategy": strategy,
            "config_a": config_a,
            "config_b": config_b,
            "config_a_results": metrics_a,
            "config_b_results": metrics_b,
            **verdict,
        }

    def run_regime_ab(self) -> dict:
        """
        Test current config vs aggressive config per regime.
        For each regime, compare current settings vs loosened settings.

        This is the self-evolution engine: find regimes where being
        more aggressive is statistically better.
        """
        print("\n" + "=" * 60)
        print("  A/B TEST: Regime-Specific Aggressiveness")
        print("=" * 60)

        # Current (A) vs Aggressive (B) for bias_momentum
        current_config = {
            "min_tf_votes": STRATEGIES["bias_momentum"].get("min_tf_votes", 3),
            "min_momentum": STRATEGIES["bias_momentum"].get("min_momentum", 55),
            "stop_ticks": STRATEGIES["bias_momentum"].get("stop_ticks", 9),
        }
        aggressive_config = {
            "min_tf_votes": max(1, current_config["min_tf_votes"] - 1),
            "min_momentum": max(25, current_config["min_momentum"] - 15),
            "stop_ticks": current_config["stop_ticks"] + 2,
        }

        print(f"\n  Config A (current):    {current_config}")
        print(f"  Config B (aggressive): {aggressive_config}")

        # Run both
        print("\n  Running current config...")
        results_a = _run_backtest(self.bars, ["bias_momentum"],
                                  {"bias_momentum": current_config})
        print("  Running aggressive config...")
        results_b = _run_backtest(self.bars, ["bias_momentum"],
                                  {"bias_momentum": aggressive_config})

        # Compare per regime
        regimes_a = results_a.get("by_regime", {})
        regimes_b = results_b.get("by_regime", {})

        regime_comparison = {}
        all_regimes = set(list(regimes_a.keys()) + list(regimes_b.keys()))

        print(f"\n  {'Regime':<25s} | {'A Trades':>8s} | {'A WR%':>6s} | {'A P&L':>10s} | "
              f"{'B Trades':>8s} | {'B WR%':>6s} | {'B P&L':>10s} | Winner")
        print(f"  {'-'*25}-+-{'-'*8}-+-{'-'*6}-+-{'-'*10}-+-{'-'*8}-+-{'-'*6}-+-{'-'*10}-+-{'-'*8}")

        for regime in sorted(all_regimes):
            ra = regimes_a.get(regime, {})
            rb = regimes_b.get(regime, {})

            a_trades = ra.get("trades", 0)
            a_wins = ra.get("wins", 0)
            a_pnl = ra.get("pnl", 0)
            a_wr = round(a_wins / max(1, a_trades) * 100, 1)

            b_trades = rb.get("trades", 0)
            b_wins = rb.get("wins", 0)
            b_pnl = rb.get("pnl", 0)
            b_wr = round(b_wins / max(1, b_trades) * 100, 1)

            # Simple winner per regime
            if a_trades < 2 and b_trades < 2:
                winner = "-"
            elif b_pnl > a_pnl and b_wr >= a_wr - 5:
                winner = "B (aggr)"
            elif a_pnl > b_pnl:
                winner = "A (curr)"
            else:
                winner = "TIE"

            regime_comparison[regime] = {
                "a": {"trades": a_trades, "wr": a_wr, "pnl": round(a_pnl, 2)},
                "b": {"trades": b_trades, "wr": b_wr, "pnl": round(b_pnl, 2)},
                "winner": winner,
            }

            print(f"  {regime:<25s} | {a_trades:>8d} | {a_wr:>5.1f}% | ${a_pnl:>9.2f} | "
                  f"{b_trades:>8d} | {b_wr:>5.1f}% | ${b_pnl:>9.2f} | {winner}")

        return {
            "current_config": current_config,
            "aggressive_config": aggressive_config,
            "overall_a": _extract_metrics(results_a),
            "overall_b": _extract_metrics(results_b),
            "regime_comparison": regime_comparison,
        }

    def run_full_evolution(self) -> dict:
        """
        The big one: test current config against 5 experimental variations:
        1. Current (baseline)
        2. Looser tf_votes (2 everywhere)
        3. Looser momentum (35 everywhere)
        4. Wider stops (12 ticks)
        5. Tighter stops (6 ticks)

        Pick the winner. Write recommendation to logs/research/ab_test_YYYY-MM-DD.json
        """
        print("\n" + "=" * 60)
        print("  A/B FULL EVOLUTION TEST")
        print("=" * 60)

        # Build the 5 variants
        current = {
            "min_tf_votes": STRATEGIES["bias_momentum"].get("min_tf_votes", 3),
            "min_momentum": STRATEGIES["bias_momentum"].get("min_momentum", 55),
            "stop_ticks": STRATEGIES["bias_momentum"].get("stop_ticks", 9),
        }

        variants = {
            "1_current": current,
            "2_loose_tf": {**current, "min_tf_votes": 2},
            "3_loose_momentum": {**current, "min_momentum": 35},
            "4_wider_stops": {**current, "stop_ticks": 12},
            "5_tighter_stops": {**current, "stop_ticks": 6},
        }

        # Run all variants
        all_metrics = {}
        all_raw = {}
        strategy = "bias_momentum"

        for label, params in variants.items():
            sys.stdout.write(f"\r  Testing: {label:30s}")
            sys.stdout.flush()
            raw = _run_backtest(self.bars, [strategy], {strategy: params})
            metrics = _extract_metrics(raw)
            all_metrics[label] = metrics
            all_raw[label] = raw

        print()

        # Score all variants
        metrics_list = list(all_metrics.values())
        scores = {}
        for label, metrics in all_metrics.items():
            scores[label] = _score_config(metrics, metrics_list)

        # Print comparison table
        print(f"\n  {'Variant':<25s} | {'Trades':>6s} | {'WR%':>6s} | {'P&L':>10s} | "
              f"{'PF':>6s} | {'MaxDD':>8s} | {'Score':>6s}")
        print(f"  {'-'*25}-+-{'-'*6}-+-{'-'*6}-+-{'-'*10}-+-{'-'*6}-+-{'-'*8}-+-{'-'*6}")

        best_label = None
        best_score = -1

        for label in variants:
            m = all_metrics[label]
            s = scores[label]
            marker = ""
            if s > best_score:
                best_score = s
                best_label = label

            print(f"  {label:<25s} | {m['total_trades']:>6d} | {m['win_rate']:>5.1f}% | "
                  f"${m['total_pnl']:>9.2f} | {m['profit_factor']:>5.2f} | "
                  f"${m['max_drawdown']:>7.2f} | {s:>5.3f}")

        # Mark winner
        print(f"\n  WINNER: {best_label} (score: {best_score:.4f})")
        print(f"  Config: {variants[best_label]}")

        # Run regime A/B
        print("\n  --- Regime Aggressiveness Test ---")
        regime_results = self.run_regime_ab()

        # Build full result
        result = {
            "timestamp": datetime.now().isoformat(),
            "data_file": self.data_path,
            "bars_tested": len(self.bars),
            "strategy": strategy,
            "variants": {
                label: {
                    "params": variants[label],
                    "metrics": all_metrics[label],
                    "score": scores[label],
                }
                for label in variants
            },
            "winner": best_label,
            "winner_params": variants[best_label],
            "winner_score": best_score,
            "winner_metrics": all_metrics[best_label],
            "current_metrics": all_metrics["1_current"],
            "regime_analysis": regime_results,
            "improvement_vs_current": {
                "pnl_diff": round(
                    all_metrics[best_label]["total_pnl"] - all_metrics["1_current"]["total_pnl"], 2
                ),
                "wr_diff": round(
                    all_metrics[best_label]["win_rate"] - all_metrics["1_current"]["win_rate"], 1
                ),
                "pf_diff": round(
                    all_metrics[best_label]["profit_factor"] - all_metrics["1_current"]["profit_factor"], 2
                ),
            },
        }

        # Generate recommendations
        recommendations = []
        if best_label != "1_current":
            imp = result["improvement_vs_current"]
            recommendations.append(
                f"Switch to '{best_label}' config: {variants[best_label]} "
                f"(+${imp['pnl_diff']:.2f} P&L, {imp['wr_diff']:+.1f}% WR, {imp['pf_diff']:+.2f} PF)"
            )
        else:
            recommendations.append("Current config is optimal. No changes needed.")

        # Regime recommendations
        for regime, data in regime_results.get("regime_comparison", {}).items():
            if data["winner"] == "B (aggr)" and data["b"]["trades"] >= 3:
                recommendations.append(
                    f"Consider aggressive config for {regime}: "
                    f"{data['b']['trades']}T, {data['b']['wr']}%WR, ${data['b']['pnl']:.2f}"
                )

        result["recommendations"] = recommendations

        # Save to logs/research/
        output_dir = os.path.join(os.path.dirname(__file__), "..", "logs", "research")
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        json_path = os.path.join(output_dir, f"ab_test_{ts}.json")
        with open(json_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n  Results saved: {json_path}")

        result["json_path"] = json_path
        return result


# =====================================================================
# AI Analysis (optional)
# =====================================================================

def _ai_analyze(result: dict) -> str:
    """Send A/B test results to Gemini for analysis."""
    import asyncio

    prompt = f"""You are an expert algorithmic trading researcher analyzing A/B test results for MNQ futures.

DATA:
- Instrument: MNQ (Micro Nasdaq futures), $0.50/tick, $2/point
- Strategy tested: {result.get('strategy', 'bias_momentum')}
- Bars: {result.get('bars_tested', 0)}

VARIANT RESULTS:
{json.dumps(result.get('variants', {}), indent=2, default=str)}

WINNER: {result.get('winner', 'N/A')}
Winner params: {result.get('winner_params', {})}
Winner metrics: {json.dumps(result.get('winner_metrics', {}), indent=2)}
Current metrics: {json.dumps(result.get('current_metrics', {}), indent=2)}

REGIME ANALYSIS:
{json.dumps(result.get('regime_analysis', {}).get('regime_comparison', {}), indent=2, default=str)}

QUESTIONS:
1. Is the winning config statistically meaningful or noise?
2. Which regime-specific changes have the strongest signal?
3. Are there overfitting risks with any variant?
4. What should the trader implement first?

Be specific with numbers. Keep response under 400 words."""

    try:
        from agents.ai_client import ask_gemini

        async def _query():
            return await ask_gemini(
                prompt=prompt,
                system="You are a quantitative trading researcher. Be concise and data-driven.",
                model_name="gemini-2.5-flash",
                max_tokens=1500,
                temperature=0.3,
                timeout_s=30.0,
            )

        response = asyncio.run(_query())
        return response or "No response from Gemini."
    except Exception as e:
        return f"AI analysis failed: {e}"


# =====================================================================
# CLI
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Phoenix Bot A/B Parameter Tester"
    )
    parser.add_argument(
        "--data", required=True,
        help="Path to historical CSV data (e.g. C:\\temp\\mnq_historical.csv)"
    )
    parser.add_argument(
        "--ai", action="store_true",
        help="Use Gemini to analyze results"
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    logging.getLogger("ABTester").setLevel(logging.INFO)

    print("\n" + "=" * 60)
    print("  PHOENIX BOT -- A/B PARAMETER TESTER")
    print(f"  Data: {args.data}")
    print(f"  AI analysis: {'Yes' if args.ai else 'No'}")
    print("=" * 60)

    # Load data
    print("\n  Loading historical data...")
    logging.disable(logging.NOTSET)
    logging.getLogger("Backtester").setLevel(logging.INFO)

    start_time = time.time()
    tester = ABTester(args.data)
    logging.getLogger("Backtester").setLevel(logging.WARNING)

    print(f"  Loaded {len(tester.bars):,} bars")
    if tester.bars:
        print(f"  Date range: {tester.bars[0]['timestamp'][:10]} to {tester.bars[-1]['timestamp'][:10]}")

    # Run full evolution test
    results = tester.run_full_evolution()
    elapsed = time.time() - start_time

    # Print recommendations
    print(f"\n" + "=" * 60)
    print("  RECOMMENDATIONS")
    print("=" * 60)
    for i, rec in enumerate(results.get("recommendations", []), 1):
        print(f"  {i}. {rec}")

    # AI analysis
    if args.ai:
        print(f"\n" + "=" * 60)
        print("  AI ANALYSIS (Gemini)")
        print("=" * 60)
        ai_response = _ai_analyze(results)
        print(f"\n{ai_response}")

        # Append to saved JSON
        json_path = results.get("json_path")
        if json_path and os.path.exists(json_path):
            try:
                with open(json_path, "r") as f:
                    data = json.load(f)
                data["ai_analysis"] = ai_response
                with open(json_path, "w") as f:
                    json.dump(data, f, indent=2, default=str)
                print(f"\n  AI analysis appended to {json_path}")
            except Exception as e:
                print(f"\n  Could not update JSON: {e}")

    # Final summary
    print(f"\n" + "=" * 60)
    print("  A/B TEST COMPLETE")
    print(f"  Total runtime: {elapsed:.1f}s")
    print(f"  Winner: {results.get('winner', 'N/A')}")
    imp = results.get("improvement_vs_current", {})
    if imp.get("pnl_diff", 0) != 0:
        print(f"  Improvement: +${imp['pnl_diff']:.2f} P&L, "
              f"{imp['wr_diff']:+.1f}% WR, {imp['pf_diff']:+.2f} PF")
    print(f"  Report: {results.get('json_path', 'N/A')}")
    print(f"\n  NOTE: No live config was modified. Review and apply manually.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
