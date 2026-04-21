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
from typing import Dict, List, Optional, Tuple

from enum import Enum

logger = logging.getLogger("MenthorQGamma")


# ─── Constants (Phase 5 moves these to config/settings.py) ──────────

# Phase 5 (2026-04-20): sourced from config/settings.py. Module-level
# aliases are preserved so tests and direct callers see the same names;
# tuning is now a one-line change in config/settings.py, no re-push.
from config.settings import (
    TICK_SIZE,
    MENTHORQ_HVL_BUFFER_TICKS as HVL_TRANSITION_BUFFER_TICKS,
    MENTHORQ_WALL_BUFFER_TICKS as WALL_PROXIMITY_BUFFER_TICKS,
    MENTHORQ_NO_TRADE_INTO_WALL_TICKS as NO_TRADE_INTO_WALL_BUFFER_TICKS,
    MENTHORQ_NET_GEX_STRONG_THRESHOLD as NET_GEX_STRONG_THRESHOLD,
    MENTHORQ_NET_GEX_NORMAL_THRESHOLD as NET_GEX_NORMAL_THRESHOLD,
)


# ─── Data model ──────────────────────────────────────────────────────

class GammaRegime(Enum):
    # B27: 6-value enum driven by Net GEX magnitude (MenthorQ authoritative
    # signal), falling back to HVL proxy when Net GEX not in the paste.
    POSITIVE_STRONG = "positive_strong"
    POSITIVE_NORMAL = "positive_normal"
    NEUTRAL = "neutral"
    NEGATIVE_NORMAL = "negative_normal"
    NEGATIVE_STRONG = "negative_strong"
    UNKNOWN = "unknown"


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
    # B27: GEX magnitude + IV context. Optional — pre-B27 pastes and
    # pastes without these keys still parse cleanly with None.
    net_gex: Optional[float] = None
    total_gex: Optional[float] = None
    iv_30d: Optional[float] = None

    @property
    def is_complete(self) -> bool:
        """All Tier 1 wall-level fields populated. Net GEX / Total GEX / IV
        are regime-classification enrichment and NOT required for
        is_complete — walls remain the primary trading-decision data."""
        return all(v is not None for v in (
            self.hvl,
            self.hvl_0dte,
            self.call_resistance,
            self.call_resistance_0dte,
            self.put_support,
            self.put_support_0dte,
            self.gamma_wall_0dte,
        ))

    @property
    def has_net_gex_classification(self) -> bool:
        """True when regime can be classified from Net GEX magnitude
        (authoritative MenthorQ signal) rather than the HVL proxy."""
        return self.net_gex is not None


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
    # B27: GEX magnitudes and IV.
    "net gex": "net_gex",
    "netgex": "net_gex",
    "total gex": "total_gex",
    "totalgex": "total_gex",
    "iv": "iv_30d",
    "iv 30d": "iv_30d",
    "iv30d": "iv_30d",
}

# Keys whose values accept K/M/B suffix expansion — magnitude-style fields
# (GEX absolute values). IV stays plain-numeric.
_MAGNITUDE_KEYS = frozenset({"net_gex", "total_gex"})

_SUFFIX_MULTIPLIERS = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}

_GEX_RE = re.compile(r"^gex\s*(\d{1,2})$")
_BL_RE = re.compile(r"^bl\s*(\d{1,2})$")
_SYMBOL_RE = re.compile(r"^\$([A-Z0-9]+)\s*:\s*(.*)$", re.DOTALL)


def _normalize_key(raw: str) -> str:
    """Lowercase, collapse internal whitespace, strip ends."""
    return re.sub(r"\s+", " ", raw.strip().lower())


