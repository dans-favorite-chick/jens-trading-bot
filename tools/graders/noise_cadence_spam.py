"""P3 — noise_area cadence log silence + on-cadence visibility.

Predicts: after the silent-cadence change (Fix C 2026-04-24), the
"BLOCKED gate:not_on_30min_cadence" log should fire ≤5 times per
session, AND there should be exactly one "[EVAL] noise_area: ON
CADENCE" log entry per 30-minute window between 09:30 and 14:30 CT
(11 windows expected: 9:30, 10:00, 10:30, ... 14:30).
"""

from __future__ import annotations

from datetime import time

from .base import BasePredictionGrader, GradeResult
from ..log_parsers.sim_bot_log import filter_events


WINDOW_START = time(9, 30)
WINDOW_END = time(14, 30)


class NoiseCadenceSpamGrader(BasePredictionGrader):
    prediction_id = "P3"
    label = "noise_area cadence log silence"
    quant_units = "count"
    quant_threshold = 5  # max BLOCKED-cadence lines per session

    def grade(self, events, baseline) -> GradeResult:
        na = filter_events(events, strategy="noise_area")
        cadence_blocks = [
            e for e in na if e.gate == "not_on_30min_cadence"
        ]
        on_cadence = [
            e for e in na
            if "ON CADENCE" in e.message and
               e.ts and WINDOW_START <= e.ts.time() <= WINDOW_END
        ]
        # Bucket ON CADENCE by 30-min window
        windows_seen = set()
        for e in on_cadence:
            if e.ts:
                t = e.ts.replace(second=0, microsecond=0,
                                 minute=(0 if e.ts.minute < 30 else 30))
                windows_seen.add(t)

        n_blocks = len(cadence_blocks)
        n_unique_windows = len(windows_seen)

        # Qualitative: exactly one ON CADENCE per 30-min window in the
        # 09:30-14:30 CT range. We allow 8-12 (gives slack for boot/disconnect).
        qual_pass = 8 <= len(on_cadence) <= 12 and n_unique_windows >= 8

        qual_obs = (
            f"{len(on_cadence)} ON CADENCE log line(s) across {n_unique_windows} "
            f"unique 30-min windows in 09:30-14:30 CT (target: ~11 windows, ≤1 per window)"
        )

        return GradeResult(
            prediction_id=self.prediction_id,
            label=self.label,
            quant_pass=n_blocks <= self.quant_threshold,
            quant_value=n_blocks,
            quant_threshold=self.quant_threshold,
            quant_units=self.quant_units,
            qual_pass=qual_pass,
            qual_observation=qual_obs,
            overall_pass=(n_blocks <= self.quant_threshold) and qual_pass,
            detail={"n_cadence_blocks": n_blocks,
                    "n_on_cadence": len(on_cadence),
                    "n_unique_windows": n_unique_windows},
        )
