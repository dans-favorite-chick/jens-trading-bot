"""Tests for analytics/verifier.py.

Phoenix Strategy Oracle - Task 4 (Phase 3 verifier).

The verifier is the pure-Python defense layer against LLM hallucination,
training-data lookahead artifacts, and inappropriate causal claims. These
tests cover:

- Number extraction & narrative-vs-facts reconciliation
- Event-keyword-near-date scan (training-data memorization tells)
- Causal language detection with statistical-reason whitelist
- Finding classification (TRANSCRIPTION vs INTERPRETATION) for the
  selective lookahead downgrade
- Top-level `verify_report` orchestration including the Tier-4
  adversarial scenario from spec sec 16
- Public surface invariants (forbidden imports, __all__)

All tests are deterministic. No network, no LLM, no file I/O.
"""
from __future__ import annotations

import os
import re

import pytest

from analytics import verifier as v


# ---------------------------------------------------------------------------
# extract_numbers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label, text, expected",
    [
        # ---- Single-number happy paths ---------------------------------
        # (Minor 14: consolidated from the original 11 individual methods
        # plus edge cases called out in the review.)
        ("percentage", "WR = 52.1%", [("52.1%", 0.521)]),
        ("currency_with_commas", "max drawdown $1,840.50",
         [("$1,840.50", 1840.5)]),
        ("equals_assignment", "n=87", [("87", 87.0)]),
        ("negative_number", "max drawdown -1840.5", [("-1840.5", -1840.5)]),
        # Negative percentage: a real edge case for return-series reporting.
        ("negative_percentage", "OOS PF drift -12.5%",
         [("-12.5%", -0.125)]),
        # ---- Multi-number narratives -----------------------------------
        ("multiple_kv_pairs", "DSR=0.71, BHY-p=0.018",
         [("0.71", 0.71), ("0.018", 0.018)]),
        ("mixed_narrative", "WR dropped from 52.1% to 38.9% (n=35)",
         [("52.1%", 0.521), ("38.9%", 0.389), ("35", 35.0)]),
        # ---- Rejection cases (expected list is empty) ------------------
        ("rejects_bare_year", "This was 2024 data", []),
        ("rejects_ordinal_1st", "1st quarter results", []),
        ("rejects_embedded_in_identifier", "q4_2024 results", []),
        ("rejects_date_components", "session_date 2022-10-15 trades", []),
        # Scientific notation: the grammar does not match the exponent
        # (the trailing 'e' is whitelisted in `extract_numbers` to leave
        # room for future support, but the exponent digits are not yet
        # consumed). Current behavior: extract the mantissa "1.5"; the
        # "e3" tail is left untouched. The contract pinned here is "no
        # crash + no wrong full value" - we get the mantissa, not 1500.
        # If true scientific support is added, update this case.
        ("scientific_notation_mantissa_only",
         "energy 1.5e3 watts", [("1.5", 1.5)]),
    ],
)
def test_extract_numbers_parametrized(label, text, expected):
    """Table-driven extract_numbers tests (Minor 14).

    `expected` is a list of (raw, value) tuples that MUST appear in the
    output. For rejection cases the list is empty - the test asserts
    nothing was extracted. Order is checked when the case lists more
    than one element."""
    out = v.extract_numbers(text)
    if not expected:
        assert out == [], f"{label}: expected no extractions, got {out!r}"
        return
    # Multi-element cases: compare as ordered (raw, value-rounded) lists.
    assert len(out) == len(expected), (
        f"{label}: got {len(out)} numbers, expected {len(expected)}: "
        f"{out!r} vs {expected!r}"
    )
    for (raw_got, val_got), (raw_exp, val_exp) in zip(out, expected):
        assert raw_got == raw_exp, f"{label}: raw mismatch"
        assert val_got == pytest.approx(val_exp), (
            f"{label}: value mismatch for {raw_exp!r}"
        )


class TestExtractNumbersBehavior:
    """Additional extract_numbers cases that do not fit the parametrized
    table (e.g. assertions about set-membership rather than exact
    ordering)."""

    def test_signed_positive(self):
        # The current grammar may or may not retain the '+' in the raw
        # token; we only require the parsed value equals 0.05.
        out = v.extract_numbers("delta +0.05 confirmed")
        assert len(out) >= 1
        values = [val for _, val in out]
        assert any(val == pytest.approx(0.05) for val in values)


# ---------------------------------------------------------------------------
# verify_numbers_in_facts
# ---------------------------------------------------------------------------

