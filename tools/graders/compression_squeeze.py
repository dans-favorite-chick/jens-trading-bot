"""P5 — compression_breakout squeeze_not_held drop vs 48h baseline.

Predicts: after raising `min_squeeze_bars` 5→12 (Fix E 2026-04-24),
the count of `squeeze_not_held_min_bars` NO_SIGNAL events should
drop ≥30% vs the pre-fix 48h baseline, AND at least one
`squeeze_held` (or successful breakout) log line should appear.
"""

from __future__ import annotations

from .base import BasePredictionGrader, GradeResult
from ..log_parsers.sim_bot_log import filter_events


class CompressionSqueezeGrader(BasePredictionGrader):
    prediction_id = "P5"
    label = "compression_breakout squeeze_not_held drop vs baseline"
    quant_units = "% drop"
    quant_threshold = 0.30   # need ≥30% drop

    def grade(self, events, baseline) -> GradeResult:
        cb = filter_events(events, strategy="compression_breakout")
        squeeze_not_held = [
            e for e in cb if "squeeze_not_held_min_bars" in e.message
        ]
        n_today = len(squeeze_not_held)

        baseline_n = baseline.get("compression_breakout_squeeze_not_held_per_session", 0)
        if baseline_n <= 0:
            # No baseline → can't grade quantitatively
            drop = 0.0
            quant_pass = True
            quant_note = "no baseline available; quant skipped"
        else:
            drop = max(0.0, (baseline_n - n_today) / baseline_n)
            quant_pass = drop >= self.quant_threshold
            quant_note = f"baseline={baseline_n}, today={n_today}, drop={drop*100:.1f}%"

        # Qualitative: any successful breakout / squeeze_held line?
        held = any(
            ("squeeze_held" in e.message or
             ("compression_breakout" in (e.strategy or "") and e.kind in {"SIGNAL", "TRADE"}))
            for e in cb
        )
        qual_obs = (
            f"squeeze_held / breakout signal seen: {'yes' if held else 'no'} ({quant_note})"
        )

        return GradeResult(
            prediction_id=self.prediction_id,
            label=self.label,
            quant_pass=quant_pass,
            quant_value=drop,
            quant_threshold=self.quant_threshold,
            quant_units=self.quant_units,
            qual_pass=held,
            qual_observation=qual_obs,
            overall_pass=quant_pass and held,
            detail={"squeeze_not_held_today": n_today,
                    "baseline": baseline_n,
                    "drop_pct": drop},
        )
