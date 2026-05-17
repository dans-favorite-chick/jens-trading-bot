"""
Phoenix Bot — Liquidity Level Tracker + Sweep Detector
=======================================================

PHASE A foundation for the NQ Liquidity Sweep Reversal (LSR) strategy.

CORE IDEA
---------
A "liquidity level" is a price where retail stops cluster:
  - Prior Day High / Low (PDH/PDL) — RTH session yesterday
  - Premarket Session High / Low (PSH/PSL) — 03:00-08:30 CT today
  - Opening Range High / Low (ORH/ORL) — first 15 min of cash session
  - Recent swing high/low — local extremes ≥6 1-min bars old, unviolated

A "sweep" happens when price PIERCES a level then closes BACK INSIDE.
The classic stop-hunt pattern: institutions push price beyond the level
to trigger retail stops, absorb the liquidity, then reverse.

This module is a pure, stateless detector. State persistence is the
caller's responsibility (the strategy will save/restore via JSON).

USAGE
-----
    levels = LiquidityLevelTracker()
    levels.update_pdh_pdl(yesterday_bars_1m_rth)
    levels.update_psh_psl(today_premarket_bars_1m)
    levels.update_orh_orl(today_or_bars_1m)
    levels.refresh_swing_levels(recent_bars_1m)

    for level in levels.active_levels():
        sweep = detect_sweep(level, last_1m_bar)
        if sweep is not None:
            # handle the sweep event

DEPENDENCIES
------------
None. Pure stdlib. Takes Bar-like objects with .open/.high/.low/.close/
.volume/.start_time/.end_time attributes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Optional, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# Constants — anchored to NQ / MNQ cash session
# ────────────────────────────────────────────────────────────────────
TICK_SIZE = 0.25                       # MNQ tick size
_CT = ZoneInfo("America/Chicago")
_ET = ZoneInfo("America/New_York")

# Session boundaries in CT (NQ cash session = 8:30 CT - 15:00 CT)
RTH_OPEN_CT = dtime(8, 30)
RTH_CLOSE_CT = dtime(15, 0)
PREMARKET_OPEN_CT = dtime(3, 0)       # Standard premarket window for NQ
OR_DURATION_MINUTES = 15
OR_END_CT = dtime(8, 45)              # OR completes at 08:45 CT

# Sweep classification thresholds
DEFAULT_MIN_PENETRATION_TICKS = 2     # Wick must clear level by ≥2 ticks
DEFAULT_MIN_WICK_PCT = 0.50           # Rejection wick ≥50% of bar range
DEFAULT_MIN_BUFFER_TICKS = 1          # Close must be ≥1 tick back inside
DEFAULT_LEVEL_COOLOFF_MIN = 60        # Don't re-trade same level within 60 min

# Swing detection
DEFAULT_SWING_LOOKBACK_BARS = 6       # Swing must be ≥N bars old
DEFAULT_SWING_PEAK_WINDOW = 3         # A swing high is local max over ±N bars


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────
@dataclass
class LiquidityLevel:
    """A tracked liquidity level."""
    name: str                          # "PDH", "PDL", "PSH", "PSL", "ORH", "ORL", "SwingH_HHMM", "SwingL_HHMM"
    price: float                       # the level price
    side: str                          # "HIGH" or "LOW" (long sweep at low, short sweep at high)
    created_at: float                  # unix timestamp when level was set
    consumed_at: Optional[float] = None  # unix timestamp when last traded (cooloff)
    metadata: dict = field(default_factory=dict)


@dataclass
class SweepEvent:
    """A detected sweep — caller decides whether to trade it."""
    level: LiquidityLevel
    direction: str                     # "LONG" if low was swept (price reverses up), "SHORT" if high swept
    bar_high: float
    bar_low: float
    bar_close: float
    bar_open: float
    bar_volume: float
    wick_depth_ticks: int              # how far past the level the wick went
    wick_pct_of_range: float           # rejection wick size as % of bar range
    structural_stop_ticks: int         # ticks from close to wick extreme (proposed stop distance)
    bar_end_ts: float

    def __str__(self) -> str:
        return (
            f"SweepEvent({self.direction} {self.level.name}@{self.level.price:.2f} "
            f"wick={self.wick_depth_ticks}t pct={self.wick_pct_of_range:.0%} "
            f"stop={self.structural_stop_ticks}t)"
        )


# ────────────────────────────────────────────────────────────────────
# Tracker
# ────────────────────────────────────────────────────────────────────
class LiquidityLevelTracker:
    """Tracks active liquidity levels for the current session.

    State is in-memory only; the strategy owns persistence (JSON file).
    Call update_*() methods at the appropriate times during the session;
    call active_levels() to get levels to check for sweeps.
    """

    def __init__(self,
                 level_cooloff_minutes: int = DEFAULT_LEVEL_COOLOFF_MIN,
                 swing_lookback_bars: int = DEFAULT_SWING_LOOKBACK_BARS,
                 swing_peak_window: int = DEFAULT_SWING_PEAK_WINDOW):
        self._levels: dict[str, LiquidityLevel] = {}
        self._cooloff_seconds = level_cooloff_minutes * 60
        self._swing_lookback = swing_lookback_bars
        self._swing_peak_window = swing_peak_window

    # ── Public API ─────────────────────────────────────────────────
    def active_levels(self, now_ts: Optional[float] = None) -> list[LiquidityLevel]:
        """Return levels that aren't currently in cooloff."""
        now_ts = now_ts if now_ts is not None else _now_ts()
        return [
            lv for lv in self._levels.values()
            if lv.consumed_at is None
            or (now_ts - lv.consumed_at) > self._cooloff_seconds
        ]

    def mark_level_consumed(self, level_name: str, now_ts: Optional[float] = None) -> None:
        """Mark a level as 'just traded' — won't be returned for cooloff period."""
        if level_name in self._levels:
            self._levels[level_name].consumed_at = now_ts if now_ts is not None else _now_ts()

    def invalidate_level(self, level_name: str) -> None:
        """Remove a level entirely (e.g., after price closed cleanly past it)."""
        self._levels.pop(level_name, None)

    def get(self, level_name: str) -> Optional[LiquidityLevel]:
        return self._levels.get(level_name)

    def all_levels(self) -> dict[str, LiquidityLevel]:
        """Return ALL levels (including consumed ones) — diagnostic use."""
        return dict(self._levels)

    # ── Level updaters ─────────────────────────────────────────────
    def _set_or_preserve(self, name: str, price: float, side: str) -> None:
        """Set a named level, but preserve consumed_at if it already exists
        with the same price. This prevents losing cooloff state when
        update_pdh_pdl/etc. is called multiple times during a session."""
        existing = self._levels.get(name)
        ts = _now_ts()
        if existing is not None and abs(existing.price - price) < 1e-9:
            # Same price — preserve all state (consumed_at especially)
            return
        # Different price or new level — create. If we had a consumed_at,
        # keep it ONLY if the new price is very close (same level, refined)
        consumed_at = None
        if existing is not None and abs(existing.price - price) < 2 * TICK_SIZE:
            consumed_at = existing.consumed_at
        self._levels[name] = LiquidityLevel(
            name=name, price=price, side=side,
            created_at=ts, consumed_at=consumed_at,
        )

    def update_pdh_pdl(self, yesterday_rth_bars_1m: list) -> None:
        """Set PDH/PDL from yesterday's RTH session bars (08:30-15:00 CT).

        Caller must pre-filter bars to RTH only — this method trusts the input.
        Preserves consumed_at state if called multiple times.
        """
        if not yesterday_rth_bars_1m:
            logger.debug("[LiquidityLevels] PDH/PDL skipped: no bars provided")
            return
        pdh = max(b.high for b in yesterday_rth_bars_1m)
        pdl = min(b.low for b in yesterday_rth_bars_1m)
        self._set_or_preserve("PDH", pdh, "HIGH")
        self._set_or_preserve("PDL", pdl, "LOW")
        logger.info(f"[LiquidityLevels] PDH={pdh:.2f} PDL={pdl:.2f}")

    def update_psh_psl(self, today_premarket_bars_1m: list) -> None:
        """Set PSH/PSL from today's premarket session (03:00-08:30 CT)."""
        if not today_premarket_bars_1m:
            return
        psh = max(b.high for b in today_premarket_bars_1m)
        psl = min(b.low for b in today_premarket_bars_1m)
        self._set_or_preserve("PSH", psh, "HIGH")
        self._set_or_preserve("PSL", psl, "LOW")
        logger.info(f"[LiquidityLevels] PSH={psh:.2f} PSL={psl:.2f}")

    def update_orh_orl(self, today_or_bars_1m: list) -> None:
        """Set ORH/ORL from today's first 15 min of cash session (08:30-08:45 CT)."""
        if not today_or_bars_1m:
            return
        orh = max(b.high for b in today_or_bars_1m)
        orl = min(b.low for b in today_or_bars_1m)
        self._set_or_preserve("ORH", orh, "HIGH")
        self._set_or_preserve("ORL", orl, "LOW")
        logger.info(f"[LiquidityLevels] ORH={orh:.2f} ORL={orl:.2f}")

    def refresh_swing_levels(self, recent_bars_1m: list, current_price: float) -> None:
        """Recompute swing high/low levels from recent bars.

        A swing high at bar i: bar[i].high is greater than the highs of bars
        [i-N, i+N] (excluding bar i itself), where N = swing_peak_window.
        The swing must be at least swing_lookback_bars OLD (so we don't trade
        levels that just formed seconds ago and haven't been tested).

        Also auto-invalidates any old swing level that current_price has
        already passed (no point keeping a swing high if we're already 50t above it).
        """
        # First, prune old swings the current price has already cleared
        for name in list(self._levels.keys()):
            if not name.startswith("Swing"):
                continue
            lv = self._levels[name]
            if lv.side == "HIGH" and current_price > lv.price + 2 * TICK_SIZE:
                logger.debug(f"[LiquidityLevels] invalidate {name}: price past")
                del self._levels[name]
            elif lv.side == "LOW" and current_price < lv.price - 2 * TICK_SIZE:
                logger.debug(f"[LiquidityLevels] invalidate {name}: price past")
                del self._levels[name]

        if len(recent_bars_1m) < self._swing_lookback + 2 * self._swing_peak_window + 1:
            return

        # Look for swing highs and lows. A swing high is bar[i] such that
        # bar[i].high > all bars in [i-W, i+W] (excluding i). Same for lows.
        # We only consider swings where i + lookback_bars <= len-1 (so the
        # swing is at least lookback_bars OLD).
        W = self._swing_peak_window
        bars = recent_bars_1m
        n = len(bars)

        for i in range(W, n - W - self._swing_lookback):
            window_highs = [bars[j].high for j in range(i - W, i + W + 1) if j != i]
            window_lows = [bars[j].low for j in range(i - W, i + W + 1) if j != i]

            # FIX: detect plateau peaks. A bar is a swing high if its high
            # is >= ALL window neighbors AND strictly > at least one of
            # them. This way a plateau like [22018, 22020, 22020, 22020, 22018]
            # gets detected as a swing at the middle bar (or first plateau bar).
            bar_high = bars[i].high
            bar_low = bars[i].low
            if (all(bar_high >= h for h in window_highs)
                    and any(bar_high > h for h in window_highs)):
                name = self._swing_name(bars[i].end_time, "H")
                if name not in self._levels:
                    self._levels[name] = LiquidityLevel(
                        name=name,
                        price=bar_high,
                        side="HIGH",
                        created_at=float(bars[i].end_time),
                    )

            if (all(bar_low <= l for l in window_lows)
                    and any(bar_low < l for l in window_lows)):
                name = self._swing_name(bars[i].end_time, "L")
                if name not in self._levels:
                    self._levels[name] = LiquidityLevel(
                        name=name,
                        price=bar_low,
                        side="LOW",
                        created_at=float(bars[i].end_time),
                    )

    # ── Helpers ────────────────────────────────────────────────────
    @staticmethod
    def _swing_name(end_ts: float, kind: str) -> str:
        dt = datetime.fromtimestamp(end_ts, tz=_CT)
        return f"Swing{kind}_{dt.strftime('%H%M')}"

    # ── Serialization ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            name: {
                "name": lv.name,
                "price": lv.price,
                "side": lv.side,
                "created_at": lv.created_at,
                "consumed_at": lv.consumed_at,
            }
            for name, lv in self._levels.items()
        }

    @classmethod
    def from_dict(cls, data: dict, **kwargs) -> "LiquidityLevelTracker":
        t = cls(**kwargs)
        for name, d in data.items():
            t._levels[name] = LiquidityLevel(
                name=d["name"],
                price=float(d["price"]),
                side=d["side"],
                created_at=float(d["created_at"]),
                consumed_at=float(d["consumed_at"]) if d.get("consumed_at") is not None else None,
            )
        return t


