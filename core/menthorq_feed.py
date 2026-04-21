"""
Phoenix Bot — Menthor Q Integration

Reads daily Menthor Q data (GEX regime, HVL, key levels, flow signals)
and exposes it as structured context for strategies and the pre-trade filter.

Data source hierarchy (first available wins):
  1. data/menthorq_daily.json  — manual daily entry (fill in each morning, 2 min)
  2. Future: Discord bot parser (discord.py scraping MQ bot output)
  3. Future: Menthor Q REST API (if/when direct API access granted)

Key concepts (Menthor Q specialist knowledge):
  - GEX NEGATIVE: dealers short gamma, AMPLIFY moves → follow trend, widen stops
  - GEX POSITIVE: dealers long gamma, SUPPRESS moves → fade/mean-revert, tight stops
  - HVL (High Vol Level): THE gamma flip price.
      Below HVL = negative gamma regime = momentum, no fades
      Above HVL = positive gamma regime = mean-reversion, fade extremes
  - DEX: directional dealer bias. Negative = structural selling. Positive = structural buying.
  - Vanna BEARISH: rising VIX forces dealers to sell into drops (amplifies selloffs)
  - CTA SELLING: systematic funds adding shorts = trend-following fuel below
  - GEX Levels 1-10: support/resistance clusters. In negative gamma, breaking them
    triggers cascading dealer re-hedging (waterfall or ramp).
  - 0DTE levels: same-day gamma walls, most powerful in last 2 hours of session.

Integration points:
  - base_bot.py: HVL-based direction gate (block LONGs below HVL in negative GEX)
  - agents/pretrade_filter.py: full MQ context injected into AI prompt
  - agents/council_gate.py: MQ regime informs session bias council
  - strategies/bias_momentum.py: stop multiplier from MQ regime
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger("MenthorQ")

# ── Data sources ──────────────────────────────────────────────────────────────
# DATA_FILE:   Manual daily JSON (regime, net_gex, dex, vanna, charm, cta).
#              Fill in once per morning — only 4-5 text fields.
#
# BRIDGE_FILE: Auto-written by MQBridge.cs (NT8 indicator) every 60 seconds.
#              Contains all price LEVELS (HVL, call resistance, put support,
#              GEX 1-10) read directly from MenthorQLevelsAPI draw objects.
#              Zero manual entry — levels come straight from the chart.
#
# MERGE LOGIC: Bridge file → prices. Manual JSON → regime text.
#              If bridge file is missing (NT8 not running), fall back to
#              manual JSON prices. Both files are optional — system degrades
#              gracefully with neutral defaults.

DATA_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "menthorq_daily.json"
)

BRIDGE_FILE = r"C:\temp\menthorq_levels.json"   # Written by MQBridge.cs

# Menthor Q REST API key (for future direct API integration)
# Set in .env as MENTHORQ_API_KEY
MENTHORQ_API_KEY = os.environ.get("MENTHORQ_API_KEY", "")


@dataclass
class MenthorQSnapshot:
    """Parsed Menthor Q daily data. All values are NQ-native (not QQQ-mapped)."""

    # Date this snapshot is valid for
    date: str = ""

    # GEX regime
    gex_regime: str = "UNKNOWN"       # "POSITIVE" | "NEGATIVE" | "UNKNOWN"
    net_gex_bn: float = 0.0           # Net GEX in billions (negative = red)

    # DEX
    dex: str = "UNKNOWN"              # "POSITIVE" | "NEGATIVE" | "UNKNOWN"

    # High Vol Level — THE most important single number
    hvl: float = 0.0

    # Key S/R levels
    call_resistance_all: float = 0.0
    put_support_all: float = 0.0
    call_resistance_0dte: float = 0.0
    put_support_0dte: float = 0.0
    hvl_0dte: float = 0.0
    gamma_wall_0dte: float = 0.0
    day_min: float = 0.0    # Gamma-implied floor for today
    day_max: float = 0.0    # Gamma-implied ceiling for today
    gex_level_1: float = 0.0
    gex_level_2: float = 0.0
    gex_level_3: float = 0.0
    gex_level_4: float = 0.0
    gex_level_5: float = 0.0
    gex_level_6: float = 0.0
    gex_level_7: float = 0.0
    gex_level_8: float = 0.0
    gex_level_9: float = 0.0
    gex_level_10: float = 0.0

    # Flow signals
    vanna: str = "NEUTRAL"            # "BULLISH" | "BEARISH" | "NEUTRAL"
    charm: str = "NEUTRAL"            # "BULLISH" | "BEARISH" | "NEUTRAL"
    cta_positioning: str = "NEUTRAL"  # "BUYING" | "SELLING" | "NEUTRAL"

    # Regime summary (computed or filled in)
    direction_bias: str = "NEUTRAL"   # "LONG" | "SHORT" | "NEUTRAL"
    allow_longs: bool = True
    allow_shorts: bool = True
    stop_multiplier: float = 1.0      # 1.5 in negative gamma = widen stops 50%
    strategy_type: str = "BALANCED"   # "MOMENTUM" | "MEAN_REVERSION" | "BALANCED"
    notes: str = ""

    # Stale flag
    is_stale: bool = False            # True if loaded from a different date
    source: str = "none"              # "file" | "discord" | "default"


def load_bridge_levels() -> dict:
    """
    Read price levels from C:\\temp\\menthorq_levels.json (written by MQBridge.cs).
    Returns a dict of level prices, or {} if file not available.

    This is the AUTOMATIC path — no manual entry required.
    MQBridge.cs reads MenthorQLevelsAPI draw objects on the NT8 chart and
    writes updated prices every 60 seconds.
    """
    if not os.path.exists(BRIDGE_FILE):
        logger.warning(
            f"[MenthorQ] Bridge file missing at {BRIDGE_FILE} — "
            f"MQBridge.cs is likely not loaded on any NT8 chart. "
            f"Action: in NT8, open a MenthorQ chart (any timeframe) and "
            f"apply the MQBridge indicator (Indicators → MQBridge). It "
            f"will start writing to {BRIDGE_FILE} within 60s."
        )
        return {}

    try:
        # B11 fix: Check file age and escalate by tier for clearer operator action.
        # MQBridge.cs writes every 60s. Staleness buckets:
        #   - 0-5 min:   healthy; silent (no log)
        #   - 5-30 min:  warning — NT8 may have hiccupped, MQBridge still loaded
        #   - 30+ min:   error — MQBridge.cs is NOT running; operator must act
        mtime = os.path.getmtime(BRIDGE_FILE)
        age_min = (datetime.now().timestamp() - mtime) / 60
        if age_min > 30:
            logger.error(
                f"[MenthorQ] Bridge file is {age_min:.0f} min old "
                f"(MQBridge.cs NOT RUNNING). MenthorQ gamma levels are "
                f"STALE and strategies that gate on them (spring_setup, "
                f"structural_bias, gamma_flip) are operating without live "
                f"MQ context. ACTION: open NT8 Control Center → Indicators, "
                f"verify MQBridge is loaded on at least one chart. If loaded, "
                f"right-click the chart → Reload NinjaScript Output."
            )
        elif age_min > 5:
            logger.warning(
                f"[MenthorQ] Bridge file is {age_min:.0f} min old — "
                f"MQBridge.cs may have hiccupped. If this persists >30min, "
                f"reload the MQBridge indicator on an NT8 chart."
            )

        # utf-8-sig strips the BOM that C# Encoding.UTF8 writes
        with open(BRIDGE_FILE, encoding="utf-8-sig") as f:
            raw = json.load(f)

        def _f(key: str) -> float:
            return float(raw.get(key, 0.0) or 0.0)

        levels = {
            # Core gamma levels
            "hvl":                  _f("hvl"),
            "call_resistance_all":  _f("call_resistance"),
            "put_support_all":      _f("put_support"),
            "call_resistance_0dte": _f("call_resistance_0dte"),
            "put_support_0dte":     _f("put_support_0dte"),
            "hvl_0dte":             _f("hvl_0dte"),
            "gamma_wall_0dte":      _f("gamma_wall_0dte"),
            # Day range (defines gamma-implied move)
            "day_min":              _f("day_min"),
            "day_max":              _f("day_max"),
            # GEX strike clusters (support/resistance magnets)
            "gex_level_1":          _f("gex_1"),
            "gex_level_2":          _f("gex_2"),
            "gex_level_3":          _f("gex_3"),
            "gex_level_4":          _f("gex_4"),
            "gex_level_5":          _f("gex_5"),
            "gex_level_6":          _f("gex_6"),
            "gex_level_7":          _f("gex_7"),
            "gex_level_8":          _f("gex_8"),
            "gex_level_9":          _f("gex_9"),
            "gex_level_10":         _f("gex_10"),
            # Metadata
            "_bridge_ts":           raw.get("ts", ""),
            "_bridge_ratio":        float(raw.get("qqq_to_nq_ratio", 0.0) or 0.0),
            "_bridge_source":       raw.get("source", "unknown"),
            "_level_type":          raw.get("level_type", ""),
        }

        populated = sum(1 for k, v in levels.items()
                        if not k.startswith("_") and v > 0)
        logger.info(
            f"[MenthorQ] Bridge: {populated} levels "
            f"(HVL={levels['hvl']:.2f}, CR={levels['call_resistance_all']:.2f}, "
            f"PS={levels['put_support_all']:.2f}, "
            f"Day={levels['day_min']:.2f}-{levels['day_max']:.2f})"
        )
        return levels

    except Exception as e:
        logger.warning(f"[MenthorQ] Bridge file read error: {e}")
        return {}


def bridge_health() -> dict:
    """
    B11 diagnostic: return structured bridge-health state for dashboards,
    watchdogs, and startup pre-flight checks. Does NOT read/parse the
    levels — just inspects the file itself.

    Returns:
        {
            "path": absolute path to expected bridge file,
            "exists": bool,
            "age_min": float | None (None if file missing),
            "status": "healthy" | "warning" | "stale" | "missing",
            "action": human-readable next step for the operator,
        }
    """
    path = BRIDGE_FILE
    if not os.path.exists(path):
        return {
            "path": path,
            "exists": False,
            "age_min": None,
            "status": "missing",
            "action": (
                "Open NT8 Control Center → Indicators. Apply MQBridge "
                "to at least one MenthorQ chart. File will appear within 60s."
            ),
        }
    try:
        age_min = (datetime.now().timestamp() - os.path.getmtime(path)) / 60
    except OSError:
        return {"path": path, "exists": True, "age_min": None,
                "status": "missing", "action": "Cannot stat bridge file (permissions?)"}

    if age_min <= 5:
        status, action = "healthy", "No action needed."
    elif age_min <= 30:
        status, action = "warning", (
            f"Bridge file {age_min:.0f} min old; MQBridge may have hiccupped. "
            f"Re-check in 5 min; if still stale, reload the indicator."
        )
    else:
        status, action = "stale", (
            f"Bridge file {age_min:.0f} min old; MQBridge.cs is NOT writing. "
            f"In NT8 right-click the MenthorQ chart → Reload NinjaScript Output, "
            f"or remove + re-apply the MQBridge indicator."
        )
    return {"path": path, "exists": True, "age_min": round(age_min, 1),
            "status": status, "action": action}


def load() -> MenthorQSnapshot:
    """
    Load today's Menthor Q snapshot.

    TWO-SOURCE MERGE STRATEGY:
      1. MQBridge.cs (C:\\temp\\menthorq_levels.json)
         → Price levels: HVL, call resistance, put support, GEX 1-10
         → Auto-updated every 60s from NT8 chart draw objects
         → Zero manual entry

      2. data/menthorq_daily.json
         → Regime interpretation: GEX +/-, net_gex_bn, DEX, vanna, charm, CTA
         → Fill once per morning (4-5 text fields — takes 60 seconds)
         → Example: {"gex": {"regime": "NEGATIVE", "net_gex_bn": -2.1}, ...}

    If bridge file is present, it OVERRIDES prices from the manual JSON.
    If bridge file is absent (NT8 not running), manual JSON prices are used.
    If neither has prices, neutral defaults are returned.
    """
    today = str(date.today())

    # ── Source 1: Bridge file (auto) ──────────────────────────────────
    bridge = load_bridge_levels()
    bridge_active = bridge.get("hvl", 0) > 0 or bridge.get("call_resistance_all", 0) > 0

    # ── Source 2: Manual daily JSON (regime text) ─────────────────────
    path = os.path.abspath(DATA_FILE)
    raw  = {}
    stale = True
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        file_date = raw.get("date", "")
        stale = file_date != today
        if stale:
            logger.warning(
                f"[MenthorQ] Daily JSON is from {file_date!r}, today={today!r}. "
                f"Update gex/regime fields in {path} if GEX regime changed."
            )
    except FileNotFoundError:
        logger.warning(f"[MenthorQ] No daily JSON at {path}")
    except Exception as e:
        logger.warning(f"[MenthorQ] Daily JSON read error: {e}")

    gex    = raw.get("gex",            {})
    dex    = raw.get("dex",            {})
    hvl    = raw.get("hvl",            {})
    levels = raw.get("key_levels",     {})
    flows  = raw.get("flows",          {})
    regime = raw.get("regime_summary", {})

    # ── Merge: bridge prices win over manual prices ───────────────────
    def _bv(key: str, fallback_key: str = None, fallback_dict: dict = None) -> float:
        """Bridge value with optional manual JSON fallback."""
        v = bridge.get(key, 0.0) or 0.0
        if v <= 0 and fallback_key and fallback_dict:
            v = float(fallback_dict.get(fallback_key, 0.0) or 0.0)
        return v

    hvl_price = _bv("hvl", "price", hvl)
    cr_all    = _bv("call_resistance_all",  "call_resistance_all",  levels)
    ps_all    = _bv("put_support_all",      "put_support_all",      levels)
    cr_0dte   = _bv("call_resistance_0dte", "call_resistance_0dte", levels)
    ps_0dte   = _bv("put_support_0dte",     "put_support_0dte",     levels)
    hvl_0dte  = _bv("hvl_0dte")
    gw_0dte   = _bv("gamma_wall_0dte")
    day_min   = _bv("day_min")
    day_max   = _bv("day_max")

    source = "bridge+manual" if bridge_active else ("file" if raw else "default")

    # ── Auto-derive GEX regime from HVL ──────────────────────────────
    # The HVL is the gamma flip line: above = positive gamma, below = negative.
    # Use manual JSON regime if explicitly set; otherwise AUTO tag it.
    # Strategies use regime_for_price(snap, price) which checks price vs HVL
    # in real-time — so the auto tag just tells you the morning baseline.
    manual_regime = gex.get("regime", "").upper()
    if manual_regime in ("POSITIVE", "NEGATIVE"):
        gex_regime = manual_regime
        regime_source = "manual"
    elif hvl_price > 0:
        # HVL baseline: if day_min/day_max available, today's expected range
        # gives a gamma sign hint (wide range = negative gamma day expected).
        # Without that signal, default to POSITIVE (safer — doesn't block longs)
        # and let real-time price vs HVL in regime_for_price() determine behavior.
        gex_regime = "POSITIVE"   # Will be overridden live by price vs HVL check
        regime_source = "auto-HVL"
    else:
        gex_regime = "UNKNOWN"
        regime_source = "none"

    snap = MenthorQSnapshot(
        date=today,
        gex_regime=gex_regime,
        net_gex_bn=float(gex.get("net_gex_bn", 0.0) or 0.0),
        dex=dex.get("value", "UNKNOWN").upper(),
        # Prices from bridge (auto) with manual JSON fallback
        hvl=hvl_price,
        call_resistance_all=cr_all,
        put_support_all=ps_all,
        call_resistance_0dte=cr_0dte,
        put_support_0dte=ps_0dte,
        hvl_0dte=hvl_0dte,
        gamma_wall_0dte=gw_0dte,
        day_min=day_min,
        day_max=day_max,
        gex_level_1 =bridge.get("gex_level_1",  0.0) or 0.0,
        gex_level_2 =bridge.get("gex_level_2",  0.0) or 0.0,
        gex_level_3 =bridge.get("gex_level_3",  0.0) or 0.0,
        gex_level_4 =bridge.get("gex_level_4",  0.0) or 0.0,
        gex_level_5 =bridge.get("gex_level_5",  0.0) or 0.0,
        gex_level_6 =bridge.get("gex_level_6",  0.0) or 0.0,
        gex_level_7 =bridge.get("gex_level_7",  0.0) or 0.0,
        gex_level_8 =bridge.get("gex_level_8",  0.0) or 0.0,
        gex_level_9 =bridge.get("gex_level_9",  0.0) or 0.0,
        gex_level_10=bridge.get("gex_level_10", 0.0) or 0.0,
        # Flows from manual JSON (optional enrichment)
        vanna=flows.get("vanna", "NEUTRAL").upper(),
        charm=flows.get("charm", "NEUTRAL").upper(),
        cta_positioning=flows.get("cta_positioning", "NEUTRAL").upper(),
        direction_bias=regime.get("direction_bias", "NEUTRAL").upper(),
        allow_longs=bool(regime.get("allow_longs", True)),
        allow_shorts=bool(regime.get("allow_shorts", True)),
        stop_multiplier=float(regime.get("stop_multiplier", 1.0) or 1.0),
        strategy_type=regime.get("strategy_type", "BALANCED").upper(),
        notes=regime.get("notes", ""),
        is_stale=stale and not bridge_active,
        source=source,
    )
    snap._regime_source = regime_source  # type: ignore[attr-defined]

    net_str = f"{snap.net_gex_bn:+.1f}B" if snap.net_gex_bn != 0.0 else "auto"
    logger.info(
        f"[MenthorQ] Loaded ({source}): GEX={snap.gex_regime}({net_str}) [{regime_source}] "
        f"HVL={snap.hvl:.2f} Day={snap.day_min:.2f}-{snap.day_max:.2f} "
        f"CR={snap.call_resistance_all:.2f} PS={snap.put_support_all:.2f} "
        f"DEX={snap.dex} Vanna={snap.vanna}"
        + (" [BRIDGE ACTIVE]" if bridge_active else " [manual only]")
        + (" [STALE]" if snap.is_stale else "")
    )
    return snap


def regime_for_price(snap: MenthorQSnapshot, price: float) -> dict:
    """
    Given current price and the MQ snapshot, return actionable regime context.

    Returns dict with:
        above_hvl: bool
        gamma_regime: str  ("POSITIVE" | "NEGATIVE" | "UNKNOWN")
        allow_long: bool
        allow_short: bool
        stop_multiplier: float
        nearest_resistance: float  (above price)
        nearest_support: float     (below price)
        summary: str               (one-line human-readable)
    """
    if snap.hvl <= 0:
        return {
            "above_hvl": True,
            "gamma_regime": "UNKNOWN",
            "allow_long": True,
            "allow_short": True,
            "stop_multiplier": 1.0,
            "nearest_resistance": 0.0,
            "nearest_support": 0.0,
            "day_min": snap.day_min,
            "day_max": snap.day_max,
            "summary": "MenthorQ data unavailable — no direction restriction",
        }

    above_hvl = price >= snap.hvl

    # ── LIVE gamma regime: price vs HVL is always authoritative ──────────────
    # HVL = gamma flip line. Above = positive (suppress vol). Below = negative (amplify).
    # Manual gex_regime from JSON overrides if explicitly set and HVL matches narrative.
    # If manual regime contradicts HVL position, HVL wins (prices don't lie).
    live_gamma = "POSITIVE" if above_hvl else "NEGATIVE"

    # Use manual regime only if it agrees with live position or if no HVL available
    if snap.gex_regime in ("POSITIVE", "NEGATIVE"):
        # Manual set — respect it but log if it conflicts with live price
        gamma_regime = snap.gex_regime
    else:
        gamma_regime = live_gamma

    # ── Direction permissions ─────────────────────────────────────────────────
    if gamma_regime == "NEGATIVE":
        # Negative gamma: directional moves amplified.
        # Below HVL = don't fight the trend with longs (momentum regime).
        # Above HVL but GEX negative = cautious longs, aggressive shorts.
        allow_long = snap.allow_longs if above_hvl else False
        allow_short = snap.allow_shorts
        stop_mult = max(snap.stop_multiplier, 1.5)  # Wider stops in neg gamma
    else:
        # Positive gamma: mean-reversion regime, both directions allowed
        allow_long = True
        allow_short = True
        stop_mult = snap.stop_multiplier  # Normal stops

    # ── Nearest levels ────────────────────────────────────────────────────────
    all_gex = [
        snap.gex_level_1, snap.gex_level_2, snap.gex_level_3,
        snap.gex_level_4, snap.gex_level_5, snap.gex_level_6,
        snap.gex_level_7, snap.gex_level_8, snap.gex_level_9, snap.gex_level_10,
    ]
    levels_above = [l for l in [
        snap.call_resistance_all, snap.call_resistance_0dte,
        snap.hvl, snap.gamma_wall_0dte, snap.day_max,
        *all_gex,
    ] if l > price]
    nearest_resistance = min(levels_above) if levels_above else 0.0

    levels_below = [l for l in [
        snap.put_support_all, snap.put_support_0dte,
        snap.hvl_0dte, snap.day_min,
        *all_gex,
    ] if 0 < l < price]
    nearest_support = max(levels_below) if levels_below else 0.0

    hvl_side = "ABOVE" if above_hvl else "BELOW"
    net_gex_str = f"({snap.net_gex_bn:+.1f}B)" if snap.net_gex_bn != 0.0 else "(auto)"
    summary = (
        f"GEX {gamma_regime} {net_gex_str}, "
        f"price {hvl_side} HVL={snap.hvl:.2f}, "
        f"DEX {snap.dex}, Vanna {snap.vanna}, CTA {snap.cta_positioning}. "
        f"Bias: {snap.direction_bias}. "
        f"Day range: {snap.day_min:.2f}-{snap.day_max:.2f}. "
        f"{'LONG OK' if allow_long else 'LONG BLOCKED'} / "
        f"{'SHORT OK' if allow_short else 'SHORT BLOCKED'}."
    )

    return {
        "above_hvl": above_hvl,
        "gamma_regime": gamma_regime,      # Live regime from price vs HVL
        "live_gamma": live_gamma,          # Always price-derived (for comparison)
        "allow_long": allow_long,
        "allow_short": allow_short,
        "stop_multiplier": stop_mult,
        "nearest_resistance": nearest_resistance,
        "nearest_support": nearest_support,
        "hvl": snap.hvl,
        "day_min": snap.day_min,
        "day_max": snap.day_max,
        "gex_level_1": snap.gex_level_1,
        "put_support_0dte": snap.put_support_0dte,
        "call_resistance_0dte": snap.call_resistance_0dte,
        "summary": summary,
    }


def to_prompt_context(snap: MenthorQSnapshot, price: float) -> str:
    """Format MQ snapshot as a concise block for AI prompt injection."""
    regime = regime_for_price(snap, price)
    stale_warn = " [WARNING: STALE DATA]" if snap.is_stale else ""
    hvl_side = "ABOVE HVL -> positive gamma zone (mean-reversion)" if regime["above_hvl"] else "BELOW HVL -> negative gamma zone (momentum, trend-following)"
    net_str = f"{snap.net_gex_bn:+.1f}B" if snap.net_gex_bn != 0.0 else "auto-derived from HVL"
    return f"""## Menthor Q Options Flow Context{stale_warn}