class TestVerifyNumbersInFacts:
    def _facts(self):
        return {
            "strategies": {
                "bias_momentum": {
                    "metrics": {
                        "dsr": 0.71,
                        "n_trades": 87,
                        "win_rate": 0.521,
                        "bhy_p_adjusted": 0.018,
                        "max_drawdown_dollars": -1840.5,
                    },
                    "gates": {"all_pass_for_proposal": False},
                }
            },
            "findings": [
                # Anything in here MUST be skipped during walk.
                {"id": "f1", "claim": "fabricated value 0.99 appears here"}
            ],
        }

    def test_clean_narrative_passes(self):
        result = v.verify_numbers_in_facts(
            "DSR = 0.71, n = 87",
            self._facts(),
        )
        assert result["ok"] is True
        assert result["unmatched"] == []

    def test_fabricated_number_fails(self):
        result = v.verify_numbers_in_facts(
            "DSR = 0.85 was striking",
            self._facts(),
        )
        assert result["ok"] is False
        assert len(result["unmatched"]) >= 1
        # The fabricated value should appear in unmatched.
        unmatched_values = [val for _, val in result["unmatched"]]
        assert any(val == pytest.approx(0.85) for val in unmatched_values)

    def test_tolerance_pass(self):
        # 0.711 vs 0.71 -> relative drift ~0.0014, within 0.005 default.
        result = v.verify_numbers_in_facts(
            "DSR = 0.711",
            self._facts(),
        )
        assert result["ok"] is True

    def test_tolerance_fail(self):
        # 0.72 vs 0.71 -> relative drift ~0.014, above 0.005 default.
        result = v.verify_numbers_in_facts(
            "DSR = 0.72",
            self._facts(),
        )
        assert result["ok"] is False

    def test_findings_array_excluded(self):
        # The narrative references 0.99 which exists only in findings.
        # The walker MUST skip findings, so this is unmatched.
        result = v.verify_numbers_in_facts(
            "Claim DSR=0.99 found",
            self._facts(),
        )
        assert result["ok"] is False

    def test_empty_narrative(self):
        result = v.verify_numbers_in_facts("", self._facts())
        assert result["ok"] is True
        assert result["unmatched"] == []

    def test_percentage_match(self):
        # 52.1% in narrative -> 0.521. facts has win_rate=0.521.
        result = v.verify_numbers_in_facts(
            "WR = 52.1%",
            self._facts(),
        )
        assert result["ok"] is True

    def test_negative_number_match(self):
        # Narrative cites -1840.5; facts has max_drawdown_dollars=-1840.5.
        result = v.verify_numbers_in_facts(
            "max drawdown was -1840.5",
            self._facts(),
        )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# check_lookahead_keywords
# ---------------------------------------------------------------------------

class TestCheckLookaheadKeywords:
    def test_crash_near_date_violates(self):
        result = v.check_lookahead_keywords("the market crashed in 2022-10")
        assert result["ok"] is False
        assert len(result["violations"]) >= 1
        viol = result["violations"][0]
        assert viol["date"] == "2022-10"
        assert "crash" in viol["keyword"].lower()

    def test_bare_date_with_stats_ok(self):
        # session_date with n_trades - no event keyword.
        result = v.check_lookahead_keywords(
            "session_date 2022-10-15 had n=42 trades"
        )
        assert result["ok"] is True
        assert result["violations"] == []

    def test_case_insensitive_keyword(self):
        result = v.check_lookahead_keywords("Market CRASHED in 2022-10")
        assert result["ok"] is False

    def test_multiple_violations(self):
        text = ("the market crashed in 2022-10 and then Fed pivot in 2023-03 "
                "drove a rally")
        result = v.check_lookahead_keywords(text)
        assert result["ok"] is False
        assert len(result["violations"]) >= 2

    def test_window_size_excludes_far_keyword(self):
        # Place the keyword 12 tokens away from the date. Date at
        # position 0, then 11 filler "x" tokens, then "crashed" at
        # position 12. With window_tokens=10, the keyword is outside.
        text = "2022-10 " + " ".join(["x"] * 11) + " crashed"
        result = v.check_lookahead_keywords(text, window_tokens=10)
        assert result["ok"] is True

    def test_fomc_near_date_violates(self):
        result = v.check_lookahead_keywords(
            "the FOMC meeting in 2022-09 moved markets"
        )
        assert result["ok"] is False

    def test_bare_date_no_event_keyword_ok(self):
        result = v.check_lookahead_keywords(
            "data window 2022-10 to 2026-05"
        )
        assert result["ok"] is True
        assert result["violations"] == []

    def test_no_date_no_violation(self):
        # Event keyword exists but no date; that's not the lookahead pattern.
        result = v.check_lookahead_keywords("the market crashed badly")
        assert result["ok"] is True

    def test_multiple_keywords_same_date_all_recorded(self):
        # Regression for Task 4 review (Important 4): the previous
        # implementation broke after the FIRST matching keyword per
        # date, dropping diagnostic info. Both "crashed" and "rallied"
        # sit within the +/-10-token window of 2022-10 - both should
        # appear in the violations list.
        result = v.check_lookahead_keywords(
            "the market crashed and rallied in 2022-10"
        )
        assert result["ok"] is False
        keywords = {viol["keyword"] for viol in result["violations"]}
        # Both event-word stems should be present. We match against the
        # lowercased keyword forms emitted by the scanner.
        assert any(kw.startswith("crash") for kw in keywords)
        assert any(kw.startswith("rall") for kw in keywords)
        assert len(result["violations"]) >= 2

    def test_comma_attached_event_keyword_violates(self):
        # Regression for Task 4 review (Minor 12): an event-word with a
        # comma butted up against it ("crashed,") must still be detected
        # in the window. Word-boundary matching is what makes this work.
        result = v.check_lookahead_keywords(
            "the market crashed, in 2022-10 trading was thin"
        )
        assert result["ok"] is False
        assert len(result["violations"]) >= 1


