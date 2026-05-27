"""F-19 regression — grader queries config, never a hardcoded retired list.

Audit reference: docs/audits/SYNTHESIS_2026-05-24.md F-19
("Grader-config divergence: un-retired spring_setup + grader expecting
retired").

What this guards
----------------
The post-session grader at ``tools/grade_open_predictions.py`` was
historically wired to assert silence on a hardcoded strategy name
(``spring_setup``). When the operator un-retired ``spring_setup`` in
``config/strategies.py`` on 2026-05-17, the grader started firing false
"spring_setup should have zero logs" alarms — and the operator learned
to ignore the grader, which is the slowest poison in any engineering
system.

The post-F-19 grader queries ``config.strategies.STRATEGIES`` at run
time. A strategy is considered "retired for grading purposes" iff:

    cfg.get("retired") is truthy OR cfg.get("enabled", True) is False

This test pins the contract:

1. Un-retiring a strategy in config (set ``enabled=True``, no
   ``retired`` flag) must NOT cause the grader to false-alarm on its
   log lines.
2. A genuinely retired strategy (``enabled=False`` OR ``retired=True``)
   that DOES emit log lines must still trigger a grader failure.
3. The helper ``_retired_strategies`` correctly classifies all three
   states: live, disabled-only, explicit-retired.

The grader's existing GradeResult shape (prediction_id="P6",
quant/qual/overall_pass keys) is unchanged — only the source of truth
for "which strategies SHOULD be silent" has moved from a string literal
to the config dict.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tools.graders.spring_silence import (
    SpringSilenceGrader,
    _retired_strategies,
)
from tools.log_parsers.sim_bot_log import LogEvent


def _ev(ts: str, module: str, message: str, *,
        strategy: str | None = None, kind: str = "OTHER",
        gate: str | None = None, level: str = "INFO") -> LogEvent:
    """Tiny LogEvent factory."""
    return LogEvent(
        ts=datetime.fromisoformat(ts),
        level=level,
        module=module,
        message=message,
        kind=kind,
        strategy=strategy,
        gate=gate,
        raw_line=f"{ts} [{module}] {level} {message}",
    )


# ─────────────────────────────────────────────────────────────────────
# _retired_strategies helper (pure function, no config dependency)
# ─────────────────────────────────────────────────────────────────────

class TestRetiredStrategiesHelper:
    """Pure-function test: feed a STRATEGIES-shaped dict, get the right
    retired list back regardless of what's in the real config."""

    def test_explicit_retired_flag_classified_retired(self):
        cfg = {
            "alpha": {"enabled": False, "retired": True},
        }
        assert _retired_strategies(cfg) == ["alpha"]

    def test_disabled_without_retired_flag_classified_retired(self):
        # enabled=False alone is enough — the grader treats "off in
        # prod" as functionally retired for silence-assertion purposes.
        cfg = {
            "beta": {"enabled": False},
        }
        assert _retired_strategies(cfg) == ["beta"]

    def test_enabled_strategy_not_classified_retired(self):
        cfg = {
            "gamma": {"enabled": True, "validated": True},
        }
        assert _retired_strategies(cfg) == []

    def test_missing_enabled_key_defaults_to_live(self):
        # Safer default: a strategy block lacking ``enabled`` is treated
        # as live and is NOT asserted-silent. Over-firing on a missing
        # key would be a regression of the bug F-19 fixes.
        cfg = {
            "delta": {"validated": True},   # no enabled key
        }
        assert _retired_strategies(cfg) == []

    def test_mixed_set_sorted_off_only(self):
        cfg = {
            "live_one":    {"enabled": True},
            "retired_one": {"enabled": False, "retired": True},
            "disabled":    {"enabled": False},
            "live_two":    {"enabled": True, "validated": True},
        }
        assert sorted(_retired_strategies(cfg)) == ["disabled", "retired_one"]

    def test_non_dict_value_ignored(self):
        # Defensive: a malformed STRATEGIES entry (e.g. someone stuck a
        # string there) shouldn't crash the helper.
        cfg = {
            "live":  {"enabled": True},
            "junk":  "not a dict",
        }
        assert _retired_strategies(cfg) == []


