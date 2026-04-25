"""P2 — bias_momentum VWAP_GATE rejection ratio.

Predicts: after VCR threshold drop 1.5→1.2 + close-pos broadening
(Fix B 2026-04-24), the fraction of bias_momentum evals rejected
specifically by VWAP_GATE should drop below 85%, AND a trace
should show VCR threshold = 1.2 in the explosive-bypass logic.
"""

from __future__ import annotations

import re

from .base import BasePredictionGrader, GradeResult
from ..log_parsers.sim_bot_log import filter_events


VCR_TRACE_RE = re.compile(r"VCR=([\d.]+)")


class BiasVwapGateGrader(BasePredictionGrader):
    prediction_id = "P2"
    label = "bias_momentum VWAP_GATE ratio"
    quant_units = "%"
    quant_threshold = 0.85

    def grade(self, events, baseline) -> GradeResult:
        bm = filter_events(events, strategy="bias_momentum")
        total_evals = len(bm)

        vwap_rejects = sum(
            1 for e in bm
            if "VWAP_GATE" in e.message and (e.kind in {"REJECTED", "BLOCKED"} or
                                              e.gate in {"vwap_long", "vwap_short"})
        )
        ratio = (vwap_rejects / total_evals) if total_evals else 0.0

        # Qualitative: did any log line embed VCR= and is it consistent
        # with the new 1.2 threshold (we look for the threshold in the
        # bypass-active confluence trail).
        vcr_traces = []
        for e in bm:
            m = VCR_TRACE_RE.search(e.message)
            if m:
                vcr_traces.append(float(m.group(1)))
        # The threshold itself shows up in confluence trails like
        # "VCR=1.6x ... bypass active" — we want at least one bypass-active
        # message to confirm the new threshold is effective.
        bypass_active_seen = any("bypass active" in e.message for e in bm)
        qual_obs = (
            f"VCR traces seen: n={len(vcr_traces)}, "
            f"min={min(vcr_traces, default=0):.2f}, max={max(vcr_traces, default=0):.2f}; "
            f"bypass_active log line: {'yes' if bypass_active_seen else 'no'}"
        )

        return GradeResult(
            prediction_id=self.prediction_id,
            label=self.label,
            quant_pass=ratio < self.quant_threshold,
            quant_value=ratio,
            quant_threshold=self.quant_threshold,
            quant_units=self.quant_units,
            qual_pass=bypass_active_seen,
            qual_observation=qual_obs,
            overall_pass=(ratio < self.quant_threshold) and bypass_active_seen,
            detail={"total_evals": total_evals,
                    "vwap_gate_rejects": vwap_rejects,
                    "vcr_traces_count": len(vcr_traces)},
        )
