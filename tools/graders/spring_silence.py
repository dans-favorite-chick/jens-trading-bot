"""P6 — spring_setup full silence after retire.

Predicts: after retiring spring_setup (Fix F 2026-04-24,
enabled=False in config/strategies.py), zero log lines containing
`strategy=spring_setup` should appear, AND no init line for it.
"""

from __future__ import annotations

from .base import BasePredictionGrader, GradeResult
from ..log_parsers.sim_bot_log import filter_events


class SpringSilenceGrader(BasePredictionGrader):
    prediction_id = "P6"
    label = "spring_setup retired (zero log lines)"
    quant_units = "count"
    quant_threshold = 0   # zero events allowed

    def grade(self, events, baseline) -> GradeResult:
        spring = filter_events(events, strategy="spring_setup")

        # Filter out events that are clearly historical (e.g. session_debriefer
        # ingesting yesterday's recap). For this grader we look at fresh log
        # entries only — the strategy name appearing inside a JSON debrief
        # payload is unavoidable and shouldn't fail the grade.
        fresh = []
        for e in spring:
            # Skip noisy patterns where spring_setup is a substring of an
            # AI-debrief JSON body sent to Anthropic / Gemini.
            if "anthropic._base_client" in e.module or "google" in e.module.lower():
                continue
            if "Request options" in e.message:
                continue
            fresh.append(e)

        n_fresh = len(fresh)

        # Init line specifically: Strategies: [...] should NOT include 'spring_setup'
        init_lines = [
            e for e in events
            if "Strategies:" in e.message and "spring_setup" in e.message
        ]

        qual_pass = len(init_lines) == 0
        qual_obs = (
            f"strategies init line includes spring_setup: "
            f"{'yes (FAIL)' if init_lines else 'no'}"
        )

        return GradeResult(
            prediction_id=self.prediction_id,
            label=self.label,
            quant_pass=n_fresh <= self.quant_threshold,
            quant_value=n_fresh,
            quant_threshold=self.quant_threshold,
            quant_units=self.quant_units,
            qual_pass=qual_pass,
            qual_observation=qual_obs,
            overall_pass=(n_fresh <= self.quant_threshold) and qual_pass,
            detail={"n_fresh_lines": n_fresh,
                    "n_init_lines": len(init_lines),
                    "sample_lines": [e.raw_line[:200] for e in fresh[:3]]},
        )
