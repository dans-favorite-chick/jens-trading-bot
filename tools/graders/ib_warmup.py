"""P4 — ib_breakout warmup completion within 10 min of session open.

Predicts: after dropping `ib_minutes` 30→10 (Fix D 2026-04-24), the
first ib_breakout log line that ISN'T `warmup_incomplete` should
appear within 10 minutes of session open (08:30 CT), AND no warmup
logs should fire after that point in the same session.
"""

from __future__ import annotations

from datetime import time, timedelta

from .base import BasePredictionGrader, GradeResult
from ..log_parsers.sim_bot_log import filter_events


SESSION_OPEN = time(8, 30)


class IbWarmupGrader(BasePredictionGrader):
    prediction_id = "P4"
    label = "ib_breakout warmup-clear within 10min"
    quant_units = "minutes"
    quant_threshold = 10.0

    def grade(self, events, baseline) -> GradeResult:
        ib = filter_events(events, strategy="ib_breakout")
        if not ib:
            return GradeResult(
                prediction_id=self.prediction_id, label=self.label,
                quant_pass=False, quant_value=999.0,
                quant_threshold=self.quant_threshold,
                quant_units=self.quant_units,
                qual_pass=False,
                qual_observation="no ib_breakout events in log — strategy may not be loaded",
                overall_pass=False, detail={"events": 0},
            )

        # Find the first non-warmup event after session open
        first_clear = None
        first_clear_minutes = 999.0
        for e in ib:
            if not e.ts or e.ts.time() < SESSION_OPEN:
                continue
            if "warmup_incomplete" not in e.message:
                first_clear = e
                # Compute minutes after session open
                session_open_dt = e.ts.replace(hour=SESSION_OPEN.hour,
                                                minute=SESSION_OPEN.minute,
                                                second=0, microsecond=0)
                first_clear_minutes = (e.ts - session_open_dt).total_seconds() / 60
                break

        # Qualitative: any warmup logs AFTER the first clear?
        post_clear_warmup = []
        if first_clear and first_clear.ts:
            for e in ib:
                if (e.ts and e.ts > first_clear.ts and
                    "warmup_incomplete" in e.message):
                    post_clear_warmup.append(e)
        qual_pass = first_clear is not None and len(post_clear_warmup) == 0
        qual_obs = (
            f"first non-warmup at +{first_clear_minutes:.1f}min after open; "
            f"{len(post_clear_warmup)} warmup logs AFTER that point"
            if first_clear else
            "ib_breakout never cleared warmup in this session"
        )

        return GradeResult(
            prediction_id=self.prediction_id,
            label=self.label,
            quant_pass=first_clear_minutes <= self.quant_threshold,
            quant_value=first_clear_minutes,
            quant_threshold=self.quant_threshold,
            quant_units=self.quant_units,
            qual_pass=qual_pass,
            qual_observation=qual_obs,
            overall_pass=(first_clear_minutes <= self.quant_threshold) and qual_pass,
            detail={"first_clear_ts": first_clear.ts.isoformat() if first_clear and first_clear.ts else None,
                    "post_clear_warmup_count": len(post_clear_warmup),
                    "n_events": len(ib)},
        )