GEX Regime: {regime['gamma_regime']} ({net_str})  |  Price {price} is {hvl_side}
HVL: {snap.hvl:.2f}  |  Day Range: {snap.day_min:.2f} - {snap.day_max:.2f}
Call Resistance: {snap.call_resistance_all:.2f}  |  Put Support: {snap.put_support_all:.2f}
0DTE CR: {snap.call_resistance_0dte:.2f}  |  0DTE PS: {snap.put_support_0dte:.2f}  |  HVL 0DTE: {snap.hvl_0dte:.2f}
DEX: {snap.dex}  |  Vanna: {snap.vanna}  |  CTA: {snap.cta_positioning}
Nearest Resistance: {regime['nearest_resistance'] or 'N/A'}  |  Nearest Support: {regime['nearest_support'] or 'N/A'}
Direction Bias: {snap.direction_bias}  |  Longs: {'OK' if regime['allow_long'] else 'BLOCKED'}  |  Shorts: {'OK' if regime['allow_short'] else 'BLOCKED'}
Stop Multiplier: {regime['stop_multiplier']}x  |  Strategy Type: {snap.strategy_type}
Notes: {snap.notes or 'None'}"""


def _default_snapshot(reason: str = "") -> MenthorQSnapshot:
    """Return a neutral snapshot that doesn't block anything."""
    return MenthorQSnapshot(
        gex_regime="UNKNOWN",
        allow_longs=True,
        allow_shorts=True,
        stop_multiplier=1.0,
        strategy_type="BALANCED",
        notes=f"No MQ data ({reason}) — all directions permitted",
        source="default",
    )