# ────────────────────────────────────────────────────────────────────
# Sweep detector — pure function
# ────────────────────────────────────────────────────────────────────
def detect_sweep(level: LiquidityLevel,
                 bar,
                 *,
                 min_penetration_ticks: int = DEFAULT_MIN_PENETRATION_TICKS,
                 min_wick_pct: float = DEFAULT_MIN_WICK_PCT,
                 min_buffer_ticks: int = DEFAULT_MIN_BUFFER_TICKS,
                 tick_size: float = TICK_SIZE) -> Optional[SweepEvent]:
    """Check whether `bar` swept `level` and closed back inside.

    Long sweep (at a LOW level):
      bar.low < level.price - min_penetration_ticks*tick_size
      AND bar.close > level.price + min_buffer_ticks*tick_size
      AND (bar.close - bar.low) >= min_wick_pct * (bar.high - bar.low)

    Short sweep (at a HIGH level): mirror.

    Returns SweepEvent if conditions met, None otherwise.
    """
    bar_high = float(getattr(bar, "high"))
    bar_low = float(getattr(bar, "low"))
    bar_close = float(getattr(bar, "close"))
    bar_open = float(getattr(bar, "open", bar_close))
    bar_volume = float(getattr(bar, "volume", 0))
    # CRITICAL: reject corrupt OHLC (NaN/Inf would bypass downstream checks
    # because NaN comparisons all evaluate False)
    import math as _math
    if not all(_math.isfinite(x) for x in (bar_high, bar_low, bar_close)):
        return None
    if not _math.isfinite(level.price):
        return None
    bar_range = bar_high - bar_low
    if bar_range <= 0:
        return None

    bar_end_ts = float(getattr(bar, "end_time", _now_ts()))

    pen = min_penetration_ticks * tick_size
    buf = min_buffer_ticks * tick_size

    # LONG sweep: low pierced level downward, close back above
    if level.side == "LOW":
        if not (bar_low < level.price - pen):
            return None
        if not (bar_close > level.price + buf):
            return None
        lower_wick = bar_close - bar_low
        wick_pct = lower_wick / bar_range
        if wick_pct < min_wick_pct:
            return None
        wick_depth_ticks = int(round((level.price - bar_low) / tick_size))
        # Structural stop sits just below the wick extreme
        stop_distance = bar_close - bar_low + 2 * tick_size
        structural_stop_ticks = int(round(stop_distance / tick_size))
        return SweepEvent(
            level=level,
            direction="LONG",
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_open=bar_open,
            bar_volume=bar_volume,
            wick_depth_ticks=wick_depth_ticks,
            wick_pct_of_range=wick_pct,
            structural_stop_ticks=structural_stop_ticks,
            bar_end_ts=bar_end_ts,
        )

    # SHORT sweep: high pierced level upward, close back below
    if level.side == "HIGH":
        if not (bar_high > level.price + pen):
            return None
        if not (bar_close < level.price - buf):
            return None
        upper_wick = bar_high - bar_close
        wick_pct = upper_wick / bar_range
        if wick_pct < min_wick_pct:
            return None
        wick_depth_ticks = int(round((bar_high - level.price) / tick_size))
        stop_distance = bar_high - bar_close + 2 * tick_size
        structural_stop_ticks = int(round(stop_distance / tick_size))
        return SweepEvent(
            level=level,
            direction="SHORT",
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_open=bar_open,
            bar_volume=bar_volume,
            wick_depth_ticks=wick_depth_ticks,
            wick_pct_of_range=wick_pct,
            structural_stop_ticks=structural_stop_ticks,
            bar_end_ts=bar_end_ts,
        )

    return None


