"""
Phoenix Bot — Anchored VWAP Manager

NEW CAPABILITY: Anchored VWAP (AVWAP) tracking with automatic anchor detection.

WHY AVWAP MATTERS:
Standard VWAP resets every day. Anchored VWAP starts from a SPECIFIC bar and
accumulates volume-weighted from that point forward, never resetting.

Brian Shannon (CMT, professional trader, "Maximum Trading Gains With Anchored
VWAP" 2023) considers AVWAP "the absolute truth of the relationship between
a stock's supply and demand, and is 100% objective."

The AVWAP from a major event (FOMC, earnings, gap) tells you the average
price at which all post-event traders are positioned. If price is ABOVE the
AVWAP, those traders are profitable → bullish. If BELOW, they're losing → bearish.
When price RETESTS the AVWAP, you get high-probability bounce/rejection points.

RESEARCH BASIS:
  - Brian Shannon's book + Alphatrends teaching (decade+ of validation)
  - Tradingsim AVWAP guides
  - Forex Tester / FTO professional research
  - Backtest evidence: Brian Shannon-inspired VWAP+RSI(2) strategy on SPY 1h
    Jan 2017-Nov 2025: PF 1.692 (very strong) over 9 years

ANCHOR EVENTS (auto-detected by this module):
  - Daily session open (always)
  - Yesterday's high (always)
  - Yesterday's low (always)
  - Highest volume bar of current session (continuously updated)
  - Recent gap > 0.5% from prior close
  - Recent volatility expansion bar (range > 2x ATR)
  - Manual anchor (e.g., FOMC at 1pm ET) — passed via add_manual_anchor()

USAGE:
    from core.anchored_vwap import AnchoredVWAPManager

    # Initialize at bot startup
    avwap_mgr = AnchoredVWAPManager()

    # On every tick or bar:
    avwap_mgr.update(bars_5m, current_price)

    # Get current AVWAP levels
    levels = avwap_mgr.get_active_avwaps()
    # → {"session_open": 22150.5, "yday_high": 22210.0, ...}

    # Add manual anchor (e.g., FOMC announcement at 1pm)
    avwap_mgr.add_manual_anchor(name="fomc_2pm", anchor_time=fomc_time)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Optional


@dataclass
class AVWAPState:
    """Tracks state for a single anchored VWAP."""
    name: str
    anchor_time: datetime
    cumulative_pv: float = 0.0
    cumulative_v: float = 0.0
    current_value: float = 0.0
    last_update_time: Optional[datetime] = None
    bar_count: int = 0


class AnchoredVWAPManager:
    """
    Manages multiple anchored VWAPs simultaneously.

    Anchors are auto-detected from price action (gaps, volume spikes,
    volatility expansion) and from time-based events (session open,
    yesterday's high/low). Manual anchors can also be added.

    Anchors are automatically retired after a configurable lifespan
    (default 5 days) so the list stays manageable.
    """

    DEFAULT_ANCHOR_LIFESPAN_DAYS = 5
    GAP_THRESHOLD_PCT = 0.5  # 0.5% gap triggers an anchor
    VOLATILITY_EXPANSION_ATR_MULT = 2.0
    VOLUME_SPIKE_MULT = 3.0  # 3x avg volume triggers an anchor

    def __init__(self, anchor_lifespan_days: int = DEFAULT_ANCHOR_LIFESPAN_DAYS):
        self.anchor_lifespan = timedelta(days=anchor_lifespan_days)
        self.avwaps: Dict[str, AVWAPState] = {}
        self._last_processed_bar_time: Optional[datetime] = None
        self._current_session_date: Optional[date] = None
        self._current_session_high: float = 0.0
        self._current_session_high_bar_time: Optional[datetime] = None
        self._current_session_low: float = float("inf")
        self._current_session_low_bar_time: Optional[datetime] = None
        self._current_session_high_vol: float = 0.0
        self._current_session_high_vol_bar_time: Optional[datetime] = None

    # ─── PUBLIC API ────────────────────────────────────────────────────

    def update(self, bars: List, current_price: float = None) -> None:
        """
        Update all AVWAPs with the latest bars.

        Auto-detects new anchors and retires expired ones.
        """
        if not bars:
            return

        # Detect anchors that should exist
        self._auto_detect_anchors(bars)

        # Retire expired anchors
        self._retire_expired_anchors()

        # Update each AVWAP with new bars since last update
        for state in self.avwaps.values():
            self._recalculate_avwap(state, bars)

    def add_manual_anchor(
        self,
        name: str,
        anchor_time: datetime,
    ) -> None:
        """Add a manually specified anchor (e.g., for FOMC events)."""
        if name in self.avwaps:
            return  # Already exists
        self.avwaps[name] = AVWAPState(name=name, anchor_time=anchor_time)

    def get_active_avwaps(self) -> Dict[str, float]:
        """Return current AVWAP values keyed by anchor name."""
        return {
            name: state.current_value
            for name, state in self.avwaps.items()
            if state.current_value > 0
        }

    def get_nearest_avwap(
        self,
        price: float,
        max_distance_pct: float = 1.0,
    ) -> Optional[tuple[str, float]]:
        """
        Return the AVWAP closest to current price, if within max_distance_pct.
        Useful for "is price testing an AVWAP right now?"
        """
        nearest = None
        nearest_distance = float("inf")
        for name, state in self.avwaps.items():
            if state.current_value <= 0:
                continue
            distance = abs(price - state.current_value)
            distance_pct = distance / price * 100
            if distance_pct > max_distance_pct:
                continue
            if distance < nearest_distance:
                nearest_distance = distance
                nearest = (name, state.current_value)
        return nearest

    def is_price_testing_avwap(
        self,
        price: float,
        atr: float,
        proximity_atr_mult: float = 0.3,
    ) -> Optional[tuple[str, float, str]]:
        """
        Check if current price is testing any AVWAP from above or below.

        Returns (avwap_name, avwap_value, "from_above" or "from_below") or None.
        """
        threshold = atr * proximity_atr_mult
        for name, state in self.avwaps.items():
            if state.current_value <= 0:
                continue
            if abs(price - state.current_value) > threshold:
                continue
            if price > state.current_value:
                return (name, state.current_value, "from_above")
            else:
                return (name, state.current_value, "from_below")
        return None

    # ─── ANCHOR AUTO-DETECTION ─────────────────────────────────────────

    def _auto_detect_anchors(self, bars: List) -> None:
        """Detect anchors that should exist based on bar data."""
        if not bars:
            return

        # Track session boundaries (using bar_time -> date)
        latest_bar = bars[-1]
        latest_bar_dt = self._bar_time_to_dt(latest_bar)
        if latest_bar_dt is None:
            return

        latest_date = latest_bar_dt.date()

        # Detect new session
        if self._current_session_date != latest_date:
            # New session — anchor session_open
            self._current_session_date = latest_date
            self._current_session_high = 0.0
            self._current_session_low = float("inf")
            self._current_session_high_vol = 0.0

            # Find first bar of this session
            for b in bars:
                b_dt = self._bar_time_to_dt(b)
                if b_dt is not None and b_dt.date() == latest_date:
                    self._add_anchor_if_new(
                        f"session_open_{latest_date}",
                        b_dt,
                    )
                    break

            # Anchor yesterday's high/low if we have prior session data
            yday = latest_date - timedelta(days=1)
            yday_high = 0.0
            yday_low = float("inf")
            yday_high_time = None
            yday_low_time = None
            for b in bars:
                b_dt = self._bar_time_to_dt(b)
                if b_dt is None or b_dt.date() != yday:
                    continue
                if b.high > yday_high:
                    yday_high = b.high
                    yday_high_time = b_dt
                if b.low < yday_low:
                    yday_low = b.low
                    yday_low_time = b_dt

            if yday_high_time:
                self._add_anchor_if_new(
                    f"yday_high_{yday}",
                    yday_high_time,
                )
            if yday_low_time:
                self._add_anchor_if_new(
                    f"yday_low_{yday}",
                    yday_low_time,
                )

        # Track current session highest-vol bar (for "session HVL" anchor)
        for b in bars[-10:]:  # Check recent bars only
            b_dt = self._bar_time_to_dt(b)
            if b_dt is None or b_dt.date() != latest_date:
                continue
            if b.volume > self._current_session_high_vol:
                self._current_session_high_vol = b.volume
                self._current_session_high_vol_bar_time = b_dt
                # Replace the anchor if this is now the highest-vol bar
                key = f"session_high_vol_{latest_date}"
                if key in self.avwaps:
                    del self.avwaps[key]
                self._add_anchor_if_new(key, b_dt)

        # Detect gap from prior close (>0.5%)
        if len(bars) >= 2:
            for i in range(max(1, len(bars) - 50), len(bars)):
                prev_close = bars[i - 1].close
                curr_open = bars[i].open
                gap_pct = abs(curr_open - prev_close) / prev_close * 100
                if gap_pct >= self.GAP_THRESHOLD_PCT:
                    b_dt = self._bar_time_to_dt(bars[i])
                    if b_dt is None:
                        continue
                    direction = "up" if curr_open > prev_close else "down"
                    key = f"gap_{direction}_{b_dt.strftime('%Y%m%d_%H%M')}"
                    self._add_anchor_if_new(key, b_dt)

        # Detect volatility expansion (range > 2x ATR)
        atr = self._calc_atr(bars, 14)
        if atr is not None and atr > 0:
            for i in range(max(0, len(bars) - 20), len(bars)):
                bar = bars[i]
                bar_range = bar.high - bar.low
                if bar_range > atr * self.VOLATILITY_EXPANSION_ATR_MULT:
                    b_dt = self._bar_time_to_dt(bar)
                    if b_dt is None:
                        continue
                    key = f"vol_expansion_{b_dt.strftime('%Y%m%d_%H%M')}"
                    self._add_anchor_if_new(key, b_dt)

    def _add_anchor_if_new(self, name: str, anchor_time: datetime) -> None:
        """Add anchor if not already present."""
        if name in self.avwaps:
            return
        self.avwaps[name] = AVWAPState(name=name, anchor_time=anchor_time)

    def _retire_expired_anchors(self) -> None:
        """Remove anchors older than the lifespan."""
        now = datetime.now(timezone.utc)
        expired = [
            name for name, state in self.avwaps.items()
            if (now - state.anchor_time) > self.anchor_lifespan
        ]
        for name in expired:
            del self.avwaps[name]

    # ─── AVWAP CALCULATION ─────────────────────────────────────────────

    def _recalculate_avwap(self, state: AVWAPState, bars: List) -> None:
        """Recalculate AVWAP value from anchor_time forward."""
        cumulative_pv = 0.0
        cumulative_v = 0.0
        bar_count = 0

        for b in bars:
            b_dt = self._bar_time_to_dt(b)
            if b_dt is None or b_dt < state.anchor_time:
                continue
            typical = (b.high + b.low + b.close) / 3
            cumulative_pv += typical * b.volume
            cumulative_v += b.volume
            bar_count += 1

        if cumulative_v > 0:
            state.current_value = cumulative_pv / cumulative_v
            state.cumulative_pv = cumulative_pv
            state.cumulative_v = cumulative_v
            state.bar_count = bar_count
            state.last_update_time = datetime.now(timezone.utc)

    # ─── HELPERS ───────────────────────────────────────────────────────

    def _bar_time_to_dt(self, bar) -> Optional[datetime]:
        """Convert bar's start_time (epoch) to UTC datetime."""
        ts = getattr(bar, "start_time", None)
        if ts is None or ts <= 0:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return None

    def _calc_atr(self, bars: List, period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        tr = []
        for i in range(1, len(bars)):
            c, p = bars[i], bars[i - 1]
            tr.append(max(
                c.high - c.low,
                abs(c.high - p.close),
                abs(c.low - p.close),
            ))
        return sum(tr[-period:]) / min(len(tr), period) if tr else None

    # ─── DASHBOARD INTEGRATION ─────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return state for dashboard display."""
        return {
            "active_anchors": len(self.avwaps),
            "anchors": [
                {
                    "name": state.name,
                    "anchor_time": state.anchor_time.isoformat(),
                    "current_value": round(state.current_value, 2),
                    "bars_since_anchor": state.bar_count,
                }
                for state in self.avwaps.values()
                if state.current_value > 0
            ],
        }