# ---------------------------------------------------------------------------
# check_causal_language
# ---------------------------------------------------------------------------

class TestCheckCausalLanguage:
    def test_because_fed_pivoted_violates(self):
        result = v.check_causal_language(
            "this rallied because the Fed pivoted at 2022-11"
        )
        assert result["ok"] is False
        assert len(result["violations"]) >= 1

    def test_because_n_below_30_ok(self):
        result = v.check_causal_language(
            "interpretation skipped because n < 30"
        )
        assert result["ok"] is True

    def test_due_to_cpi_violates(self):
        result = v.check_causal_language(
            "the drawdown happened due to CPI release"
        )
        assert result["ok"] is False

    def test_due_to_insufficient_sample_size_ok(self):
        result = v.check_causal_language(
            "no proposal staged due to insufficient sample size"
        )
        assert result["ok"] is True

    def test_no_event_keyword_no_violation(self):
        result = v.check_causal_language(
            "we passed because n = 200 satisfied the floor"
        )
        assert result["ok"] is True

    def test_caused_by_fomc_violates(self):
        result = v.check_causal_language(
            "the spike was caused by FOMC"
        )
        assert result["ok"] is False

    def test_due_to_date_violates(self):
        # Causal phrase + date in window should flag even without keyword.
        result = v.check_causal_language(
            "this happened due to the events of 2022-10"
        )
        assert result["ok"] is False

    def test_fed_tolerance_is_not_whitelisted(self):
        # Regression for Task 4 review (Critical 1): the bare "tolerance"
        # whitelist token previously matched "Fed tolerance for inflation"
        # and silently passed a real exogenous causal claim. The intent of
        # the whitelist was to legitimize the Oracle's `tolerance_pct`
        # parameter language, NOT prose about Fed policy. Only the
        # tighter parameter forms (tolerance_pct, tolerance =, etc.)
        # should still legitimize methodology talk.
        result = v.check_causal_language(
            "strategy declined due to Fed tolerance for inflation risk"
        )
        assert result["ok"] is False
        assert len(result["violations"]) >= 1

    def test_tolerance_pct_parameter_still_whitelisted(self):
        # Companion to test_fed_tolerance_is_not_whitelisted: confirm we
        # did not over-rotate. The Oracle's internal tolerance_pct
        # parameter is legitimate methodology talk and must still pass.
        result = v.check_causal_language(
            "no plateau because tolerance_pct = 0.10 exceeded"
        )
        assert result["ok"] is True

    def test_n_trades_declined_is_not_whitelisted(self):
        # Regression for Task 4 review (Important 5): bare "n_trades"
        # previously whitelisted "n_trades declined in 2022-10" which is
        # actually an exogenous causal claim about a market period. The
        # tightened whitelist requires a comparator (n_trades <, =,
        # below, exceeded) to legitimize the prose.
        result = v.check_causal_language(
            "signal degraded because n_trades declined in 2022-10"
        )
        assert result["ok"] is False
        assert len(result["violations"]) >= 1

    def test_n_trades_comparator_still_whitelisted(self):
        # Companion to test_n_trades_declined_is_not_whitelisted: confirm
        # the tightened forms still pass for legitimate methodology talk.
        result = v.check_causal_language(
            "skipped because n_trades < 30 below the floor"
        )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# classify_finding_type