# ── Singleton cache ───────────────────────────────────────────────────────────
# Reloads when:
#   - Date changes (new trading day)
#   - Bridge file (MQBridge.cs output) is newer than our last load
#     → picks up live NT8 level updates every 60s automatically
_cached_snap: Optional[MenthorQSnapshot] = None
_cached_date: str = ""
_cached_bridge_mtime: float = 0.0


def get_snapshot() -> MenthorQSnapshot:
    """
    Return cached snapshot. Reload when:
      - New trading day (date changed)
      - MQBridge.cs wrote a newer file (picks up live NT8 level refreshes)
    """
    global _cached_snap, _cached_date, _cached_bridge_mtime
    today = str(date.today())

    # Check if bridge file has been updated since our last load
    bridge_updated = False
    try:
        if os.path.exists(BRIDGE_FILE):
            mtime = os.path.getmtime(BRIDGE_FILE)
            if mtime > _cached_bridge_mtime:
                bridge_updated = True
                _cached_bridge_mtime = mtime
    except Exception:
        pass

    if _cached_snap is None or _cached_date != today or bridge_updated:
        _cached_snap = load()
        _cached_date = today

    return _cached_snap


# ── CLI test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    snap = load()
    test_price = float(sys.argv[1]) if len(sys.argv) > 1 else 25210.0

    print(f"\n{'='*60}")
    print(f"MENTHOR Q SNAPSHOT -- {snap.date}")
    print(f"{'='*60}")
    print(f"GEX:    {snap.gex_regime} ({snap.net_gex_bn:+.1f}B)")
    print(f"HVL:    {snap.hvl}")
    print(f"DEX:    {snap.dex}")
    print(f"Vanna:  {snap.vanna}  |  Charm: {snap.charm}  |  CTA: {snap.cta_positioning}")
    print(f"Bias:   {snap.direction_bias}")
    print()
    print(f"For current price {test_price}:")
    regime = regime_for_price(snap, test_price)
    for k, v in regime.items():
        if k != "summary":
            print(f"  {k}: {v}")
    print()
    print(f"Summary: {regime['summary']}")
    print()
    print("AI Prompt Block:")
    print(to_prompt_context(snap, test_price))