def _parse_numeric_value(raw: str, allow_suffix: bool = False) -> float:
    """
    Parse a comma-separated value token into a float.

    If allow_suffix is True, accepts K / M / B suffix multipliers (case-
    insensitive), e.g. "3.92M" → 3_920_000. Leading +/- is preserved.
    Raises ValueError on unparseable input.
    """
    s = raw.strip()
    if not s:
        raise ValueError(f"empty value token")
    if allow_suffix and s[-1].upper() in _SUFFIX_MULTIPLIERS:
        mult = _SUFFIX_MULTIPLIERS[s[-1].upper()]
        return float(s[:-1]) * mult
    return float(s)


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
        # B27
        "net_gex": None,
        "total_gex": None,
        "iv_30d": None,
    }
    gex_by_idx: Dict[int, float] = {}

    for raw_key, raw_val in pairs:
        norm = _normalize_key(raw_key)

        attr = ALIAS_MAP.get(norm)
        try:
            value = _parse_numeric_value(
                raw_val,
                allow_suffix=(attr in _MAGNITUDE_KEYS),
            )
        except ValueError:
            raise ValueError(
                f"non-numeric value for key {raw_key!r}: {raw_val!r}"
            )

        if attr is not None:
            fields[attr] = value
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
        net_gex=fields["net_gex"],
        total_gex=fields["total_gex"],
        iv_30d=fields["iv_30d"],
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
        net_gex=base.net_gex,
        total_gex=base.total_gex,
        iv_30d=base.iv_30d,
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


# ─── Phase 3: regime classification + decision functions ────────────

# Wall-set attribute names, in preference order (0DTE preferred).
_LONG_WALL_ATTRS = (
    "call_resistance_0dte",
    "gamma_wall_0dte",
    "call_resistance",
    "one_d_max",
)
_SHORT_WALL_ATTRS = (
    "put_support_0dte",
    "put_support",
    "one_d_min",
)
# Preferred protective-stop walls by direction (0DTE first).
_LONG_STOP_ATTRS = ("put_support_0dte", "put_support")
_SHORT_STOP_ATTRS = ("call_resistance_0dte", "call_resistance")


def _pts_to_ticks(points: float) -> float:
    return points / TICK_SIZE


def _effective_hvl(levels: GammaLevels) -> Optional[float]:
    """0DTE HVL preferred; monthly HVL fallback."""
    if levels.hvl_0dte is not None:
        return levels.hvl_0dte
    return levels.hvl


def classify_regime(
    price: float, levels: Optional[GammaLevels]
) -> GammaRegime:
    """
    B27: Net GEX magnitude is the primary (authoritative MenthorQ) signal.
    HVL position is the fallback when Net GEX is absent from the paste.

    Net GEX primary path:
      net_gex >  +STRONG    → POSITIVE_STRONG
      net_gex >  +NORMAL    → POSITIVE_NORMAL
      |net_gex| <= NORMAL   → NEUTRAL
      net_gex >= -STRONG    → NEGATIVE_NORMAL
      net_gex <  -STRONG    → NEGATIVE_STRONG

    HVL fallback path (when levels.net_gex is None):
      price >= hvl + buffer → POSITIVE_NORMAL
      price <= hvl - buffer → NEGATIVE_NORMAL
      else                  → NEUTRAL
      no hvl                → UNKNOWN
    """
    if levels is None:
        return GammaRegime.UNKNOWN

    if levels.net_gex is not None:
        ng = levels.net_gex
        if ng > NET_GEX_STRONG_THRESHOLD:
            return GammaRegime.POSITIVE_STRONG
        if ng > NET_GEX_NORMAL_THRESHOLD:
            return GammaRegime.POSITIVE_NORMAL
        if ng >= -NET_GEX_NORMAL_THRESHOLD:
            return GammaRegime.NEUTRAL
        if ng >= -NET_GEX_STRONG_THRESHOLD:
            return GammaRegime.NEGATIVE_NORMAL
        return GammaRegime.NEGATIVE_STRONG

    # Fallback — best-effort classification from HVL proxy.
    logger.debug("classifier using HVL proxy (no Net GEX in paste)")
    hvl = _effective_hvl(levels)
    if hvl is None:
        return GammaRegime.UNKNOWN

    buffer_pts = HVL_TRANSITION_BUFFER_TICKS * TICK_SIZE
    if price >= hvl + buffer_pts:
        return GammaRegime.POSITIVE_NORMAL
    if price <= hvl - buffer_pts:
        return GammaRegime.NEGATIVE_NORMAL
    return GammaRegime.NEUTRAL


