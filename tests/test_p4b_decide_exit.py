"""
P4b — decide_exit() priority function tests.

Ships in SHADOW MODE: the function is not yet wired as the authoritative
decision point in base_bot's tick loop. Tests verify the priority ordering
and invariants so a future fresh-brain session can safely flip from
sequential if-blocks to candidate-list + decide_exit().

Run: python -m unittest tests.test_p4b_decide_exit -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestPriorityTable(unittest.TestCase):
    def test_pending_exit_highest(self):
        from core.exit_decision import priority_of
        self.assertEqual(priority_of("pending_exit"), 0)

    def test_hard_stop_beats_target(self):
        from core.exit_decision import priority_of
        self.assertLess(priority_of("hard_stop"), priority_of("target_hit"))

    def test_eod_flat_beats_chandelier_trail(self):
        from core.exit_decision import priority_of
        self.assertLess(priority_of("eod_flat_universal"),
                        priority_of("chandelier_trail_hit"))

    def test_chandelier_beats_managed_exit(self):
        from core.exit_decision import priority_of
        self.assertLess(priority_of("chandelier_trail_hit"),
                        priority_of("signal_flip"))

    def test_unknown_reason_has_low_priority(self):
        from core.exit_decision import priority_of
        self.assertGreater(priority_of("totally_made_up_reason"),
                           priority_of("time_stop"))


class TestDecideExitBasics(unittest.TestCase):
    def test_empty_list_no_exit(self):
        from core.exit_decision import decide_exit
        d = decide_exit([])
        self.assertFalse(d.should_exit)
        self.assertEqual(d.reason, "")

    def test_single_candidate_wins(self):
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([ExitCandidate(reason="target_hit")])
        self.assertTrue(d.should_exit)
        self.assertEqual(d.reason, "target_hit")

    def test_decision_bool_conversion(self):
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([ExitCandidate(reason="target_hit")])
        self.assertTrue(bool(d))
        self.assertFalse(bool(decide_exit([])))


class TestPriorityCollisions(unittest.TestCase):
    def test_pending_beats_everything(self):
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([
            ExitCandidate(reason="chandelier_trail_hit"),
            ExitCandidate(reason="pending_exit"),
            ExitCandidate(reason="target_hit"),
        ])
        self.assertEqual(d.reason, "pending_exit")

    def test_hard_stop_beats_target(self):
        """If both stop and target fire on same tick — stop wins."""
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([
            ExitCandidate(reason="target_hit"),
            ExitCandidate(reason="hard_stop"),
        ])
        self.assertEqual(d.reason, "hard_stop")

    def test_eod_flat_beats_managed_exit(self):
        """Noise Area managed exit vs session close → EoD flat wins (safer)."""
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([
            ExitCandidate(reason="signal_flip"),
            ExitCandidate(reason="eod_flat_universal"),
        ])
        self.assertEqual(d.reason, "eod_flat_universal")

    def test_chandelier_beats_trend_stall(self):
        """Spec-based exit (Chandelier, ORB) beats heuristic (stall detector)."""
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([
            ExitCandidate(reason="trend_stall"),
            ExitCandidate(reason="chandelier_trail_hit"),
        ])
        self.assertEqual(d.reason, "chandelier_trail_hit")

    def test_tie_preserves_insertion_order(self):
        """If two candidates have the SAME priority, first inserted wins."""
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([
            ExitCandidate(reason="stop_hit"),      # priority 1
            ExitCandidate(reason="bracket_stop"),  # priority 1 (tie)
        ])
        self.assertEqual(d.reason, "stop_hit")  # first inserted


class TestScaleOutIsNotExit(unittest.TestCase):
    def test_scale_out_alone_does_not_exit(self):
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([ExitCandidate(reason="scale_out_partial")])
        self.assertFalse(d.should_exit)
        self.assertIn("scale_out_partial", d.candidates_considered)

    def test_scale_out_with_real_exit_still_exits(self):
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([
            ExitCandidate(reason="scale_out_partial"),
            ExitCandidate(reason="target_hit"),
        ])
        self.assertTrue(d.should_exit)
        self.assertEqual(d.reason, "target_hit")


class TestWouldOverride(unittest.TestCase):
    def test_higher_priority_overrides(self):
        from core.exit_decision import would_override
        self.assertTrue(would_override("hard_stop", "target_hit"))

    def test_lower_priority_does_not_override(self):
        from core.exit_decision import would_override
        self.assertFalse(would_override("target_hit", "hard_stop"))

    def test_same_priority_does_not_override(self):
        from core.exit_decision import would_override
        self.assertFalse(would_override("stop_hit", "bracket_stop"))


class TestExplainString(unittest.TestCase):
    def test_explain_mentions_all_candidates(self):
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([
            ExitCandidate(reason="chandelier_trail_hit"),
            ExitCandidate(reason="pending_exit"),
        ])
        self.assertIn("chandelier_trail_hit", d.explain)
        self.assertIn("pending_exit", d.explain)
        self.assertIn("wins", d.explain)

    def test_explain_for_single_candidate_does_not_say_wins(self):
        from core.exit_decision import decide_exit, ExitCandidate
        d = decide_exit([ExitCandidate(reason="target_hit")])
        self.assertNotIn("wins", d.explain)


class TestShadowModeNotYetWired(unittest.TestCase):
    """P4b ships in SHADOW MODE — docstring on the module must warn
    against flipping to active until validation is done."""

    def test_module_docstring_warns_shadow_mode(self):
        import core.exit_decision as mod
        self.assertIn("SHADOW MODE", mod.__doc__)
        self.assertIn("DO NOT wire", mod.__doc__)


if __name__ == "__main__":
    unittest.main()
