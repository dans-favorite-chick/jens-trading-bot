"""Phoenix market-state classifier (Phase 8, 2026-06-01).

Computes three per-5m-bar signals and a composite label describing the
current market microstructure regime. This module is OBSERVATIONAL only:
no strategy reads it as a gate in this phase. It exists so the Strategy
Oracle can split per-strategy performance by market state and so the
operator can later wire selective gating.

Signals (per 5m bar, 20-bar lookback):
    realized_vol     = ATR_5m / mean(ATR_5m over last 20 bars)
    trend_strength   = abs(EMA21 - EMA50) / ATR_5m
    choppiness_index = 100 * log10(sum(ATR_5m, 20) / (max_high_20 - min_low_20)) / log10(20)

Composite label priority (FIRST match wins):
    1. WHIPSAW_HIGH_VOL  : realized_vol > 1.5 AND choppiness_index > 50
    2. CHOPPY            : choppiness_index > 61.8 AND trend_strength < 0.2
    3. COMPRESSED        : realized_vol < 0.7
    4. TRENDING_HIGH_VOL : trend_strength > 0.5 AND realized_vol > 1.3
                          AND choppiness_index < 50
    5. TRENDING_NORMAL   : trend_strength > 0.3 AND 0.8 <= realized_vol <= 1.3
    6. NEUTRAL           : otherwise

Live wiring
-----------
The class accepts a tick_aggregator instance and reads ATR_5m + EMA21
from it. EMA50 is maintained internally (tick_aggregator does not
expose it and the project policy is "no refactoring tick_aggregator").

Backfill wiring
---------------
`tools/warehouse/backfill_market_state.py` constructs a tick_agg-less
MarketState via the `from_bar()` classmethod-style helpers, feeding
synthetic bars from the historical 5m CSV. ATR_5m for backfill is
maintained internally too (Wilder-smoothed 14-period TR, matching
tick_aggregator semantics).

Thread-safety
-------------
Not thread-safe. The bot writes from a single bar-close callback.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


# Composite-label priority thresholds (spec sec 8.1)
_RV_HIGH_WHIPSAW = 1.5
_CHOP_WHIPSAW = 50.0
_CHOP_CHOPPY = 61.8
_TS_CHOPPY = 0.2
_RV_COMPRESSED = 0.7
_TS_TRENDING_HIGH_VOL = 0.5
_RV_TRENDING_HIGH_VOL = 1.3
_CHOP_TRENDING_HIGH_VOL = 50.0
_TS_TRENDING_NORMAL = 0.3
_RV_TRENDING_NORMAL_LO = 0.8
_RV_TRENDING_NORMAL_HI = 1.3

LOOKBACK_BARS = 20
ATR_PERIOD = 14


def _classify(realized_vol: float, trend_strength: float,
              choppiness_index: float) -> str:
    """Pure label function. Priority order matters (FIRST match wins)."""
    # 1. Whipsaw — violent + chop
    if realized_vol > _RV_HIGH_WHIPSAW and choppiness_index > _CHOP_WHIPSAW:
        return "WHIPSAW_HIGH_VOL"
    # 2. Choppy — explicit chop with weak trend
    if choppiness_index > _CHOP_CHOPPY and trend_strength < _TS_CHOPPY:
        return "CHOPPY"
    # 3. Compressed — coiled
    if realized_vol < _RV_COMPRESSED:
        return "COMPRESSED"
    # 4. Trending with elevated vol
    if (
        trend_strength > _TS_TRENDING_HIGH_VOL
        and realized_vol > _RV_TRENDING_HIGH_VOL
        and choppiness_index < _CHOP_TRENDING_HIGH_VOL
    ):
        return "TRENDING_HIGH_VOL"
    # 5. Normal trend
    if (
        trend_strength > _TS_TRENDING_NORMAL
        and _RV_TRENDING_NORMAL_LO <= realized_vol <= _RV_TRENDING_NORMAL_HI
    ):
        return "TRENDING_NORMAL"
    return "NEUTRAL"


@dataclass
class _BarSnapshot:
    """Stored per-bar snapshot used by history()."""
    label: str
    realized_vol: float
    trend_strength: float
    choppiness_index: float
    computed_at: str


class MarketState:
    """Per-bar market-state classifier.

    Two construction modes:

    1. Live: pass a tick_aggregator instance. ATR_5m and EMA21 are read
       from it at each `on_bar_close()`; EMA50 is maintained internally.

    2. Backfill / synthetic: pass `tick_agg=None`. The caller must use
       `on_synthetic_bar(close, high, low)` and the class will maintain
       ATR_5m, EMA21, and EMA50 internally from the supplied bar stream.

    Both modes maintain the rolling 20-bar buffer of (ATR_5m, high, low)
    needed for choppiness_index, plus a rolling history of snapshots for
    `history(n_bars)`.
    """

    def __init__(self, tick_aggregator: Any = None,
                 history_capacity: int = 512) -> None:
        self.tick_agg = tick_aggregator

        # Rolling 20-bar buffer for choppiness / realized_vol denom.
        self._atr_buf: deque[float] = deque(maxlen=LOOKBACK_BARS)
        self._high_buf: deque[float] = deque(maxlen=LOOKBACK_BARS)
        self._low_buf: deque[float] = deque(maxlen=LOOKBACK_BARS)

        # Internal EMAs (always — EMA21 for backfill, EMA50 always since
        # tick_aggregator does not expose it).
        self._ema21: float = 0.0
        self._ema21_count: int = 0
        self._ema50: float = 0.0
        self._ema50_count: int = 0
        self._k21 = 2.0 / 22.0
        self._k50 = 2.0 / 51.0

        # Internal ATR (used in backfill mode; Wilder-smoothed 14-period).
        self._tr_history: deque[float] = deque(maxlen=ATR_PERIOD)
        self._atr_internal: float = 0.0
        self._prev_close: Optional[float] = None

        # Snapshot history.
        self._history: deque[_BarSnapshot] = deque(maxlen=history_capacity)

        # Last computed snapshot (for current()).
        self._last: Optional[_BarSnapshot] = None

    # ---- public API ------------------------------------------------------

    def current(self) -> dict[str, Any]:
        """Return the most recently computed snapshot, or a NEUTRAL stub
        if no bars have been ingested yet.
        """
        if self._last is None:
            return {
                "label": "NEUTRAL",
                "realized_vol": 0.0,
                "trend_strength": 0.0,
                "choppiness_index": 0.0,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }
        return self._snapshot_to_dict(self._last)

    def history(self, n_bars: int) -> list[dict[str, Any]]:
        """Return up to `n_bars` most-recent snapshots (newest last)."""
        if n_bars <= 0:
            return []
        snaps = list(self._history)[-n_bars:]
        return [self._snapshot_to_dict(s) for s in snaps]

    # ---- bar-ingest entry points -----------------------------------------

    def on_bar_close(self, close: float, high: float, low: float,
                     bar_ts: Optional[datetime] = None) -> dict[str, Any]:
        """Live ingest: pull ATR_5m + EMA21 from the tick_aggregator if
        present, otherwise maintain internally. Then compute label.

        Returns the snapshot dict for this bar.
        """
        if self.tick_agg is not None:
            atr_5m = float(self.tick_agg.atr.get("5m", 0.0)) \
                if hasattr(self.tick_agg, "atr") else 0.0
            ema21 = float(getattr(self.tick_agg, "ema21", 0.0))
            # Tick aggregator doesn't expose EMA50 — keep our own,
            # synced off the same close stream.
            self._update_ema50(close)
            # Track our own EMA21 too so that backfill and live paths
            # use the same EMA semantics if the tick_agg signal is
            # missing (warm-up).
            self._update_ema21(close)
            if ema21 == 0.0:
                ema21 = self._ema21
            if atr_5m == 0.0:
                self._update_atr_internal(close, high, low)
                atr_5m = self._atr_internal
        else:
            self._update_atr_internal(close, high, low)
            self._update_ema21(close)
            self._update_ema50(close)
            atr_5m = self._atr_internal
            ema21 = self._ema21

        return self._compute_and_store(
            atr_5m=atr_5m,
            ema21=ema21,
            ema50=self._ema50,
            high=high,
            low=low,
            bar_ts=bar_ts,
        )

    def on_synthetic_bar(self, close: float, high: float, low: float,
                         bar_ts: Optional[datetime] = None) -> dict[str, Any]:
        """Backfill-only ingest: always uses internal ATR/EMA21/EMA50."""
        self._update_atr_internal(close, high, low)
        self._update_ema21(close)
        self._update_ema50(close)
        return self._compute_and_store(
            atr_5m=self._atr_internal,
            ema21=self._ema21,
            ema50=self._ema50,
            high=high,
            low=low,
            bar_ts=bar_ts,
        )

    # ---- internals -------------------------------------------------------

    def _update_atr_internal(self, close: float, high: float,
                             low: float) -> None:
        """Wilder-smoothed 14-period TR, matching tick_aggregator."""
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._tr_history.append(tr)
        if len(self._tr_history) > 0:
            # Use simple mean of last <=14 TRs to match tick_aggregator
            # (which also uses a 14-deque sma — see tick_aggregator.py
            # line ~471: self.atr[k] = sum(self._tr_history[k]) / len(...)).
            self._atr_internal = sum(self._tr_history) / len(self._tr_history)
        self._prev_close = close

    def _update_ema21(self, close: float) -> None:
        self._ema21_count += 1
        if self._ema21_count <= 21:
            # SMA warm-up.
            self._ema21 = (
                (self._ema21 * (self._ema21_count - 1) + close)
                / self._ema21_count
            )
        else:
            self._ema21 = close * self._k21 + self._ema21 * (1 - self._k21)

    def _update_ema50(self, close: float) -> None:
        self._ema50_count += 1
        if self._ema50_count <= 50:
            self._ema50 = (
                (self._ema50 * (self._ema50_count - 1) + close)
                / self._ema50_count
            )
        else:
            self._ema50 = close * self._k50 + self._ema50 * (1 - self._k50)

    def _compute_and_store(self, atr_5m: float, ema21: float, ema50: float,
                           high: float, low: float,
                           bar_ts: Optional[datetime]) -> dict[str, Any]:
        # Push current bar into rolling buffers AFTER capturing it for
        # this bar's snapshot; that way realized_vol denom and choppy
        # numerator both include the current bar.
        self._atr_buf.append(float(atr_5m))
        self._high_buf.append(float(high))
        self._low_buf.append(float(low))

        # realized_vol
        if len(self._atr_buf) >= 2 and sum(self._atr_buf) > 0:
            mean_atr = sum(self._atr_buf) / len(self._atr_buf)
            realized_vol = atr_5m / mean_atr if mean_atr > 0 else 0.0
        else:
            realized_vol = 0.0

        # trend_strength
        if atr_5m > 0:
            trend_strength = abs(ema21 - ema50) / atr_5m
        else:
            trend_strength = 0.0

        # choppiness_index — needs >= 2 bars and range > 0
        n = len(self._atr_buf)
        if n >= 2:
            sum_atr = sum(self._atr_buf)
            range_high = max(self._high_buf)
            range_low = min(self._low_buf)
            rng = range_high - range_low
            if rng > 0 and sum_atr > 0:
                # Spec: log10(n_bars) — use actual buffer length so the
                # index is well-defined during the first 19 bars too.
                denom_log = math.log10(n) if n > 1 else 1.0
                if denom_log > 0:
                    choppiness_index = 100.0 * (
                        math.log10(sum_atr / rng) / denom_log
                    )
                else:
                    choppiness_index = 0.0
            else:
                choppiness_index = 0.0
        else:
            choppiness_index = 0.0

        label = _classify(realized_vol, trend_strength, choppiness_index)

        ts = (bar_ts or datetime.now(timezone.utc))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        snap = _BarSnapshot(
            label=label,
            realized_vol=float(realized_vol),
            trend_strength=float(trend_strength),
            choppiness_index=float(choppiness_index),
            computed_at=ts.isoformat(),
        )
        self._history.append(snap)
        self._last = snap
        return self._snapshot_to_dict(snap)

    @staticmethod
    def _snapshot_to_dict(snap: _BarSnapshot) -> dict[str, Any]:
        return {
            "label": snap.label,
            "realized_vol": snap.realized_vol,
            "trend_strength": snap.trend_strength,
            "choppiness_index": snap.choppiness_index,
            "computed_at": snap.computed_at,
        }


__all__ = ["MarketState", "LOOKBACK_BARS"]
