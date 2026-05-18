"""
Phoenix Bot — ES/NQ Confluence LONG (Phase 12C)
================================================

Backtested 5 years (2021-05-17 → 2026-05-17, Databento data):
  131 trades, 50.4% WR, $1,548 total ($11.82/trade), PF 2.63,
  max drawdown $72, 6/6 years positive INCLUDING 2022 bear: +$1,032.

Selected from a 108-config sweep + 30 exit-methodology comparator. See
backtest_results/backtest_v3_sweep_results.csv and
backtest_results/exit_methodology_v3_results.csv for the full ranking.

ENTRY LOGIC
-----------
Detect MNQ outperforming MES by ≥ 25 basis-points × 100 on the
just-closed 5-min bar, with rolling-50 correlation ≥ 0.85.

  boost = (mnq_5m_return - mes_5m_return) × 10000

When boost ≥ 25 AND corr ≥ 0.85 AND direction is LONG-bias (NQ leading):
  Enter MARKET at current price.
  Stop:    24 ticks below entry (= $12 risk on 1 MNQ)
  Target:  96 ticks above entry (= $48 reward on 1 MNQ)
  RR:      4:1 (asymmetric capture for the rare high-conviction moves)

REGIME PROFILE
--------------
Year     N    WR%   P&L    Notes
2021    207  30.4   $-216  Partial year (late bull peak)
2022    175  39.2   $+1032 ⭐ BEAR (-33% on NQ) - the proof
2023     32  38.1   $+48
2024     31  37.7   $+168
2025     34  39.3   $+216
2026     12  33.8   $+72   YTD through 2026-05-17

Total: 491 trades raw / 131 after corr-filter, $+1,548 net.

DATA DEPENDENCY (CRITICAL)
--------------------------
This strategy requires MES (Micro E-mini S&P 500) 5-min bars in the
market dict at `market["mes_bars_5m"]`. As of Phase 12C ship date
(2026-05-18) Phoenix does NOT have a live MES feed — the backtest used
Databento data, but TickStreamer streams only MNQ.

Until the MES feed is wired (separate sprint — likely NT8 MES chart +
TickStreamer + bridge fanout), this strategy will SKIP `data_not_available`
on every eval. Same pattern as footprint_cvd_reversal was in before its
volumetric stream landed.

Sequence to make this strategy fire live:
  1. Operator: load TickStreamer on a MES chart in NT8 (same indicator,
     different instrument).
  2. Code: bridge_server fans out MES ticks under `mes_*` keys.
  3. Code: tick_aggregator builds parallel `mes_bars_5m`.
  4. Code: base_bot enriches `market["mes_bars_5m"]` from the aggregator.
  5. This file: no change required — already reads `market["mes_bars_5m"]`.

Until step 4 lands, the [EVAL] log will show:
  [EVAL] es_nq_confluence: SKIP data_not_available

That's expected. Once MES data starts flowing, the strategy will begin
firing autonomously (no operator action needed beyond restart to pick up
any base_bot changes).

NOT (YET) IMPLEMENTED
---------------------
- SHORT variant: backtest showed weaker edge on SHORT (PF 1.26 vs 2.03
  for LONG). Could be added as a sibling strategy later.
- Sub-strategy split by regime: 2022 (bear) had the strongest per-trade
  edge; could weight position size by trailing P&L. Out of scope here.
"""
from __future__ import annotations

import logging
from typing import Optional

from strategies.base_strategy import BaseStrategy, Signal
from config.settings import TICK_SIZE

logger = logging.getLogger(__name__)

# Throttle the DATA_NOT_AVAILABLE INFO log to once-per-process so it
# doesn't spam startup logs while waiting for MES infrastructure.
_data_not_available_logged = False