# ─────────────────────────────────────────────────────────────────────
# F-19 scenario coverage
# ─────────────────────────────────────────────────────────────────────

class TestF19SpringSetupReEnabled:
    """The headline F-19 scenario: ``spring_setup`` is re-enabled in
    config and DOES emit log lines. The grader must NOT raise a false
    'spring_setup should have zero logs' alarm.
    """

    def test_un_retiring_spring_setup_silences_false_alarm(self, monkeypatch):
        # Simulate the post-2026-05-17 config: spring_setup back on,
        # nothing else retired.
        import config.strategies as strategies_mod
        monkeypatch.setattr(
            strategies_mod, "STRATEGIES",
            {
                "spring_setup":  {"enabled": True,  "validated": True},
                "bias_momentum": {"enabled": True,  "validated": True},
            },
            raising=True,
        )

        # Build a session log where spring_setup is loud (signals,
        # bar evals, an init-line membership) — exactly the situation
        # that broke the legacy grader.
        events = [
            _ev("2026-05-20T09:30:00", "Bot",
                "Strategies: ['bias_momentum','spring_setup']",
                strategy="spring_setup"),
            _ev("2026-05-20T09:31:15", "strategies.spring_setup",
                "[EVAL] spring_setup: NO_SIGNAL no_spring_wick",
                strategy="spring_setup", kind="EVAL", level="DEBUG"),
            _ev("2026-05-20T09:45:00", "strategies.spring_setup",
                "spring_setup: SIGNAL long @ 27450",
                strategy="spring_setup", kind="SIGNAL"),
        ]

        r = SpringSilenceGrader().grade(events, {})

        assert r.quant_pass, (
            f"un-retired spring_setup must not fail quant; "
            f"got value={r.quant_value} units={r.quant_units}"
        )
        assert r.qual_pass, (
            f"un-retired spring_setup must not fail qual; "
            f"got {r.qual_observation!r}"
        )
        assert r.overall_pass, (
            "un-retired spring_setup must overall-pass — this is the "
            "F-19 false-alarm scenario"
        )
        assert r.prediction_id == "P6", "P6 ID preserved for report format"
        assert "spring_setup" not in r.detail["retired_strategies"]

    def test_genuinely_retired_strategy_emitting_logs_still_fails(self, monkeypatch):
        # The flip side: if a strategy IS retired in config but the bot
        # is still emitting log lines for it, the grader MUST fail.
        # This is the original assertion the grader was created to
        # enforce — moving the source of truth to config must not lose
        # it.
        import config.strategies as strategies_mod
        monkeypatch.setattr(
            strategies_mod, "STRATEGIES",
            {
                "bias_momentum":  {"enabled": True},
                "ghost_strategy": {"enabled": False, "retired": True,
                                   "retired_reason": "anti-edge on MNQ"},
            },
            raising=True,
        )

        # Ghost strategy is loud — the cleanup wasn't complete.
        events = [
            _ev("2026-05-20T09:30:00", "Bot",
                "Strategies: ['bias_momentum','ghost_strategy']",
                strategy="ghost_strategy"),
            _ev("2026-05-20T09:31:00", "strategies.ghost_strategy",
                "[EVAL] ghost_strategy: NO_SIGNAL retired_zombie_line",
                strategy="ghost_strategy", kind="EVAL", level="DEBUG"),
            _ev("2026-05-20T09:32:00", "strategies.ghost_strategy",
                "[EVAL] ghost_strategy: NO_SIGNAL retired_zombie_line_2",
                strategy="ghost_strategy", kind="EVAL", level="DEBUG"),
        ]

        r = SpringSilenceGrader().grade(events, {})

        assert not r.quant_pass, (
            f"genuinely-retired ghost_strategy emitted {r.quant_value} "
            f"fresh log lines — quant MUST fail"
        )
        assert not r.qual_pass, (
            f"genuinely-retired ghost_strategy in init-line — "
            f"qual MUST fail; observation: {r.qual_observation!r}"
        )
        assert not r.overall_pass
        assert r.detail["per_strategy_fresh_counts"]["ghost_strategy"] >= 2
        assert "ghost_strategy" in r.detail["retired_strategies"]

    def test_empty_config_vacuously_passes(self, monkeypatch):
        # Edge: no retired strategies at all. The grader should not
        # crash and should report a vacuously-true pass rather than
        # leaving the operator wondering whether the check ran.
        import config.strategies as strategies_mod
        monkeypatch.setattr(
            strategies_mod, "STRATEGIES",
            {"live_only": {"enabled": True}},
            raising=True,
        )

        events = [
            _ev("2026-05-20T09:30:00", "Bot",
                "Strategies: ['live_only']", strategy=None),
        ]
        r = SpringSilenceGrader().grade(events, {})
        assert r.overall_pass
        assert r.detail["retired_strategies"] == []
        assert "vacuously satisfied" in r.qual_observation

    def test_disabled_only_strategy_also_asserted_silent(self, monkeypatch):
        # F-19 corollary: a strategy with ``enabled=False`` but no
        # explicit ``retired`` flag (e.g. ``vwap_pullback`` after the
        # vwap_pullback_v2 supersession) must ALSO be silence-asserted.
        # Without this, "soft disable" creates an audit blind spot.
        import config.strategies as strategies_mod
        monkeypatch.setattr(
            strategies_mod, "STRATEGIES",
            {
                "vwap_pullback":    {"enabled": False, "validated": False},
                "vwap_pullback_v2": {"enabled": True,  "validated": True},
            },
            raising=True,
        )

        # vwap_pullback shouldn't be in the init line anymore.
        events = [
            _ev("2026-05-20T09:30:00", "Bot",
                "Strategies: ['vwap_pullback','vwap_pullback_v2']",
                strategy="vwap_pullback"),
        ]
        r = SpringSilenceGrader().grade(events, {})
        assert not r.qual_pass
        assert "vwap_pullback" in r.qual_observation


