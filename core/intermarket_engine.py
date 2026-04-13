"""
Phoenix Bot — Intermarket Correlation Engine

Tracks rolling correlations between NQ and correlated instruments:
- DXY (US Dollar Index) — typically inverse to NQ
- TLT/10Y Bonds — rate sensitivity
- VIX — fear gauge, inverse to NQ
- SPY/ES — broad market correlation (NQ usually leads)

When correlations break down (NQ rallying while VIX spikes),
it flags a divergence warning that strategies can factor in.
"""

import time
import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("Intermarket")


@dataclass
class CorrelationPair:
    """Tracks rolling correlation between NQ and another instrument."""
    name: str
    expected_direction: str  # "positive" or "negative" (normal correlation)
    nq_values: deque = field(default_factory=lambda: deque(maxlen=20))
    other_values: deque = field(default_factory=lambda: deque(maxlen=20))
    correlation: float = 0.0
    divergence: bool = False
    divergence_strength: float = 0.0
    last_update: float = 0.0


class IntermarketEngine:
    """
    Computes rolling correlations and detects divergences
    between NQ and correlated instruments.
    """

    def __init__(self, window: int = 20):
        self.window = window
        self.pairs = {
            "DXY": CorrelationPair(name="DXY", expected_direction="negative"),
            "VIX": CorrelationPair(name="VIX", expected_direction="negative"),
            "TLT": CorrelationPair(name="TLT", expected_direction="negative"),
            "SPY": CorrelationPair(name="SPY", expected_direction="positive"),
        }
        self._last_fetch: float = 0
        self._fetch_interval = 300  # 5 minutes
        self._last_nq_price: float = 0
        self._risk_off_score: float = 0  # 0-100, higher = more risk-off

    def update_nq(self, price: float):
        """Called on each 5m bar close with NQ price."""
        self._last_nq_price = price
        for pair in self.pairs.values():
            pair.nq_values.append(price)

    def update_instrument(self, name: str, value: float):
        """Update an intermarket instrument value."""
        if name not in self.pairs:
            return
        pair = self.pairs[name]
        pair.other_values.append(value)
        pair.last_update = time.time()

        # Recalculate correlation if we have enough data
        if len(pair.nq_values) >= 5 and len(pair.other_values) >= 5:
            self._compute_correlation(pair)

    def update_from_external(self, data: dict):
        """Bulk update from external data fetch.

        data: {"DXY": 104.5, "VIX": 18.2, "TLT": 87.3, "SPY": 520.1}
        """
        for name, value in data.items():
            if isinstance(value, (int, float)) and value > 0:
                self.update_instrument(name, value)

        self._compute_risk_off()

    def _compute_correlation(self, pair: CorrelationPair):
        """Compute Pearson correlation and detect divergences."""
        n = min(len(pair.nq_values), len(pair.other_values))
        if n < 5:
            return

        nq = list(pair.nq_values)[-n:]
        other = list(pair.other_values)[-n:]

        # Compute returns (% change)
        nq_returns = [(nq[i] - nq[i-1]) / nq[i-1] if nq[i-1] != 0 else 0
                      for i in range(1, len(nq))]
        other_returns = [(other[i] - other[i-1]) / other[i-1] if other[i-1] != 0 else 0
                         for i in range(1, len(other))]

        if len(nq_returns) < 3:
            return

        # Pearson correlation
        n_ret = len(nq_returns)
        mean_nq = sum(nq_returns) / n_ret
        mean_other = sum(other_returns) / n_ret

        cov = sum((nq_returns[i] - mean_nq) * (other_returns[i] - mean_other)
                  for i in range(n_ret)) / n_ret
        std_nq = (sum((r - mean_nq) ** 2 for r in nq_returns) / n_ret) ** 0.5
        std_other = (sum((r - mean_other) ** 2 for r in other_returns) / n_ret) ** 0.5

        if std_nq > 0 and std_other > 0:
            pair.correlation = cov / (std_nq * std_other)
        else:
            pair.correlation = 0.0

        # Detect divergence
        expected_negative = pair.expected_direction == "negative"
        if expected_negative:
            # Normal: NQ up → instrument down (negative correlation)
            # Divergence: both moving same direction
            pair.divergence = pair.correlation > 0.3
        else:
            # Normal: NQ up → instrument up (positive correlation)
            # Divergence: moving opposite directions
            pair.divergence = pair.correlation < -0.3

        if pair.divergence:
            pair.divergence_strength = abs(pair.correlation) * 100
            logger.warning(f"[INTERMARKET] {pair.name} DIVERGENCE: "
                         f"corr={pair.correlation:.2f} (expected {pair.expected_direction})")

    def _compute_risk_off(self):
        """Compute overall risk-off score from all pairs."""
        score = 50  # Neutral baseline

        vix = self.pairs["VIX"]
        if vix.other_values:
            latest_vix = vix.other_values[-1]
            if latest_vix > 25:
                score += 20
            elif latest_vix > 20:
                score += 10
            elif latest_vix < 15:
                score -= 10

        # DXY rising = risk off for NQ
        dxy = self.pairs["DXY"]
        if len(dxy.other_values) >= 2:
            dxy_change = (dxy.other_values[-1] - dxy.other_values[-2]) / dxy.other_values[-2]
            if dxy_change > 0.002:  # DXY up > 0.2%
                score += 10
            elif dxy_change < -0.002:
                score -= 10

        # Count active divergences
        divergence_count = sum(1 for p in self.pairs.values() if p.divergence)
        score += divergence_count * 10

        self._risk_off_score = max(0, min(100, score))

    def get_risk_signal(self) -> dict:
        """Get current intermarket risk signal for strategy consumption."""
        divergences = [
            {"name": p.name, "correlation": round(p.correlation, 2),
             "strength": round(p.divergence_strength, 0)}
            for p in self.pairs.values() if p.divergence
        ]

        return {
            "risk_off_score": round(self._risk_off_score, 0),
            "risk_level": ("HIGH" if self._risk_off_score > 70
                          else "ELEVATED" if self._risk_off_score > 55
                          else "NORMAL" if self._risk_off_score > 40
                          else "LOW"),
            "divergences": divergences,
            "divergence_count": len(divergences),
            "recommendation": self._get_recommendation(),
        }

    def _get_recommendation(self) -> str:
        if self._risk_off_score > 70:
            return "High risk-off: reduce size, tighten stops"
        elif self._risk_off_score > 55:
            return "Elevated risk: be selective, favor quality setups"
        elif self._risk_off_score < 30:
            return "Risk-on environment: momentum strategies favored"
        return "Normal conditions"

    def to_dict(self) -> dict:
        pairs_data = {}
        for name, p in self.pairs.items():
            pairs_data[name] = {
                "correlation": round(p.correlation, 3),
                "divergence": p.divergence,
                "divergence_strength": round(p.divergence_strength, 0),
                "data_points": len(p.other_values),
                "last_value": round(p.other_values[-1], 2) if p.other_values else None,
                "stale": (time.time() - p.last_update > 600) if p.last_update > 0 else True,
            }
        return {
            "pairs": pairs_data,
            "risk_off_score": round(self._risk_off_score, 0),
            "risk_signal": self.get_risk_signal(),
        }
