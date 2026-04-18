"""
Phoenix Bot — Economic Calendar Auto-Risk

Automatically adjusts trading risk parameters based on proximity
to high-impact economic events (FOMC, CPI, NFP, etc.).

Rules:
- 30 min before high-impact event: halve position size
- 5 min before: block new entries entirely
- 15 min after: widen stops 1.5x (volatility expansion)
- Low-impact events: no adjustment
"""

import time
import logging
import asyncio
from datetime import datetime, timedelta
from dataclasses import dataclass, field

logger = logging.getLogger("CalendarRisk")

# High-impact event keywords
HIGH_IMPACT = {
    "FOMC", "Federal Funds Rate", "Interest Rate Decision",
    "CPI", "Consumer Price Index", "Core CPI",
    "NFP", "Non-Farm Payrolls", "Nonfarm Payrolls",
    "GDP", "Gross Domestic Product",
    "PCE", "Personal Consumption", "Core PCE",
    "PPI", "Producer Price Index",
    "Retail Sales", "Unemployment Rate",
    "ISM Manufacturing", "ISM Services",
}

MEDIUM_IMPACT = {
    "Jobless Claims", "Initial Claims", "Continuing Claims",
    "Durable Goods", "Housing Starts", "Building Permits",
    "Consumer Confidence", "Michigan Sentiment",
    "Trade Balance", "Current Account",
    "Industrial Production", "Capacity Utilization",
}


@dataclass
class CalendarEvent:
    name: str
    timestamp: float  # unix timestamp
    impact: str  # "HIGH", "MEDIUM", "LOW"
    country: str = "US"


@dataclass
class RiskAdjustment:
    size_multiplier: float = 1.0  # 1.0 = normal, 0.5 = half, 0.0 = blocked
    stop_multiplier: float = 1.0  # 1.0 = normal, 1.5 = widened
    blocked: bool = False
    reason: str = ""
    next_event: str = ""
    minutes_until: float = 999
    minutes_since: float = 999


class CalendarRiskManager:
    def __init__(self, check_interval_min: int = 5):
        self._events: list[CalendarEvent] = []
        self._last_fetch: float = 0
        self._fetch_interval = check_interval_min * 60
        self._enabled = True

    async def refresh_calendar(self):
        """Fetch calendar from external data module."""
        try:
            from core.external_data import get_economic_calendar
            cal = await get_economic_calendar()
            # Parse events from the calendar data
            events = cal.get("events", [])
            self._events = []
            for ev in events:
                name = ev.get("name", "")
                impact = self._classify_impact(name)
                ts = ev.get("timestamp", 0)
                if ts > 0:
                    self._events.append(CalendarEvent(
                        name=name, timestamp=ts, impact=impact
                    ))
            self._last_fetch = time.time()
            logger.info(f"[CALENDAR] Loaded {len(self._events)} events, "
                       f"{sum(1 for e in self._events if e.impact == 'HIGH')} high-impact")
        except Exception as e:
            logger.warning(f"[CALENDAR] Fetch failed (non-blocking): {e}")
            self._last_fetch = time.time()  # Back off — don't retry until next interval

    def _classify_impact(self, event_name: str) -> str:
        name_upper = event_name.upper()
        for keyword in HIGH_IMPACT:
            if keyword.upper() in name_upper:
                return "HIGH"
        for keyword in MEDIUM_IMPACT:
            if keyword.upper() in name_upper:
                return "MEDIUM"
        return "LOW"

    def get_risk_adjustment(self) -> RiskAdjustment:
        """Get current risk adjustment based on calendar events.

        Call this before every trade entry to check if risk should be modified.
        """
        if not self._enabled:
            return RiskAdjustment()

        now = time.time()

        # Auto-refresh if stale
        if now - self._last_fetch > self._fetch_interval:
            # Non-blocking — will be picked up next call
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self.refresh_calendar())
            except Exception:
                pass

        # Find nearest upcoming and most recent high-impact event
        nearest_future = None
        nearest_past = None

        for ev in self._events:
            if ev.impact not in ("HIGH", "MEDIUM"):
                continue
            delta = ev.timestamp - now
            minutes = delta / 60

            if delta > 0:  # Future event
                if nearest_future is None or ev.timestamp < nearest_future.timestamp:
                    nearest_future = ev
            elif delta > -3600:  # Past event (within last hour)
                if nearest_past is None or ev.timestamp > nearest_past.timestamp:
                    nearest_past = ev

        adj = RiskAdjustment()

        # Check proximity to future HIGH-impact event
        if nearest_future and nearest_future.impact == "HIGH":
            minutes_until = (nearest_future.timestamp - now) / 60
            adj.next_event = nearest_future.name
            adj.minutes_until = minutes_until

            if minutes_until <= 5:
                # BLOCK — too close to event
                adj.blocked = True
                adj.size_multiplier = 0.0
                adj.reason = f"BLOCKED: {nearest_future.name} in {minutes_until:.0f}min"
                logger.warning(f"[CALENDAR RISK] {adj.reason}")
            elif minutes_until <= 30:
                # REDUCE — halve position size
                adj.size_multiplier = 0.5
                adj.reason = f"REDUCED: {nearest_future.name} in {minutes_until:.0f}min"
                logger.info(f"[CALENDAR RISK] {adj.reason}")

        # Check if we're in post-event volatility expansion (15 min after)
        if nearest_past and nearest_past.impact == "HIGH":
            minutes_since = (now - nearest_past.timestamp) / 60
            adj.minutes_since = minutes_since

            if minutes_since <= 15:
                # WIDEN stops — volatility expansion after release
                adj.stop_multiplier = 1.5
                if not adj.reason:
                    adj.reason = f"POST-EVENT: {nearest_past.name} {minutes_since:.0f}min ago — wider stops"
                    logger.info(f"[CALENDAR RISK] {adj.reason}")

        return adj

    def to_dict(self) -> dict:
        adj = self.get_risk_adjustment()
        upcoming = [
            {"name": e.name, "impact": e.impact,
             "minutes_until": max(0, (e.timestamp - time.time()) / 60)}
            for e in sorted(self._events, key=lambda x: x.timestamp)
            if e.timestamp > time.time() and e.impact in ("HIGH", "MEDIUM")
        ][:5]
        return {
            "enabled": self._enabled,
            "adjustment": {
                "size_multiplier": adj.size_multiplier,
                "stop_multiplier": adj.stop_multiplier,
                "blocked": adj.blocked,
                "reason": adj.reason,
            },
            "upcoming_events": upcoming,
            "events_loaded": len(self._events),
        }
