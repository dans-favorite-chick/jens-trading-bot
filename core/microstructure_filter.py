"""
Phoenix Bot -- Microstructure Execution Filter

Fast 2-second post-signal check: is this setup actually tradeable RIGHT NOW?
Checks spread stability, DOM support, delta follow-through, price direction,
and DOM signal alignment.

OBSERVATION + ADVISORY -- returns a score, does NOT block trades.

2026-05-22 (pt8) — B-031 sign-flip per agent ab84603a forensic.
The original formula rewarded "perfect tape" (tight spread + bid-heavy DOM
for LONG + positive CVD + price rising into entry) — empirically anti-edge
with IC = -0.152 across 12,039 trades (statistically significant: z ≈ 16+).
That tape pattern is the canonical adverse-selection trap (informed money
unloading into retail chase). Block 1 (spread) is kept as a true cost
penalty; blocks 2-5 are inverted so the score now PENALIZES adverse-
selection conditions instead of rewarding them.

Until 1,000-trade live A/B confirms IC has flipped to ~+0.15, this stays
ADVISORY-ONLY (base_bot.py:4281-4289 — score is logged, never blocks).
Set INVERT_PER_B031=False at the top of this file to revert to legacy
behavior for direct A/B comparison.
"""

import logging
from collections import deque

logger = logging.getLogger("MicrostructureFilter")

TICK_SIZE = 0.25
# MNQ normal spread is 1 tick (0.25). Wider than 2 ticks = thin tape.
MAX_NORMAL_SPREAD_TICKS = 2

# 2026-05-22 (pt8): B-031 sign-flip toggle. See module docstring above.
# True = corrected (PENALIZE adverse-selection tape); False = legacy
# (REWARD adverse-selection tape; the IC -0.152 broken behavior).
INVERT_PER_B031 = True


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
        # 2026-05-22 pt8 (B-031 inversion): top-of-book DOM stacks that AGREE
        # with your trade direction are the canonical adverse-selection trap.
        # Visible liquidity on the side you're entering is precisely the
        # resting orders that informed sweep flow is about to lift you into.
        # Reward DOM OPPOSING (absorbers waiting); penalize DOM SUPPORTING.
        dom_bid_heavy = market.get("dom_bid_heavy", False)
        dom_ask_heavy = market.get("dom_ask_heavy", False)

        if direction == "LONG":
            dom_supports = dom_bid_heavy
            dom_opposes = dom_ask_heavy
        else:
            dom_supports = dom_ask_heavy
            dom_opposes = dom_bid_heavy

        if INVERT_PER_B031:
            if dom_supports:
                score -= 25
                issues.append("DOM supports direction (adverse-selection trap)")
            elif not dom_opposes:
                score -= 10
                issues.append("DOM neutral -- no absorption side identified")
        else:
            # Legacy (anti-edge) path — kept for A/B comparison.
            if not dom_supports:
                if dom_opposes:
                    score -= 25
                    issues.append(f"DOM opposing {direction} signal")
                else:
                    score -= 10
                    issues.append("DOM neutral -- no strong support for direction")

        # ── 3. Delta follow-through (CVD direction) ──────────────────
        # 2026-05-22 pt8 (B-031 inversion): CVD already pointing in your
        # trade direction = retail/momentum flow has *already moved* price;
        # you're entering on top of the move, not at the start of it.
        # Reward CVD divergence (delta pointing AGAINST you = informed
        # money quietly building a position behind retail noise).
        cvd = market.get("cvd", 0)
        bar_delta = market.get("bar_delta", 0)

        if direction == "LONG":
            delta_confirms = cvd > 0 or bar_delta > 0
        else:
            delta_confirms = cvd < 0 or bar_delta < 0

        if INVERT_PER_B031:
            if delta_confirms:
                score -= 20
                issues.append(
                    f"Delta confirming -- adverse-selection (retail chasing) "
                    f"CVD={cvd:.0f}, bar_delta={bar_delta:.0f}"
                )
        else:
            if not delta_confirms:
                score -= 20
                issues.append(
                    f"Delta not confirming: CVD={cvd:.0f}, bar_delta={bar_delta:.0f} "
                    f"vs {direction}"
                )

        # ── 4. Recent price direction (chase-entry detector) ─────────
        # 2026-05-22 pt8 (B-031 inversion): "price falling into LONG" is
        # the dip you wanted to buy, not a sign of weakness. Penalize the
        # opposite — price RISING into a LONG signal is a chase entry.
        prices = list(self._recent_prices)
        price_with_signal = True
        if len(prices) >= 3:
            last_3 = prices[-3:]
            if direction == "LONG":
                rising = all(last_3[i] < last_3[i + 1] for i in range(len(last_3) - 1))
                falling = all(last_3[i] > last_3[i + 1] for i in range(len(last_3) - 1))
            else:
                rising = all(last_3[i] > last_3[i + 1] for i in range(len(last_3) - 1))
                falling = all(last_3[i] < last_3[i + 1] for i in range(len(last_3) - 1))

            if INVERT_PER_B031:
                if rising:
                    score -= 15
                    issues.append(f"Price chasing into {direction} signal (last 3 ticks with-signal)")
                    price_with_signal = False
            else:
                if falling:
                    score -= 15
                    issues.append(f"Price moving against {direction} signal (last 3 ticks counter)")
                    price_with_signal = False

        # ── 5. DOM signal alignment ──────────────────────────────────
        # 2026-05-22 pt8 (B-031 inversion): dom_analyzer's iceberg /
        # absorption scores in your direction = same adverse-selection
        # pattern (informed money's visible footprint absorbing your side).
        dom_sig = market.get("dom_signal", {})
        dom_direction = dom_sig.get("direction")
        dom_strength = dom_sig.get("strength", 0)

        dom_aligned = False
        if INVERT_PER_B031:
            if dom_direction == direction and dom_strength > 20:
                dom_aligned = True
                score -= 15
                issues.append(
                    f"DOM signal aligns with {direction} (adverse-selection, "
                    f"strength={dom_strength:.0f})"
                )
            elif dom_direction and dom_direction != direction and dom_strength > 30:
                score = min(100, score + 5)
        else:
            if dom_direction == direction and dom_strength > 20:
                dom_aligned = True
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
