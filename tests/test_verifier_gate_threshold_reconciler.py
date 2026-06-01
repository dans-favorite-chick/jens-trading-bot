"""
2026-06-01 master fix Phase 7 — tests for the verifier's gate-threshold
citation reconciler.

Run #6 of the Oracle (2026-06-01) had 6 REFUTED findings rejected by
the verifier because their rationales cited gate threshold values
("HLZ t < -3.0 required") that appeared in facts.json gate_thresholds
only in their unsigned form (+3.0). This pinned the verifier's
behavior so a sign-symmetric magnitude match works while a fabricated
number still gets rejected.
"""
from __future__ import annotations


# ──────────────────────────────────────────────────────────────────────
# Setup: a synthetic facts panel that mirrors what compute_engine emits
# ──────────────────────────────────────────────────────────────────────


def _facts_with_one_strategy():
    return {
        "run_mode": "research",
        "run_date": "2026-06-01",
        "window_start": "2021-06-02",
        "window_end": "2026-05-30",
        "strategies": {
            "test_strat": {
                "metrics": {
                    "n_trades": 500,
                    "win_rate": 0.55,
                    "profit_factor": 1.45,
                    "hlz_t_stat": -7.77,        # signed already
                    "dsr": 0.96,
                    "psr": 0.92,
                },
                "gates": {
                    "n_floor": True,
                    "all_pass_for_proposal": False,
                    "failed_gates": ["hlz_3_0"],
                },
                "gate_thresholds": {
                    "dsr_high": 0.95,
                    "psr": 0.90,
                    "hlz_t_stat": 3.0,           # UNSIGNED in panel
                    "n_floor": 30,
                    "wfe_ratio_min": 0.6,
                },
            },
        },
    }


# ──────────────────────────────────────────────────────────────────────
# (a) Signed citation of unsigned threshold — previously rejected,
# now passes after sign-symmetric matching.
# ──────────────────────────────────────────────────────────────────────


def test_signed_gate_threshold_citation_now_matches():
    from analytics.verifier import verify_numbers_in_facts
    facts = _facts_with_one_strategy()
    narrative = (
        "test_strat REFUTED: HLZ t-statistic of -7.77 is well below the "
        "-3.0 threshold required for proposal eligibility."
    )
    result = verify_numbers_in_facts(narrative, facts)
    # Both "-7.77" (matches signed strategy metric) and "-3.0" (matches
    # via sign symmetry against the unsigned +3.0 gate threshold) must
    # reconcile.
    assert result["ok"] is True, (
        f"verifier still rejecting signed threshold citation: "
        f"unmatched={result['unmatched']}"
    )


def test_unsigned_gate_threshold_citation_still_matches():
    """Sanity check — the historical pass-through (unsigned citation
    matching unsigned panel value) must still work."""
    from analytics.verifier import verify_numbers_in_facts
    facts = _facts_with_one_strategy()
    narrative = "test_strat HLZ t-stat 3.0 threshold; observed |t|=7.77."
    result = verify_numbers_in_facts(narrative, facts)
    assert result["ok"] is True


# ──────────────────────────────────────────────────────────────────────
# (b) Fabricated number — still rejected
# ──────────────────────────────────────────────────────────────────────


def test_fabricated_number_still_rejected():
    from analytics.verifier import verify_numbers_in_facts
    facts = _facts_with_one_strategy()
    # 42.7 appears nowhere in facts.json — not as a leaf, not as a
    # negation, not as a metric. Must be rejected.
    narrative = "test_strat shows a Sortino of 42.7 (anomalously high)."
    result = verify_numbers_in_facts(narrative, facts)
    assert result["ok"] is False
    assert any(raw == "42.7" or val == 42.7
               for raw, val in result["unmatched"])


def test_fabricated_negative_number_still_rejected():
    """A negative fabricated number must also be rejected — the
    sign-symmetric fix only helps when the MAGNITUDE is in facts."""
    from analytics.verifier import verify_numbers_in_facts
    facts = _facts_with_one_strategy()
    narrative = "test_strat shows a max drawdown of -12,345."
    result = verify_numbers_in_facts(narrative, facts)
    assert result["ok"] is False


# ──────────────────────────────────────────────────────────────────────
# (c) Strategy metric citation — already worked, still works
# ──────────────────────────────────────────────────────────────────────


def test_strategy_metric_negative_value_matches():
    from analytics.verifier import verify_numbers_in_facts
    facts = _facts_with_one_strategy()
    # hlz_t_stat = -7.77 is already signed in the metrics dict; this
    # works whether sign-symmetry is on or off.
    narrative = "HLZ t-stat -7.77 measured on 500 trades; PF 1.45."
    result = verify_numbers_in_facts(narrative, facts)
    assert result["ok"] is True


def test_metric_magnitude_works_in_both_signs():
    """A metric stored as -7.77 must reconcile when the narrative
    cites +7.77 (e.g., '|t|=7.77') AND when it cites -7.77 (e.g.,
    'HLZ t=-7.77')."""
    from analytics.verifier import verify_numbers_in_facts
    facts = _facts_with_one_strategy()
    narrative_signed = "test_strat HLZ t=-7.77."
    narrative_abs = "test_strat |HLZ t|=7.77."
    r_signed = verify_numbers_in_facts(narrative_signed, facts)
    r_abs = verify_numbers_in_facts(narrative_abs, facts)
    assert r_signed["ok"] is True
    assert r_abs["ok"] is True


# ──────────────────────────────────────────────────────────────────────
# Zero handling — sign-symmetric collection must not double-count zero
# ──────────────────────────────────────────────────────────────────────


def test_zero_value_is_not_double_emitted():
    """Some gate thresholds or metrics legitimately equal 0. The
    walker should add x and -x, but for x == 0 both are the same;
    confirm no infinite loop / no false positive at zero."""
    from analytics.verifier import _walk_facts_numbers
    out: list[float] = []
    _walk_facts_numbers({"some_metric": 0, "other_metric": 5.0}, out)
    # 0 appears once (not twice), 5.0 and -5.0 both appear.
    assert out.count(0.0) == 1, f"zero double-emitted: {out}"
    assert 5.0 in out
    assert -5.0 in out
