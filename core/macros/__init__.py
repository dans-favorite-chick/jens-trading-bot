"""Phoenix Phase B+ §3.3 — Structured FRED macro feed.

Promotes ad-hoc FRED calls in core.market_intel into a cached layer with
regime-shift detection and history persistence. See fred_feed.py for the
main entry point (FredMacroFeed) and regime_history.py for persistence.
"""
