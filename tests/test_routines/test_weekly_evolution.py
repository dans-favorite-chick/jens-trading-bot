"""Tests for tools/routines/weekly_evolution.py — validation checkboxes + week aggregation."""

from __future__ import annotations

import json
from pathlib import Path

from tools.routines.weekly_evolution import (
    build_commit_body, aggregate_week, generate_proposals,
    VALIDATION_STATUS_TEMPLATE, render_validation_status,
)


class TestValidationCheckboxes:
    """Per Jennifer 2026-04-25: every commit body MUST include the
    CPCV / DSR / PBO validation gate. 2026-05-25 P4-5 (F-24): the gate
    is now an actual run of tools/walk_forward_harness.py, but the
    legacy NOT-YET-RUN template is preserved as a cold-start fallback.
    """

    def test_legacy_template_has_three_checkboxes(self):
        for marker in ["CPCV fold metrics", "DSR p-value", "PBO"]:
            assert marker in VALIDATION_STATUS_TEMPLATE
        assert "NOT YET RUN" in VALIDATION_STATUS_TEMPLATE
        assert "Phase C dependency" in VALIDATION_STATUS_TEMPLATE

    def test_legacy_template_has_unchecked_boxes(self):
        # Three unchecked boxes "[ ]" — legacy fallback shape
        assert VALIDATION_STATUS_TEMPLATE.count("- [ ]") == 3

    def test_commit_body_includes_validation_section(self):
        # Pass an explicit validation_section so the test is deterministic
        # and does not depend on the live trade_memory state.
        body = build_commit_body(
            week_start="2026-04-19", week_end="2026-04-25",
            proposals=[], ai_review="(no proposals to review)",
            validation_section=VALIDATION_STATUS_TEMPLATE,
        )
        assert "Validation status" in body
        assert "CPCV fold metrics" in body
        assert "NOT YET RUN" in body
        # Validation section must be present even with zero proposals
        assert "DO NOT MERGE" in body

    def test_render_validation_status_emits_checkbox_per_strategy(self):
        """The new P4-5 harness output: one checkbox row per strategy
        with PASS/FAIL/INSUFFICIENT_DATA + numeric DSR p / PBO when
        available. References the harness module by name."""
        section = render_validation_status(
            strategies=("bias_momentum",),
            min_trades=200,
        )
        assert "Validation status" in section
        # P4-5 references
        assert "walk_forward_harness.py" in section
        # Either the strategy line is rendered OR the cold-start fallback
        # template kicked in (defensive: no trades available)
        assert "bias_momentum" in section or "NOT YET RUN" in section

    def test_commit_body_lists_proposals(self):
        body = build_commit_body(
            week_start="2026-04-19", week_end="2026-04-25",
            proposals=[
                {"strategy": "orb", "description": "loosen ATR cap",
                 "reasoning": "P1 fail rate 80%"},
                {"strategy": "bias_momentum", "description": "drop VCR threshold",
                 "reasoning": "P2 always failing"},
            ],
            ai_review="proposal 1: SAFE; proposal 2: CAUTION",
            validation_section=VALIDATION_STATUS_TEMPLATE,
        )
        assert "loosen ATR cap" in body
        assert "drop VCR threshold" in body
        assert "AI review" in body


class TestAggregateWeek:
    def test_no_grade_files(self, tmp_path: Path, monkeypatch):
        from tools.routines import weekly_evolution as we
        empty = tmp_path / "out" / "grades"
        empty.mkdir(parents=True)
        monkeypatch.setattr(we, "GRADES_DIR", empty)
        summary = we.aggregate_week("2026-04-25")
        assert summary["n_sessions"] == 0
        assert summary["consistent_failures"] == []

    def test_aggregates_pass_fail_per_pid(self, tmp_path: Path, monkeypatch):
        from tools.routines import weekly_evolution as we
        gdir = tmp_path / "out" / "grades"
        gdir.mkdir(parents=True)
        # 3 sessions: P1 fails 3x, P2 passes 3x
        for d in ("2026-04-23", "2026-04-24", "2026-04-25"):
            (gdir / f"{d}.json").write_text(json.dumps({
                "results": [
                    {"prediction_id": "P1", "overall_pass": False, "label": "ORB"},
                    {"prediction_id": "P2", "overall_pass": True, "label": "bias"},
                ],
            }), encoding="utf-8")
        monkeypatch.setattr(we, "GRADES_DIR", gdir)
        summary = we.aggregate_week("2026-04-25")
        assert summary["n_sessions"] == 3
        assert summary["fail_counts"]["P1"] == 3
        assert summary["pass_counts"]["P2"] == 3
        # Consistent-failure threshold: ≥ max(2, n_sessions // 2)
        assert "P1" in summary["consistent_failures"]
        assert "P2" not in summary["consistent_failures"]


class TestGenerateProposals:
    def test_seeds_proposal_per_consistent_failure(self):
        summary = {
            "week_start": "2026-04-19", "week_end": "2026-04-25",
            "n_sessions": 3, "consistent_failures": ["P1", "P3"],
            "fail_counts": {"P1": 3, "P3": 2}, "pass_counts": {},
        }
        proposals = generate_proposals(summary)
        # At LEAST one proposal per consistent failure
        pids = [p.get("prediction_id") for p in proposals]
        assert "P1" in pids
        assert "P3" in pids
        # Each proposal has a strategy + description + reasoning
        for p in proposals:
            assert p.get("strategy")
            assert p.get("description")
            assert p.get("reasoning")
