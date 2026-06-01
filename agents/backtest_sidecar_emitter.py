"""
agents/backtest_sidecar_emitter.py - Phase 2 warehouse sidecar emitter.

Writes a ``<csv_stem>.run.json`` next to a trades CSV produced by
``agents/backtest_engine.py``. The sidecar conforms to spec section 5.5:

    {
      "schema_version": 1,
      "strategy": "<name>" | null,
      "params": { ... },
      "code_sha": "<short git sha>",
      "seed": int | null,
      "lookback_start": "YYYY-MM-DDTHH:MM:SSZ" | null,
      "lookback_end":   "YYYY-MM-DDTHH:MM:SSZ" | null,
      "engine_version": "phoenix_agent_backtest@<date>",
      "friction_per_rt_usd": 4.82,
      "friction_applied": true,
      "logical_group": "agent_backtest_engine",
      "notes": ""
    }

Defaults:
  friction_per_rt_usd = phoenix_real_backtest._round_turn_friction_dollars()
                       (commission + exchange + 2-tick slippage; ~$4.82 on MNQ).
  friction_applied    = true (the engine sets prb.APPLY_EXECUTION_DECAY=True).
  logical_group       = "agent_backtest_engine".
  engine_version      = "phoenix_agent_backtest@<spec_date>".

Decoupled from the engine so any future agent that emits a warehouse-compatible
CSV can re-use this entrypoint.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Union

SCHEMA_VERSION  = 1
ENGINE_VERSION  = "phoenix_agent_backtest@2026-05-31"
LOGICAL_GROUP   = "agent_backtest_engine"


def _git_sha(cwd: Optional[Path] = None) -> str:
    """Return short git SHA at HEAD or 'unknown' on any failure."""
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
    """Coerce date | datetime | pd.Timestamp | str to ISO 8601 UTC with Z."""
    if ts is None:
        return None
    if isinstance(ts, str):
        if len(ts) == 10 and ts[4] == "-" and ts[7] == "-":
            return f"{ts}T00:00:00Z"
        return ts
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat().replace("+00:00", "Z")
    if isinstance(ts, date):
        return f"{ts.isoformat()}T00:00:00Z"
    iso = getattr(ts, "isoformat", None)
    if callable(iso):
        return iso().replace("+00:00", "Z")
    return str(ts)


def _default_friction_per_rt_usd() -> float:
    """Derive round-turn friction from config (matches the live bot's accounting).

    Falls back to the spec-locked 4.82 if config is unreachable.
    """
    try:
        import tools.phoenix_real_backtest as prb
        return float(prb._round_turn_friction_dollars())
    except Exception:
        return 4.82


def emit_engine_sidecar(
    csv_path: Union[str, Path],
    *,
    strategy: Optional[str],
    params: Optional[dict] = None,
    lookback_start=None,
    lookback_end=None,
    seed: Optional[int] = None,
    friction_per_rt_usd: Optional[float] = None,
    notes: str = "",
    code_sha: Optional[str] = None,
    engine_version: Optional[str] = None,
    logical_group: Optional[str] = None,
) -> Path:
    """Write ``<csv_stem>.run.json`` next to ``csv_path`` and return its Path.

    Never raises on git/path issues (worst case: code_sha='unknown'). Idempotent
    by overwrite; the warehouse no-delete policy stores both old and new under
    distinct content-hash run_ids.
    """
    csv_path = Path(csv_path)
    sidecar_path = csv_path.with_name(csv_path.stem + ".run.json")
    fric = (friction_per_rt_usd
            if friction_per_rt_usd is not None
            else _default_friction_per_rt_usd())
    payload = {
        "schema_version":      SCHEMA_VERSION,
        "strategy":            strategy,
        "params":              params or {},
        "code_sha":            code_sha or _git_sha(csv_path.parent),
        "seed":                seed,
        "lookback_start":      _iso_utc(lookback_start),
        "lookback_end":        _iso_utc(lookback_end),
        "engine_version":      engine_version or ENGINE_VERSION,
        "friction_per_rt_usd": fric,
        "friction_applied":    True,
        "logical_group":       logical_group or LOGICAL_GROUP,
        "notes":               notes,
    }
    sidecar_path.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    return sidecar_path


__all__ = [
    "emit_engine_sidecar",
    "SCHEMA_VERSION",
    "ENGINE_VERSION",
    "LOGICAL_GROUP",
]
