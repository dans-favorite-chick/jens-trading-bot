"""
sidecar.py - Emit warehouse sidecar JSON next to a result CSV.

The DuckDB warehouse ingester reads `<csv-stem>.run.json` for run metadata
(strategy, params, code SHA, seed, lookback range, friction, logical group)
when ingesting CSVs. This module is the canonical EMITTER — every writer in
the framework that produces a CSV consumed by the warehouse calls
`emit_sidecar()` after the CSV is written.

Schema contract (schema_version = 1):

    {
      "schema_version": 1,
      "strategy": "name" | null,         # null for multi-strategy CSVs
      "params": { ... },                  # strategy config OR {"per_strategy": {...}}
      "code_sha": "<git SHA at write>",   # short SHA at HEAD
      "seed": int | null,                 # null if non-deterministic
      "lookback_start": "YYYY-MM-DDTHH:MM:SSZ" | null,
      "lookback_end":   "YYYY-MM-DDTHH:MM:SSZ" | null,
      "engine_version": "phoenix_portfolio_backtest@<date>",
      "friction_per_rt_usd": float | null,
      "logical_group": "phase13_wfa" | "portfolio_wfa" | ... | null,
      "notes": ""
    }

Only `schema_version` is non-optional; all other fields default to None / {}.
Idempotent: overwrites a prior sidecar for the same CSV. The warehouse's
no-delete policy stores both old and new under distinct content-hash run_ids.
"""
from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Union

SCHEMA_VERSION = 1
ENGINE_VERSION = "phoenix_portfolio_backtest@2026-05-31"


def _git_sha(cwd: Optional[Path] = None) -> str:
    """Return short git SHA at HEAD, or 'unknown' on any failure."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(cwd) if cwd else None,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _iso_utc(ts) -> Optional[str]:
    """Coerce a date/datetime/str to ISO 8601 UTC string, or None.

    Accepts:
      - None -> None
      - str 'YYYY-MM-DD' -> 'YYYY-MM-DDT00:00:00Z' (upgrade bare date)
      - str (other) -> returned as-is (caller already formatted)
      - date -> 'YYYY-MM-DDT00:00:00Z'
      - datetime -> ISO with Z suffix; assumes UTC if tz-naive
    """
    if ts is None:
        return None
    if isinstance(ts, str):
        # Bare 'YYYY-MM-DD' -> add UTC midnight; anything else trust caller.
        if len(ts) == 10 and ts[4] == "-" and ts[7] == "-":
            return f"{ts}T00:00:00Z"
        return ts
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat().replace("+00:00", "Z")
    if isinstance(ts, date):
        return f"{ts.isoformat()}T00:00:00Z"
    return str(ts)


def emit_sidecar(
    csv_path: Union[str, Path],
    *,
    strategy: Optional[str] = None,
    params: Optional[dict] = None,
    seed: Optional[int] = None,
    lookback_start=None,
    lookback_end=None,
    friction_per_rt_usd: Optional[float] = None,
    logical_group: Optional[str] = None,
    notes: str = "",
    code_sha: Optional[str] = None,
    engine_version: Optional[str] = None,
) -> Path:
    """Write `<csv_stem>.run.json` next to `csv_path`.

    Returns the sidecar Path. Never raises on git/path issues (worst case:
    code_sha='unknown'). Caller can pass any subset of fields; unset ones land
    as null in the JSON.
    """
    csv_path = Path(csv_path)
    sidecar_path = csv_path.with_name(csv_path.stem + ".run.json")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "strategy": strategy,
        "params": params or {},
        "code_sha": code_sha or _git_sha(csv_path.parent),
        "seed": seed,
        "lookback_start": _iso_utc(lookback_start),
        "lookback_end": _iso_utc(lookback_end),
        "engine_version": engine_version or ENGINE_VERSION,
        "friction_per_rt_usd": friction_per_rt_usd,
        "logical_group": logical_group,
        "notes": notes,
    }
    sidecar_path.write_text(json.dumps(payload, indent=2, default=str),
                            encoding="utf-8")
    return sidecar_path