# ────────────────────────────────────────────────────────────────────
# Bar filtering helpers — for caller convenience
# ────────────────────────────────────────────────────────────────────
def filter_bars_rth(bars: list, date_ct: Optional[datetime] = None) -> list:
    """Filter bars to RTH session (08:30-15:00 CT) for given date.

    If date_ct is None, uses the date of the most recent bar.
    """
    if not bars:
        return []
    if date_ct is None:
        last = bars[-1]
        date_ct = datetime.fromtimestamp(float(last.end_time), tz=_CT)

    target_date = date_ct.date()
    out = []
    for b in bars:
        bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
        if bt.date() != target_date:
            continue
        if bt.time() < RTH_OPEN_CT or bt.time() >= RTH_CLOSE_CT:
            continue
        out.append(b)
    return out


def filter_bars_premarket(bars: list, date_ct: Optional[datetime] = None) -> list:
    """Filter bars to premarket session (03:00-08:30 CT)."""
    if not bars:
        return []
    if date_ct is None:
        last = bars[-1]
        date_ct = datetime.fromtimestamp(float(last.end_time), tz=_CT)
    target_date = date_ct.date()
    out = []
    for b in bars:
        bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
        if bt.date() != target_date:
            continue
        if bt.time() < PREMARKET_OPEN_CT or bt.time() >= RTH_OPEN_CT:
            continue
        out.append(b)
    return out