# ---------------------------------------------------------------------------

class TestClassifyFindingType:
    def _facts(self):
        return {
            "strategies": {
                "bias_momentum": {
                    "metrics": {
                        "dsr": 0.71,
                        "n_trades": 87,
                        "bhy_p_adjusted": 0.018,
                    },
                    "gates": {},
                }
            },
        }

    def test_pure_transcription(self):
        finding = {
            "id": "f1",
            "rationale": "DSR = 0.71, n = 87, BHY-p = 0.018",
        }
        assert v.classify_finding_type(finding, self._facts()) == "TRANSCRIPTION"

    def test_synthesis_interpretation(self):
        finding = {
            "id": "f2",
            "rationale": "DSR = 0.71 reflects strong post-pivot positioning",
        }
        # 0.71 matches but "post-pivot positioning" is interpretive; however
        # the classification logic is number-based: every numeric MUST appear
        # in facts. Here it does, so this still counts as TRANSCRIPTION by the
        # number-trace test. The narrative-level lookahead/causal checks are
        # separate. Confirm contract: number-trace only.
        result = v.classify_finding_type(finding, self._facts())
        assert result == "TRANSCRIPTION"

    def test_fabricated_number_interpretation(self):
        finding = {
            "id": "f3",
            "rationale": "DSR = 0.99 suggests excellence",
        }
        assert v.classify_finding_type(finding, self._facts()) == "INTERPRETATION"

    def test_empty_rationale_interpretation(self):
        finding = {"id": "f4", "rationale": ""}
        assert v.classify_finding_type(finding, self._facts()) == "INTERPRETATION"

    def test_missing_rationale_interpretation(self):
        finding = {"id": "f5"}
        assert v.classify_finding_type(finding, self._facts()) == "INTERPRETATION"

    def test_gate_threshold_reference_is_transcription(self):
        # Regression for Task 4 review (Important 6): a rationale citing
        # a gate threshold value (e.g. 0.95 DSR gate) used to misclassify
        # as INTERPRETATION because the constant did not exist as a leaf
        # in facts. We now emit `gate_thresholds` into the panel from
        # compute_engine, and this test confirms the verifier can trace
        # 0.95 back to facts.X.gate_thresholds.dsr_high.
        facts = {
            "strategies": {
                "X": {
                    "metrics": {"dsr": 0.97, "n_trades": 200},
                    "gates": {},
                    "gate_thresholds": {
                        "dsr_high": 0.95,
                        "dsr_luck_floor": 0.90,
                        "psr": 0.90,
                    },
                }
            },
        }
        finding = {
            "id": "f_gate",
            "rationale": "DSR cleared the 0.95 gate",
        }
        assert v.classify_finding_type(finding, facts) == "TRANSCRIPTION"

    def test_classify_accepts_cached_num_check(self):
        # Important 6 / Minor 10: classify_finding_type should accept a
        # pre-computed num_check (cached by verify_report's rejection
        # probe) and skip the duplicate walk through facts.
        facts = self._facts()
        finding = {
            "id": "f_cached",
            "rationale": "DSR = 0.71, n = 87",
        }
        # Forge a cached check that would force INTERPRETATION even
        # though the rationale is actually transcribable. This proves
        # the cache is honored (not silently recomputed).
        forged = {"ok": False, "unmatched": [("0.71", 0.71)]}
        result = v.classify_finding_type(finding, facts, num_check=forged)
        assert result == "INTERPRETATION"


# ---------------------------------------------------------------------------
# verify_report
# ---------------------------------------------------------------------------

