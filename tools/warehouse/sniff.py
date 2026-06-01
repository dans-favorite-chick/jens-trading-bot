"""
tools.warehouse.sniff — CSV-kind detection and WFA filename strategy sniffer.

Kind rules (tried in order, spec §5.2):
  trades       has entry_ts AND entry_price AND pnl_dollars
  wfa_windows  has window_idx AND oos_pf AND is_pf
  wfa_summary  has strategy AND mean_oos_pf AND pct_windows_degraded
  summary      first col is 'strategy'|'name'; rest numeric; no entry_ts
  mixed        has trade signature AND at least one aggregate-metric col
  derived      filename matches convenience pattern; none of above matched
  error        no match
"""

import csv
import logging
import re
from pathlib import Path
from typing import Literal

from tools.warehouse.known_strategies import get_known_strategies

log = logging.getLogger(__name__)

CsvKind = Literal["trades", "wfa_windows", "wfa_summary", "summary", "mixed", "derived", "error"]

# Aggregate metric column names used by the mixed/summary sniffer
AGGREGATE_METRIC_COLS = {"profit_factor", "sharpe", "win_rate", "max_dd", "n_trades"}

# Convenience CSV filename patterns → 'derived' kind
# Note: ordered to avoid accidental matches before content-based kind detection.
# The sniff_kind() function tries trades/wfa/summary FIRST; this only fires for
# files that pass none of those structural checks.
DERIVED_PATTERNS = re.compile(
    r"^("
    r"phase\d+_"
    r"|microstructure_lift"
    r"|phase3_"
    r"|phoenix_"
    r"|backtest_v3_sweep_results"
    r"|backtest_v3_"
    r"|exit_methodology_"
    r"|_dom_"
    r"|opening_session_sub_"
    r")",
    re.IGNORECASE,
)

# WFA Phase-13 shard filename → strategy suffix
WFA_P13_RE = re.compile(r"^wfa_windows_p13_(?P<strategy>[a-z][a-z0-9_]*)\.csv$")

# Safe identifier regex (for import table name derivation)
SAFE_IDENT = re.compile(r"[^A-Za-z0-9_]")
SAFE_IDENT_CHECK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_header(csv_path: Path) -> list[str]:
    """Read only the header row of a CSV. Returns list of lowercase stripped column names."""
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            row = next(reader)
        except StopIteration:
            return []
    return [c.strip().lower() for c in row]


def sniff_kind(csv_path: Path) -> tuple[CsvKind, list[str]]:
    """Return (csv_kind, header_columns) for a CSV file."""
    header = read_header(csv_path)
    cols = set(header)

    # 1. trades
    if {"entry_ts", "entry_price", "pnl_dollars"} <= cols:
        has_aggregate = bool(cols & AGGREGATE_METRIC_COLS)
        if has_aggregate:
            return "mixed", header
        return "trades", header

    # 2. wfa_windows
    if {"window_idx", "oos_pf", "is_pf"} <= cols:
        return "wfa_windows", header

    # 3. wfa_summary
    if {"strategy", "mean_oos_pf", "pct_windows_degraded"} <= cols:
        return "wfa_summary", header

    # 4. summary — first col is 'strategy' or 'name', rest numeric, no entry_ts
    if header and header[0] in ("strategy", "name") and "entry_ts" not in cols:
        return "summary", header

    # 5. derived — filename matches convenience pattern
    if DERIVED_PATTERNS.match(csv_path.name):
        return "derived", header

    log.warning("unknown CSV kind for %s — header: %s", csv_path.name, header)
    return "error", header


def sniff_strategy_from_filename(path: Path) -> str | None:
    """Match wfa_windows_p13_<suffix>.csv → strategy key, using known_strategies for resolution.

    Returns None for multi-strategy WFA files (wfa_windows.csv, shardA/B, etc.).
    """
    m = WFA_P13_RE.match(path.name)
    if not m:
        return None  # multi-strategy file; runs.strategy stays NULL by design

    candidate = m.group("strategy")
    known = get_known_strategies()

    # Exact match
    if candidate in known:
        return candidate

    # Suffix match: 'asian' → 'a_asian_continuation'
    suffix_matches = [s for s in known if s == candidate or s.endswith("_" + candidate)]
    if len(suffix_matches) == 1:
        return suffix_matches[0]

    log.warning(
        "wfa filename sniff: %s  candidate=%r  matches=%r → strategy=NULL",
        path.name, candidate, suffix_matches,
    )
    return None


def safe_import_table_name(csv_path: Path) -> str:
    """Derive a safe SQL identifier for a derived/import table from the CSV filename."""
    stem = csv_path.stem.lower()
    sanitized = SAFE_IDENT.sub("_", stem)
    if not sanitized or not sanitized[0].isalpha():
        sanitized = "f_" + sanitized
    table_name = f"import_{sanitized}"
    if not SAFE_IDENT_CHECK.match(table_name):
        raise ValueError(f"Cannot derive safe table name from {csv_path.name!r}: got {table_name!r}")
    return table_name


# Alias for plan-driven test compatibility.
sniff_csv_kind = sniff_kind
