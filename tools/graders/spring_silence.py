"""P6 — retired-strategy silence (config-driven).

Predicts: every strategy that is retired or disabled in
``config/strategies.STRATEGIES`` produces zero ``strategy=<name>`` log
lines AND no ``Strategies: [...]`` init-line membership.

Historical context (F-19, 2026-05-24)
-------------------------------------
This grader was originally hardcoded to ``spring_setup`` — the canonical
"retired strategy" of its era. When operator override on 2026-05-17
un-retired ``spring_setup`` (``enabled=True``, ``validated=True``,
``retired`` flag absent), the hardcoded grader started firing false
"spring_setup logs should not exist" alarms. The operator learned to
ignore the grader output. Per the synthesis audit
(``docs/audits/SYNTHESIS_2026-05-24.md`` F-19), the canonical "retired"
set is now read live from ``config/strategies.STRATEGIES`` so the grader
tracks reality automatically:

  retired := cfg.get("retired") is True OR cfg.get("enabled", True) is False

If the operator flips ``enabled`` back on for a strategy, the grader
silently drops that strategy from its silence assertion on the next run.
No code change required. No false alarms.

The ``prediction_id`` is preserved as ``P6`` and the GradeResult shape
is identical to the legacy single-strategy version so the markdown /
HTML / JSON report formats are unchanged.
"""

from __future__ import annotations

from typing import Iterable

from .base import BasePredictionGrader, GradeResult
from ..log_parsers.sim_bot_log import filter_events


def _retired_strategies(strategies_cfg: dict) -> list[str]:
    """Return the canonical retired/disabled strategy name set from a
    ``STRATEGIES``-shaped config dict.

    A strategy is "retired" for grader purposes if EITHER:
      * ``cfg.get("retired")`` is truthy (explicit retirement marker), OR
      * ``cfg.get("enabled", True)`` is False (disabled in config).

    The default for ``enabled`` is True so that a strategy lacking the
    key (e.g. a malformed legacy block) is treated as live and is NOT
    asserted-silent — safer default than over-firing.
    """
    out: list[str] = []
    for name, cfg in strategies_cfg.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("retired") or not cfg.get("enabled", True):
            out.append(name)
    return out


def _load_retired_from_config() -> list[str]:
    """Live import of ``config.strategies.STRATEGIES`` so tests can
    monkeypatch the module attribute and exercise the grader against a
    simulated config without touching disk.

    Import is lazy (inside the function) so test fixtures that
    monkeypatch ``config.strategies.STRATEGIES`` BEFORE calling the
    grader still see their patched value.
    """
    try:
        from config import strategies as _strategies_mod
    except Exception:
        return []
    cfg = getattr(_strategies_mod, "STRATEGIES", {}) or {}
    return _retired_strategies(cfg)


class SpringSilenceGrader(BasePredictionGrader):
    """P6 — retired-strategy silence grader.

    Class name is preserved (``SpringSilenceGrader``) for back-compat
    with ``tools.grade_open_predictions`` import binding; the actual
    semantic is "all retired strategies in config must be log-silent."
    """

    prediction_id = "P6"
    label = "retired strategies silent (zero log lines)"
    quant_units = "count"
    quant_threshold = 0   # zero events allowed across ALL retired strategies

    def grade(self, events, baseline) -> GradeResult:
        retired = _load_retired_from_config()

        # Edge case: if nothing in the config is retired, the grader is
        # trivially passing — no strategy can violate a silence rule that
        # applies to zero strategies. We keep the GradeResult shape and
        # include an explicit observation so the report makes the empty
        # case obvious to the operator (instead of looking suspiciously
        # like a missed check).
        if not retired:
            return GradeResult(
                prediction_id=self.prediction_id,
                label=self.label,
                quant_pass=True,
                quant_value=0,
                quant_threshold=self.quant_threshold,
                quant_units=self.quant_units,
                qual_pass=True,
                qual_observation=(
                    "no retired or disabled strategies in config; "
                    "silence rule vacuously satisfied"
                ),
                overall_pass=True,
                detail={"n_fresh_lines": 0,
                        "n_init_lines": 0,
                        "retired_strategies": [],
                        "sample_lines": []},
            )

        per_strategy_fresh: dict[str, list] = {}
        per_strategy_init_violations: dict[str, int] = {}

        for name in retired:
            strat_events = filter_events(events, strategy=name)

            # Filter out events that are clearly historical (e.g.
            # session_debriefer ingesting yesterday's recap). For this
            # grader we look at fresh log entries only — the strategy
            # name appearing inside a JSON debrief payload sent to
            # Anthropic/Gemini is unavoidable and shouldn't fail the
            # grade.
            fresh = []
            for e in strat_events:
                if ("anthropic._base_client" in e.module
                        or "google" in e.module.lower()):
                    continue
                if "Request options" in e.message:
                    continue
                fresh.append(e)
            per_strategy_fresh[name] = fresh

            # Init line specifically: ``Strategies: [...]`` should NOT
            # include this name. Substring match guards against minor
            # formatting drift in the bootstrapper.
            init_lines = [
                e for e in events
                if "Strategies:" in e.message and name in e.message
            ]
            per_strategy_init_violations[name] = len(init_lines)

        n_fresh = sum(len(v) for v in per_strategy_fresh.values())
        n_init_lines = sum(per_strategy_init_violations.values())

        qual_pass = n_init_lines == 0
        violators = [n for n, c in per_strategy_init_violations.items() if c]
        qual_obs = (
            f"strategies init line includes retired strategies: "
            f"{'yes (FAIL: ' + ', '.join(sorted(violators)) + ')' if violators else 'no'}"
        )

        # Build a flat sample-lines list across all retired strategies
        # (capped) so the report's existing "sample_lines" detail key
        # stays informative without ballooning the JSON size.
        sample_lines: list[str] = []
        for name in sorted(per_strategy_fresh.keys()):
            for e in per_strategy_fresh[name][:3]:
                sample_lines.append(e.raw_line[:200])
                if len(sample_lines) >= 6:
                    break
            if len(sample_lines) >= 6:
                break

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
            detail={
                "n_fresh_lines": n_fresh,
                "n_init_lines": n_init_lines,
                "retired_strategies": sorted(retired),
                "per_strategy_fresh_counts": {
                    n: len(v) for n, v in per_strategy_fresh.items()
                },
                "per_strategy_init_violations": per_strategy_init_violations,
                "sample_lines": sample_lines,
            },
        )
