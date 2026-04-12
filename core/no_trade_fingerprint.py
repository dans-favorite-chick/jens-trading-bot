"""
Phoenix Bot -- No-Trade Fingerprint Engine

Learns recurring conditions before BAD trades and creates a compact blacklist.
NOT a hard gate -- outputs a risk_score (0-100) that strategies and the
pre-trade filter can factor in. Observation + advisory only.

Feeds into:
  - Pre-trade filter (risk score context)
  - Dashboard (fingerprint display)
  - Session Debriefer (pattern analysis)
"""

import json
import os
import time
import logging
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger("NoTradeFingerprint")

PERF_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "performance")

# Need at least this many losses with matching conditions before creating a fingerprint
MIN_LOSSES_FOR_FINGERPRINT = 3


def _time_bucket(ts: float) -> str:
    """Convert timestamp to 30-minute bucket string."""
    try:
        dt = datetime.fromtimestamp(ts)
        minute = (dt.minute // 30) * 30
        return f"{dt.hour:02d}:{minute:02d}"
    except Exception:
        return "UNKNOWN"


def _atr_bucket(atr: float) -> str:
    """Classify ATR into low/med/high."""
    if atr <= 0:
        return "UNKNOWN"
    if atr < 100:
        return "LOW"
    elif atr < 200:
        return "MEDIUM"
    else:
        return "HIGH"


def _cvd_direction(cvd: float) -> str:
    """Classify CVD direction."""
    if cvd > 50:
        return "BULLISH"
    elif cvd < -50:
        return "BEARISH"
    else:
        return "FLAT"


def _price_vs_vwap(price: float, vwap: float) -> str:
    """Classify price position relative to VWAP."""
    if vwap <= 0:
        return "UNKNOWN"
    diff_ticks = (price - vwap) / 0.25  # TICK_SIZE
    if diff_ticks > 4:
        return "ABOVE"
    elif diff_ticks < -4:
        return "BELOW"
    else:
        return "AT"


def _entry_score_bucket(score: float) -> str:
    """Classify entry score."""
    if score >= 45:
        return "HIGH"
    elif score >= 30:
        return "MEDIUM"
    else:
        return "LOW"


def _extract_conditions(trade: dict, market: dict) -> dict:
    """
    Extract the environmental conditions at trade entry.
    These form the dimensions of our fingerprint matching.
    """
    snapshot = trade.get("market_snapshot", market)
    entry_time = trade.get("entry_time", time.time())

    return {
        "regime": snapshot.get("regime", "UNKNOWN"),
        "time_bucket": _time_bucket(entry_time),
        "atr_bucket": _atr_bucket(snapshot.get("atr_5m", 0)),
        "cvd_direction": _cvd_direction(snapshot.get("cvd", 0)),
        "price_vs_vwap": _price_vs_vwap(
            snapshot.get("price", 0), snapshot.get("vwap", 0)
        ),
        "entry_score_bucket": _entry_score_bucket(trade.get("entry_score", 0)),
    }


def _conditions_to_key(conditions: dict, dims: list[str]) -> str:
    """Create a hashable key from selected condition dimensions."""
    return "|".join(str(conditions.get(d, "?")) for d in dims)


class NoTradeFingerprint:
    """
    Learns what conditions precede losing trades.
    Builds a blacklist of 'fingerprints' that the bot should avoid.

    NOT a hard gate -- outputs a risk_score (0-100) that strategies
    and the pre-trade filter can factor in. Observation + advisory.
    """

    # Dimension combinations to check for recurring loss patterns.
    # 2-dim combos catch broad patterns, 3-dim combos catch specific ones.
    _DIMENSION_COMBOS = [
        # 2-dim: broad patterns
        ["regime", "time_bucket"],
        ["regime", "atr_bucket"],
        ["regime", "cvd_direction"],
        ["time_bucket", "cvd_direction"],
        ["regime", "entry_score_bucket"],
        # 3-dim: specific patterns
        ["regime", "time_bucket", "cvd_direction"],
        ["regime", "atr_bucket", "entry_score_bucket"],
        ["regime", "time_bucket", "entry_score_bucket"],
    ]

    def __init__(self):
        self._fingerprints = []       # Learned bad-trade patterns
        self._loss_conditions = []    # Raw conditions from all losses (for learning)
        self._file = os.path.join(PERF_DIR, "no_trade_fingerprints.json")
        self._load()

    # ─── Learning ──────────────────────────────────────────────────

    def learn_from_trade(self, trade: dict, market: dict):
        """
        After every losing trade, extract the conditions that preceded it
        and check if a fingerprint pattern has emerged.
        """
        if trade.get("result") != "LOSS":
            return

        conditions = _extract_conditions(trade, market)
        conditions["pnl_ticks"] = trade.get("pnl_ticks", 0)
        conditions["trade_id"] = trade.get("trade_id", "")
        conditions["timestamp"] = time.time()
        self._loss_conditions.append(conditions)

        # Keep last 200 loss conditions for pattern matching
        self._loss_conditions = self._loss_conditions[-200:]

        # Rebuild fingerprints from accumulated loss data
        self._rebuild_fingerprints()
        self._save()

        logger.info(f"[FINGERPRINT] Learned from loss {trade.get('trade_id', '?')}: "
                     f"regime={conditions['regime']} time={conditions['time_bucket']} "
                     f"atr={conditions['atr_bucket']} cvd={conditions['cvd_direction']}")

    def _rebuild_fingerprints(self):
        """
        Scan all accumulated loss conditions for recurring patterns.
        A fingerprint emerges when MIN_LOSSES_FOR_FINGERPRINT losses
        share the same condition combination.
        """
        new_fingerprints = []

        for dims in self._DIMENSION_COMBOS:
            # Group losses by this dimension combo
            groups = defaultdict(list)
            for cond in self._loss_conditions:
                key = _conditions_to_key(cond, dims)
                groups[key].append(cond)

            for key, losses in groups.items():
                if len(losses) >= MIN_LOSSES_FOR_FINGERPRINT:
                    # This is a recurring loss pattern
                    avg_pnl = sum(c.get("pnl_ticks", 0) for c in losses) / len(losses)
                    dim_values = dict(zip(dims, key.split("|")))

                    # Build human-readable description
                    desc_parts = [f"{d}={v}" for d, v in dim_values.items()]
                    description = f"Losses when {', '.join(desc_parts)}"

                    new_fingerprints.append({
                        "dimensions": dims,
                        "key": key,
                        "values": dim_values,
                        "loss_count": len(losses),
                        "avg_loss_ticks": round(avg_pnl, 1),
                        "description": description,
                        "severity": min(100, len(losses) * 15),  # More losses = higher severity
                        "last_seen": max(c.get("timestamp", 0) for c in losses),
                    })

        self._fingerprints = new_fingerprints
        if new_fingerprints:
            logger.info(f"[FINGERPRINT] {len(new_fingerprints)} patterns identified")

    # ─── Risk Scoring ──────────────────────────────────────────────

    def get_risk_score(self, market: dict, session_info: dict,
                       signal: dict, trade_count_today: int) -> dict:
        """
        Score current conditions against learned fingerprints.

        Returns: {
            risk_score: 0-100 (0=safe, 100=matches every bad pattern),
            matching_fingerprints: [{pattern, loss_count, description}],
            recommendation: "CLEAR" | "CAUTION" | "HIGH_RISK",
        }
        """
        if not self._fingerprints:
            return {
                "risk_score": 0,
                "matching_fingerprints": [],
                "recommendation": "CLEAR",
            }

        # Build current conditions from market + signal data
        current = {
            "regime": session_info.get("regime", "UNKNOWN"),
            "time_bucket": _time_bucket(time.time()),
            "atr_bucket": _atr_bucket(market.get("atr_5m", 0)),
            "cvd_direction": _cvd_direction(market.get("cvd", 0)),
            "price_vs_vwap": _price_vs_vwap(
                market.get("price", 0), market.get("vwap", 0)
            ),
            "entry_score_bucket": _entry_score_bucket(
                signal.get("entry_score", 0) if isinstance(signal, dict)
                else getattr(signal, "entry_score", 0)
            ),
        }

        matching = []
        total_severity = 0

        for fp in self._fingerprints:
            # Check if current conditions match this fingerprint
            current_key = _conditions_to_key(current, fp["dimensions"])
            if current_key == fp["key"]:
                matching.append({
                    "pattern": fp["description"],
                    "loss_count": fp["loss_count"],
                    "avg_loss_ticks": fp["avg_loss_ticks"],
                    "severity": fp["severity"],
                })
                total_severity += fp["severity"]

        # Combine severities (diminishing returns -- cap at 100)
        if matching:
            # Use highest single match + 30% of remaining
            severities = sorted([m["severity"] for m in matching], reverse=True)
            risk_score = severities[0]
            for s in severities[1:]:
                risk_score += s * 0.3
            risk_score = min(100, round(risk_score))
        else:
            risk_score = 0

        # Add bonus risk for high trade count (fatigue factor)
        if trade_count_today >= 5:
            risk_score = min(100, risk_score + 10)
        elif trade_count_today >= 3:
            risk_score = min(100, risk_score + 5)

        # Recommendation
        if risk_score >= 60:
            recommendation = "HIGH_RISK"
        elif risk_score >= 30:
            recommendation = "CAUTION"
        else:
            recommendation = "CLEAR"

        if matching:
            logger.info(f"[FINGERPRINT] Risk score={risk_score} ({recommendation}), "
                         f"{len(matching)} patterns match: "
                         f"{matching[0]['pattern']}")

        return {
            "risk_score": risk_score,
            "matching_fingerprints": matching,
            "recommendation": recommendation,
        }

    # ─── Dashboard ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """For dashboard and AI agents."""
        return {
            "fingerprint_count": len(self._fingerprints),
            "loss_samples": len(self._loss_conditions),
            "fingerprints": [
                {
                    "pattern": fp["description"],
                    "loss_count": fp["loss_count"],
                    "avg_loss_ticks": fp["avg_loss_ticks"],
                    "severity": fp["severity"],
                }
                for fp in sorted(self._fingerprints,
                                 key=lambda x: x["severity"], reverse=True)
            ][:10],  # Top 10 worst patterns
        }

    # ─── Persistence ───────────────────────────────────────────────

    def _load(self):
        """Load fingerprint data from disk."""
        try:
            os.makedirs(PERF_DIR, exist_ok=True)
            if os.path.exists(self._file):
                with open(self._file, "r") as f:
                    data = json.load(f)
                self._fingerprints = data.get("fingerprints", [])
                self._loss_conditions = data.get("loss_conditions", [])
                logger.info(f"Loaded {len(self._fingerprints)} fingerprints, "
                             f"{len(self._loss_conditions)} loss samples")
        except Exception as e:
            logger.warning(f"Could not load fingerprint data: {e}")
            self._fingerprints = []
            self._loss_conditions = []

    def _save(self):
        """Persist fingerprint data to disk."""
        try:
            os.makedirs(PERF_DIR, exist_ok=True)
            with open(self._file, "w") as f:
                json.dump({
                    "fingerprints": self._fingerprints,
                    "loss_conditions": self._loss_conditions,
                    "last_updated": time.time(),
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save fingerprint data: {e}")
