"""Regression tests for FINDING-2026-06-05-PHANTOMGUARD-DEDUP-BUG.

Pre-fix, `tools/watcher_agent.py`'s `_check_*` methods emitted one
Finding per spot/deep cycle for the entire duration of an active
condition. A 12-hour SILENT_STALL produced 700+ incident files; the
operator accumulated 29k incident files total and had to silence the
Twilio SMS channel — which is precisely why today's real 12-hour
SILENT_STALL went undetected.

The fix: a `FindingDedup` per-category state machine that collapses
per-cycle re-emits to per-transition emits. It emits:

  - phase=OPEN       on RESOLVED→ACTIVE (or first-ever sighting)
  - phase=ESCALATED  while ACTIVE and elapsed >= ESCALATION_INTERVAL_S
                      since last emit (default 15 min)
  - phase=RESOLVED   on ACTIVE→RESOLVED (when a category present last
                      cycle is absent this cycle)
  - silent           otherwise — the dedup half of the fix

Tests are behavioral per FINDING-2026-06-04-STRUCTURAL-TEST-PATTERN:
they observe the OUTPUT list `emits` from `FindingDedup.cycle(...)`
rather than poking at internal state. State is only inspected when
the master prompt explicitly required a state-shape assertion.

Run: pytest tests/test_phantomguard_dedup.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.watcher_agent import Finding, FindingDedup


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

class _FakeClock:
    """Injectable monotonic clock for deterministic time advancement."""

    def __init__(self, start: float = 0.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, dt_s: float) -> None:
        self.now += float(dt_s)


def _stall_finding(detail_tag: str = "") -> Finding:
    return Finding(
        severity="RED_ALERT",
        category="silent_stall",
        detail=f"NT8 SILENT_STALL active — {detail_tag}",
        context={"tick_age_s": 99.0},
    )


def _ai_finding(detail_tag: str = "") -> Finding:
    return Finding(
        severity="MAJOR",
        category="ai_unresponsive",
        detail=f"Gemini quota exhausted — {detail_tag}",
        context={"quota_error": "429"},
    )


def _new_dedup(escalation_s: float = 900.0,
               start: float = 0.0) -> tuple[FindingDedup, _FakeClock]:
    clock = _FakeClock(start)
    dedup = FindingDedup(
        escalation_interval_s=escalation_s,
        now_fn=clock,
        iso_now_fn=lambda: f"FAKE-ISO-{clock.now:.1f}",
    )
    return dedup, clock


# ─────────────────────────────────────────────────────────────────
# CORE EMIT RULES
# ─────────────────────────────────────────────────────────────────

class TestOpenTransition:
    def test_first_sighting_emits_open(self):
        """Master prompt Phase 3.5 rule 1: OPEN transition emits ONE
        incident tagged phase=OPEN."""
        dedup, _ = _new_dedup()
        emits = dedup.cycle([_stall_finding("cycle 1")])
        assert len(emits) == 1
        assert emits[0].category == "silent_stall"
        assert emits[0].context.get("phase") == "OPEN"
        assert emits[0].context.get("emit_count") == 1


class TestActiveSuppression:
    def test_open_open_open_emits_one(self):
        """Master prompt Phase 3.5 acceptance test 1: three OPEN-cycle
        ticks must emit exactly ONE incident, not three.

        This is the pre-fix flood pattern: pre-fix watcher emitted one
        per spot tick (~60s cadence) for the entire active window.
        """
        dedup, clock = _new_dedup(escalation_s=900.0)
        all_emits: list[Finding] = []
        for i in range(3):
            all_emits += dedup.cycle([_stall_finding(f"cycle {i}")])
            clock.advance(60.0)  # 60s between cycles (spot loop cadence)
        assert len(all_emits) == 1, (
            f"Expected ONE incident for repeat OPENs within escalation "
            f"window, got {len(all_emits)}. This is the pre-fix flood "
            f"bug regression."
        )
        assert all_emits[0].context.get("phase") == "OPEN"


class TestResolvedTransition:
    def test_open_open_resolved_emits_two(self):
        """Master prompt Phase 3.5 acceptance test 2: OPEN persists,
        then condition clears — must emit OPEN then RESOLVED in order.

        The RESOLVED emit is the "all clear" the operator needs to
        confidently un-silence the SMS channel.
        """
        dedup, clock = _new_dedup(escalation_s=900.0)
        all_emits: list[Finding] = []
        all_emits += dedup.cycle([_stall_finding("c1")])
        clock.advance(60.0)
        all_emits += dedup.cycle([_stall_finding("c2")])
        clock.advance(60.0)
        # Condition clears — no findings this cycle.
        all_emits += dedup.cycle([])
        assert len(all_emits) == 2
        assert all_emits[0].context.get("phase") == "OPEN"
        assert all_emits[1].context.get("phase") == "RESOLVED"
        assert all_emits[1].category == "silent_stall"
        # RESOLVED finding must carry the active duration so the
        # operator sees how long the condition was active.
        assert "active_duration_s" in all_emits[1].context


class TestEscalation:
    def test_escalation_after_900s(self):
        """Master prompt Phase 3.5 acceptance test 3: ACTIVE > 900s
        emits a second ESCALATED incident at the 900s mark."""
        dedup, clock = _new_dedup(escalation_s=900.0)
        all_emits: list[Finding] = []
        # Cycle 1 — OPEN
        all_emits += dedup.cycle([_stall_finding("open")])
        assert len(all_emits) == 1
        # Cycles 2–14 (still under 900s) — silent
        for i in range(13):
            clock.advance(60.0)  # 13 * 60s = 780s
            all_emits += dedup.cycle([_stall_finding(f"active {i}")])
        assert len(all_emits) == 1, (
            f"No escalation should fire under {900}s; got "
            f"{len(all_emits)} emits."
        )
        # Cycle 16 — past 900s mark, must escalate
        clock.advance(180.0)  # 780 + 180 = 960s total
        all_emits += dedup.cycle([_stall_finding("escalation cycle")])
        assert len(all_emits) == 2
        assert all_emits[1].context.get("phase") == "ESCALATED"
        assert all_emits[1].context.get("emit_count") == 2

    def test_escalation_uses_interval_not_total_age(self):
        """Escalation should re-fire every ESCALATION_INTERVAL_S after
        each emit, not at multiples of the original since_ts. So 2
        escalations after 2x interval, 3 after 3x, etc."""
        dedup, clock = _new_dedup(escalation_s=900.0)
        emits: list[Finding] = []
        emits += dedup.cycle([_stall_finding("c0")])     # OPEN
        clock.advance(910.0)
        emits += dedup.cycle([_stall_finding("c1")])     # ESCALATED #2
        clock.advance(910.0)
        emits += dedup.cycle([_stall_finding("c2")])     # ESCALATED #3
        assert len(emits) == 3
        assert emits[0].context["phase"] == "OPEN"
        assert emits[1].context["phase"] == "ESCALATED"
        assert emits[2].context["phase"] == "ESCALATED"
        assert emits[2].context["emit_count"] == 3


class TestConcurrentCategories:
    def test_two_categories_emit_independent_opens(self):
        """Master prompt Phase 3.5 acceptance test 4: silent_stall +
        ai_unresponsive must emit INDEPENDENT OPEN incidents — one
        category's emit must not silence the other."""
        dedup, _ = _new_dedup()
        emits = dedup.cycle([_stall_finding("c1"), _ai_finding("c1")])
        assert len(emits) == 2
        cats = {e.category for e in emits}
        assert cats == {"silent_stall", "ai_unresponsive"}
        # Both must be tagged OPEN, with independent emit_count=1.
        for e in emits:
            assert e.context.get("phase") == "OPEN"
            assert e.context.get("emit_count") == 1

    def test_one_clears_while_other_persists(self):
        """If silent_stall clears but ai_unresponsive persists, only
        silent_stall gets a RESOLVED — ai stays silent (still ACTIVE)."""
        dedup, clock = _new_dedup(escalation_s=900.0)
        # Cycle 1 — both OPEN.
        dedup.cycle([_stall_finding("c1"), _ai_finding("c1")])
        clock.advance(60.0)
        # Cycle 2 — only ai still present.
        emits = dedup.cycle([_ai_finding("c2")])
        assert len(emits) == 1
        assert emits[0].category == "silent_stall"
        assert emits[0].context.get("phase") == "RESOLVED"


