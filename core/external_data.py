"""
Phoenix Bot — External data adapter layer.

Thin shim over core.market_intel so calendar_risk.py and other consumers
have a stable import surface. The adapter also normalizes the Finnhub
calendar payload into the {"events": [...]} shape that calendar_risk expects.

Fixes: KNOWN_ISSUES "CalendarRisk fetch consistently fails — No module named
'core.external_data'" (2026-04-17).
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("ExternalData")


async def get_economic_calendar() -> dict:
    """
    Wrapper around core.market_intel.get_economic_calendar() that returns
    a payload shaped for CalendarRiskManager.

    Returns:
        {
            "events": [
                {"name": str, "timestamp": float (unix), "impact": "HIGH"|"MEDIUM"|"LOW", "country": "US"},
                ...
            ],
            "count": int,
            "trade_restricted": bool,
        }
    """
    try:
        from core.market_intel import get_economic_calendar as _raw_get
    except Exception as e:
        logger.warning(f"market_intel import failed: {e}")
        return {"events": [], "count": 0, "trade_restricted": False}

    try:
        raw = await _raw_get()
    except Exception as e:
        logger.warning(f"market_intel.get_economic_calendar failed: {e}")
        return {"events": [], "count": 0, "trade_restricted": False}

    # market_intel returns events_today with "time" as "HH:MM". Convert to unix ts.
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events = []
    for ev in raw.get("events_today", []):
        time_str = ev.get("time", "")
        ts = 0.0
        if time_str:
            try:
                dt = datetime.strptime(f"{today_str} {time_str}", "%Y-%m-%d %H:%M")
                dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            except ValueError:
                ts = 0.0
        events.append({
            "name": ev.get("name", ""),
            "timestamp": ts,
            "impact": ev.get("impact", "LOW"),
            "country": "US",  # market_intel already filters to US
        })

    return {
        "events": events,
        "count": len(events),
        "trade_restricted": bool(raw.get("trade_restricted", False)),
    }
