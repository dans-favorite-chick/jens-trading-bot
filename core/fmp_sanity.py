"""
Phoenix Bot — FMP Market Data Cross-Check

Financial Modeling Prep (https://site.financialmodelingprep.com/) is an
external source of truth for quote data. After the 2026-04-24 incident
(see core/price_sanity.py for the backstory) Jennifer explicitly asked
for an external cross-check so a locally-corrupted tick stream cannot
silently drive trade decisions.

What this module does:
  * Periodically fetches a reference quote from FMP (NDX index is the
    cleanest MNQ analogue; QQQ + a configured ratio is the fallback).
  * Converts the reference quote to an MNQ-equivalent price.
  * Publishes that price into `core.price_sanity.set_external_reference`.
  * Exposes `check_mnq_vs_fmp(local_price)` for direct cross-check.
  * Never raises on HTTP / parse error — failure is silent and returns
    None. The bot is expected to keep running (FMP is advisory, not
    authoritative).

Keys:
  FMP_API_KEY — free tier at https://site.financialmodelingprep.com/
                 (rate-limited, sufficient for a 30-60s poll).

Env overrides:
  FMP_REFERENCE_SYMBOL — default "QQQ". Use "SPY" as a backup if QQQ
                          endpoint is ever rate-limited. `^NDX` is
                          paid-tier only (402) — not usable with a
                          free key.
  FMP_CACHE_TTL_S      — default 30
  QQQ_TO_NQ_RATIO      — already present in .env; used to convert the
                          QQQ quote to an MNQ-equivalent reference.

Endpoint note:
  We call FMP's `stable` endpoint (https://financialmodelingprep.com/stable/quote).
  The legacy `/api/v3/quote/{sym}` returns 403 Forbidden on free keys
  as of 2026-04; stable is the current free-tier path.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("FMPSanity")


@dataclass
class _Cached:
    price: float = 0.0
    symbol: str = ""
    ts: float = 0.0
    error: str = ""


_cache = _Cached()
_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


def _cache_ttl_s() -> float:
    return _env_float("FMP_CACHE_TTL_S", 30.0)


def _api_key() -> str:
    return os.environ.get("FMP_API_KEY", "").strip()


def _reference_symbol() -> str:
    return _env_str("FMP_REFERENCE_SYMBOL", "QQQ")


# ═══════════════════════════════════════════════════════════════════════
# FMP HTTP fetch
# ═══════════════════════════════════════════════════════════════════════

def _fetch_quote(symbol: str, timeout_s: float = 3.0) -> Optional[float]:
    """Low-level FMP quote fetch. Returns latest price or None on any error.

    Uses FMP's `stable` endpoint (the free-tier path as of 2026-04).
    Response shape: JSON list with one dict containing `price`. Non-200,
    empty list, missing key, connection error, timeout — all normalized
    to None and logged at DEBUG (not WARNING) so FMP outages don't spam
    the bot log.
    """
    key = _api_key()
    if not key:
        return None
    import urllib.parse
    import urllib.request
    import json

    url = (
        "https://financialmodelingprep.com/stable/quote"
        + f"?symbol={urllib.parse.quote(symbol)}"
        + f"&apikey={urllib.parse.quote(key)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None
            body = resp.read()
        data = json.loads(body)
        if not isinstance(data, list) or not data:
            return None
        price = data[0].get("price")
        if price is None:
            return None
        return float(price)
    except Exception as e:
        # DEBUG, not WARNING — see docstring
        logger.debug(f"[FMP] quote fetch failed sym={symbol}: {e!r}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# MNQ-equivalent conversion
# ═══════════════════════════════════════════════════════════════════════

def _to_mnq_equivalent(symbol: str, price: float) -> float:
    """Convert an FMP reference price to an MNQ-equivalent price.

    NDX (Nasdaq 100 cash index) tracks MNQ within a few basis points,
    but /quote/^NDX is paid-tier on FMP (402 Forbidden for free keys).
    QQQ (Invesco QQQ ETF) tracks at roughly 1/41st of NDX; the env var
    QQQ_TO_NQ_RATIO captures the current ratio locally without hard-
    coding a drift-prone constant (update it when the index rebalances).
    SPY is a last-resort fallback that correlates but does not track —
    a divergence alert off SPY means either NQ sold off hard vs SPX OR
    data is corrupt; either way worth looking at.
    """
    symbol = symbol.upper()
    if symbol in ("^NDX", "NDX"):
        return price  # 1:1 enough for sanity checks
    if symbol == "QQQ":
        ratio = _env_float("QQQ_TO_NQ_RATIO", 41.10)
        return price * ratio
    if symbol == "SPY":
        # SPY * ~38 ≈ NQ (coarse — regime-dependent). Configurable via env.
        ratio = _env_float("SPY_TO_NQ_RATIO", 38.0)
        return price * ratio
    if symbol in ("NQ", "NQ=F", "MNQ"):
        return price
    return price


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════

def get_reference_mnq_price(force_refresh: bool = False) -> Optional[float]:
    """Return the current FMP-derived MNQ-equivalent price (cached).

    Returns None when:
      * FMP_API_KEY is unset
      * All fetches failed within the TTL window
      * The derived price was non-positive / parse failure

    This function is intentionally lightweight (one HTTP call cached 30s)
    so it is safe to call from hot paths. For a scheduled background
    refresh, use `poll_loop()` below.
    """
    now = time.time()
    with _lock:
        if not force_refresh and _cache.price > 0 and (now - _cache.ts) < _cache_ttl_s():
            return _cache.price

    symbol = _reference_symbol()
    raw = _fetch_quote(symbol)
    if raw is None and symbol != "QQQ":
        # Fall back to QQQ (free-tier friendly, tight NQ tracking).
        raw = _fetch_quote("QQQ")
        if raw is not None:
            symbol = "QQQ"
    if raw is None and symbol != "SPY":
        # Last-resort: SPY. Very loose NQ tracking but any-data beats no-data.
        raw = _fetch_quote("SPY")
        if raw is not None:
            symbol = "SPY"
    if raw is None:
        with _lock:
            _cache.error = "all_sources_failed"
        return None

    mnq_eq = _to_mnq_equivalent(symbol, raw)
    if mnq_eq <= 0:
        return None

    with _lock:
        _cache.price = mnq_eq
        _cache.symbol = symbol
        _cache.ts = now
        _cache.error = ""

    # Publish to the sanity module so order-submit can fall back to this
    # when the local tick stream is silent or suspect.
    try:
        from core import price_sanity
        price_sanity.set_external_reference(mnq_eq, source=f"fmp:{symbol}")
    except Exception:
        pass

    return mnq_eq


def check_mnq_vs_fmp(local_price: float, tolerance: float = 0.01) -> dict:
    """One-shot cross-check of a local MNQ price against FMP reference.

    Returns:
      {"ok": bool, "local": float, "reference": float, "deviation_pct": float,
       "source": str, "reason": str}

    When FMP is unavailable, `ok` is True (fail-open) with reason
    "fmp_unavailable" so the caller doesn't halt trading on a third-party
    API outage. The guard for corruption is the local tick-sanity check
    (core.price_sanity) — FMP is a belt-and-suspenders confirmation.
    """
    ref = get_reference_mnq_price()
    if ref is None or ref <= 0:
        return {
            "ok": True,
            "local": local_price,
            "reference": None,
            "deviation_pct": None,
            "source": None,
            "reason": "fmp_unavailable",
        }
    if local_price is None or local_price <= 0:
        return {
            "ok": False,
            "local": local_price,
            "reference": ref,
            "deviation_pct": None,
            "source": _cache.symbol,
            "reason": "local_price_invalid",
        }
    dev = abs(local_price - ref) / ref
    return {
        "ok": dev <= tolerance,
        "local": local_price,
        "reference": ref,
        "deviation_pct": dev,
        "source": _cache.symbol,
        "reason": "ok" if dev <= tolerance else f"deviation {dev*100:.2f}% > {tolerance*100:.2f}%",
    }


# ═══════════════════════════════════════════════════════════════════════
# Background poll loop (optional)
# ═══════════════════════════════════════════════════════════════════════

async def poll_loop(interval_s: float = 60.0, divergence_threshold_pct: float = 0.015,
                    _unused_halt: float = None) -> None:
    """Asyncio task that refreshes FMP every `interval_s` seconds.

    2026-04-24 Jennifer: rather than writing a HALT marker on local/FMP
    divergence, we flip the price_sanity module into "fmp_primary" mode
    — new entries are soft-blocked but the bot keeps processing ticks
    and managing open positions. When local agrees with FMP for
    `_AGREE_POLLS_TO_HEAL` consecutive polls, we flip back. Both flips
    fire Telegram alerts so Jennifer can investigate the NT8 side.

    `_unused_halt` is kept in the signature for backward compatibility
    with any old call sites still passing `halt_on_divergence_pct=` by
    keyword — it has no effect.

    Safe to leave disabled: only runs if FMP_API_KEY is set.
    """
    import asyncio
    from core import price_sanity

    if not _api_key():
        logger.info("[FMP] poll_loop: FMP_API_KEY not set, skipping background sanity polling")
        return

    logger.info(
        f"[FMP] poll_loop started interval={interval_s}s "
        f"threshold={divergence_threshold_pct*100:.2f}% "
        f"(mode-flip on {price_sanity._DIVERGENT_POLLS_TO_FLIP} consecutive divergences; "
        f"heal on {price_sanity._AGREE_POLLS_TO_HEAL} consecutive agrees)"
    )

    try:
        from core.telegram_notifier import notify_alert as _tg_alert
    except Exception:
        _tg_alert = None

    while True:
        try:
            ref = get_reference_mnq_price(force_refresh=True)
            snap = price_sanity.snapshot()
            local = snap.get("last_accepted_price") or 0.0
            if ref and ref > 0 and local > 0:
                dev = abs(local - ref) / ref
                logger.info(
                    f"[FMP] check local={local:.2f} ref={ref:.2f} "
                    f"dev={dev*100:.2f}% mode={snap.get('mode')}"
                )
                flip = price_sanity.record_fmp_check(
                    local_price=local, fmp_price=ref,
                    deviation_pct=dev, threshold_pct=divergence_threshold_pct,
                )
                if flip == "fmp_primary" and _tg_alert:
                    try:
                        await _tg_alert(
                            "⚠️ FMP fallback engaged",
                            f"Local MNQ {local:.2f} diverged from FMP {ref:.2f} "
                            f"({dev*100:.2f}%). New entries soft-blocked. Existing "
                            f"positions still managed. Will auto-heal when stream "
                            f"agrees with FMP again."
                        )
                    except Exception as e:
                        logger.debug(f"[FMP] telegram alert failed: {e!r}")
                elif flip == "local_primary" and _tg_alert:
                    try:
                        await _tg_alert(
                            "✅ FMP fallback cleared",
                            f"Local MNQ stream healed (agrees with FMP "
                            f"{ref:.2f}). Resuming normal entry flow."
                        )
                    except Exception as e:
                        logger.debug(f"[FMP] telegram alert failed: {e!r}")
        except Exception as e:
            logger.warning(f"[FMP] poll_loop iter error (non-blocking): {e!r}")

        await asyncio.sleep(interval_s)
