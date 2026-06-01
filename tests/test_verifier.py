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

class TestExtractNumbers:
    def test_percentage_to_proportion(self):
        out = v.extract_numbers("WR = 52.1%")
        # Expect one (raw, value) where raw retains the % and value is 0.521.
        assert len(out) == 1
        raw, value = out[0]
        assert raw == "52.1%"
        assert value == pytest.approx(0.521)

    def test_currency_with_commas(self):
        out = v.extract_numbers("max drawdown $1,840.50")
        assert len(out) == 1
        raw, value = out[0]
        assert raw == "$1,840.50"
        assert value == pytest.approx(1840.5)

    def test_equals_assignment(self):
        out = v.extract_numbers("n=87")
        assert len(out) == 1
        raw, value = out[0]
        assert raw == "87"
        assert value == pytest.approx(87.0)

    def test_multiple_kv_pairs(self):
        out = v.extract_numbers("DSR=0.71, BHY-p=0.018")
        values = {raw: val for raw, val in out}
        assert "0.71" in values
        assert "0.018" in values
        assert values["0.71"] == pytest.approx(0.71)
        assert values["0.018"] == pytest.approx(0.018)

    def test_rejects_bare_year(self):
        out = v.extract_numbers("This was 2024 data")
        # 2024 alone should not be a number; it's a year token.
        assert all(raw != "2024" for raw, _ in out)

    def test_rejects_ordinal(self):
        out = v.extract_numbers("1st quarter results")
        # "1st" should not be extracted as 1.
        assert all(raw != "1" for raw, _ in out)
        assert len(out) == 0

    def test_rejects_embedded_in_identifier(self):
        out = v.extract_numbers("q4_2024 results")
        # Neither 4 nor 2024 should come out of q4_2024.
        assert len(out) == 0

    def test_negative_number(self):
        out = v.extract_numbers("max drawdown -1840.5")
        assert len(out) == 1
        raw, value = out[0]
        assert raw == "-1840.5"
        assert value == pytest.approx(-1840.5)

    def test_signed_positive(self):
        out = v.extract_numbers("delta +0.05 confirmed")
        assert len(out) >= 1
        # Either +0.05 or 0.05; whichever we pick the value must be 0.05.
        # We assert that at least one extracted value equals 0.05.
        values = [val for _, val in out]
        assert any(val == pytest.approx(0.05) for val in values)

    def test_mixed_narrative(self):
        text = "WR dropped from 52.1% to 38.9% (n=35)"
        out = v.extract_numbers(text)
        # 3 numbers: 0.521, 0.389, 35
        assert len(out) == 3
        values = sorted(val for _, val in out)
        # ascending: 0.389, 0.521, 35.0
        assert values[0] == pytest.approx(0.389)
        assert values[1] == pytest.approx(0.521)
        assert values[2] == pytest.approx(35.0)

    def test_does_not_extract_date_components(self):
        # YYYY-MM date should not contribute spurious numbers.
        out = v.extract_numbers("session_date 2022-10-15 trades")
        assert len(out) == 0


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
        # Place the keyword 11 tokens away from the date.
        filler = " ".join(["x"] * 11)
        text = f"{filler} crash 2022-10"  # 11 fillers, then keyword, then date
        # Actually: filler has 11 tokens, then "crash", then "2022-10".
        # Reorder so date is at position 0 and crash is at position 12.
        text = "2022-10 " + " ".join(["x"] * 11) + " crashed"
        result = v.check_lookahead_keywords(text, window_tokens=10)
        # Crashed is 12 tokens after the date, outside window=10.
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

    def test_tier4_adversarial_rejects_fabricated_finding(self):
        # Spec sec 16, Tier 4: inject a synthetic precomputed fact with
        # n=10 (below n=30 floor), dsr=0.99. The verifier must REJECT any
        # narrative mentioning the synthetic finding.
        facts = {
            "strategies": {
                "X": {
                    "metrics": {"dsr": 0.99, "n_trades": 10},
                    "gates": {"all_pass_for_proposal": False},
                }
            },
        }
        # A "finding" that the orchestrator tried to write quoting the
        # synthetic dsr=0.99. The narrative cites 0.99 which traces into
        # facts.X.metrics.dsr, BUT a real defense is also needed at the
        # finding level: n=10 < 30 should never produce a published
        # finding. The verifier handles claims; the orchestrator-side gate
        # handles the floor. We confirm the verifier surfaces the finding.
        # Adversarial variant: narrative quotes dsr=0.95 (fabricated, not
        # in facts) and ties it to strategy X.
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
