"""Regression test for the pandas 3.0 datetime-precision bug.

Background
----------
pandas 3.0 changed the default datetime precision from nanoseconds to
microseconds. The common idiom

    df["ts"].astype("int64") // 10**9

silently returns values 1000x too small under pandas 3.0 because the
underlying values are microseconds, not nanoseconds. Two Phase 13 spawn
agents independently hit this bug (Sprints A and B). See
docs/PANDAS_30_DATETIME_AUDIT.md for the full inventory.

These tests are written so they:
  1. Document the unsafe behavior empirically (BAD pattern).
  2. Lock in the safe alternative (precast to datetime64[ns, UTC]).
  3. Verify the patched confluence analyzer loader returns plausible
     epoch seconds.

If pandas changes their default back to ns in a future release, the BAD
test will start passing — that is fine; the audit doc explains why.

ASCII only on Windows.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# A timestamp far enough into the future that the units are unambiguous.
SAMPLE_ISO = "2026-01-15T14:30:00Z"
# pre-computed correct epoch seconds for the sample timestamp.
EXPECTED_EPOCH_S = 1768487400


def test_bad_pattern_returns_wrong_magnitude_under_pandas_3():
    """Document the bug: naive astype int64 // 1e9 is 1000x too small."""
    if int(pd.__version__.split(".")[0]) < 3:
        pytest.skip("pandas < 3.0 stored ns by default; bug does not apply")
    ts = pd.to_datetime([SAMPLE_ISO], utc=True)
    bad = int(ts.astype("int64")[0] // 1_000_000_000)
    # Bug yields microseconds // 1e9 = ~1.77e6 (a Linux timestamp from 1970!)
    assert bad < 10_000_000, (
        f"Expected the broken idiom to return microseconds-divided value "
        f"(~1.77e6) under pandas 3.0; got {bad}. Did pandas change defaults?"
    )
    # The true epoch seconds value is ~1.77e9, three orders of magnitude bigger.
    assert EXPECTED_EPOCH_S // bad >= 900, (
        f"Bug factor should be ~1000x but is {EXPECTED_EPOCH_S / max(bad, 1):.1f}"
    )


def test_safe_pattern_precast_ns_returns_correct_epoch_seconds():
    """The fix: cast to datetime64[ns, UTC] before int64."""
    ts = pd.to_datetime([SAMPLE_ISO], utc=True)
    safe = int(ts.astype("datetime64[ns, UTC]").astype("int64")[0] // 1_000_000_000)
    assert safe == EXPECTED_EPOCH_S, (
        f"Safe precast pattern should give {EXPECTED_EPOCH_S}; got {safe}"
    )


def test_safe_pattern_timestamp_method_returns_correct_epoch_seconds():
    """Alternative safe form: per-row .timestamp()."""
    ts = pd.to_datetime([SAMPLE_ISO, "2026-01-15T14:35:00Z"], utc=True)
    safe = ts.to_series().apply(lambda t: int(t.timestamp())).tolist()
    assert safe == [EXPECTED_EPOCH_S, EXPECTED_EPOCH_S + 300], (
        f"timestamp() per-row should give correct epoch seconds; got {safe}"
    )


def test_numpy_explicit_ns_unit_is_safe():
    """numpy form used by phoenix_early_reversal_signals and friends."""
    ts = pd.Timestamp(SAMPLE_ISO).tz_convert("UTC").tz_localize(None)
    ns = np.datetime64(ts, "ns").astype("int64")
    assert ns == EXPECTED_EPOCH_S * 1_000_000_000


def test_numpy_values_astype_view_is_safe():
    """The ts_event.values.astype('datetime64[ns]').view('int64') idiom."""
    df = pd.DataFrame(
        {"price": [1.0, 2.0]},
        index=pd.to_datetime([SAMPLE_ISO, "2026-01-15T14:35:00Z"], utc=True),
    )
    # The fast-path used in phoenix_early_reversal_signals.TickIndex.
    # The .astype("datetime64[ns]") here pins precision before .view.
    ns = df.index.values.astype("datetime64[ns]").view("int64")
    assert int(ns[0]) == EXPECTED_EPOCH_S * 1_000_000_000
    # Verify the 5-minute delta is exactly 300 seconds.
    assert int(ns[1] - ns[0]) == 300 * 1_000_000_000


# ---------------------------------------------------------------------------
# Integration check: the fixed confluence analyzer loader
# ---------------------------------------------------------------------------
def _load_confluence_module():
    """Load phoenix_sr_confluence_analyzer as a module without importing tools/."""
    path = Path(__file__).resolve().parents[1] / "tools" / "phoenix_sr_confluence_analyzer.py"
    if not path.exists():
        pytest.skip(f"confluence analyzer not present at {path}")
    spec = importlib.util.spec_from_file_location("phoenix_sr_confluence_analyzer", path)
    mod = importlib.util.module_from_spec(spec)
    # The analyzer module may try to import heavy backtest deps at import-time.
    # Catch ImportError so this test still protects the load_5m_bars function.
    # 2026-05-20 SHIP AUDIT pt2 (B-016): swapped pytest.skip → pytest.fail.
    # An import-time failure is exactly the kind of pandas 3.0 regression
    # this test exists to catch; SKIPPED-in-CI was masking it as green.
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        pytest.fail(f"confluence module failed to import: {exc!r}")
    return mod


def test_confluence_load_5m_bars_returns_plausible_epoch(tmp_path):
    """End-to-end: synth a 5m-bars CSV, run load_5m_bars, assert epoch sanity."""
    mod = _load_confluence_module()
    if not hasattr(mod, "load_5m_bars"):
        pytest.skip("load_5m_bars not present (module refactored?)")

    csv = tmp_path / "synth_5m.csv"
    csv.write_text(
        "ts_utc,open,high,low,close,volume\n"
        "2026-01-15T14:30:00Z,24000,24010,23990,24005,100\n"
        "2026-01-15T14:35:00Z,24005,24015,24000,24012,120\n"
        "2026-01-15T14:40:00Z,24012,24020,24008,24018,140\n",
        encoding="utf-8",
    )
    df = mod.load_5m_bars(csv)
    assert "epoch" in df.columns
    epochs = df["epoch"].tolist()
    # Plausible range: anywhere from 2017 (1.5e9) to 2049 (2.5e9).
    for e in epochs:
        assert 1_500_000_000 <= e <= 2_500_000_000, (
            f"epoch {e} is outside plausible range — datetime precision bug "
            f"likely reintroduced. Expected values near {EXPECTED_EPOCH_S}."
        )
    # Each 5m bar should be exactly 300 seconds apart.
    deltas = [int(epochs[i + 1] - epochs[i]) for i in range(len(epochs) - 1)]
    assert all(d == 300 for d in deltas), (
        f"5m bars should be 300s apart; got {deltas}. Datetime bug?"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