def filter_bars_or(bars: list, date_ct: Optional[datetime] = None) -> list:
    """Filter bars to today's Opening Range (08:30-08:45 CT)."""
    if not bars:
        return []
    if date_ct is None:
        last = bars[-1]
        date_ct = datetime.fromtimestamp(float(last.end_time), tz=_CT)
    target_date = date_ct.date()
    out = []
    for b in bars:
        bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
        if bt.date() != target_date:
            continue
        if bt.time() < RTH_OPEN_CT or bt.time() >= OR_END_CT:
            continue
        out.append(b)
    return out


def filter_bars_yesterday_rth(bars: list, today_ct: Optional[datetime] = None) -> list:
    """Filter bars to yesterday's RTH session.

    For a 24/5 bot the bars deque spans multiple days; this isolates
    yesterday's cash session for PDH/PDL computation.
    """
    if not bars:
        return []
    if today_ct is None:
        last = bars[-1]
        today_ct = datetime.fromtimestamp(float(last.end_time), tz=_CT)
    # Walk back to find "yesterday" as the most recent weekday before today
    yesterday_date = _previous_trading_day(today_ct.date())
    out = []
    for b in bars:
        bt = datetime.fromtimestamp(float(b.end_time), tz=_CT)
        if bt.date() != yesterday_date:
            continue
        if bt.time() < RTH_OPEN_CT or bt.time() >= RTH_CLOSE_CT:
            continue
        out.append(b)
    return out


def _previous_trading_day(today):
    """Return previous trading day (skips Sat/Sun)."""
    from datetime import timedelta
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def _now_ts() -> float:
    import time
    return time.time()
