"""
portfolio_backtest — Phoenix portfolio-wide backtest, WFA & microstructure framework
====================================================================================

A modular layer ON TOP of ``tools/phoenix_real_backtest.py`` (which already drives
Phoenix's REAL strategy classes with no-look-ahead enrichment, conservative fills,
and a round-turn friction model). This package adds the macro-robustness,
walk-forward, and sub-second microstructure analytics that the standalone harness
does not provide.

Phase coverage (see docs in each module):

  PHASE 1 — 5-year macro robustness (analytics.py, wfa.py)
    1.1 Walk-Forward Analysis (12m IS / 3m OOS, >20% OOS-PF-degradation flag)  -> wfa.py
    1.2 MAE/MFE distributions -> mathematically-derived stop/target            -> analytics.py
    1.3 Volatility-regime mapping (ATR percentile + trend; VIX optional)       -> analytics.py
    1.4 Time-of-day performance buckets (ET sessions)                          -> analytics.py
    1.5 Consecutive losses / max $ drawdown / max time-under-water             -> analytics.py

  PHASE 2 — 5-month (actually ~2-month) sub-second microstructure (microstructure.py)
    2.1 Intermarket SMT/convergence    -> BAR-LEVEL only (no MES tick data)
    2.2 CVD order-flow absorption       -> tick-level (43.8M MNQ TBBO ticks)
    2.3 DOM liquidity-sweep / stop-hunt -> NOT COMPUTABLE (TBBO is top-of-book; no L2 depth)
    2.4 Internal delta clusters / trail -> tick-level

  PHASE 3 — execution guardrails + reporting (report.py, run_portfolio_backtest.py)
    Frictional reality (reuses phoenix_real_backtest friction model),
    no-look-ahead enforcement (.shift(1) on all rolling metrics),
    multi-tier validation separation + with/without-microstructure lift table.

DATA NOTE
---------
This package is developed inside a git worktree that does NOT contain the
(gitignored) ~2.5 GB of tick parquets / OHLCV CSVs. All data access therefore
routes through ``paths.DATA_ROOT`` (the main checkout) — never relative paths.

INTERPRETER
-----------
Run with the canonical bot interpreter (pandas 3.x, numpy 2.x, pyarrow 24,
scipy, databento):

    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe
"""

from __future__ import annotations

__all__ = ["paths", "analytics"]

__version__ = "0.1.0"
