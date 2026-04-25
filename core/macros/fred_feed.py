"""Phoenix Phase B+ §3.3 — FRED macro feed with TTL caching.

Promotes the ad-hoc api.stlouisfed.org calls from core.market_intel into
a structured cached layer. Four series tracked:

  - DFF       : Federal Funds Rate (daily)
  - CPIAUCSL  : CPI (monthly) -> we compute YoY from latest 13 obs
  - UNRATE    : Unemployment Rate (monthly)
  - T10Y2Y    : 10Y - 2Y treasury spread (daily)

Design:
  - On-disk JSON cache at data/cache/fred_<series>.json (~200 bytes each).
  - Cache TTL default 3600s (1 hour). Stale-but-present cache is preferred
    over a hard failure on transient network issues.
  - Best-effort: on any HTTP/network error, fall back to last cached value
    (even if expired) and log WARN. Never raise.
  - Stdlib urllib.request only — no new package dependencies.

Regime-shift thresholds:
  - DFF (FFR)        : any change (Fed decisions are discrete; 25bp = shift)
  - UNRATE           : > 0.10pp change
  - T10Y2Y           : sign flip (inverted <-> steepening)
  - CPI YoY          : > 0.30pp change
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("FredMacroFeed")

# ----- Constants -----

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

SERIES_DFF = "DFF"
SERIES_CPIAUCSL = "CPIAUCSL"
SERIES_UNRATE = "UNRATE"
SERIES_T10Y2Y = "T10Y2Y"

ALL_SERIES = (SERIES_DFF, SERIES_CPIAUCSL, SERIES_UNRATE, SERIES_T10Y2Y)

DEFAULT_CACHE_TTL_S = 3600  # 1 hour
HTTP_TIMEOUT_S = 5.0

# Regime-shift thresholds
THRESH_UNRATE_PP = 0.10
THRESH_CPI_YOY_PP = 0.30
# DFF: any change. T10Y2Y: sign flip only.

# Default cache directory: <project_root>/data/cache/
_DEFAULT_CACHE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "cache"
)


# ----- Data types -----


@dataclass(frozen=True)
class MacroSnapshot:
    """Latest values for the four tracked FRED series."""

    ffr: float
    cpi_yoy: float | None
    unemployment: float
    yield_curve_2y10y: float
    fetched_at_iso: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegimeShiftEvent:
    """A single series crossed its regime-shift threshold."""

    series: str
    prev_value: float
    curr_value: float
    magnitude: float  # absolute change (pp for rates, raw for spread)
    direction: str    # e.g. "FFR_CUT", "UNRATE_RISE", "CURVE_INVERSION"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----- The feed -----


class FredMacroFeed:
    """Structured FRED feed with on-disk TTL caching.

    Public API:
      - get_snapshot()           -> MacroSnapshot
      - detect_regime_shift(p,c) -> list[RegimeShiftEvent]
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get(
            "FRED_API_KEY", ""
        )
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _DEFAULT_CACHE_DIR
        self.cache_ttl_s = int(cache_ttl_s)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover - directory creation is best-effort
            logger.warning("could not create cache dir %s: %s", self.cache_dir, e)

    # ---- cache helpers ----

    def _cache_path(self, series_id: str) -> Path:
        return self.cache_dir / f"fred_{series_id}.json"

    def _read_cache(self, series_id: str) -> dict[str, Any] | None:
        p = self._cache_path(series_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("cache read failed for %s: %s", series_id, e)
            return None

    def _write_cache(self, series_id: str, payload: dict[str, Any]) -> None:
        p = self._cache_path(series_id)
        try:
            p.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:  # pragma: no cover - disk write best-effort
            logger.warning("cache write failed for %s: %s", series_id, e)

    def _is_fresh(self, entry: dict[str, Any]) -> bool:
        try:
            return (time.time() - float(entry.get("cached_at", 0))) < self.cache_ttl_s
        except Exception:
            return False

    # ---- HTTP layer ----

    def _http_get_observations(
        self, series_id: str, limit: int = 1
    ) -> list[dict[str, Any]]:
        """Fetch latest `limit` observations for `series_id` from FRED.

        Returns the parsed observations list. Raises on network/HTTP errors;
        the caller is responsible for catching and falling back to cache.
        """
        params = {
            "series_id": series_id,
            "sort_order": "desc",
            "limit": str(limit),
            "file_type": "json",
            "api_key": self.api_key,
        }
        url = FRED_BASE_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "PhoenixBot/1.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            if resp.status != 200:
                raise RuntimeError(f"FRED HTTP {resp.status} for {series_id}")
            data = json.loads(resp.read().decode("utf-8"))
        return list(data.get("observations") or [])

    # ---- Per-series fetchers ----

    def _fetch_series_value(self, series_id: str) -> float | None:
        """Fetch the latest scalar value for a single-value series.

        Used for DFF, UNRATE, T10Y2Y (CPI YoY needs separate logic).
        Cache-first: returns fresh cache if present, else hits HTTP and
        writes cache. On HTTP failure, falls back to any prior cached
        value (even expired) and logs WARN.
        """
        cached = self._read_cache(series_id)
        if cached is not None and self._is_fresh(cached):
            v = cached.get("value")
            return float(v) if v is not None else None

        try:
            obs = self._http_get_observations(series_id, limit=1)
            value: float | None = None
            if obs:
                raw = obs[0].get("value")
                if raw not in (None, "", "."):
                    value = float(raw)
            self._write_cache(
                series_id,
                {
                    "series_id": series_id,
                    "value": value,
                    "cached_at": time.time(),
                    "obs_date": obs[0].get("date") if obs else None,
                },
            )
            return value
        except Exception as e:
            logger.warning(
                "FRED fetch for %s failed (%s); using last cached value", series_id, e
            )
            if cached is not None:
                v = cached.get("value")
                return float(v) if v is not None else None
            return None

    def _fetch_cpi_yoy(self) -> float | None:
        """CPI YoY = (latest CPIAUCSL / 12-months-prior - 1) * 100."""
        cache_key = "CPIAUCSL_YOY"
        cached = self._read_cache(cache_key)
        if cached is not None and self._is_fresh(cached):
            v = cached.get("value")
            return float(v) if v is not None else None

        try:
            obs = self._http_get_observations(SERIES_CPIAUCSL, limit=13)
            value: float | None = None
            if len(obs) >= 13:
                latest_raw = obs[0].get("value")
                year_ago_raw = obs[12].get("value")
                if (
                    latest_raw not in (None, "", ".")
                    and year_ago_raw not in (None, "", ".")
                ):
                    latest = float(latest_raw)
                    year_ago = float(year_ago_raw)
                    if year_ago != 0:
                        value = round((latest - year_ago) / year_ago * 100.0, 2)
            self._write_cache(
                cache_key,
                {
                    "series_id": cache_key,
                    "value": value,
                    "cached_at": time.time(),
                    "obs_date": obs[0].get("date") if obs else None,
                },
            )
            return value
        except Exception as e:
            logger.warning(
                "FRED CPI YoY fetch failed (%s); using last cached value", e
            )
            if cached is not None:
                v = cached.get("value")
                return float(v) if v is not None else None
            return None

    # ---- Public: snapshot ----

    def get_snapshot(self) -> MacroSnapshot:
        """Return latest values for all 4 series. Cache-first, never raises."""
        ffr = self._fetch_series_value(SERIES_DFF)
        cpi_yoy = self._fetch_cpi_yoy()
        unrate = self._fetch_series_value(SERIES_UNRATE)
        curve = self._fetch_series_value(SERIES_T10Y2Y)

        # Coerce missing scalars to NaN-equivalents so MacroSnapshot remains
        # populated. We use 0.0 for required fields with a None guard upstream
        # via the dataclass — but since MacroSnapshot expects float for ffr /
        # unemployment / yield_curve, fall back to 0.0 only when the series
        # is genuinely unavailable. Callers should treat 0.0 as "unknown"
        # only in conjunction with the absence of recent shifts.
        snap = MacroSnapshot(
            ffr=float(ffr) if ffr is not None else 0.0,
            cpi_yoy=float(cpi_yoy) if cpi_yoy is not None else None,
            unemployment=float(unrate) if unrate is not None else 0.0,
            yield_curve_2y10y=float(curve) if curve is not None else 0.0,
            fetched_at_iso=datetime.now(timezone.utc).isoformat(),
        )
        return snap

    # ---- Public: regime-shift detection ----

    @staticmethod
    def detect_regime_shift(
        prev: MacroSnapshot, curr: MacroSnapshot
    ) -> list[RegimeShiftEvent]:
        """Return RegimeShiftEvents for any series that crossed thresholds."""
        events: list[RegimeShiftEvent] = []

        # FFR: any change is a shift (Fed decisions are discrete)
        if prev.ffr != curr.ffr and (prev.ffr > 0.0 or curr.ffr > 0.0):
            delta = curr.ffr - prev.ffr
            direction = "FFR_CUT" if delta < 0 else "FFR_HIKE"
            events.append(
                RegimeShiftEvent(
                    series="DFF",
                    prev_value=prev.ffr,
                    curr_value=curr.ffr,
                    magnitude=abs(delta),
                    direction=direction,
                )
            )

        # UNRATE: > 0.1pp
        if abs(curr.unemployment - prev.unemployment) > THRESH_UNRATE_PP and (
            prev.unemployment > 0.0 or curr.unemployment > 0.0
        ):
            delta = curr.unemployment - prev.unemployment
            direction = "UNRATE_RISE" if delta > 0 else "UNRATE_FALL"
            events.append(
                RegimeShiftEvent(
                    series="UNRATE",
                    prev_value=prev.unemployment,
                    curr_value=curr.unemployment,
                    magnitude=abs(delta),
                    direction=direction,
                )
            )

        # T10Y2Y: sign flip
        prev_curve = prev.yield_curve_2y10y
        curr_curve = curr.yield_curve_2y10y
        if (prev_curve >= 0 and curr_curve < 0):
            events.append(
                RegimeShiftEvent(
                    series="T10Y2Y",
                    prev_value=prev_curve,
                    curr_value=curr_curve,
                    magnitude=abs(curr_curve - prev_curve),
                    direction="CURVE_INVERSION",
                )
            )
        elif (prev_curve < 0 and curr_curve >= 0):
            events.append(
                RegimeShiftEvent(
                    series="T10Y2Y",
                    prev_value=prev_curve,
                    curr_value=curr_curve,
                    magnitude=abs(curr_curve - prev_curve),
                    direction="CURVE_STEEPENING",
                )
            )

        # CPI YoY: > 0.3pp
        if prev.cpi_yoy is not None and curr.cpi_yoy is not None:
            cpi_delta = curr.cpi_yoy - prev.cpi_yoy
            if abs(cpi_delta) > THRESH_CPI_YOY_PP:
                direction = "CPI_RISE" if cpi_delta > 0 else "CPI_FALL"
                events.append(
                    RegimeShiftEvent(
                        series="CPI_YOY",
                        prev_value=prev.cpi_yoy,
                        curr_value=curr.cpi_yoy,
                        magnitude=abs(cpi_delta),
                        direction=direction,
                    )
                )

        return events


__all__ = [
    "FredMacroFeed",
    "MacroSnapshot",
    "RegimeShiftEvent",
    "ALL_SERIES",
    "SERIES_DFF",
    "SERIES_CPIAUCSL",
    "SERIES_UNRATE",
    "SERIES_T10Y2Y",
]
