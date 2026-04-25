"""P1 — ORB `or_too_wide` rejection ratio.

Predicts: after ATR-adaptive max_or_size (Fix A 2026-04-24), the
fraction of ORB evals blocked by `gate:or_too_wide` between 09:30
and 11:00 CT should drop below 40%, AND at least one ORB candidate
should reach the entry stage (= a SIGNAL, REJECTED-not-or_too_wide,
or TRADE entry).
"""

from __future__ import annotations

from datetime import time

from .base import BasePredictionGrader, GradeResult
from ..log_parsers.sim_bot_log import filter_events


WINDOW_START = time(9, 30)
WINDOW_END = time(11, 0)


class OrbOrTooWideGrader(BasePredictionGrader):
    prediction_id = "P1"
    label = "ORB or_too_wide ratio"
    quant_units = "%"
    quant_threshold = 0.40

    def grade(self, events, baseline) -> GradeResult:
        orb_events = filter_events(events, strategy="orb",
                                   ct_window=(WINDOW_START, WINDOW_END))
        # Treat any orb-tagged event in the window as a "total or eval".
        # In practice every tick triggers one EVAL line per strategy.
        total = len(orb_events) or 0
        wide = sum(1 for e in orb_events if e.gate == "or_too_wide")
        ratio = (wide / total) if total else 0.0

        # Qualitative: at least one candidate that ISN'T or_too_wide.
        non_wide_kinds = {"SIGNAL", "REJECTED", "TRADE"}
        non_wide = [
            e for e in orb_events
            if (e.gate not in {"or_too_wide", "already_traded_today",
                                "or_too_tight", "entry_window_expired"}
                and e.kind in non_wide_kinds)
        ]
        qual_obs = (
            f"{len(non_wide)} non-or_too_wide ORB candidate(s) reached entry stage"
            if non_wide else
            "0 ORB candidates reached entry stage"
        )

        return GradeResult(
            prediction_id=self.prediction_id,
            label=self.label,
            quant_pass=ratio < self.quant_threshold,
            quant_value=ratio,
            quant_threshold=self.quant_threshold,
            quant_units=self.quant_units,
            qual_pass=len(non_wide) >= 1,
            qual_observation=qual_obs,
            overall_pass=(ratio < self.quant_threshold) and (len(non_wide) >= 1),
            detail={"total": total, "or_too_wide": wide,
                    "window_ct": [str(WINDOW_START), str(WINDOW_END)]},
        )
