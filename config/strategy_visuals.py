"""Visual identity for each active strategy.

Single source of truth for color + chart symbol used by:
  - dashboard/server.py /api/active-strategies endpoint
  - dashboard.html Strategy Legend card + strategy toggle dots
  - ninjatrader/PhoenixTradeMarkers.cs (consumes color via JSONL)

Hues are spread evenly around the HSL wheel so the 10 active strategies
are visually distinguishable side-by-side. When a strategy is added to
config/strategies.py with `retired != True`, add it here AND to the
config/strategies.py block in the same edit — `get_visual()` falls back
to neutral gray + circle for unknown names so dashboards never crash,
but the operator should not have to read NT8 charts in gray.
"""
from __future__ import annotations


# Hue spread: 10 active strategies → step ~36° around HSL wheel.
# Symbols chosen so direction-agnostic (no up/down implied) plus a few
# distinct geometries the NT8 indicator can draw with Draw.Triangle/
# Draw.Dot/Draw.Diamond/Draw.Square/Draw.Cross/Draw.Star.
STRATEGY_VISUALS: dict[str, dict[str, str]] = {
    # ── Validated, currently live-eligible (validated=True, retired!=True) ──
    "bias_momentum":         {"color": "#3fb950", "symbol": "triangleUp"},   # green — canary
    "ib_breakout":           {"color": "#58a6ff", "symbol": "triangleDown"}, # blue
    "opening_session":       {"color": "#bc8cff", "symbol": "diamond"},      # purple
    "vwap_band_pullback":    {"color": "#d29922", "symbol": "circle"},       # amber
    "spring_setup":          {"color": "#ff7b72", "symbol": "square"},       # coral
    "vwap_pullback_v2":      {"color": "#ffa657", "symbol": "cross"},        # orange
    "a_asian_continuation":  {"color": "#79c0ff", "symbol": "star"},         # sky
    "e_multi_day_breakout":  {"color": "#56d364", "symbol": "triangleUp"},   # mint
    "g_inside_bar_breakout": {"color": "#ff9ec7", "symbol": "diamond"},      # pink
    "raschke_baseline":      {"color": "#d2a8ff", "symbol": "square"},       # lavender

    # ── Sim-only / validated=False ──
    # Kept here so they render distinctly in sim_bot's UI even though they
    # don't reach live. Add as needed when a new strategy ships.
    "dom_pullback":          {"color": "#a371f7", "symbol": "circle"},
    "nq_lsr":                {"color": "#f0883e", "symbol": "cross"},
    "orb_v2":                {"color": "#39c5cf", "symbol": "diamond"},
    "es_nq_confluence":      {"color": "#e3b341", "symbol": "star"},
}


_FALLBACK = {"color": "#8b949e", "symbol": "circle"}  # dim gray, neutral dot


def get_visual(name: str) -> dict[str, str]:
    """Return visual config for ``name`` (color + symbol).

    Always returns a usable dict — unknown names fall back to neutral gray
    + circle so the dashboard / NT8 indicator never crash on a freshly
    added strategy. Add the strategy to ``STRATEGY_VISUALS`` to give it a
    distinct identity.
    """
    return STRATEGY_VISUALS.get(name, _FALLBACK).copy()


__all__ = ["STRATEGY_VISUALS", "get_visual"]