class TestVerifyReport:
    def _clean_facts(self):
        return {
            "strategies": {
                "bias_momentum": {
                    "metrics": {
                        "dsr": 0.71,
                        "n_trades": 200,
                        "bhy_p_adjusted": 0.018,
                    },
                    "gates": {"all_pass_for_proposal": True},
                }
            },
        }

    def test_clean_report(self):
        narrative = "DSR=0.71, n=200, BHY-p=0.018 all clear."
        findings = [
            {
                "id": "f1",
                "rationale": "DSR=0.71 with n=200",
                "confidence": "HIGH",
            }
        ]
        result = v.verify_report(
            self._clean_facts(),
            narrative,
            findings,
            lookahead_active=False,
        )
        assert result["ok"] is True
        assert result["rejected_findings"] == []
        assert result["downgrades"] == []

    def test_fabricated_dsr_rejected(self):
        # Fabricated-number defense: the narrative AND the finding cite a
        # DSR that does not appear anywhere in facts. The verifier must
        # reject the finding outright.
        facts = {
            "strategies": {
                "X": {
                    "metrics": {"dsr": 0.99, "n_trades": 10},
                    "gates": {"all_pass_for_proposal": False},
                }
            },
        }
        narrative = "Strategy X shows DSR=0.95 with n=10"
        findings = [
            {
                "id": "f_adversarial",
                "rationale": "DSR=0.95 with n=10",
                "confidence": "MEDIUM",
            }
        ]
        result = v.verify_report(
            facts,
            narrative,
            findings,
            lookahead_active=False,
        )
        assert result["ok"] is False
        assert "f_adversarial" in result["rejected_findings"]
        assert "f_adversarial" in result["rejection_reasons"]

    def test_verifier_does_not_enforce_n_floor(self):
        # Contract boundary: the n>=30 floor is the orchestrator's job,
        # NOT the verifier's. If a finding cites DSR=0.99 with n=10 and
        # BOTH values are present in facts, the verifier classifies it as
        # a clean transcription and lets it through. The orchestrator
        # must drop it upstream via its INSUFFICIENT-tier gate.
        #
        # This test pins the contract: future maintainers should not
        # silently push the floor down into the verifier.
        facts = {
            "strategies": {
                "X": {
                    "metrics": {"dsr": 0.99, "n_trades": 10},
                    "gates": {"all_pass_for_proposal": False},
                }
            },
        }
        narrative = "Strategy X shows DSR=0.99 with n=10"
        findings = [
            {
                "id": "f_low_n",
                "rationale": "DSR=0.99 with n=10",
                "confidence": "MEDIUM",
            }
        ]
        result = v.verify_report(
            facts,
            narrative,
            findings,
            lookahead_active=False,
        )
        # Both DSR=0.99 and n=10 trace to facts. No causal/lookahead
        # issues. Therefore: no rejection, ok=True. The orchestrator
        # owns the n-floor decision.
        assert result["ok"] is True
        assert result["rejected_findings"] == []

    def test_lookahead_active_downgrades_interpretation(self):
        facts = {
            "strategies": {
                "bias_momentum": {
                    "metrics": {
                        "dsr": 0.71,
                        "n_trades": 200,
                    },
                    "gates": {},
                }
            },
        }
        # f1 is a TRANSCRIPTION (numbers trace to facts).
        # f2 is an INTERPRETATION (cites a fabricated 0.85).
        findings = [
            {"id": "f1", "rationale": "DSR=0.71, n=200", "confidence": "HIGH"},
            {"id": "f2", "rationale": "DSR=0.85 striking", "confidence": "HIGH"},
        ]
        narrative = "DSR=0.71, n=200 holds."
        result = v.verify_report(
            facts,
            narrative,
            findings,
            lookahead_active=True,
        )
        # f2 has fabricated numbers -> rejected outright.
        # The downgrade applies to INTERPRETATION findings that survive. We
        # add another interpretation that doesn't get rejected to verify the
        # downgrade tier-shift.
        findings2 = [
            {"id": "f1", "rationale": "DSR=0.71, n=200", "confidence": "HIGH"},
            # f2: rationale empty -> INTERPRETATION by default; no fabricated
            # numbers -> survives -> should be downgraded.
            {"id": "f2", "rationale": "", "confidence": "HIGH"},
        ]
        result = v.verify_report(
            facts,
            narrative,
            findings2,
            lookahead_active=True,
        )
        # f1 is TRANSCRIPTION -> no downgrade.
        # f2 is INTERPRETATION -> HIGH -> MEDIUM.
        downgrade_ids = [d["finding_id"] for d in result["downgrades"]]
        assert "f1" not in downgrade_ids
        assert "f2" in downgrade_ids
        f2_d = next(d for d in result["downgrades"] if d["finding_id"] == "f2")
        assert f2_d["old"] == "HIGH"
        assert f2_d["new"] == "MEDIUM"

    def test_lookahead_low_stays_low(self):
        # LOW confidence INTERPRETATION should stay LOW (no further downgrade).
        facts = {
            "strategies": {
                "bias_momentum": {
                    "metrics": {"dsr": 0.71, "n_trades": 50},
                    "gates": {},
                }
            },
        }
        findings = [
            {"id": "f1", "rationale": "", "confidence": "LOW"},
        ]
        result = v.verify_report(
            facts,
            "DSR=0.71",
            findings,
            lookahead_active=True,
        )
        # LOW unchanged per spec.
        downgrades = [d for d in result["downgrades"]
                      if d["finding_id"] == "f1"]
        assert downgrades == []

    def test_missing_finding_id_rejected_with_fabricated_number(self):
        # Regression for Task 4 review (Critical 2): a finding without
        # an id used to be silently skipped, letting a fabricated
        # rationale slip through unchecked. The verifier must now reject
        # id-less findings outright AND still surface any content issues.
        facts = {
            "strategies": {
                "bias_momentum": {
                    "metrics": {"dsr": 0.71, "n_trades": 200},
                    "gates": {},
                }
            },
        }
        findings = [
            # Two id-less findings: one with a fabricated number, one
            # with a clean rationale. BOTH should land in rejected, the
            # first one with a content reason concatenated.
            {"rationale": "DSR=0.99 striking", "confidence": "HIGH"},
            {"rationale": "DSR=0.71", "confidence": "HIGH"},
        ]
        result = v.verify_report(
            facts,
            "DSR=0.71 holds.",
            findings,
            lookahead_active=False,
        )
        assert result["ok"] is False
        # Both id-less findings should have synthetic ids in the
        # rejected list.
        assert any("missing_id_" in fid
                   for fid in result["rejected_findings"])
        assert len(result["rejected_findings"]) == 2
        # The first finding's reason must mention both structural AND
        # the fabricated-number issue.
        first_id = "missing_id_0"
        assert first_id in result["rejection_reasons"]
        first_reason = result["rejection_reasons"][first_id]
        assert "missing finding id" in first_reason
        assert "unmatched_numbers" in first_reason
        # The second finding's reason is purely structural.
        second_id = "missing_id_1"
        assert second_id in result["rejection_reasons"]
        assert result["rejection_reasons"][second_id] == (
            "structural: missing finding id"
        )

    def test_narrative_with_lookahead_violation_flags(self):
        facts = {
            "strategies": {
                "bias_momentum": {
                    "metrics": {"dsr": 0.71, "n_trades": 200},
                    "gates": {},
                }
            },
        }
        narrative = "the market crashed in 2022-10 and DSR=0.71"
        findings = []
        result = v.verify_report(
            facts,
            narrative,
            findings,
            lookahead_active=False,
        )
        assert result["lookahead_check"]["ok"] is False
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

