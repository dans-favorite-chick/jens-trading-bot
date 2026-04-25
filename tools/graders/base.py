"""
BasePredictionGrader — the contract every grader implements.

A grader takes one session's parsed log events, applies a quantitative
threshold and a qualitative observation, and emits a GradeResult that
the harness aggregates into JSON / Markdown / HTML reports.

Each grader is one file under graders/ and is registered by name in
grade_open_predictions.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class GradeResult:
    """One grader's verdict for one session."""
    prediction_id: str            # "P1" .. "P6"
    label: str                    # Short human label (e.g. "ORB or_too_wide")
    quant_pass: bool
    quant_value: float            # Computed metric (e.g. ratio, count, age)
    quant_threshold: float        # The cutoff
    quant_units: str              # "%", "count", "minutes", "1/0"
    qual_pass: bool
    qual_observation: str         # 1-line text describing what was seen
    overall_pass: bool            # True only if BOTH quant and qual passed
    detail: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def emoji(self) -> str:
        return "✅" if self.overall_pass else "❌"


class BasePredictionGrader:
    """Subclass per prediction. Override `grade(events, baseline)`.

    Subclasses MUST set:
      prediction_id : "P1" .. "P6"
      label         : short text
      quant_units   : "%" / "count" / "minutes" / etc.
      quant_threshold: float

    The `grade` method receives an events list (parsed log entries) and
    a baseline dict (loaded from out/baselines/*.json) and returns a
    GradeResult. It must NOT raise — wrap any unexpected error in a
    GradeResult with overall_pass=False and notes=<traceback>.
    """

    prediction_id: str = ""
    label: str = ""
    quant_units: str = ""
    quant_threshold: float = 0.0

    def grade(self, events: list[dict], baseline: dict) -> GradeResult:
        raise NotImplementedError

    def _safe_grade(self, events: list[dict], baseline: dict) -> GradeResult:
        """Wrapper that catches grader errors so one bad grader doesn't
        crash the harness. Returns a failing GradeResult on exception."""
        import traceback
        try:
            return self.grade(events, baseline)
        except Exception as e:
            return GradeResult(
                prediction_id=self.prediction_id,
                label=self.label,
                quant_pass=False, quant_value=0.0,
                quant_threshold=self.quant_threshold,
                quant_units=self.quant_units,
                qual_pass=False,
                qual_observation=f"grader error: {e!r}",
                overall_pass=False,
                notes=traceback.format_exc(),
            )