class TestBotRestartBehavior:
    def test_fresh_dedup_re_emits_open_for_persistent_condition(self):
        """Master prompt Phase 3.5 acceptance test 5: bot-restart
        mid-incident — post-restart watcher is a FRESH FindingDedup
        instance (in-memory state), so if the underlying condition is
        still true, the first cycle after restart emits a fresh OPEN.

        Documented rationale: state lives in-process for v1 simplicity.
        The behavioral consequence (one extra OPEN per restart) is
        acceptable because the operator EXPECTS to see a "still on
        fire" alert after a restart — the alternative (skipping the
        re-emit) would leave the operator unsure whether the daemon
        even came back up.
        """
        # Pre-restart: condition is active.
        d1, c1 = _new_dedup()
        e1 = d1.cycle([_stall_finding("pre-restart")])
        assert len(e1) == 1
        assert e1[0].context.get("phase") == "OPEN"

        # Restart — fresh dedup instance, condition still present.
        d2, _ = _new_dedup()
        e2 = d2.cycle([_stall_finding("post-restart")])
        assert len(e2) == 1
        assert e2[0].context.get("phase") == "OPEN", (
            "After restart, the persistent condition must re-emit as "
            "OPEN. Silent-no-emit would leave the operator blind to "
            "whether the watcher came back up correctly."
        )