class TestPublicSurface:
    def test_no_forbidden_imports(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "analytics",
            "verifier.py",
        )
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        forbidden_patterns = [
            r"^\s*import\s+anthropic",
            r"^\s*from\s+anthropic\b",
            r"^\s*import\s+claude",
            r"^\s*from\s+claude\b",
            r"^\s*import\s+openai",
            r"^\s*from\s+openai\b",
            r"^\s*from\s+bots\b",
            r"^\s*import\s+bots\b",
            r"^\s*from\s+core\b",
            r"^\s*import\s+core\b",
            r"^\s*from\s+bridge\b",
            r"^\s*import\s+bridge\b",
            r"^\s*from\s+data_feeds\b",
            r"^\s*import\s+data_feeds\b",
            # Verifier must not import the engine it verifies (module
            # independence: orchestrator wires them together).
            r"^\s*from\s+analytics\.compute_engine\b",
            r"^\s*import\s+analytics\.compute_engine\b",
        ]
        for pat in forbidden_patterns:
            assert not re.search(pat, text, re.MULTILINE), (
                f"forbidden import found matching {pat!r}"
            )

    def test_all_exports_complete(self):
        expected = {
            "extract_numbers",
            "verify_numbers_in_facts",
            "check_lookahead_keywords",
            "check_causal_language",
            "classify_finding_type",
            "verify_report",
            "DEFAULT_EVENT_KEYWORDS",
            "DEFAULT_CAUSAL_PHRASES",
            "DEFAULT_TOLERANCE",
            "DEFAULT_WINDOW_TOKENS",
        }
        actual = set(v.__all__)
        assert expected.issubset(actual), (
            f"missing exports: {expected - actual}"
        )
        # And every name in __all__ must resolve.
        for name in v.__all__:
            assert hasattr(v, name), f"{name} in __all__ but not defined"
