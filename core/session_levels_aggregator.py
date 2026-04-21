"""
Phoenix Bot — Session Levels Aggregator (Phase 4A)

Computes and tracks all prior-day + live opening-session levels that
feed the Opening Session strategy family. Pure data layer — not yet
wired into tick_aggregator (Phase 4B will integrate).

Responsibilities
================
1. Prior-day data (computed once at startup, refreshed daily at 00:01 CT):
   - OHLC from the prior day's JSONL history (5-min bars)
   - Volume profile POC/VAH (upper 70% value area) / VAL (lower)
   - Standard floor-trader pivot points (PP, R1, R2, S1, S2)
   - Prior-day avg 5-min volume (fed to opening-type classifier)

2. Premarket tracking (7:00 – 8:30 CT, 1-min bars):
   - pmh/pml rolling high/low; frozen at 8:30 CT

3. RTH opening tracking (8:30 – 9:30 CT):
   - rth_open_price (first 1m bar open at 8:30)
   - rth_5min_high/low/close/volume (first 5m bar, 8:30-8:35)
   - rth_15min_high/low (8:30-8:45)
   - rth_60min_high/low (8:30-9:30 — the IB)

4. Opening-type classification (at 8:35 CT):
   - Invokes core.session_levels.classify_opening_type
   - Result cached on the aggregator

5. Auction-Out 8:45 check (opening_holds_outside_at_845):
   - At 8:45 CT, bool: did price stay outside prior-day range, or return?

6. ORB first-break tracking (from 8:45 CT onward):
   - Locks in "LONG" or "SHORT" the first time a 5-min close breaks the
     15-min opening range. Persists for the session.

Error handling
==============
All fields default to None. Missing JSONL, parse errors, or insufficient
data log warnings and leave fields None rather than raising.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

from config.settings import TICK_SIZE
from core.session_levels import calc_pivot_points, classify_opening_type

logger = logging.getLogger("SessionLevelsAggregator")

_PREMARKET_START = dtime(7, 0)
_RTH_OPEN = dtime(8, 30)
_RTH_5M_COMPLETE = dtime(8, 35)
_RTH_15M_COMPLETE = dtime(8, 45)
_RTH_60M_COMPLETE = dtime(9, 30)
_VALUE_AREA_FRACTION = 0.70
_MIN_BARS_FOR_VA = 10


@dataclass
class _BarLike:
    """Minimal bar protocol used by update() — accepts anything with these attrs."""
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class SessionLevelsAggregator:
    """Single-instance per bot. See module docstring."""

    def __init__(self, bot_name: str, history_dir: str | Path = "logs/history"):
        self.bot_name = bot_name
        self.history_dir = Path(history_dir)

        # Prior-day fields
        self.prior_day_open: Optional[float] = None
        self.prior_day_high: Optional[float] = None
        self.prior_day_low: Optional[float] = None
        self.prior_day_close: Optional[float] = None
        self.prior_day_poc: Optional[float] = None
        self.prior_day_vah: Optional[float] = None
        self.prior_day_val: Optional[float] = None
        self.pivot_pp: Optional[float] = None
        self.pivot_r1: Optional[float] = None
        self.pivot_r2: Optional[float] = None
        self.pivot_s1: Optional[float] = None
        self.pivot_s2: Optional[float] = None
        self._avg_5min_volume: Optional[float] = None

        self._prior_day_loaded_for: Optional[date] = None

        # Live state
        self._current_date: Optional[date] = None
        self._last_now_ct: Optional[datetime] = None
        self._init_live_state()

    def _init_live_state(self) -> None:
        # Premarket
        self.pmh: Optional[float] = None
        self.pml: Optional[float] = None
        self._premarket_frozen: bool = False

        # RTH
        self.rth_open_price: Optional[float] = None
        self.rth_5min_high: Optional[float] = None
        self.rth_5min_low: Optional[float] = None
        self.rth_5min_close: Optional[float] = None
        self._rth_5min_volume: Optional[float] = None
        self.rth_15min_high: Optional[float] = None
        self.rth_15min_low: Optional[float] = None
        self.rth_60min_high: Optional[float] = None
        self.rth_60min_low: Optional[float] = None
        self._rth_5min_captured: bool = False

        # Opening type + auction-out
        self.opening_type: Optional[str] = None
        self.opening_holds_outside_at_845: Optional[bool] = None
        self._auction_out_set: bool = False

        # ORB break
        self.orb_first_break_direction: Optional[str] = None

    # ═══════════════════════════════════════════════════════════════
    # Prior-day computation
    # ═══════════════════════════════════════════════════════════════
    def load_prior_day(self, target_date: Optional[date] = None) -> None:
        """
        Find the most recent trading day strictly before target_date whose
        JSONL history file exists; compute prior-day OHLC, volume profile,
        and pivots from its 5-min bars.
        """
        today = target_date if target_date else datetime.now().date()
        fname: Optional[Path] = None
        found_date: Optional[date] = None

        for days_back in range(1, 11):
            candidate_date = today - timedelta(days=days_back)
            candidate = self.history_dir / f"{candidate_date.isoformat()}_{self.bot_name}.jsonl"
            if candidate.exists():
                fname = candidate
                found_date = candidate_date
                break

        if fname is None or found_date is None:
            logger.warning(
                "[SESSION_LEVELS] no prior-day JSONL found within 10 days of %s",
                today,
            )
            self._prior_day_loaded_for = today
            return

        self._compute_prior_day(fname)
        self._prior_day_loaded_for = today
        logger.info(
            "[SESSION_LEVELS] loaded prior day %s from %s (O=%s H=%s L=%s C=%s)",
            found_date, fname.name,
            self.prior_day_open, self.prior_day_high,
            self.prior_day_low, self.prior_day_close,
        )

    def _compute_prior_day(self, fname: Path) -> None:
        bars_5m = self._read_5m_bars(fname)
        if not bars_5m:
            logger.warning("[SESSION_LEVELS] no 5m bars in %s", fname.name)
            return

        self.prior_day_open = float(bars_5m[0].get("open"))
        self.prior_day_close = float(bars_5m[-1].get("close"))
        self.prior_day_high = max(float(b.get("high", 0.0)) for b in bars_5m)
        self.prior_day_low = min(float(b.get("low", float("inf"))) for b in bars_5m)

        # Avg 5-min volume — feeds OPEN_DRIVE classifier volume check.
        vols = [float(b.get("volume", 0) or 0) for b in bars_5m]
        if vols:
            self._avg_5min_volume = sum(vols) / len(vols)

        # Volume profile — needs at least 10 bars to be meaningful.
        if len(bars_5m) >= _MIN_BARS_FOR_VA:
            poc, vah, val = self._compute_volume_profile(bars_5m)
            self.prior_day_poc = poc
            self.prior_day_vah = vah
            self.prior_day_val = val

        # Pivots.
        if all(v is not None for v in (
            self.prior_day_high, self.prior_day_low, self.prior_day_close,
        )):
            piv = calc_pivot_points(
                self.prior_day_high, self.prior_day_low, self.prior_day_close,
            )
            self.pivot_pp = piv["pp"]
            self.pivot_r1 = piv["r1"]
            self.pivot_r2 = piv["r2"]
            self.pivot_s1 = piv["s1"]
            self.pivot_s2 = piv["s2"]

    def _read_5m_bars(self, fname: Path) -> list[dict]:
        bars: list[dict] = []
        try:
            with open(fname, "r", encoding="utf-8") as f:
                for line_no, raw in enumerate(f, 1):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "[SESSION_LEVELS] skip corrupt line %d in %s: %s",
                            line_no, fname.name, e,
                        )
                        continue
                    if obj.get("event") != "bar":
                        continue
                    if obj.get("timeframe") != "5m":
                        continue
                    bars.append(obj)
        except OSError as e:
            logger.warning("[SESSION_LEVELS] failed to read %s: %s", fname, e)
        return bars

    def _compute_volume_profile(
        self, bars_5m: list[dict]
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """
        TPO-style approximation: each bar's volume distributed uniformly
        across its high-low range in 1-tick bins, then the POC is the bin
        with maximum total volume; VAH/VAL are the extremes of the 70%
        value area expanded greedily from POC.
        """
        volumes_by_tick: dict[int, float] = {}
        for b in bars_5m:
            h = b.get("high")
            l = b.get("low")
            v = b.get("volume", 0) or 0
            if h is None or l is None or v <= 0 or h < l:
                continue
            n_ticks = int(round((float(h) - float(l)) / TICK_SIZE)) + 1
            if n_ticks <= 0:
                continue
            vol_per_tick = float(v) / n_ticks
            low_idx = int(round(float(l) / TICK_SIZE))
            for i in range(n_ticks):
                idx = low_idx + i
                volumes_by_tick[idx] = volumes_by_tick.get(idx, 0.0) + vol_per_tick

        if not volumes_by_tick:
            return (None, None, None)

        total_volume = sum(volumes_by_tick.values())
        if total_volume <= 0:
            return (None, None, None)

        poc_idx = max(volumes_by_tick, key=lambda k: volumes_by_tick[k])
        poc_price = poc_idx * TICK_SIZE

        target = total_volume * _VALUE_AREA_FRACTION
        current_sum = volumes_by_tick[poc_idx]
        lo_idx = hi_idx = poc_idx
        while current_sum < target:
            above = volumes_by_tick.get(hi_idx + 1, 0.0)
            below = volumes_by_tick.get(lo_idx - 1, 0.0)
            if above == 0 and below == 0:
                break
            if above >= below:
                hi_idx += 1
                current_sum += above
            else:
                lo_idx -= 1
                current_sum += below

        vah = hi_idx * TICK_SIZE
        val = lo_idx * TICK_SIZE
        return (poc_price, vah, val)

    # ═══════════════════════════════════════════════════════════════
    # Live update
    # ═══════════════════════════════════════════════════════════════
    def update(
        self,
        now_ct: datetime,
        bar_1m: Any = None,
        bar_5m: Any = None,
    ) -> None:
        """
        Called by tick_aggregator on each bar close. `bar_1m` and `bar_5m`
        may be a Bar instance (duck-typed: open/high/low/close/volume),
        None, or both — caller passes whichever just completed.
        """
        self._maybe_daily_reset(now_ct)
        self._last_now_ct = now_ct

        self._update_premarket(now_ct, bar_1m)
        self._update_rth_open(now_ct, bar_1m)
        self._update_rth_windows(now_ct, bar_1m, bar_5m)
        self._update_opening_type_at_835(now_ct)
        self._update_auction_out_at_845(now_ct, bar_1m)
        self._update_orb_break(now_ct, bar_5m)

    def _maybe_daily_reset(self, now_ct: datetime) -> None:
        today = now_ct.date()
        if self._current_date is None:
            self._current_date = today
            return
        if today != self._current_date:
            self._current_date = today
            self._init_live_state()

    def _update_premarket(self, now_ct: datetime, bar_1m: Any) -> None:
        if bar_1m is None:
            return
        t = now_ct.time()
        # Freeze at 08:30 regardless of whether a bar arrives.
        if t >= _RTH_OPEN:
            self._premarket_frozen = True
            return
        if t < _PREMARKET_START:
            return
        if self._premarket_frozen:
            return
        self.pmh = bar_1m.high if self.pmh is None else max(self.pmh, bar_1m.high)
        self.pml = bar_1m.low if self.pml is None else min(self.pml, bar_1m.low)

    def _update_rth_open(self, now_ct: datetime, bar_1m: Any) -> None:
        if bar_1m is None:
            return
        if self.rth_open_price is not None:
            return
        if now_ct.time() >= _RTH_OPEN and now_ct.time() < dtime(14, 30):
            self.rth_open_price = bar_1m.open

    def _update_rth_windows(
        self, now_ct: datetime, bar_1m: Any, bar_5m: Any,
    ) -> None:
        t = now_ct.time()

        # First 5m bar (8:30-8:35) — captured on the 5m bar whose close fires at 8:35.
        if bar_5m is not None and not self._rth_5min_captured \
                and _RTH_OPEN <= t <= _RTH_5M_COMPLETE:
            self.rth_5min_high = bar_5m.high
            self.rth_5min_low = bar_5m.low
            self.rth_5min_close = bar_5m.close
            self._rth_5min_volume = float(getattr(bar_5m, "volume", 0) or 0)
            self._rth_5min_captured = True

        # 15-min window accumulator (8:30-8:45) uses 1m bars.
        if bar_1m is not None and _RTH_OPEN <= t < _RTH_15M_COMPLETE:
            if self.rth_15min_high is None or bar_1m.high > self.rth_15min_high:
                self.rth_15min_high = bar_1m.high
            if self.rth_15min_low is None or bar_1m.low < self.rth_15min_low:
                self.rth_15min_low = bar_1m.low

        # 60-min window accumulator (8:30-9:30) uses 1m bars.
        if bar_1m is not None and _RTH_OPEN <= t < _RTH_60M_COMPLETE:
            if self.rth_60min_high is None or bar_1m.high > self.rth_60min_high:
                self.rth_60min_high = bar_1m.high
            if self.rth_60min_low is None or bar_1m.low < self.rth_60min_low:
                self.rth_60min_low = bar_1m.low

    def _update_opening_type_at_835(self, now_ct: datetime) -> None:
        if self.opening_type is not None:
            return
        if now_ct.time() < _RTH_5M_COMPLETE:
            return
        if not self._rth_5min_captured:
            return

        market = {
            "rth_open_price": self.rth_open_price,
            "rth_5min_high": self.rth_5min_high,
            "rth_5min_low": self.rth_5min_low,
            "rth_5min_close": self.rth_5min_close,
            "rth_5min_volume": self._rth_5min_volume,
            "avg_5min_volume": self._avg_5min_volume,
            "prior_day_vah": self.prior_day_vah,
            "prior_day_val": self.prior_day_val,
            "prior_day_high": self.prior_day_high,
            "prior_day_low": self.prior_day_low,
        }
        self.opening_type = classify_opening_type(market)

    def _update_auction_out_at_845(self, now_ct: datetime, bar_1m: Any) -> None:
        if self._auction_out_set:
            return
        if now_ct.time() < _RTH_15M_COMPLETE:
            return
        if bar_1m is None:
            return
        if self.prior_day_high is None or self.prior_day_low is None:
            return

        price = float(bar_1m.close)
        self.opening_holds_outside_at_845 = bool(
            price > self.prior_day_high or price < self.prior_day_low
        )
        self._auction_out_set = True

    def _update_orb_break(self, now_ct: datetime, bar_5m: Any) -> None:
        if self.orb_first_break_direction is not None:
            return
        if bar_5m is None:
            return
        if now_ct.time() < _RTH_15M_COMPLETE:
            return
        if self.rth_15min_high is None or self.rth_15min_low is None:
            return

        if bar_5m.close > self.rth_15min_high:
            self.orb_first_break_direction = "LONG"
        elif bar_5m.close < self.rth_15min_low:
            self.orb_first_break_direction = "SHORT"

    # ═══════════════════════════════════════════════════════════════
    # Public snapshot accessor
    # ═══════════════════════════════════════════════════════════════
    def get_levels_dict(self) -> dict:
        """Return all fields as a dict ready to merge into snapshot()."""
        return {
            "now_ct": self._last_now_ct or datetime.now(),
            # Prior day
            "prior_day_open": self.prior_day_open,
            "prior_day_high": self.prior_day_high,
            "prior_day_low": self.prior_day_low,
            "prior_day_close": self.prior_day_close,
            "prior_day_poc": self.prior_day_poc,
            "prior_day_vah": self.prior_day_vah,
            "prior_day_val": self.prior_day_val,
            # Pivots
            "pivot_pp": self.pivot_pp,
            "pivot_r1": self.pivot_r1,
            "pivot_r2": self.pivot_r2,
            "pivot_s1": self.pivot_s1,
            "pivot_s2": self.pivot_s2,
            # Premarket
            "pmh": self.pmh,
            "pml": self.pml,
            # RTH opening
            "rth_open_price": self.rth_open_price,
            "rth_5min_high": self.rth_5min_high,
            "rth_5min_low": self.rth_5min_low,
            "rth_5min_close": self.rth_5min_close,
            "rth_15min_high": self.rth_15min_high,
            "rth_15min_low": self.rth_15min_low,
            "rth_60min_high": self.rth_60min_high,
            "rth_60min_low": self.rth_60min_low,
            # Classification
            "opening_type": self.opening_type,
            "opening_holds_outside_at_845": self.opening_holds_outside_at_845,
            "orb_first_break_direction": self.orb_first_break_direction,
            # Volume context (enables OPEN_DRIVE classifier downstream)
            "avg_5min_volume": self._avg_5min_volume,
            "rth_5min_volume": self._rth_5min_volume,
        }
