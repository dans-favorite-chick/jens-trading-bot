"""
Phoenix Bot — Session Tagger

Classifies a timestamp into a trading session bucket for analytics.
Used by lab bot (24/7) to tag every paper trade so reflector agent can
analyze per-session performance.

Sessions (CDT):
  ASIA        17:00-02:00  (Tokyo open through Tokyo lunch)
  LONDON      02:00-07:00  (London open, most liquid European hours)
  US_PRE      07:00-08:30  (pre-market, typically thin but event-driven)
  US_RTH      08:30-15:00  (Regular Trading Hours, highest volume)
  US_CLOSE    15:00-16:00  (close auction, institutional rebalancing)
  PAUSE       16:00-17:00  (CME 1-hour daily pause)

Research: NQ session character varies significantly by session. Pinning works
in US_RTH last 90 min. Breakouts more common in LONDON open. Fades dominate
lunch (~11:30-13:00 CDT). Overnight gaps often fill during LONDON.
"""

from __future__ import annotations

from datetime import datetime, time


def session_for(ts: datetime) -> str:
    """Return session tag for a timestamp (local CDT)."""
    t = ts.time()

    # ASIA: 17:00-02:00 (crosses midnight)
    if t >= time(17, 0) or t < time(2, 0):
        return "ASIA"
    if t < time(7, 0):
        return "LONDON"
    if t < time(8, 30):
        return "US_PRE"
    if t < time(15, 0):
        return "US_RTH"
    if t < time(16, 0):
        return "US_CLOSE"
    if t < time(17, 0):
        return "PAUSE"
    return "UNKNOWN"


def is_rth(ts: datetime) -> bool:
    """True during Regular Trading Hours."""
    return session_for(ts) == "US_RTH"


def session_edge_multiplier(session: str) -> float:
    """
    Preliminary per-session size multiplier. Subject to adjustment
    once reflector agent has data on per-session performance.

    - US_RTH: full size — highest liquidity, best edge
    - US_CLOSE: 0.7× — pinning + institutional noise
    - LONDON: 0.7× — decent liquidity but different dynamics
    - US_PRE: 0.5× — thin
    - ASIA: 0.4× — thinnest
    - PAUSE: 0× — don't trade
    """
    return {
        "US_RTH": 1.0,
        "US_CLOSE": 0.7,
        "LONDON": 0.7,
        "US_PRE": 0.5,
        "ASIA": 0.4,
        "PAUSE": 0.0,
        "UNKNOWN": 0.5,
    }.get(session, 0.5)