def distance_to_nearest_wall(
    price: float, direction: str, levels: Optional[GammaLevels]
) -> Tuple[str, float]:
    """
    Distance (in ticks) to the nearest wall the trade would approach.

    LONG  → scans walls ABOVE price (resistance-family).
    SHORT → scans walls BELOW price (support-family).

    Returns (wall_attr_name, distance_in_ticks). When no opposing
    wall exists in the correct direction, returns ("none", 9999).
    """
    if levels is None:
        return ("none", 9999.0)

    direction = direction.upper()
    if direction == "LONG":
        attrs = _LONG_WALL_ATTRS
        above = True
    elif direction == "SHORT":
        attrs = _SHORT_WALL_ATTRS
        above = False
    else:
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")

    best_name = "none"
    best_ticks = 9999.0
    for attr in attrs:
        val = getattr(levels, attr, None)
        if val is None:
            continue
        if above and val <= price:
            continue
        if not above and val >= price:
            continue
        ticks = _pts_to_ticks(abs(val - price))
        if ticks < best_ticks:
            best_ticks = ticks
            best_name = attr

    return (best_name, best_ticks)


def is_price_near_level(
    price: float,
    levels: Optional[GammaLevels],
    buffer_ticks: Optional[int] = None,
) -> List[Tuple[str, float, str]]:
    """
    Return all named levels within buffer_ticks of price, regardless
    of which side they sit on. Each entry: (name, distance_ticks, side)
    where side is 'above' or 'below'.

    Used by is_entry_into_wall for countertrend-reversal-zone logic:
    e.g. SHORT entered just below a support level has price about to
    be bounced back up into the short.
    """
    if levels is None:
        return []
    if buffer_ticks is None:
        buffer_ticks = WALL_PROXIMITY_BUFFER_TICKS

    out: List[Tuple[str, float, str]] = []
    for attr in (
        "call_resistance", "put_support", "hvl",
        "one_d_min", "one_d_max",
        "call_resistance_0dte", "put_support_0dte", "hvl_0dte",
        "gamma_wall_0dte",
    ):
        val = getattr(levels, attr, None)
        if val is None:
            continue
        ticks = _pts_to_ticks(abs(val - price))
        if ticks <= buffer_ticks:
            side = "above" if val > price else "below"
            out.append((attr, ticks, side))
    return out


def is_entry_into_wall(
    price: float, direction: str, levels: Optional[GammaLevels]
) -> Optional[str]:
    """
    Combined entry filter. Rejects a candidate signal when either:

    (1) Direction-of-travel: next opposing wall in the trade's path
        is within NO_TRADE_INTO_WALL_BUFFER_TICKS (would slam into it).
    (2) Countertrend-reversal zone:
          LONG just BELOW a resistance/gamma-wall → rejection bounce.
          SHORT just ABOVE a support              → bounce back up
            into the short.

    Returns the offending wall's attr name, or None if entry is safe.
    """
    if levels is None:
        return None

    # Check 1 — direction of travel.
    name, ticks = distance_to_nearest_wall(price, direction, levels)
    if name != "none" and ticks < NO_TRADE_INTO_WALL_BUFFER_TICKS:
        return name

    # Check 2 — countertrend reversal proximity.
    direction_u = direction.upper()
    for near_name, _dist, side in is_price_near_level(price, levels):
        lname = near_name.lower()
        if direction_u == "SHORT" and side == "above" and "support" in lname:
            return near_name
        if direction_u == "LONG" and side == "below" and (
            "resistance" in lname or "gamma_wall" in lname
        ):
            return near_name
    return None


