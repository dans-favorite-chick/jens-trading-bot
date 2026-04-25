"""Phoenix Phase B+ §3.3 — Lightweight regime-history persistence.

Append-only JSONL log of (snapshot, shifts) pairs. Each line is a JSON
object: {"snapshot": {...}, "shifts": [...]}.

File path: <project_root>/data/regime_history.jsonl

Used by:
  - tools/fred_poll.py to record poll results
  - agents/council_gate.py voter prompt to surface recent shifts
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from core.macros.fred_feed import MacroSnapshot, RegimeShiftEvent

logger = logging.getLogger("RegimeHistory")

_DEFAULT_HISTORY_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "regime_history.jsonl"
)


class RegimeHistory:
    """JSONL-backed regime history. All operations best-effort; never raises."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else _DEFAULT_HISTORY_PATH
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover
            logger.warning("could not create history dir %s: %s", self.path.parent, e)

    # ---- write ----

    def record(
        self,
        snapshot: MacroSnapshot,
        shifts: list[RegimeShiftEvent],
    ) -> None:
        """Append one entry to the JSONL log."""
        entry = {
            "snapshot": snapshot.to_dict(),
            "shifts": [s.to_dict() for s in shifts],
            "recorded_at_epoch": time.time(),
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:  # pragma: no cover - disk write best-effort
            logger.warning("regime history write failed: %s", e)

    # ---- read ----

    def _iter_entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except Exception as e:
            logger.warning("regime history read failed: %s", e)
        return out

    def get_recent_shifts(self, hours: int = 24) -> list[RegimeShiftEvent]:
        """Return RegimeShiftEvents recorded within the last `hours`."""
        cutoff = time.time() - (hours * 3600)
        out: list[RegimeShiftEvent] = []
        for entry in self._iter_entries():
            recorded_at = float(entry.get("recorded_at_epoch", 0))
            if recorded_at < cutoff:
                continue
            for s in entry.get("shifts", []) or []:
                try:
                    out.append(
                        RegimeShiftEvent(
                            series=str(s.get("series", "")),
                            prev_value=float(s.get("prev_value", 0.0)),
                            curr_value=float(s.get("curr_value", 0.0)),
                            magnitude=float(s.get("magnitude", 0.0)),
                            direction=str(s.get("direction", "")),
                        )
                    )
                except Exception:
                    continue
        return out

    def get_last_snapshot(self) -> MacroSnapshot | None:
        """Return the most recent snapshot, or None if no history."""
        entries = self._iter_entries()
        if not entries:
            return None
        last = entries[-1].get("snapshot")
        if not isinstance(last, dict):
            return None
        try:
            return MacroSnapshot(
                ffr=float(last.get("ffr", 0.0)),
                cpi_yoy=(
                    float(last["cpi_yoy"])
                    if last.get("cpi_yoy") is not None
                    else None
                ),
                unemployment=float(last.get("unemployment", 0.0)),
                yield_curve_2y10y=float(last.get("yield_curve_2y10y", 0.0)),
                fetched_at_iso=str(last.get("fetched_at_iso", "")),
            )
        except Exception:
            return None


__all__ = ["RegimeHistory"]
