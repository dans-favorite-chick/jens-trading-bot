"""
Phoenix Bot — ES Gamma Confirmation

⚠️  EFFECTIVELY DORMANT 2026-05-06 (Sprint J cleanup) ⚠️
Originally pulled ES (S&P 500 E-mini) MenthorQ gamma data to confirm
or diverge from NQ. With the MenthorQ subscription cancelled, the NQ
side that fed `nq_regime` is always "UNKNOWN", and the manual ES
regime file (memory/procedural/es_regime.json) is no longer updated.
check_confirmation() therefore always returns the "ES UNAVAILABLE,
confluence_adjust=0" no-op path and is harmless to call.

The module is preserved for backward compat with strategies that may
still call it as a context check. If/when an alternative gamma data
source is integrated, callers' interface stays the same.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ESConfirmation")

PHOENIX_ROOT = Path(__file__).parent.parent
ES_REGIME_FILE = PHOENIX_ROOT / "memory" / "procedural" / "es_regime.json"


@dataclass
class ESConfirmation:
    nq_regime: str
    es_regime: str
    aligned: bool               # True when both same direction
    confluence_adjust: int      # +5 if aligned, -5 if diverged, 0 if ES data unavailable
    es_data_available: bool
    reasoning: list[str]


def load_es_regime(today: date = None) -> Optional[dict]:
    """Load ES regime from manual daily update file. Returns None if stale or missing."""
    if today is None:
        today = date.today()
    if not ES_REGIME_FILE.exists():
        return None
    try:
        with open(ES_REGIME_FILE, "r") as f:
            data = json.load(f)
        file_date = datetime.strptime(data.get("date", "1900-01-01"), "%Y-%m-%d").date()
        if file_date != today:
            logger.debug(f"ES regime file stale: {file_date} != {today}")
            return None
        return data
    except Exception as e:
        logger.warning(f"ES regime load failed: {e}")
        return None


def check_confirmation(nq_regime: str, today: date = None) -> ESConfirmation:
    """
    Compare NQ regime against ES (from manual daily file).
    Returns confluence adjustment that strategies apply to composite bias score.

    Sprint J 2026-05-06: NQ-side regime is always "UNKNOWN" since MQ
    cancelled, so this always returns the no-op path
    (es_data_available=False, confluence_adjust=0).
    """
    es_data = load_es_regime(today)
    if es_data is None:
        return ESConfirmation(
            nq_regime=nq_regime, es_regime="UNAVAILABLE",
            aligned=False, confluence_adjust=0,
            es_data_available=False,
            reasoning=["ES regime data not available for today (manual update needed)"],
        )

    es_regime = str(es_data.get("regime", "UNKNOWN")).upper()
    es_gex = es_data.get("net_gex_bn")

    # Alignment check: same sign regimes
    aligned = (nq_regime == es_regime and nq_regime in ("POSITIVE", "NEGATIVE"))

    adjust = 0
    reasons = [f"NQ {nq_regime} vs ES {es_regime}"]
    if aligned:
        adjust = +5
        reasons.append("aligned → +5 confluence bonus")
    elif nq_regime != es_regime and nq_regime in ("POSITIVE", "NEGATIVE") and es_regime in ("POSITIVE", "NEGATIVE"):
        # True divergence — both sides have data
        adjust = -5
        reasons.append("DIVERGED → -5 confluence penalty, trade with caution")
    else:
        reasons.append("insufficient comparison (one or both UNKNOWN)")

    if es_gex:
        reasons.append(f"ES GEX: {es_gex:+.1f}B")

    return ESConfirmation(
        nq_regime=nq_regime, es_regime=es_regime,
        aligned=aligned, confluence_adjust=adjust,
        es_data_available=True,
        reasoning=reasons,
    )


def seed_es_regime_file(regime: str, net_gex_bn: float = None,
                        today: date = None) -> None:
    """Helper to write an ES regime file. Called from morning refresh task."""
    if today is None:
        today = date.today()
    ES_REGIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "date": today.isoformat(),
        "regime": regime,
        "_updated_at": datetime.now().isoformat(),
    }
    if net_gex_bn is not None:
        data["net_gex_bn"] = net_gex_bn
    with open(ES_REGIME_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Seeded ES regime file: {regime} for {today}")