def natural_stop_for_entry(
    direction: str,
    entry_price: float,
    levels: Optional[GammaLevels],
    min_stop_ticks: int = 8,
    max_stop_ticks: int = 40,
) -> Optional[float]:
    """
    Stop just past the protective wall on the opposite side of entry.
    LONG: 1 tick BELOW put_support_0dte (or put_support).
    SHORT: 1 tick ABOVE call_resistance_0dte (or call_resistance).

    Returns None if no protective wall sits within
    [min_stop_ticks, max_stop_ticks] of entry.
    """
    if levels is None:
        return None
    direction = direction.upper()

    if direction == "LONG":
        attrs = _LONG_STOP_ATTRS
        sign = -1  # stop below entry
    elif direction == "SHORT":
        attrs = _SHORT_STOP_ATTRS
        sign = +1  # stop above entry
    else:
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")

    for attr in attrs:
        wall = getattr(levels, attr, None)
        if wall is None:
            continue
        # Wall must be on the correct side of entry.
        if sign < 0 and wall >= entry_price:
            continue
        if sign > 0 and wall <= entry_price:
            continue
        stop = wall + sign * TICK_SIZE  # 1 tick past the wall
        distance_ticks = _pts_to_ticks(abs(entry_price - stop))
        if min_stop_ticks <= distance_ticks <= max_stop_ticks:
            return stop
    return None


def find_level_clusters(
    levels: Optional[GammaLevels], tolerance_ticks: int = 12
) -> List[dict]:
    """
    Find clusters of levels within tolerance_ticks of each other.
    Returns list sorted by cluster size descending; 2+ members →
    conviction="HIGH", single → "LOW" (but singletons are excluded).
    """
    if levels is None:
        return []

    points: List[Tuple[str, float]] = []
    for attr in (
        "call_resistance", "put_support", "hvl",
        "one_d_min", "one_d_max",
        "call_resistance_0dte", "put_support_0dte", "hvl_0dte",
        "gamma_wall_0dte",
    ):
        val = getattr(levels, attr, None)
        if val is not None:
            points.append((attr, float(val)))
    for i, val in enumerate(levels.gex_levels, start=1):
        points.append((f"gex_{i}", float(val)))
    for i, val in enumerate(levels.blind_spots, start=1):
        points.append((f"bl_{i}", float(val)))

    if not points:
        return []

    points.sort(key=lambda x: x[1])
    tolerance_pts = tolerance_ticks * TICK_SIZE

    # Single-pass grouping: adjacent values within tolerance go in
    # the same group (transitive — typical for narrow clusters).
    groups: List[List[Tuple[str, float]]] = []
    current: List[Tuple[str, float]] = [points[0]]
    for name, val in points[1:]:
        if val - current[-1][1] <= tolerance_pts:
            current.append((name, val))
        else:
            groups.append(current)
            current = [(name, val)]
    groups.append(current)

    clusters = []
    for group in groups:
        if len(group) < 2:
            continue
        vals = [v for _, v in group]
        clusters.append({
            "center": sum(vals) / len(vals),
            "members": [name for name, _ in group],
            "values": vals,
            "conviction": "HIGH" if len(group) >= 2 else "LOW",
        })
    clusters.sort(key=lambda c: len(c["members"]), reverse=True)
    return clusters


def is_at_hvl_gravity(
    price: float,
    levels: Optional[GammaLevels],
    buffer_ticks: int = 12,
) -> bool:
    """Within buffer_ticks of HVL (0DTE preferred) → positive-gamma pin."""
    if levels is None:
        return False
    hvl = _effective_hvl(levels)
    if hvl is None:
        return False
    return _pts_to_ticks(abs(price - hvl)) <= buffer_ticks


def regime_multipliers(regime: GammaRegime) -> Dict[str, float]:
    """
    Size / stop-width / target-RR multipliers per regime.
    Tighter stops + smaller targets in positive-gamma (mean-revert);
    wider in negative (momentum). Reduced size in NEUTRAL (uncertainty).
    """
    return {
        GammaRegime.POSITIVE_STRONG: {"size": 1.0, "stop": 0.7, "target_rr": 0.6},
        GammaRegime.POSITIVE_NORMAL: {"size": 1.0, "stop": 0.8, "target_rr": 0.7},
        GammaRegime.NEUTRAL:         {"size": 0.7, "stop": 1.0, "target_rr": 1.0},
        GammaRegime.NEGATIVE_NORMAL: {"size": 1.0, "stop": 1.2, "target_rr": 1.3},
        GammaRegime.NEGATIVE_STRONG: {"size": 1.0, "stop": 1.5, "target_rr": 1.6},
        GammaRegime.UNKNOWN:         {"size": 1.0, "stop": 1.0, "target_rr": 1.0},
    }[regime]
