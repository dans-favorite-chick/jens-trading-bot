"""
Phoenix Bot -- Trade Clustering

Clusters trades by conditions to find hidden edge patterns.
Uses only standard library -- no sklearn/numpy required.
Observation only: identifies patterns but does NOT change configs.
"""

import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger("TradeClustering")

MIN_TRADES_FOR_ANALYSIS = 20
MIN_CLUSTER_SIZE = 3  # Need at least 3 trades in a cluster to draw conclusions


def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    return round(wins / len(trades) * 100, 1)


def _avg_pnl(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return round(sum(t.get("pnl_dollars", 0) for t in trades) / len(trades), 2)


def _total_pnl(trades: list[dict]) -> float:
    return round(sum(t.get("pnl_dollars", 0) for t in trades), 2)


def _time_bucket(trade: dict) -> str:
    """Classify trade entry time into 30-min buckets."""
    try:
        entry_time = trade.get("entry_time", 0)
        if isinstance(entry_time, (int, float)) and entry_time > 0:
            dt = datetime.fromtimestamp(entry_time)
        elif isinstance(entry_time, str):
            dt = datetime.fromisoformat(entry_time)
        else:
            return "UNKNOWN"
        # Round to 30-min bucket
        minute = (dt.minute // 30) * 30
        return f"{dt.hour:02d}:{minute:02d}"
    except Exception:
        return "UNKNOWN"


def _atr_bucket(trade: dict) -> str:
    """Classify ATR at entry into low/medium/high."""
    snapshot = trade.get("market_snapshot", {})
    atr = snapshot.get("atr_5m", 0)
    if atr <= 0:
        return "UNKNOWN"
    if atr < 100:
        return "LOW"
    elif atr < 200:
        return "MEDIUM"
    else:
        return "HIGH"


def _cvd_direction(trade: dict) -> str:
    """Classify CVD direction at entry."""
    snapshot = trade.get("market_snapshot", {})
    cvd = snapshot.get("cvd", 0)
    if cvd > 50:
        return "BULLISH"
    elif cvd < -50:
        return "BEARISH"
    else:
        return "FLAT"


def _get_regime(trade: dict) -> str:
    """Extract regime from trade's market snapshot."""
    snapshot = trade.get("market_snapshot", {})
    return snapshot.get("regime", "UNKNOWN")


def _confluence_count(trade: dict) -> str:
    """Estimate confluence count from entry reason or snapshot."""
    # Try to extract from the trade data
    confluences = trade.get("confluences", 0)
    if isinstance(confluences, (list, tuple)):
        confluences = len(confluences)
    if isinstance(confluences, (int, float)):
        if confluences >= 5:
            return "5+"
        elif confluences >= 3:
            return "3-4"
        else:
            return "1-2"
    return "UNKNOWN"


def _build_cluster(name: str, trades: list[dict]) -> dict:
    """Build a cluster result dict."""
    wr = _win_rate(trades)
    pnl = _total_pnl(trades)
    avg = _avg_pnl(trades)

    # Generate insight
    if wr >= 70 and pnl > 0:
        insight = f"STRONG EDGE: {wr}% WR, +${pnl:.2f} total"
    elif wr >= 50 and pnl > 0:
        insight = f"Positive edge: {wr}% WR, +${pnl:.2f} total"
    elif wr < 40:
        insight = f"WEAK: {wr}% WR, ${pnl:.2f} total -- review conditions"
    else:
        insight = f"Neutral: {wr}% WR, ${pnl:.2f} total"

    return {
        "name": name,
        "trades": len(trades),
        "wins": sum(1 for t in trades if t.get("result") == "WIN"),
        "wr": wr,
        "pnl": pnl,
        "avg_pnl": avg,
        "insight": insight,
    }


class TradeClustering:
    """Cluster trades by conditions to find hidden edge patterns."""

    def analyze(self, trades: list[dict]) -> dict:
        """
        Cluster trades and find patterns. Requires 20+ trades for meaningful analysis.

        Returns:
            {
                clusters: [{name, trades, wins, wr, pnl, insight}],
                best_conditions: str,
                worst_conditions: str,
                recommendations: [str],
                trade_count: int,
            }
        """
        if len(trades) < MIN_TRADES_FOR_ANALYSIS:
            return {
                "clusters": [],
                "best_conditions": "Insufficient data",
                "worst_conditions": "Insufficient data",
                "recommendations": [f"Need {MIN_TRADES_FOR_ANALYSIS} trades, have {len(trades)}"],
                "trade_count": len(trades),
            }

        clusters = []

        # --- 1. Regime + Strategy ---
        regime_strat = defaultdict(list)
        for t in trades:
            key = f"{_get_regime(t)}|{t.get('strategy', 'unknown')}"
            regime_strat[key].append(t)
        for key, group in regime_strat.items():
            if len(group) >= MIN_CLUSTER_SIZE:
                regime, strat = key.split("|", 1)
                clusters.append(_build_cluster(f"Regime:{regime} + {strat}", group))

        # --- 2. ATR Bucket + Direction ---
        atr_dir = defaultdict(list)
        for t in trades:
            key = f"{_atr_bucket(t)}|{t.get('direction', '?')}"
            atr_dir[key].append(t)
        for key, group in atr_dir.items():
            if len(group) >= MIN_CLUSTER_SIZE:
                atr, direction = key.split("|", 1)
                clusters.append(_build_cluster(f"ATR:{atr} + {direction}", group))

        # --- 3. Time-of-Day Bucket ---
        time_groups = defaultdict(list)
        for t in trades:
            bucket = _time_bucket(t)
            time_groups[bucket].append(t)
        for bucket, group in sorted(time_groups.items()):
            if len(group) >= MIN_CLUSTER_SIZE:
                clusters.append(_build_cluster(f"Time:{bucket}", group))

        # --- 4. CVD Direction at Entry ---
        cvd_groups = defaultdict(list)
        for t in trades:
            key = _cvd_direction(t)
            cvd_groups[key].append(t)
        for key, group in cvd_groups.items():
            if len(group) >= MIN_CLUSTER_SIZE:
                clusters.append(_build_cluster(f"CVD:{key}", group))

        # --- 5. Confluence Count ---
        conf_groups = defaultdict(list)
        for t in trades:
            key = _confluence_count(t)
            conf_groups[key].append(t)
        for key, group in conf_groups.items():
            if len(group) >= MIN_CLUSTER_SIZE:
                clusters.append(_build_cluster(f"Confluences:{key}", group))

        # --- 6. Consecutive Trade Number (intra-day sequencing) ---
        # Group trades by date, then check if the Nth trade of the day performs worse
        by_date = defaultdict(list)
        for t in trades:
            try:
                entry_time = t.get("entry_time", 0)
                if isinstance(entry_time, (int, float)) and entry_time > 0:
                    dt = datetime.fromtimestamp(entry_time)
                    date_key = dt.strftime("%Y-%m-%d")
                else:
                    continue
                by_date[date_key].append(t)
            except Exception:
                continue

        seq_groups = defaultdict(list)
        for date_key, day_trades in by_date.items():
            # Sort by entry time
            day_trades.sort(key=lambda x: x.get("entry_time", 0))
            for i, t in enumerate(day_trades):
                seq_label = f"Trade#{i+1}" if i < 4 else "Trade#5+"
                seq_groups[seq_label].append(t)
        for key, group in sorted(seq_groups.items()):
            if len(group) >= MIN_CLUSTER_SIZE:
                clusters.append(_build_cluster(f"Sequence:{key}", group))

        # --- Find best and worst ---
        valid_clusters = [c for c in clusters if c["trades"] >= MIN_CLUSTER_SIZE]

        if valid_clusters:
            best = max(valid_clusters, key=lambda c: (c["wr"], c["pnl"]))
            worst = min(valid_clusters, key=lambda c: (c["wr"], c["pnl"]))
            best_conditions = f"{best['name']}: {best['wr']}% WR, ${best['pnl']:.2f}"
            worst_conditions = f"{worst['name']}: {worst['wr']}% WR, ${worst['pnl']:.2f}"
        else:
            best_conditions = "No clusters with enough data"
            worst_conditions = "No clusters with enough data"

        # --- Generate recommendations ---
        recommendations = []
        for c in valid_clusters:
            if c["wr"] >= 75 and c["pnl"] > 0:
                recommendations.append(f"EXPLOIT: {c['name']} shows strong edge ({c['wr']}% WR)")
            elif c["wr"] < 35 and c["pnl"] < 0:
                recommendations.append(f"AVOID: {c['name']} is bleeding ({c['wr']}% WR, ${c['pnl']:.2f})")

        # Check if later trades in the day underperform
        if "Sequence:Trade#1" in [c["name"] for c in valid_clusters]:
            t1 = next((c for c in valid_clusters if c["name"] == "Sequence:Trade#1"), None)
            t3_plus = [c for c in valid_clusters
                       if c["name"].startswith("Sequence:Trade#") and c["name"] != "Sequence:Trade#1"]
            if t1 and t3_plus:
                avg_late_wr = sum(c["wr"] for c in t3_plus) / len(t3_plus)
                if t1["wr"] - avg_late_wr > 15:
                    recommendations.append(
                        f"FATIGUE: First trade WR={t1['wr']}% vs later trades WR={avg_late_wr:.0f}% "
                        f"-- consider reducing later trades"
                    )

        if not recommendations:
            recommendations.append("No strong patterns detected yet -- keep collecting data")

        result = {
            "clusters": sorted(valid_clusters, key=lambda c: c["pnl"], reverse=True),
            "best_conditions": best_conditions,
            "worst_conditions": worst_conditions,
            "recommendations": recommendations,
            "trade_count": len(trades),
        }

        logger.info(f"[CLUSTERING] Analyzed {len(trades)} trades -> "
                    f"{len(valid_clusters)} clusters, "
                    f"best={best_conditions[:60]}")

        return result
