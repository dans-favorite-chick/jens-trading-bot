"""
Phoenix Bot — VIX Term Structure

Computes VIX / VIX9D / VIX3M / VIX6M ratios → contango/backwardation regime.
Integrates into MarketIntel for regime-aware signal weighting.

Research basis (2026):
- VIX/VIX3M ratio classifies market stress:
    < 0.85  = STEEP_CONTANGO  (complacent, trends run, mean-rev weakens)
    0.85-1.00 = CONTANGO       (normal market)
    1.00-1.15 = MILD_BACKWARDATION (elevated risk)
    > 1.15  = STEEP_BACKWARDATION (acute fear, often tradeable bottom)
- Contango = longer-dated > shorter-dated (normal, risk-off demand > risk-on)
- Backwardation = shorter-dated > longer-dated (fear priced in near-term)

Primary data source: CBOE index feeds (user has CBOE VIX subscription).
Fallback: yfinance (already in MarketIntel infrastructure).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("VIXTerm")

# Thresholds for regime classification
STEEP_CONTANGO_MAX = 0.85
CONTANGO_MAX = 1.00
BACKWARDATION_MAX = 1.15
# Above BACKWARDATION_MAX = STEEP_BACKWARDATION


@dataclass
class VIXTermStructure:
    vix: Optional[float]       # Spot VIX
    vix_9d: Optional[float]    # VIX9D (short-term)
    vix_3m: Optional[float]    # VIX3M (medium-term)
    vix_6m: Optional[float]    # VIX6M (longer-term) — optional
    ratio_vix_3m: Optional[float]  # VIX / VIX3M — primary regime indicator
    regime: str                # STEEP_CONTANGO | CONTANGO | MILD_BACKWARDATION | STEEP_BACKWARDATION | UNKNOWN
    source: str                # "CBOE" or "yfinance" or "stale"
    updated_at: Optional[datetime]

    def to_dict(self) -> dict:
        return {
            "vix": self.vix,
            "vix_9d": self.vix_9d,
            "vix_3m": self.vix_3m,
            "vix_6m": self.vix_6m,
            "ratio_vix_3m": round(self.ratio_vix_3m, 3) if self.ratio_vix_3m else None,
            "regime": self.regime,
            "source": self.source,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "age_minutes": (
                (datetime.now() - self.updated_at).total_seconds() / 60
                if self.updated_at else -1
            ),
        }

    def is_stale(self, max_age_min: int = 15) -> bool:
        if self.updated_at is None:
            return True
        return (datetime.now() - self.updated_at).total_seconds() / 60 > max_age_min


def classify_regime(ratio: Optional[float]) -> str:
    if ratio is None:
        return "UNKNOWN"
    if ratio < STEEP_CONTANGO_MAX:
        return "STEEP_CONTANGO"
    if ratio < CONTANGO_MAX:
        return "CONTANGO"
    if ratio < BACKWARDATION_MAX:
        return "MILD_BACKWARDATION"
    return "STEEP_BACKWARDATION"


# ─── Data source: yfinance fallback ────────────────────────────────────

def _fetch_yfinance() -> Optional[dict]:
    """Fetch VIX family from yfinance. Returns dict or None on failure."""
    try:
        import yfinance as yf
        symbols = {
            "vix": "^VIX",
            "vix_9d": "^VIX9D",
            "vix_3m": "^VIX3M",
            "vix_6m": "^VIX6M",
        }
        result = {}
        for key, sym in symbols.items():
            try:
                t = yf.Ticker(sym)
                info = t.history(period="1d", interval="5m")
                if info is not None and not info.empty:
                    last_close = float(info["Close"].iloc[-1])
                    if last_close > 0:
                        result[key] = last_close
                        continue
            except Exception as e:
                logger.debug(f"yfinance {sym} failed: {e}")
                continue
            result[key] = None
        return result
    except ImportError:
        logger.warning("yfinance not installed, skipping fallback")
        return None
    except Exception as e:
        logger.warning(f"yfinance fetch failed: {e}")
        return None


# ─── Data source: CBOE (primary) ───────────────────────────────────────
# User mentioned CBOE VIX index is active on their account.
# CBOE provides real-time data via their Data Shop API, but that requires
# enterprise credentials. For now, we defer CBOE integration and use yfinance.
# The interface is ready for CBOE plug-in when credentials are configured.

def _fetch_cboe() -> Optional[dict]:
    """
    Placeholder for CBOE direct feed. Not implemented in v1.
    When implemented: requires CBOE_DATASHOP_API_KEY env var.
    Returns None to fall through to yfinance.
    """
    import os
    if not os.environ.get("CBOE_DATASHOP_API_KEY"):
        return None
    # TODO: CBOE Data Shop integration when credentials available
    return None


def fetch_vix_term_structure() -> VIXTermStructure:
    """
    Main entry point. Returns current VIX term structure.
    Tries CBOE first, falls back to yfinance.
    """
    # Try CBOE
    data = _fetch_cboe()
    source = "CBOE"
    if data is None:
        data = _fetch_yfinance()
        source = "yfinance"

    if not data or not any(data.values()):
        return VIXTermStructure(
            vix=None, vix_9d=None, vix_3m=None, vix_6m=None,
            ratio_vix_3m=None, regime="UNKNOWN", source="stale",
            updated_at=None,
        )

    vix = data.get("vix")
    vix_9d = data.get("vix_9d")
    vix_3m = data.get("vix_3m")
    vix_6m = data.get("vix_6m")

    ratio = None
    if vix and vix_3m and vix_3m > 0:
        ratio = vix / vix_3m

    return VIXTermStructure(
        vix=vix, vix_9d=vix_9d, vix_3m=vix_3m, vix_6m=vix_6m,
        ratio_vix_3m=ratio,
        regime=classify_regime(ratio),
        source=source,
        updated_at=datetime.now(),
    )


# ─── Cached version for MarketIntel integration ────────────────────────

_cached: Optional[VIXTermStructure] = None


def get_cached(max_age_minutes: int = 10) -> VIXTermStructure:
    """Returns cached term structure, refreshes if stale."""
    global _cached
    if _cached is None or _cached.is_stale(max_age_minutes):
        _cached = fetch_vix_term_structure()
    return _cached
