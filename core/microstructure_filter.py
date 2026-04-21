"""
Phoenix Bot -- Microstructure Execution Filter

Fast 2-second post-signal check: is this setup actually tradeable RIGHT NOW?
Checks spread stability, DOM support, delta follow-through, price direction,
and DOM signal alignment.

OBSERVATION + ADVISORY -- returns a score, does NOT block trades.
"""

import logging
from collections import deque

logger = logging.getLogger("MicrostructureFilter")

TICK_SIZE = 0.25
# MNQ normal spread is 1 tick (0.25). Wider than 2 ticks = thin tape.
MAX_NORMAL_SPREAD_TICKS = 2


class MicrostructureFilter:
    """
    After a signal triggers, check microstructure health before entry.
    Not a long confirmation -- just a quick 'is the tape healthy?' check.
    """

    def __init__(self):
        # Rolling recent prices for direction check (last ~10 ticks)
        self._recent_prices: deque[float] = deque(maxlen=10)

    def update_tick(self, price: float):
        """Feed every tick so we can check recent price direction."""
        if price > 0:
            self._recent_prices.append(price)

    def check(self, market: dict, signal_direction: str) -> dict:
        """
        Quick microstructure health check. Call right before entry.

        Args:
            market: tick_aggregator.snapshot() dict
            signal_direction: "LONG" or "SHORT"

        Returns: {
            score: 0-100 (100 = perfect microstructure),
            issues: [str],
            spread_ok: bool,
            dom_supports: bool,
            delta_confirms: bool,
            recommendation: "EXECUTE" | "CAUTION" | "WAIT",
        }
        """
        score = 100
        issues = []
        direction = signal_direction.upper()

        # ── 1. Spread check ─────────────────────────────────────────
        bid = market.get("bid", 0)
        ask = market.get("ask", 0)
        if bid > 0 and ask > 0:
            spread_ticks = (ask - bid) / TICK_SIZE
        else:
            spread_ticks = 1  # Assume normal when data unavailable

        spread_ok = spread_ticks <= MAX_NORMAL_SPREAD_TICKS
        if not spread_ok:
            score -= 30
            issues.append(f"Wide spread: {spread_ticks:.0f} ticks")
        elif spread_ticks > 1:
            # Slightly wide but still tradeable
            score -= 10
            issues.append(f"Spread slightly wide: {spread_ticks:.0f} ticks")

        # ── 2. DOM imbalance persistence ─────────────────────────────
        dom_bid_heavy = market.get("dom_bid_heavy", False)
        dom_ask_heavy = market.get("dom_ask_heavy", False)

        if direction == "LONG":
            dom_supports = dom_bid_heavy
        else:
            dom_supports = dom_ask_heavy

        if not dom_supports:
            # Check if at least neutral (not opposing)
            if direction == "LONG" and dom_ask_heavy:
                score -= 25
                issues.append("DOM opposing: ask-heavy for LONG signal")
            elif direction == "SHORT" and dom_bid_heavy:
                score -= 25
                issues.append("DOM opposing: bid-heavy for SHORT signal")
            else:
                score -= 10
                issues.append("DOM neutral -- no strong support for direction")

        # ── 3. Delta follow-through (CVD direction) ──────────────────
        cvd = market.get("cvd", 0)
        bar_delta = market.get("bar_delta", 0)

        delta_confirms = False
        if direction == "LONG":
            # For longs, want positive CVD or positive bar delta
            delta_confirms = cvd > 0 or bar_delta > 0
        else:
            delta_confirms = cvd < 0 or bar_delta < 0

        if not delta_confirms:
            score -= 20
            issues.append(
                f"Delta not confirming: CVD={cvd:.0f}, bar_delta={bar_delta:.0f} "
                f"vs {direction}"
            )

        # ── 4. No immediate rejection -- price direction check ───────
        prices = list(self._recent_prices)
        price_with_signal = True
        if len(prices) >= 3:
            last_3 = prices[-3:]
            if direction == "LONG":
                # Last 3 ticks should not be falling
                if all(last_3[i] > last_3[i + 1] for i in range(len(last_3) - 1)):
                    score -= 15
                    issues.append("Price falling into LONG signal (last 3 ticks down)")
                    price_with_signal = False
            else:
                # Last 3 ticks should not be rising
                if all(last_3[i] < last_3[i + 1] for i in range(len(last_3) - 1)):
                    score -= 15
                    issues.append("Price rising into SHORT signal (last 3 ticks up)")
                    price_with_signal = False

        # ── 5. DOM signal alignment ──────────────────────────────────
        dom_sig = market.get("dom_signal", {})
        dom_direction = dom_sig.get("direction")
        dom_strength = dom_sig.get("strength", 0)

        dom_aligned = False
        if dom_direction == direction and dom_strength > 20:
            dom_aligned = True
            # Bonus for strong DOM alignment
            if dom_strength > 50:
                score = min(100, score + 5)
        elif dom_direction and dom_direction != direction and dom_strength > 30:
            score -= 15
            issues.append(
                f"DOM signal opposes: {dom_direction} strength={dom_strength:.0f}"
            )

        # ── Clamp and classify ───────────────────────────────────────
        score = max(0, min(100, score))

        if score >= 70:
            recommendation = "EXECUTE"
        elif score >= 40:
            recommendation = "CAUTION"
        else:
            recommendation = "WAIT"

        result = {
            "score": score,
            "issues": issues,
            "spread_ok": spread_ok,
            "dom_supports": dom_supports,
            "delta_confirms": delta_confirms,
            "price_with_signal": price_with_signal,
            "dom_aligned": dom_aligned,
            "recommendation": recommendation,
        }

        logger.info(
            f"[MICRO] score={score} rec={recommendation} "
            f"spread={'OK' if spread_ok else 'WIDE'} "
            f"dom={'YES' if dom_supports else 'NO'} "
            f"delta={'YES' if delta_confirms else 'NO'} "
            f"issues={len(issues)}"
        )

        return result

    def to_dict(self) -> dict:
        """For dashboard display."""
        return {
            "recent_prices_buffered": len(self._recent_prices),
        }