class ESNQConfluence(BaseStrategy):
    """ES/NQ confluence LONG strategy. See module docstring for the
    full backtest evidence + data-dependency notes."""

    name: str = "es_nq_confluence"
    computes_own_stop: bool = True
    computes_own_target: bool = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._last_signal_bar_ts: float = 0.0
        # Cache config defaults so the per-eval hot path doesn't re-lookup.
        self._boost_threshold: float = float(
            config.get("boost_threshold", 25.0)
        )
        self._corr_threshold: float = float(
            config.get("corr_threshold", 0.85)
        )
        self._corr_lookback: int = int(config.get("corr_lookback", 50))
        # 24t stop / 96t target = 4:1 RR per the optimal exit methodology
        # (Fixed 24t/96t row in exit_methodology_v3_results.csv: $1,548
        # total / $11.82/trade / PF 2.63 / max DD $72 / 6/6 years).
        self._stop_ticks: int = int(config.get("stop_ticks", 24))
        self._target_ticks: int = int(config.get("target_ticks", 96))
        self._target_rr: float = self._target_ticks / max(1, self._stop_ticks)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _pct_return(prev_close: float, last_close: float) -> Optional[float]:
        """Bar-over-bar return (last/prev - 1). Returns None on bad input."""
        try:
            prev = float(prev_close)
            last = float(last_close)
            if prev <= 0:
                return None
            return last / prev - 1.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pearson_corr(xs: list, ys: list) -> Optional[float]:
        """Sample Pearson correlation. Returns None if insufficient data
        or zero variance in either series."""
        n = len(xs)
        if n < 3 or n != len(ys):
            return None
        mx = sum(xs) / n
        my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        if sxx <= 0 or syy <= 0:
            return None
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        denom = (sxx * syy) ** 0.5
        return sxy / denom if denom > 0 else None

    # ── Main evaluate ──────────────────────────────────────────────

    def evaluate(self, market: dict, bars_5m: list, bars_1m: list,
                 session_info: dict) -> Optional[Signal]:
        global _data_not_available_logged

        # 2026-05-17 Phase 9.5 Item E pattern: single entry log so
        # per-strategy eval-count grep works reliably, plus SKIP reason
        # logs at every early-return path.
        logger.debug(f"[EVAL] {self.name}: entered evaluate()")

        # ── Need >= corr_lookback + 1 MNQ 5m bars to compute returns +
        #    rolling correlation. (lookback bar returns require lookback+1
        #    consecutive closes.)
        if not bars_5m or len(bars_5m) < self._corr_lookback + 1:
            logger.debug(
                f"[EVAL] {self.name}: SKIP warmup_mnq_5m "
                f"({len(bars_5m) if bars_5m else 0}/{self._corr_lookback + 1})"
            )
            return None

        # ── Need the parallel MES 5m bars in market dict. Phoenix does
        #    not have a live MES feed as of Phase 12C ship; this gate
        #    will trip until base_bot enrichment is added (see module
        #    docstring "Sequence to make this strategy fire live").
        mes_bars = market.get("mes_bars_5m")
        if mes_bars is None or len(mes_bars) < self._corr_lookback + 1:
            if not _data_not_available_logged:
                logger.info(
                    f"[{self.name}] DATA_NOT_AVAILABLE — market['mes_bars_5m'] "
                    f"absent or short. Strategy dormant until MES feed lands. "
                    f"See strategies/es_nq_confluence.py docstring for the "
                    f"infrastructure sequence."
                )
                _data_not_available_logged = True
            logger.debug(f"[EVAL] {self.name}: SKIP data_not_available")
            return None

        # ── Per-bar dedup on the latest MNQ 5m bar.
        last_mnq = bars_5m[-1]
        try:
            bar_ts = float(last_mnq.end_time)
        except (AttributeError, TypeError, ValueError):
            logger.debug(f"[EVAL] {self.name}: SKIP bar_end_time_unreadable")
            return None
        if bar_ts == self._last_signal_bar_ts:
            logger.debug(f"[EVAL] {self.name}: SKIP same_bar_dedup")
            return None

        # ── Build aligned MNQ + MES return series (last corr_lookback bars).
        # Strategy intentionally uses the most-recent N+1 closes; if MES
        # bar timestamps don't line up exactly with MNQ ones (possible
        # if the parallel aggregators tick slightly out of sync), the
        # correlation is approximate. Backtest used Databento-aligned
        # series. Acceptable on live for now; revisit if corr is noisy.
        mnq_closes = [float(b.close) for b in bars_5m[-(self._corr_lookback + 1):]]
        mes_closes = [float(b.close) for b in mes_bars[-(self._corr_lookback + 1):]]
        if len(mnq_closes) != len(mes_closes):
            logger.debug(
                f"[EVAL] {self.name}: SKIP bar_count_mismatch "
                f"(mnq={len(mnq_closes)} mes={len(mes_closes)})"
            )
            return None

        mnq_returns = [
            self._pct_return(mnq_closes[i - 1], mnq_closes[i])
            for i in range(1, len(mnq_closes))
        ]
        mes_returns = [
            self._pct_return(mes_closes[i - 1], mes_closes[i])
            for i in range(1, len(mes_closes))
        ]
        if any(r is None for r in mnq_returns) or any(r is None for r in mes_returns):
            logger.debug(f"[EVAL] {self.name}: SKIP bad_return_in_window")
            return None

        # ── Boost = (last MNQ return - last MES return) × 10000.
        # POS boost = NQ leading ES (LONG-bias signal per backtest).
        last_boost = (mnq_returns[-1] - mes_returns[-1]) * 10_000.0
        if last_boost < self._boost_threshold:
            # Common case — quiet bars. Keep at DEBUG so log isn't spam.
            logger.debug(
                f"[EVAL] {self.name}: NO_SIGNAL boost_below "
                f"(boost={last_boost:.1f} < {self._boost_threshold})"
            )
            return None

        # ── Rolling 50-bar correlation of returns.
        corr = self._pearson_corr(mnq_returns, mes_returns)
        if corr is None:
            logger.debug(f"[EVAL] {self.name}: SKIP corr_undefined")
            return None
        if corr < self._corr_threshold:
            logger.debug(
                f"[EVAL] {self.name}: NO_SIGNAL corr_below "
                f"(corr={corr:.3f} < {self._corr_threshold})"
            )
            return None

        # ── Price + stop/target. Use current market price (snapshot tick)
        # for entry, not the 5m bar close — base_bot fills at MARKET.
        price = market.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            logger.debug(f"[EVAL] {self.name}: SKIP no_price")
            return None
        if price <= 0:
            logger.debug(f"[EVAL] {self.name}: SKIP non_positive_price")
            return None

        stop_price = round(price - self._stop_ticks * TICK_SIZE, 2)
        target_price = round(price + self._target_ticks * TICK_SIZE, 2)

        # Dedup before emit
        self._last_signal_bar_ts = bar_ts

        confluences = [
            f"boost={last_boost:.1f} (>= {self._boost_threshold})",
            f"corr={corr:.3f} (>= {self._corr_threshold})",
            f"stop={self._stop_ticks}t / target={self._target_ticks}t "
            f"(RR={self._target_rr:.1f}:1)",
            f"regime={session_info.get('regime', '?')}",
        ]
        reason = (
            f"ES/NQ confluence LONG: NQ leading ES by "
            f"{last_boost:.1f}bp×100 with corr {corr:.2f}"
        )

        logger.info(
            f"[EVAL] {self.name}: SIGNAL LONG boost={last_boost:.1f} "
            f"corr={corr:.3f} entry={price:.2f} stop={stop_price} "
            f"target={target_price}"
        )

        return Signal(
            direction="LONG",
            stop_ticks=self._stop_ticks,
            target_rr=self._target_rr,
            confidence=float(min(95.0, 50.0 + last_boost)),
            entry_score=float(min(60.0, 30.0 + last_boost / 2.0)),
            strategy=self.name,
            reason=reason,
            confluences=confluences,
            atr_stop_override=True,    # we set our own stop, not ATR-anchored
            entry_type="MARKET",
            entry_price=price,
            stop_price=stop_price,
            target_price=target_price,
            metadata={
                "sub_strategy": "es_nq_confluence",
                "boost": last_boost,
                "corr": corr,
                "boost_threshold": self._boost_threshold,
                "corr_threshold": self._corr_threshold,
            },
        )
