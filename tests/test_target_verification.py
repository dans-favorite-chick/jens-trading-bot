"""
B63 — target-fire verification tests for write_protection_oco().

Scenario under test (Jennifer 2026-04-21): winners going +20 points into
profit then reversing to LOSS without the LIMIT target firing. Root-cause
hypothesis: one leg of the OCO pair (usually the target) was never
consumed by NT8 — stuck in incoming/ as a rejected PLACE — leaving the
position half-protected. `write_protection_oco` must detect this and
either recover the missing leg or fail loudly so the caller's 3-retry
loop engages (and ultimately flattens if all retries fail).

Three scenarios exercised:

  1. Mock both legs consumed → happy path, returns both paths.
  2. Mock only stop consumed (target stuck) → function re-stages target
     exactly once; if retry succeeds, returns both working paths.
  3. Mock neither consumed → returns [] so caller's outer 3-retry loop
     engages.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge import oif_writer


def _prep_incoming(tmp_path, monkeypatch):
    """Point oif_writer at a temp incoming/ so we can simulate consumption."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    monkeypatch.setattr(oif_writer, "OIF_INCOMING", str(incoming))
    return incoming


def _call_protect(trade_id="tid_fire_test"):
    return oif_writer.write_protection_oco(
        direction="LONG",
        qty=1,
        stop_price=26800.00,
        target_price=26850.00,
        trade_id=trade_id,
        account="Sim101",
    )


def test_both_legs_consumed_happy_path(tmp_path, monkeypatch):
    """Both stop + target consumed by NT8 → function returns both paths."""
    _prep_incoming(tmp_path, monkeypatch)

    # _verify_consumed returns list of still-present (stuck) paths. None
    # stuck = happy path.
    with patch.object(oif_writer, "_verify_consumed", return_value=[]):
        written = _call_protect()

    assert len(written) == 2, f"expected 2 paths, got {len(written)}: {written}"
    assert any("_stop" in p for p in written), "stop leg path missing"
    assert any("_target" in p for p in written), "target leg path missing"


def test_target_leg_stuck_retry_succeeds(tmp_path, monkeypatch):
    """Stop consumed but target stuck → re-place target once; retry
    succeeds → return both working paths with [PROTECT_HALF] logged."""
    _prep_incoming(tmp_path, monkeypatch)

    call_count = {"n": 0}

    def fake_verify(paths, trade_id, timeout_s=1.0):
        call_count["n"] += 1
        # First call (two paths): mark the target file as stuck, keep
        # stop as consumed. Match on _target.txt (not _target_retry.txt).
        if call_count["n"] == 1:
            return [p for p in paths if p.endswith("_target.txt")]
        # Second call (the retry verify): consumed successfully.
        return []

    with patch.object(oif_writer, "_verify_consumed", side_effect=fake_verify):
        written = _call_protect(trade_id="tid_half_target_recoverable")

    # We should have exercised the half-success recovery path.
    assert call_count["n"] == 2, \
        f"expected 2 verify calls (initial + retry), got {call_count['n']}"
    assert len(written) == 2, \
        f"after successful retry, expected 2 paths; got {written}"


def test_neither_leg_consumed_full_failure(tmp_path, monkeypatch):
    """Both legs stuck → caller's 3-retry loop engages. Returns []."""
    _prep_incoming(tmp_path, monkeypatch)

    # Every call to _verify_consumed returns ALL paths as stuck.
    def fake_verify(paths, trade_id, timeout_s=1.0):
        return list(paths)

    with patch.object(oif_writer, "_verify_consumed", side_effect=fake_verify):
        written = _call_protect(trade_id="tid_full_failure")

    assert written == [], \
        f"on full failure, expected [] so caller retries; got {written}"


def test_target_leg_stuck_retry_also_fails(tmp_path, monkeypatch):
    """Target stuck, retry ALSO stuck → give up, return [] so caller's
    outer retry re-submits the full OCO pair. Must NOT leave a lingering
    working leg in NT8 (emits cleanup CANCEL)."""
    _prep_incoming(tmp_path, monkeypatch)

    def fake_verify(paths, trade_id, timeout_s=1.0):
        # Always mark any target / retry file as stuck (stop consumes fine).
        return [p for p in paths
                if p.endswith("_target.txt") or "_retry" in p]

    with patch.object(oif_writer, "_verify_consumed", side_effect=fake_verify):
        written = _call_protect(trade_id="tid_half_target_unrecoverable")

    assert written == [], \
        f"when retry leg also fails, must return []; got {written}"
