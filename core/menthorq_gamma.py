"""
Phoenix Bot — MenthorQ Gamma Integration (B14)

Parses Jennifer's daily MenthorQ paste into an immutable GammaLevels
dataclass. Feeds regime classification, entry-wall filtering, and
natural-stop discovery (Phase 3).

Paste format (one line per file):
    $NQM2026: Key1, Value1, Key2, Value2, ...

Tier 1 fields (required for gate to function):
    HVL, HVL 0DTE, Call Resistance, Call Resistance 0DTE,
    Put Support, Put Support 0DTE, Gamma Wall 0DTE

Tier 2 fields (improve precision, optional):
    1D Min, 1D Max, GEX 1..10

Blind spots file: BL 1..10 (separate file, same format).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from enum import Enum

logger = logging.getLogger("MenthorQGamma")


# ─── Data model ──────────────────────────────────────────────────────

class GammaRegime(Enum):
    POSITIVE_GAMMA = "POSITIVE_GAMMA"
    NEGATIVE_GAMMA = "NEGATIVE_GAMMA"
    TRANSITION_ZONE = "TRANSITION_ZONE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GammaLevels:
    symbol: str
    data_date: date
    call_resistance: Optional[float]
    put_support: Optional[float]
    hvl: Optional[float]
    one_d_min: Optional[float]
    one_d_max: Optional[float]
    call_resistance_0dte: Optional[float]
    put_support_0dte: Optional[float]
    hvl_0dte: Optional[float]
    gamma_wall_0dte: Optional[float]
    gex_levels: Tuple[float, ...]
    blind_spots: Tuple[float, ...]
    loaded_at: datetime

    @property
    def is_complete(self) -> bool:
        """All Tier 1 fields populated."""
        return all(v is not None for v in (
            self.hvl,
            self.hvl_0dte,
            self.call_resistance,
            self.call_resistance_0dte,
            self.put_support,
            self.put_support_0dte,
            self.gamma_wall_0dte,
        ))


# ─── Alias map (module-level constant, inspectable by tests) ────────

# Maps normalized key (lower, whitespace collapsed) → attribute name.
# GEX N and BL N are handled separately via regex.
ALIAS_MAP: Dict[str, str] = {
    "call resistance": "call_resistance",
    "callresistance": "call_resistance",
    "put support": "put_support",
    "putsupport": "put_support",
    "hvl": "hvl",
    "1d min": "one_d_min",
    "1d_min": "one_d_min",
    "1d max": "one_d_max",
    "1d_max": "one_d_max",
    "call resistance 0dte": "call_resistance_0dte",
    "callresistance 0dte": "call_resistance_0dte",
    "put support 0dte": "put_support_0dte",
    "putsupport 0dte": "put_support_0dte",
    "hvl 0dte": "hvl_0dte",
    "gamma wall 0dte": "gamma_wall_0dte",
}

_GEX_RE = re.compile(r"^gex\s*(\d{1,2})$")
_BL_RE = re.compile(r"^bl\s*(\d{1,2})$")
_SYMBOL_RE = re.compile(r"^\$([A-Z0-9]+)\s*:\s*(.*)$", re.DOTALL)


def _normalize_key(raw: str) -> str:
    """Lowercase, collapse internal whitespace, strip ends."""
    return re.sub(r"\s+", " ", raw.strip().lower())


def _parse_pairs(text: str) -> Tuple[str, list[Tuple[str, str]]]:
    """
    Strip $SYMBOL: prefix, split on comma, return (symbol, [(key, value), ...]).
    Raises ValueError if structurally malformed.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty paste")

    m = _SYMBOL_RE.match(text)
    if not m:
        raise ValueError(
            f"paste must start with '$SYMBOL: ...' — got: {text[:60]!r}"
        )
    symbol = m.group(1)
    body = m.group(2).strip()

    if not body:
        return symbol, []

    tokens = [t.strip() for t in body.split(",")]
    if len(tokens) % 2 != 0:
        raise ValueError(
            f"odd number of comma-separated tokens ({len(tokens)}) — "
            f"expected key,value pairs. First tokens: {tokens[:6]}"
        )

    pairs = []
    for i in range(0, len(tokens), 2):
        key = tokens[i]
        val = tokens[i + 1]
        if not key:
            raise ValueError(f"empty key at position {i}")
        pairs.append((key, val))
    return symbol, pairs


def parse_gamma_paste(text: str) -> GammaLevels:
    """
    Parse a MenthorQ gamma levels paste into a GammaLevels instance.

    blind_spots is populated only via parse_blind_spots_paste or
    load_gamma_for_date — this function returns an empty tuple for
    blind_spots and data_date = today (UTC).
    """
    symbol, pairs = _parse_pairs(text)

    fields: Dict[str, Optional[float]] = {
        "call_resistance": None,
        "put_support": None,
        "hvl": None,
        "one_d_min": None,
        "one_d_max": None,
        "call_resistance_0dte": None,
        "put_support_0dte": None,
        "hvl_0dte": None,
        "gamma_wall_0dte": None,
    }
    gex_by_idx: Dict[int, float] = {}

    for raw_key, raw_val in pairs:
        norm = _normalize_key(raw_key)
        try:
            value = float(raw_val)
        except ValueError:
            raise ValueError(
                f"non-numeric value for key {raw_key!r}: {raw_val!r}"
            )

        if norm in ALIAS_MAP:
            fields[ALIAS_MAP[norm]] = value
            continue

        m = _GEX_RE.match(norm)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= 10:
                gex_by_idx[idx] = value
            else:
                logger.warning("GEX index out of range 1..10: %s", raw_key)
            continue

        logger.warning("unknown gamma key skipped: %r", raw_key)

    gex_levels = tuple(
        gex_by_idx[i] for i in sorted(gex_by_idx.keys())
    )

    return GammaLevels(
        symbol=symbol,
        data_date=datetime.now(timezone.utc).date(),
        call_resistance=fields["call_resistance"],
        put_support=fields["put_support"],
        hvl=fields["hvl"],
        one_d_min=fields["one_d_min"],
        one_d_max=fields["one_d_max"],
        call_resistance_0dte=fields["call_resistance_0dte"],
        put_support_0dte=fields["put_support_0dte"],
        hvl_0dte=fields["hvl_0dte"],
        gamma_wall_0dte=fields["gamma_wall_0dte"],
        gex_levels=gex_levels,
        blind_spots=(),
        loaded_at=datetime.now(timezone.utc),
    )