# ─────────────────────────────────────────────────────────────────
# DEFENSIVE / EDGE CASES
# ─────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_multiple_findings_same_category_one_cycle_emits_once(self):
        """If a single cycle produces TWO findings of the same
        category (e.g. tick_freshness fires both MINOR + RED variants
        — see _check_bridge_health), only the FIRST one is processed.
        The second is silently dropped to preserve dedup integrity."""
        dedup, _ = _new_dedup()
        f1 = _stall_finding("first")
        f2 = _stall_finding("second-dropped")
        emits = dedup.cycle([f1, f2])
        assert len(emits) == 1
        assert emits[0].detail.endswith("first")

    def test_empty_cycle_emits_nothing(self):
        dedup, _ = _new_dedup()
        assert dedup.cycle([]) == []

    def test_resolved_then_re_open_emits_new_open(self):
        """A category that goes RESOLVED then re-opens must emit a
        FRESH OPEN with emit_count reset to 1."""
        dedup, clock = _new_dedup()
        dedup.cycle([_stall_finding("c1")])     # OPEN #1
        clock.advance(60.0)
        dedup.cycle([])                          # RESOLVED #1
        clock.advance(60.0)
        emits = dedup.cycle([_stall_finding("c2")])  # OPEN #2
        assert len(emits) == 1
        assert emits[0].context.get("phase") == "OPEN"
        assert emits[0].context.get("emit_count") == 1

    def test_none_context_is_handled_defensively(self):
        """If a Finding arrives with context=None (legacy or test
        construction), the dedup tagging must not crash."""
        dedup, _ = _new_dedup()
        f = Finding(severity="MAJOR", category="silent_stall",
                    detail="no-context test", context={})
        f.context = None  # force-stomp to None
        emits = dedup.cycle([f])
        assert len(emits) == 1
        # Phase tagging must have created the context dict.
        assert isinstance(emits[0].context, dict)
        assert emits[0].context.get("phase") == "OPEN"


# ─────────────────────────────────────────────────────────────────
# SMS surface — Finding.as_sms() should expose the phase tag
# ─────────────────────────────────────────────────────────────────

class TestSmsSurface:
    def test_open_finding_sms_includes_open_tag(self):
        """The SMS the operator receives MUST distinguish OPEN /
        ESCALATED / RESOLVED so she knows whether to act, whether to
        keep waiting, or whether it's safe to un-silence."""
        dedup, _ = _new_dedup()
        emits = dedup.cycle([_stall_finding("for sms")])
        sms = emits[0].as_sms()
        assert "[OPEN]" in sms

    def test_resolved_finding_sms_includes_resolved_tag(self):
        dedup, clock = _new_dedup()
        dedup.cycle([_stall_finding("active")])
        clock.advance(60.0)
        emits = dedup.cycle([])
        assert len(emits) == 1
        sms = emits[0].as_sms()
        assert "[RESOLVED]" in sms


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
