"""Big-Move Signal — direct entry on a score-90+ pre-move setup.

Built from the live validation on 2026-05-15 15:11 CT: the detector
fired score=100 LONG and price ran +47pt in 8 minutes. The setup is
its own edge — no need to filter through another strategy's gates.

Entry logic:
  - Read `market["big_move_pre"]` enriched by base_bot's BigMoveDetector
  - Fire LONG when score >= 90 AND likely_direction == "LONG"
  - Fire SHORT when score >= 90 AND likely_direction == "SHORT"
  - Min hold + cooloff applied via the universal max_trades_per_day +
    daily-loss gates

Stop placement:
  - Tight ATR-anchored (1.0× ATR_5m wick anchor) → keeps stop in $50 budget
  - The actual budget-skip gate in base_bot enforces $50 hard cap

Exit:
  - Primary: BigMoveDetector exhaustion score >= 70 → big_move_exhaustion
  - Secondary: stall detector + ema_dom_exit + chandelier_trail
  - Hard stop fires only as disaster anchor

Sizing:
  - 1 contract (the only feasible size on MNQ)
  - Risk reference = actual stop distance (NOT a fictional risk-reference;
    matches the operator's $50/trade constraint exactly)
"""
from __future__ import annotations

import logging
from typing import Optional

from strategies.base_strategy import BaseStrategy, Signal
from config.settings import TICK_SIZE

logger = logging.getLogger(__name__)


class BigMoveSignal(BaseStrategy):
    """Standalone entry on the BigMoveDetector score >= 90 signature.

    The 4 stage-1 flags (vol_collapse + cvd_divergence + failed_break
    + dom_absorption) firing simultaneously is a high-conviction
    setup. Today's 15:11:19 score=100 LONG correctly predicted a +47pt
    move in 8 minutes (validation evidence — see
    docs/exit_methodology_per_strategy.md, "Big-Move signature").
    """

    name = "big_move_signal"
    # Standard bracket strategy — uses computed stop + target, not
    # managed exit. The exhaustion-exit fires via the universal
    # position-loop path (no special flag needed).

    def __init__(self, config: dict):
        super().__init__(config)
        self._last_signal_bar_ts: float = 0
        self.is_prod_bot: bool = bool(config.get("is_prod_bot", False))

    def evaluate(
        self, market: dict, bars_5m: list, bars_1m: list,
        session_info: dict,
    ) -> Optional[Signal]:
        # 2026-05-17 Phase 9.5 Item E: per-evaluate observability.
        # Single entry log so eval-count grep works reliably, plus SKIP
        # reason logs on every early-return path. Replaces the prior
        # silent-return behavior that made this strategy invisible in
        # Phase 9 per-strategy eval-count breakdown.
        logger.debug(f"[EVAL] {self.name}: entered evaluate()")

        # Read the pre-move assessment enriched by base_bot earlier in
        # the eval cycle.
        pre = market.get("big_move_pre") or {}
        score = int(pre.get("score", 0) or 0)
        direction = str(pre.get("likely_direction", "UNKNOWN"))
        flags = list(pre.get("flags") or [])

        # Threshold: only fire on score >= 90 (3 of 4 flags PLUS the
        # 4th — i.e., the most-rare full-signature setup). 75 is
        # tradable but adds noise; 90 keeps signal quality high.
        min_score = int(self.config.get("min_score", 90))
        if score < min_score:
            logger.debug(
                f"[EVAL] {self.name}: SKIP score_below_threshold "
                f"({score} < {min_score})"
            )
            return None

        if direction not in ("LONG", "SHORT"):
            logger.debug(f"[EVAL] {self.name}: NO_SIGNAL undefined direction")
            return None

        # Per-bar dedup: only fire once per most-recent 1m bar.
        if not bars_1m:
            logger.debug(f"[EVAL] {self.name}: SKIP no_bars")
            return None
        last_bar = bars_1m[-1]
        try:
            bar_ts = float(last_bar.end_time)
        except (AttributeError, TypeError, ValueError):
            logger.debug(f"[EVAL] {self.name}: SKIP bar_end_time_unreadable")
            return None
        if bar_ts == self._last_signal_bar_ts:
            # Already fired on this bar
            logger.debug(f"[EVAL] {self.name}: SKIP same_bar_dedup")
            return None
        self._last_signal_bar_ts = bar_ts

        # Stop placement: tight ATR-anchored. Use 1.0× ATR_5m (not 2.0×
        # like trend-followers) — this strategy enters at exhaustion,
        # so the structural stop is the most-recent swing extreme not
        # a wider noise band. Clamped to keep within $50 budget.
        price = float(market.get("price", 0) or 0)
        if price <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP no_price")
            return None
        atr_5m = float(market.get("atr_5m", 0) or 0)
        # Stop distance = 1.0 × ATR_5m, clamped to budget
        stop_atr_mult = float(self.config.get("stop_atr_mult", 1.0))
        stop_distance_pts = max(5.0, atr_5m * stop_atr_mult) if atr_5m > 0 else 20.0
        # Cap at 100 ticks = 25 points = $50 on MNQ (1 contract)
        max_stop_ticks = int(self.config.get("max_stop_ticks", 100))
        stop_distance_pts = min(stop_distance_pts, max_stop_ticks * TICK_SIZE)
        stop_ticks = int(stop_distance_pts / TICK_SIZE)
        # Stop price
        if direction == "LONG":
            stop_price = round(price - stop_distance_pts, 2)
        else:
            stop_price = round(price + stop_distance_pts, 2)

        # Target: 2.0× RR by default. The exhaustion-exit will likely
        # fire BEFORE the target on most trades — target is the
        # disaster-good-fortune anchor.
        target_rr = float(self.config.get("target_rr", 2.0))

        confluences = [
            f"big_move_pre score={score}/100",
            f"flags=[{', '.join(flags)}]",
            f"ATR_5m={atr_5m:.1f}pt, stop={stop_ticks}t",
            f"regime={session_info.get('regime', '?')}",
        ]

        logger.info(
            f"[EVAL] {self.name}: SIGNAL {direction} score={score} "
            f"flags={flags} entry={price:.2f} stop_ticks={stop_ticks}"
        )

        return Signal(
            direction=direction,
            stop_ticks=stop_ticks,
            target_rr=target_rr,
            confidence=float(score),  # Pass the score through as confidence
            entry_score=min(60.0, score * 0.6),
            strategy=self.name,
            reason=(
                f"big_move_pre {direction} setup — score={score}/100, "
                f"flags={'+'.join(flags)}"
            ),
            confluences=confluences,
            atr_stop_override=True,    # We computed stop_ticks, don't re-derive
            entry_type="MARKET",
            entry_price=price,
            stop_price=stop_price,
            metadata={
                "big_move_pre_score": score,
                "big_move_pre_flags": flags,
            },
        )
