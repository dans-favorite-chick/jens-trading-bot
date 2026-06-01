"""
tools.warehouse.sidecar — Load and canonicalize <csv>.run.json sidecar files.

Contract (spec §5.5):
  - All fields optional except schema_version.
  - friction_applied = True  if sidecar says friction_applied=true OR friction_per_rt_usd > 0.
  - friction_applied = False if sidecar present but says neither.
  - friction_applied = False if sidecar absent (legacy default).
"""

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSIONS = {1}


def sidecar_path(csv_path: Path) -> Path:
    """Return the expected sidecar path for a given CSV path."""
    return csv_path.with_suffix("").with_suffix(".run.json")
    # handles both foo.csv → foo.run.json


def load_sidecar(csv_path: Path) -> dict[str, Any]:
    """Load and parse the sidecar for csv_path.

    Returns a dict with:
      - All sidecar fields (if file exists and parses).
      - 'meta' sub-dict with ingester-attached provenance.

    Never raises — parse errors are recorded in meta and ingest proceeds
    (per spec §7 error table row "Sidecar JSON parse failure").

    Raises ValueError for unknown schema_version (spec §7:
    "Sidecar schema mismatch" → per-file rollback, refuse until ingester updated).
    """
    sc_path = sidecar_path(csv_path)
    meta: dict[str, Any] = {}

    if not sc_path.exists():
        meta["sidecar_missing"] = True
        return {"meta": meta}

    raw_bytes = sc_path.read_bytes()
    try:
        data = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("sidecar parse error for %s: %s", sc_path, exc)
        meta["sidecar_missing"] = True
        meta["parse_error_raw_b64"] = base64.b64encode(raw_bytes).decode()
        return {"meta": meta}

    schema_version = data.get("schema_version")
    if schema_version is not None and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unknown sidecar schema_version={schema_version!r} in {sc_path}. "
            f"Update the ingester before ingesting this file."
        )

    # Record missing fields
    expected = {
        "schema_version", "strategy", "params", "code_sha", "seed",
        "lookback_start", "lookback_end", "engine_version",
        "friction_per_rt_usd", "friction_applied", "logical_group", "notes",
    }
    missing = expected - set(data.keys())
    if missing:
        meta["missing_fields"] = sorted(missing)

    data["meta"] = meta
    return data


def canonical_sidecar(data: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes for hashing (deterministic key order, no whitespace)."""
    # Exclude 'meta' (ingester-attached, not part of the sidecar file content)
    clean = {k: v for k, v in data.items() if k != "meta"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()


@dataclass
class SidecarResult:
    run_id: str
    sidecar: dict
    sidecar_raw: dict


def load_and_hash(csv_path: Path) -> SidecarResult:
    """Load sidecar, compute run_id hash, and return a SidecarResult.

    run_id = sha256(csv_bytes + b"\\n" + canonical_sidecar_bytes)

    Returns a SidecarResult with:
      .sidecar      — parsed sidecar data (empty dict if missing or parse failed)
      .sidecar_raw  — envelope `{"sidecar": <parsed>, "meta": {...}}` for forensic
                      storage in runs.sidecar_raw, including sidecar_missing flag,
                      sidecar_parse_error message, parse_error_raw_b64 of bad bytes,
                      and missing_fields list.
    """
    csv_bytes = Path(csv_path).read_bytes()
    loaded = load_sidecar(csv_path)
    # load_sidecar returns either: parsed dict (success); {"meta": {...}} (missing/parse-error).
    meta = dict(loaded.get("meta") or {})
    if "meta" in loaded and len(loaded) == 1:
        sidecar: dict = {}        # missing or parse-failed
        if "parse_error_raw_b64" in meta and "sidecar_parse_error" not in meta:
            meta["sidecar_parse_error"] = "non-json sidecar (raw bytes preserved as b64)"
    else:
        sidecar = {k: v for k, v in loaded.items() if k != "meta"}
        meta["sidecar_present"] = True
        meta["missing_fields"] = [
            f for f in ("strategy", "params", "code_sha", "seed",
                        "lookback_start", "lookback_end")
            if f not in sidecar
        ]
    sc_bytes = canonical_sidecar(sidecar)
    run_id = hashlib.sha256(csv_bytes + b"\n" + sc_bytes).hexdigest()
    sidecar_raw = {"sidecar": sidecar, "meta": meta}
    return SidecarResult(run_id=run_id, sidecar=sidecar, sidecar_raw=sidecar_raw)


def friction_applied(sidecar_data: dict[str, Any], *, cli_override: bool | None = None) -> bool:
    """Resolve friction_applied for a run.

    Priority:
      1. CLI --mark-friction-applied flag (cli_override=True → True)
      2. Sidecar friction_applied field
      3. Sidecar friction_per_rt_usd > 0
      4. Default: False (legacy / no sidecar)
    """
    if cli_override is True:
        return True

    if sidecar_data.get("meta", {}).get("sidecar_missing"):
        return False

    explicit = sidecar_data.get("friction_applied")
    if explicit is True:
        return True

    per_rt = sidecar_data.get("friction_per_rt_usd")
    if per_rt is not None and float(per_rt) > 0:
        return True

    return False
