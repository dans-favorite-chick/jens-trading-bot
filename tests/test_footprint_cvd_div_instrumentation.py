"""footprint_cvd_reversal CVD-div instrumentation (#14, 2026-05-13).

Before #14: a trade record told us "a CVD divergence fired" but not
which kind. After: each signal carries a discrete `cvd_div_type` field
in metadata (one of `multi_bar`, `single_bar`, `both`, `none`) plus
`cvd_div_magnitude`, so post-hoc analysis can group trades by div type
and answer "do multi-bar divs outperform single-bar divs?"

These tests pin:
1. The exit branches in the source actually emit the new fields.
2. The compact reason string contains the cvd_div type for grepping.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "strategies" / "footprint_cvd_reversal.py").read_text(encoding="utf-8")


def test_source_defines_cvd_div_type_enum():
    """The 4 discrete values must all be reachable in the source."""
    for v in ('"both"', '"multi_bar"', '"single_bar"', '"none"'):
        assert v in SRC, (
            f"cvd_div_type enum value {v} not found in source — the "
            f"instrumentation must distinguish all four states."
        )


def test_source_writes_cvd_div_type_metadata():
    """The Signal's metadata must carry the discrete field for groupby."""
    assert '"cvd_div_type": cvd_div_type' in SRC, (
        "cvd_div_type should land in metadata so validation_tracker "
        "(or any post-hoc tool) can groupby trades on it."
    )


def test_source_writes_cvd_div_magnitude_metadata():
    assert '"cvd_div_magnitude": cvd_div_magnitude' in SRC, (
        "cvd_div_magnitude should land in metadata — magnitude is the "
        "main signal-strength dial we want to correlate with edge."
    )


def test_reason_field_includes_cvd_div_type():
    """A human-readable cvd_div=... tag must be in the reason string so
    the trade log is grep-friendly without parsing JSON metadata."""
    assert "[cvd_div={cvd_div_type}]" in SRC, (
        "The reason field should embed the cvd_div_type so the trade "
        "log line itself is groupable via `grep cvd_div=multi_bar`."
    )


def test_multi_bar_confluence_carries_magnitude():
    """The multi_bar confluence string should embed the magnitude
    so the confluences list (which lands in the trade record) is
    also grepable without parsing JSON."""
    assert 'cvd_divergence_multi_bar(mag=' in SRC, (
        "Multi-bar div confluence should carry its magnitude inline."
    )
