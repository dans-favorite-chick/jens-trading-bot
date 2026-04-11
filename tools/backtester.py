"""
Phoenix Bot — Backtester

Replays historical 1-min bar data through the EXACT same strategy pipeline
used in live trading. No forked logic — imports and runs the real components.

Usage:
    python tools/backtester.py --data C:\\temp\\mnq_historical.csv --strategies all
    python tools/backtester.py --data C:\\temp\\mnq_historical.csv --strategies bias_momentum,spring_setup
    python tools/backtester.py --data C:\\temp\\mnq_historical.csv --strategies all --verbose
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, date, timedelta

# Project imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.tick_aggregator import TickAggregator
from core.risk_manager import RiskManager
from core.session_manager import SessionManager
from core.position_manager import PositionManager
from core.strategy_tracker import StrategyTracker
from config.settings import TICK_SIZE
from config.strategies import STRATEGIES

from strategies.bias_momentum import BiasMomentumFollow
from strategies.spring_setup import SpringSetup
from strategies.vwap_pullback import VWAPPullback
from strategies.high_precision import HighPrecisionOnly

logger = logging.getLogger("Backtester")

STRATEGY_CLASSES = {
    "bias_momentum": BiasMomentumFollow,
    "spring_setup": SpringSetup,
    "vwap_pullback": VWAPPullback,
    "high_precision_only": HighPrecisionOnly,
}


# ─── Bar-to-Tick Synthesizer ──────────────────────────────────────

def bar_to_ticks(timestamp: str, o: float, h: float, l: float, c: float,
                 volume: int, tick_count: int) -> list[dict]:
    """
    Convert a 1-min OHLCV bar into synthetic ticks for the aggregator.

    Generates ticks in realistic order:
      Bullish bar (close > open): O → L → H → C
      Bearish bar (close < open): O → H → L → C
      Doji: O → H → L → C

    Volume split proportionally. Bid/ask estimated as price ± half spread.
    """
    spread = TICK_SIZE  # 0.25 = 1 tick spread
    ticks = []

    # Determine tick order based on bar direction
    if c >= o:
        # Bullish: open → dip to low → rally to high → settle at close
        prices = [o, l, h, c]
    else:
        # Bearish: open → rally to high → drop to low → settle at close
        prices = [o, h, l, c]

    # Add intermediate prices for better aggregator behavior
    # Insert midpoints for smoother price action
    expanded = []
    for i, p in enumerate(prices):
        expanded.append(p)
        if i < len(prices) - 1:
            mid = (p + prices[i + 1]) / 2
            # Snap to tick boundary
            mid = round(mid / TICK_SIZE) * TICK_SIZE
            expanded.append(mid)

    # Split volume across ticks
    vol_per_tick = max(1, volume // len(expanded))

    # Parse timestamp
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        dt = datetime.now()

    for i, price in enumerate(expanded):
        # Stagger timestamps within the 1-min bar
        tick_offset = (i / len(expanded)) * 59  # spread across 59 seconds
        tick_time = dt.timestamp() + tick_offset

        ticks.append({
            "type": "tick",
            "price": round(price, 2),
            "bid": round(price - spread / 2, 2),
            "ask": round(price + spread / 2, 2),
            "vol": vol_per_tick,
            "ts": datetime.fromtimestamp(tick_time).isoformat(),
        })

    return ticks


# ─── CSV Reader ───────────────────────────────────────────────────

def read_csv(filepath: str) -> list[dict]:
    """Read historical bar data from CSV exported by NT8 HistoricalExporter."""
    bars = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bars.append({
                    "timestamp": row["timestamp"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                    "tick_count": int(row.get("tickCount", row.get("tick_count", 0))),
                })
            except (ValueError, KeyError) as e:
                logger.warning(f"Skipping bad row: {e}")
    logger.info(f"Loaded {len(bars)} bars from {filepath}")
    return bars


# ─── Backtest Engine ──────────────────────────────────────────────

class Backtester:
    """
    Replays historical bars through the live trading pipeline.
    Uses the exact same components as the real bot.
    """

    def __init__(self, strategy_names: list[str] = None):
        self.aggregator = TickAggregator()
        self.risk = RiskManager()
        self.session = SessionManager()
        self.positions = PositionManager()
        self.tracker = StrategyTracker()
        self.strategies = []

        # Load strategies
        names = strategy_names or list(STRATEGY_CLASSES.keys())
        for name in names:
            if name in STRATEGY_CLASSES and name in STRATEGIES:
                strat = STRATEGY_CLASSES[name](STRATEGIES[name])
                strat.enabled = True
                self.strategies.append(strat)
                logger.info(f"Loaded strategy: {name}")

        # Results tracking
        self.trades: list[dict] = []
        self.signals_generated = 0
        self.signals_taken = 0
        self.bars_processed = 0
        self.current_date = None

        # Register bar callback
        self.aggregator.on_bar(self._on_bar)

    def run(self, bars: list[dict], verbose: bool = False) -> dict:
        """
        Run backtest on historical bar data.

        Returns dict with full results.
        """
        start_time = time.time()
        total_bars = len(bars)

        logger.info(f"Starting backtest: {total_bars} bars, "
                     f"{len(self.strategies)} strategies")

        for i, bar in enumerate(bars):
            # Track date changes for daily risk reset
            bar_date = bar["timestamp"][:10]
            if bar_date != self.current_date:
                if self.current_date is not None:
                    self.risk.reset_daily()
                self.current_date = bar_date

            # Convert bar to synthetic ticks
            ticks = bar_to_ticks(
                bar["timestamp"], bar["open"], bar["high"],
                bar["low"], bar["close"], bar["volume"],
                bar["tick_count"],
            )

            # Feed each tick through the aggregator (triggers _on_bar callback)
            for tick in ticks:
                self.aggregator.process_tick(tick)

                # Check position exits on every tick
                if not self.positions.is_flat:
                    price = tick["price"]
                    exit_reason = self.positions.check_exits(price)
                    if exit_reason:
                        self._exit_trade(price, exit_reason)

            self.bars_processed += 1

            # Progress logging
            if verbose and i % 5000 == 0 and i > 0:
                pct = (i / total_bars) * 100
                logger.info(f"  Progress: {pct:.0f}% ({i}/{total_bars} bars, "
                             f"{len(self.trades)} trades)")

        # Close any open position at end
        if not self.positions.is_flat:
            price = bars[-1]["close"]
            self._exit_trade(price, "backtest_end")

        elapsed = time.time() - start_time
        results = self._compile_results(elapsed)

        logger.info(f"Backtest complete: {elapsed:.1f}s, "
                     f"{len(self.trades)} trades, "
                     f"P&L ${results['total_pnl']:.2f}")

        return results

    def _on_bar(self, timeframe: str, bar):
        """Called by tick_aggregator when a bar completes."""
        if timeframe not in ("1m", "5m"):
            return

        regime = self.session.get_current_regime(
            datetime.fromtimestamp(bar.end_time)
        )

        # Evaluate strategies (same logic as base_bot._evaluate_strategies)
        if not self.positions.is_flat:
            return

        # Minimum bars guard
        bars_5m = list(self.aggregator.bars_5m.completed)
        bars_1m = list(self.aggregator.bars_1m.completed)
        if len(bars_5m) < 5 or len(bars_1m) < 5:
            return

        # Risk gate
        market = self.aggregator.snapshot()
        atr_5m = market.get("atr_5m", 0)
        vix_proxy = min(50, atr_5m / 4) if atr_5m > 0 else 0
        can_trade, reason = self.risk.can_trade(vix=vix_proxy)
        if not can_trade:
            return

        session_info = self.session.to_dict()

        # Run strategies
        best_signal = None
        for strat in self.strategies:
            if not strat.enabled:
                continue
            if not self.session.is_strategy_allowed(strat.name):
                continue

            try:
                signal = strat.evaluate(market, bars_5m, bars_1m, session_info)
                if signal:
                    self.signals_generated += 1
                    self.tracker.record_signal(
                        strategy=signal.strategy,
                        direction=signal.direction,
                        confidence=signal.confidence,
                        taken=False,  # Updated below if taken
                        regime=regime,
                        trade_id=signal.trade_id,
                    )
                    if signal.confidence > (best_signal.confidence if best_signal else 0):
                        best_signal = signal
            except Exception as e:
                logger.debug(f"Strategy {strat.name} error: {e}")

        if best_signal:
            self._enter_trade(best_signal, market, regime)

    def _enter_trade(self, signal, market: dict, regime: str):
        """Enter a trade (simulated — no OIF, no bridge)."""
        price = market.get("price", 0)
        atr_5m = market.get("atr_5m", 0)

        # Risk sizing
        vix_proxy = min(50, atr_5m / 4) if atr_5m > 0 else 0
        risk_dollars, tier = self.risk.get_risk_for_entry(signal.entry_score, vix=vix_proxy)
        if risk_dollars <= 0:
            return

        stop_ticks = self.risk.calculate_stop_ticks(signal.stop_ticks, atr_5m)
        contracts = self.risk.calculate_contracts(risk_dollars, stop_ticks)

        tick_value = TICK_SIZE
        if signal.direction == "LONG":
            stop_price = price - (stop_ticks * tick_value)
            target_price = price + (stop_ticks * tick_value * signal.target_rr)
        else:
            stop_price = price + (stop_ticks * tick_value)
            target_price = price - (stop_ticks * tick_value * signal.target_rr)

        self.positions.open_position(
            trade_id=signal.trade_id,
            direction=signal.direction,
            entry_price=price,
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            strategy=signal.strategy,
            reason=signal.reason,
            market_snapshot={**market, "regime": regime},
        )

        self.signals_taken += 1
        self.tracker.record_signal(
            strategy=signal.strategy,
            direction=signal.direction,
            confidence=signal.confidence,
            taken=True,
            regime=regime,
            trade_id=signal.trade_id,
        )

    def _exit_trade(self, price: float, reason: str):
        """Exit a trade and record results."""
        trade = self.positions.close_position(price, reason)
        if trade:
            trade["source"] = "backtest"
            self.risk.record_trade(trade["pnl_dollars"])
            self.tracker.record_trade(trade)
            self.trades.append(trade)

    def _compile_results(self, elapsed_s: float) -> dict:
        """Compile final backtest results."""
        total_pnl = sum(t["pnl_dollars"] for t in self.trades)
        wins = [t for t in self.trades if t["result"] == "WIN"]
        losses = [t for t in self.trades if t["result"] == "LOSS"]

        # Per-strategy breakdown
        by_strategy = {}
        for t in self.trades:
            s = t["strategy"]
            if s not in by_strategy:
                by_strategy[s] = {"trades": 0, "wins": 0, "pnl": 0.0, "results": []}
            by_strategy[s]["trades"] += 1
            by_strategy[s]["pnl"] = round(by_strategy[s]["pnl"] + t["pnl_dollars"], 2)
            by_strategy[s]["results"].append(t["result"])
            if t["result"] == "WIN":
                by_strategy[s]["wins"] += 1

        for s in by_strategy:
            bs = by_strategy[s]
            bs["win_rate"] = round(bs["wins"] / max(1, bs["trades"]) * 100, 1)
            bs["avg_pnl"] = round(bs["pnl"] / max(1, bs["trades"]), 2)

        # Per-regime breakdown
        by_regime = {}
        for t in self.trades:
            r = t.get("market_snapshot", {}).get("regime", "UNKNOWN")
            if r not in by_regime:
                by_regime[r] = {"trades": 0, "wins": 0, "pnl": 0.0}
            by_regime[r]["trades"] += 1
            by_regime[r]["pnl"] = round(by_regime[r]["pnl"] + t["pnl_dollars"], 2)
            if t["result"] == "WIN":
                by_regime[r]["wins"] += 1

        # Equity curve
        equity = []
        running_pnl = 0
        for t in self.trades:
            running_pnl += t["pnl_dollars"]
            equity.append({
                "trade_id": t.get("trade_id", ""),
                "pnl": round(running_pnl, 2),
                "strategy": t["strategy"],
                "result": t["result"],
            })

        # Max drawdown
        peak = 0
        max_dd = 0
        for e in equity:
            peak = max(peak, e["pnl"])
            dd = peak - e["pnl"]
            max_dd = max(max_dd, dd)

        return {
            "summary": {
                "total_trades": len(self.trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / max(1, len(self.trades)) * 100, 1),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl_per_trade": round(total_pnl / max(1, len(self.trades)), 2),
                "max_drawdown": round(max_dd, 2),
                "profit_factor": round(
                    sum(t["pnl_dollars"] for t in wins) /
                    max(0.01, abs(sum(t["pnl_dollars"] for t in losses))), 2
                ) if losses else float("inf"),
                "signals_generated": self.signals_generated,
                "signals_taken": self.signals_taken,
                "bars_processed": self.bars_processed,
                "elapsed_seconds": round(elapsed_s, 1),
            },
            "by_strategy": by_strategy,
            "by_regime": by_regime,
            "equity_curve": equity,
            "trades": self.trades,
            "strategy_tracker": self.tracker.get_all_summaries(),
        }


# ─── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phoenix Bot Backtester")
    parser.add_argument("--data", required=True, help="Path to CSV file from NT8 HistoricalExporter")
    parser.add_argument("--strategies", default="all",
                        help="Comma-separated strategy names, or 'all'")
    parser.add_argument("--output", default=None,
                        help="Output JSON path (default: logs/backtest/)")
    parser.add_argument("--verbose", action="store_true", help="Show progress")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    # Always show backtester logs
    logging.getLogger("Backtester").setLevel(logging.INFO)

    # Parse strategies
    if args.strategies == "all":
        strat_names = list(STRATEGY_CLASSES.keys())
    else:
        strat_names = [s.strip() for s in args.strategies.split(",")]

    # Read data
    bars = read_csv(args.data)
    if not bars:
        print("ERROR: No bars loaded. Check CSV file.")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"  PHOENIX BOT BACKTESTER")
    print(f"  Data: {args.data}")
    print(f"  Bars: {len(bars)}")
    print(f"  Date range: {bars[0]['timestamp'][:10]} to {bars[-1]['timestamp'][:10]}")
    print(f"  Strategies: {', '.join(strat_names)}")
    print(f"{'=' * 60}\n")

    # Run backtest
    bt = Backtester(strat_names)
    results = bt.run(bars, verbose=args.verbose)

    # Print results
    s = results["summary"]
    print(f"\n{'=' * 60}")
    print(f"  RESULTS")
    print(f"{'=' * 60}")
    print(f"  Total trades:     {s['total_trades']}")
    print(f"  Win rate:         {s['win_rate']}%")
    print(f"  Total P&L:        ${s['total_pnl']:.2f}")
    print(f"  Avg P&L/trade:    ${s['avg_pnl_per_trade']:.2f}")
    print(f"  Max drawdown:     ${s['max_drawdown']:.2f}")
    print(f"  Profit factor:    {s['profit_factor']}")
    print(f"  Signals gen:      {s['signals_generated']}")
    print(f"  Signals taken:    {s['signals_taken']}")
    print(f"  Bars processed:   {s['bars_processed']}")
    print(f"  Runtime:          {s['elapsed_seconds']}s")

    print(f"\n  BY STRATEGY:")
    for name, bs in results["by_strategy"].items():
        wr = bs["win_rate"]
        print(f"    {name:25s} | {bs['trades']:3d} trades | {wr:5.1f}% WR | ${bs['pnl']:8.2f} P&L")

    print(f"\n  BY REGIME:")
    for name, br in sorted(results["by_regime"].items()):
        wr = round(br["wins"] / max(1, br["trades"]) * 100, 1)
        print(f"    {name:20s} | {br['trades']:3d} trades | {wr:5.1f}% WR | ${br['pnl']:8.2f} P&L")

    # Save results
    output_dir = os.path.join(os.path.dirname(__file__), "..", "logs", "backtest")
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_path = args.output or os.path.join(output_dir, f"backtest_{ts}.json")

    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
