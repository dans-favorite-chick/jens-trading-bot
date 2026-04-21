"""
Phoenix Bot -- Equity Curve & Historical P&L Tracker

Tracks daily P&L and builds equity curve for dashboard charting
and AI learning. Records but never restricts.
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("EquityTracker")

DEFAULT_EQUITY_FILE = "logs/performance/equity_curve.json"


class EquityTracker:
    """Tracks daily P&L and builds equity curve for dashboard + AI learning."""

    def __init__(self, filepath: str = DEFAULT_EQUITY_FILE):
        self.daily_file = filepath
        self.daily_records: list[dict] = []
        self._load()

    def _load(self):
        """Load existing equity data from disk."""
        try:
            if os.path.exists(self.daily_file):
                with open(self.daily_file, "r") as f:
                    self.daily_records = json.load(f)
                logger.info(f"Loaded {len(self.daily_records)} daily records from equity file")
        except Exception as e:
            logger.warning(f"Could not load equity data: {e}")
            self.daily_records = []

    def _save(self):
        """Persist equity data to disk."""
        try:
            os.makedirs(os.path.dirname(self.daily_file), exist_ok=True)
            with open(self.daily_file, "w") as f:
                json.dump(self.daily_records, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Could not save equity data: {e}")

    def record_day(self, date_str: str, daily_pnl: float, trades: int,
                   wins: int, losses: int, strategy_breakdown: dict = None):
        """
        Record end-of-day stats. Called by session debrief or daily reset.

        Args:
            date_str: "2026-04-11" format
            daily_pnl: net P&L in dollars for the day
            trades: total trade count
            wins: winning trade count
            losses: losing trade count
            strategy_breakdown: {strategy_name: {pnl, trades, wins, losses}}
        """
        # Check if we already have a record for this date -- update if so
        for rec in self.daily_records:
            if rec.get("date") == date_str:
                rec["pnl"] = daily_pnl
                rec["trades"] = trades
                rec["wins"] = wins
                rec["losses"] = losses
                rec["win_rate"] = round(wins / max(1, trades) * 100, 1)
                rec["strategies"] = strategy_breakdown or {}
                rec["updated_at"] = datetime.now().isoformat()
                self._save()
                logger.info(f"[EQUITY] Updated {date_str}: P&L=${daily_pnl:.2f} "
                            f"({trades} trades, {wins}W/{losses}L)")
                return

        # Compute max drawdown from cumulative curve
        cum_pnl = sum(r.get("pnl", 0) for r in self.daily_records)
        new_cum = cum_pnl + daily_pnl
        peak = max((r.get("cumulative_pnl", 0) for r in self.daily_records), default=0)
        peak = max(peak, new_cum)
        drawdown = peak - new_cum if peak > 0 else 0

        record = {
            "date": date_str,
            "pnl": round(daily_pnl, 2),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(1, trades) * 100, 1),
            "cumulative_pnl": round(new_cum, 2),
            "peak_equity": round(peak, 2),
            "drawdown": round(drawdown, 2),
            "strategies": strategy_breakdown or {},
            "recorded_at": datetime.now().isoformat(),
        }

        self.daily_records.append(record)
        self._save()

        logger.info(f"[EQUITY] Recorded {date_str}: P&L=${daily_pnl:.2f} "
                    f"cum=${new_cum:.2f} ({trades} trades, {wins}W/{losses}L)")

    def get_equity_curve(self) -> list[dict]:
        """Return cumulative equity curve for dashboard charting."""
        curve = []
        cumulative = 0.0
        peak = 0.0
        for rec in self.daily_records:
            cumulative += rec.get("pnl", 0)
            peak = max(peak, cumulative)
            curve.append({
                "date": rec.get("date"),
                "daily_pnl": rec.get("pnl", 0),
                "cumulative": round(cumulative, 2),
                "peak": round(peak, 2),
                "drawdown": round(peak - cumulative, 2),
            })
        return curve

    def get_summary(self, days: int = 30) -> dict:
        """
        Return performance summary for the last N days.

        Returns: {
            total_pnl, avg_daily, max_drawdown, best_day, worst_day,
            total_trades, total_wins, total_losses, overall_wr,
            winning_days, losing_days, profit_factor
        }
        """
        records = self.daily_records[-days:] if days else self.daily_records
        if not records:
            return {
                "total_pnl": 0, "avg_daily": 0, "max_drawdown": 0,
                "best_day": 0, "worst_day": 0, "total_trades": 0,
                "total_wins": 0, "total_losses": 0, "overall_wr": 0,
                "winning_days": 0, "losing_days": 0, "profit_factor": 0,
                "days_tracked": 0,
            }

        pnls = [r.get("pnl", 0) for r in records]
        total_pnl = sum(pnls)
        total_trades = sum(r.get("trades", 0) for r in records)
        total_wins = sum(r.get("wins", 0) for r in records)
        total_losses = sum(r.get("losses", 0) for r in records)

        # Max drawdown from equity curve
        peak = 0.0
        max_dd = 0.0
        cumulative = 0.0
        for p in pnls:
            cumulative += p
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        # Profit factor
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0

        winning_days = sum(1 for p in pnls if p > 0)
        losing_days = sum(1 for p in pnls if p < 0)

        return {
            "total_pnl": round(total_pnl, 2),
            "avg_daily": round(total_pnl / len(records), 2),
            "max_drawdown": round(max_dd, 2),
            "best_day": round(max(pnls), 2),
            "worst_day": round(min(pnls), 2),
            "total_trades": total_trades,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "overall_wr": round(total_wins / max(1, total_trades) * 100, 1),
            "winning_days": winning_days,
            "losing_days": losing_days,
            "profit_factor": profit_factor,
            "days_tracked": len(records),
        }

    def to_dict(self) -> dict:
        """For dashboard API -- returns summary + recent curve."""
        return {
            "summary": self.get_summary(30),
            "equity_curve": self.get_equity_curve()[-60:],  # Last 60 days for chart
            "total_days": len(self.daily_records),
        }
