"""
paths.py — canonical data locations for the portfolio_backtest framework.

WHY THIS EXISTS
---------------
This framework is developed in a git WORKTREE (isolation from the live FREEZE
branch + running bots). The worktree checks out source code but NOT the
gitignored data:

  * data/historical/*_databento.csv          (5y OHLCV, ~1 GB)
  * data/historical/databento_tbbo/*.parquet (43.8M TBBO ticks, ~1.2 GB)
  * logs/volumetric_history.jsonl            (live 1500-tick footprint)

So every data read MUST resolve against the MAIN checkout, never the worktree's
empty ``data/``. Resolution order:

  1. ``$PHOENIX_DATA_ROOT`` environment variable, if set.
  2. The main checkout next to this worktree (…/phoenix_bot).
  3. This repo root (works when run directly inside the main checkout).

If none of the expected data files are found, ``verify()`` raises with a clear
message rather than letting a backtest silently run on missing inputs.
"""

from __future__ import annotations

import os
from pathlib import Path

# This file: <root>/tools/portfolio_backtest/paths.py  ->  root = parents[2]
_THIS_ROOT = Path(__file__).resolve().parents[2]

# Candidate main-checkout locations, in priority order.
_CANDIDATES = [
    os.environ.get("PHOENIX_DATA_ROOT"),
    # Sibling main checkout (worktree dir name carries a suffix, e.g. *_wt_framework)
    str(_THIS_ROOT.parent / "phoenix_bot"),
    str(_THIS_ROOT),
]


def _pick_data_root() -> Path:
    """Return the first candidate root that actually holds the 5y OHLCV CSV."""
    sentinel = Path("data") / "historical" / "mnq_1min_databento.csv"
    for cand in _CANDIDATES:
        if not cand:
            continue
        root = Path(cand).resolve()
        if (root / sentinel).exists():
            return root
    # Fall back to the sibling-main guess so error messages point somewhere sane.
    return (_THIS_ROOT.parent / "phoenix_bot").resolve()


DATA_ROOT: Path = _pick_data_root()

# ── Macro OHLCV (Phase 1) ─────────────────────────────────────────────
HISTORICAL = DATA_ROOT / "data" / "historical"
MNQ_1M_CSV = HISTORICAL / "mnq_1min_databento.csv"
MNQ_5M_CSV = HISTORICAL / "mnq_5min_databento.csv"
MES_1M_CSV = HISTORICAL / "mes_1min_databento.csv"
MES_5M_CSV = HISTORICAL / "mes_5min_databento.csv"

# ── Microstructure (Phase 2) ──────────────────────────────────────────
TBBO_DIR = HISTORICAL / "databento_tbbo"
CLEAN_TICKS_PARQUET = TBBO_DIR / "mnq_ticks_clean.parquet"
CLEAN_TICKS_METADATA = TBBO_DIR / "mnq_ticks_clean.metadata.json"

# ── Live footprint (Phase 2 cross-check, optional) ────────────────────
VOLUMETRIC_HISTORY = DATA_ROOT / "logs" / "volumetric_history.jsonl"

# ── Outputs (written into the worktree, never the live checkout) ──────
OUT_DIR = _THIS_ROOT / "backtest_results" / "portfolio_framework"


def verify(require_ticks: bool = False) -> None:
    """Fail fast if the macro data (and optionally tick data) is unreachable.

    Args:
        require_ticks: if True, also require the clean TBBO parquet to exist
            (needed for Phase 2). Phase 1 only needs the OHLCV CSVs.

    Raises:
        FileNotFoundError with an actionable message.
    """
    missing: list[str] = []
    for label, p in [
        ("MNQ 1m OHLCV", MNQ_1M_CSV),
        ("MNQ 5m OHLCV", MNQ_5M_CSV),
        ("MES 1m OHLCV", MES_1M_CSV),
        ("MES 5m OHLCV", MES_5M_CSV),
    ]:
        if not p.exists():
            missing.append(f"  - {label}: {p}")
    if require_ticks and not CLEAN_TICKS_PARQUET.exists():
        missing.append(f"  - TBBO clean ticks: {CLEAN_TICKS_PARQUET}")
    if missing:
        raise FileNotFoundError(
            "portfolio_backtest cannot locate required data files.\n"
            f"DATA_ROOT resolved to: {DATA_ROOT}\n"
            "Missing:\n" + "\n".join(missing) + "\n"
            "Set PHOENIX_DATA_ROOT to the main checkout (the dir containing "
            "data/historical/) or run from inside it."
        )


def summary() -> str:
    """One-line human summary of resolved paths (for logging at startup)."""
    return (
        f"DATA_ROOT={DATA_ROOT} | "
        f"ohlcv={'ok' if MNQ_1M_CSV.exists() else 'MISSING'} | "
        f"ticks={'ok' if CLEAN_TICKS_PARQUET.exists() else 'MISSING'} | "
        f"out={OUT_DIR}"
    )


if __name__ == "__main__":
    print(summary())