# ─────────────────────────────────────────────────────────────────────
# Report-shape preservation (operator habit guard)
# ─────────────────────────────────────────────────────────────────────

class TestReportShapePreserved:
    """The operator's daily monitoring workflow reads the markdown /
    JSON report. The GradeResult schema must not drift, or downstream
    dashboards and the operator's pattern-match-on-emoji habit break.
    """

    def test_grade_result_has_required_keys(self, monkeypatch):
        import config.strategies as strategies_mod
        monkeypatch.setattr(
            strategies_mod, "STRATEGIES",
            {"live": {"enabled": True}}, raising=True,
        )
        r = SpringSilenceGrader().grade([], {})

        d = r.to_dict()
        for key in ("prediction_id", "label", "quant_pass", "quant_value",
                    "quant_threshold", "quant_units", "qual_pass",
                    "qual_observation", "overall_pass", "detail"):
            assert key in d, f"GradeResult missing required key: {key}"

        assert r.prediction_id == "P6"
        assert r.quant_units == "count"
        assert r.quant_threshold == 0

    def test_detail_keeps_legacy_keys(self, monkeypatch):
        # The legacy detail dict shipped ``n_fresh_lines``,
        # ``n_init_lines``, and ``sample_lines``. Downstream renderers
        # (HTML report, JSON consumers) may key off these. Keep them.
        import config.strategies as strategies_mod
        monkeypatch.setattr(
            strategies_mod, "STRATEGIES",
            {"zombie": {"enabled": False, "retired": True}}, raising=True,
        )
        r = SpringSilenceGrader().grade([], {})
        for key in ("n_fresh_lines", "n_init_lines", "sample_lines"):
            assert key in r.detail, f"legacy detail key missing: {key}"