def parse_blind_spots_paste(text: str) -> Tuple[float, ...]:
    """
    Extract BL 1..10 in index order. Missing BLs just shrink the tuple
    — they are not an error. Unknown keys are logged and skipped.
    """
    if not text.strip():
        return ()

    try:
        _, pairs = _parse_pairs(text)
    except ValueError:
        raise

    bl_by_idx: Dict[int, float] = {}
    for raw_key, raw_val in pairs:
        norm = _normalize_key(raw_key)
        m = _BL_RE.match(norm)
        if not m:
            logger.warning("non-BL key in blind-spots paste: %r", raw_key)
            continue
        idx = int(m.group(1))
        if not 1 <= idx <= 10:
            logger.warning("BL index out of range 1..10: %s", raw_key)
            continue
        try:
            bl_by_idx[idx] = float(raw_val)
        except ValueError:
            raise ValueError(
                f"non-numeric value for {raw_key!r}: {raw_val!r}"
            )

    return tuple(bl_by_idx[i] for i in sorted(bl_by_idx.keys()))


# ─── File loading (cached by path+mtime) ────────────────────────────

_CACHE: Dict[Tuple[str, float], GammaLevels] = {}


def _parse_date_from_name(name: str) -> Optional[date]:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})_", name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def load_gamma_for_date(
    data_dir: Path, target_date: date
) -> Optional[GammaLevels]:
    """
    Load YYYY-MM-DD_levels.txt (+ _blind.txt if present) for a date.
    Returns None if levels file missing. Raises ValueError on parse
    failure with clear context.

    Cached by (levels_path, mtime) so repeated calls are cheap.
    """
    data_dir = Path(data_dir)
    stem = target_date.isoformat()
    levels_path = data_dir / f"{stem}_levels.txt"
    blind_path = data_dir / f"{stem}_blind.txt"

    if not levels_path.exists():
        return None

    levels_mtime = levels_path.stat().st_mtime
    blind_mtime = blind_path.stat().st_mtime if blind_path.exists() else 0.0
    cache_key = (str(levels_path.resolve()), levels_mtime + blind_mtime)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        levels_text = levels_path.read_text(encoding="utf-8")
        base = parse_gamma_paste(levels_text)
    except ValueError as e:
        raise ValueError(f"failed to parse {levels_path.name}: {e}") from e

    blind_spots: Tuple[float, ...] = ()
    if blind_path.exists():
        try:
            blind_text = blind_path.read_text(encoding="utf-8")
            blind_spots = parse_blind_spots_paste(blind_text)
        except ValueError as e:
            raise ValueError(
                f"failed to parse {blind_path.name}: {e}"
            ) from e

    # Rebuild with correct data_date (from filename) and blind_spots.
    result = GammaLevels(
        symbol=base.symbol,
        data_date=target_date,
        call_resistance=base.call_resistance,
        put_support=base.put_support,
        hvl=base.hvl,
        one_d_min=base.one_d_min,
        one_d_max=base.one_d_max,
        call_resistance_0dte=base.call_resistance_0dte,
        put_support_0dte=base.put_support_0dte,
        hvl_0dte=base.hvl_0dte,
        gamma_wall_0dte=base.gamma_wall_0dte,
        gex_levels=base.gex_levels,
        blind_spots=blind_spots,
        loaded_at=base.loaded_at,
    )
    _CACHE[cache_key] = result
    return result


def load_latest_gamma(
    data_dir: Path, max_age_hours: int = 30
) -> Optional[GammaLevels]:
    """
    Find the most recent *_levels.txt in data_dir and parse it.

    Returns None (with WARN log) if the file is older than
    max_age_hours. Returns None silently if no files exist.
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        logger.warning("gamma data dir does not exist: %s", data_dir)
        return None

    candidates = sorted(data_dir.glob("*_levels.txt"))
    if not candidates:
        return None

    # Pick by filename date (deterministic) then fall back to mtime.
    dated = [
        (d, p) for p in candidates
        if (d := _parse_date_from_name(p.name)) is not None
    ]
    if dated:
        dated.sort(key=lambda x: x[0])
        target_date, chosen = dated[-1]
    else:
        chosen = max(candidates, key=lambda p: p.stat().st_mtime)
        target_date = date.fromtimestamp(chosen.stat().st_mtime)

    file_mtime = datetime.fromtimestamp(
        chosen.stat().st_mtime, tz=timezone.utc
    )
    age = datetime.now(timezone.utc) - file_mtime
    if age > timedelta(hours=max_age_hours):
        logger.warning(
            "latest gamma file %s is %.1fh old (> %dh threshold) — "
            "ignoring",
            chosen.name, age.total_seconds() / 3600, max_age_hours,
        )
        return None

    return load_gamma_for_date(data_dir, target_date)
