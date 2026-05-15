"""Lock-in test for compression_breakout min_squeeze_bars calibration.

History:
  2026-04-24 (Fix E): raised from 5 -> 12 on the assumption of "60 min on 5m
                      bars" (assumed evaluate() ticked once per 5m bar).
  2026-05-13: retired the strategy after only 18 trades in 5 weeks.
  2026-05-15: un-retired after deep-dive found the 5,476 daily
              `squeeze_not_held_min_bars` events. Eval cadence is actually
              ~1.2x/min (NOT once per 5m), so the prior 12 = ~10 min, well
              below the intended 60 min. Combined with the 4-condition AND
              (TTM + ATR + Volume + Range) being tuned for equity ETFs,
              the strategy effectively never accumulated 12 consecutive
              compressed bars on MNQ. Recalibrated to 6 evals (~5 min) +
              relaxed ATR/range thresholds + per-condition instrumentation.
"""

from __future__ import annotations

from config.strategies import STRATEGIES


def test_compression_min_squeeze_bars_calibrated_for_mnq():
    """After the 2026-05-15 un-retire, compression_breakout is armed in sim
    with relaxed params. min_squeeze_bars=6 matches the ~1.2-eval/min
    cadence to give ~5 min of continuous compression — strict enough to
    filter noise, achievable enough to accumulate trades."""
    cfg = STRATEGIES["compression_breakout"]
    assert cfg["min_squeeze_bars"] == 6, (
        "If you raise this above 6, verify the firing rate is still "
        "non-zero on MNQ. Pre-2026-05-15 the value was 12 and the strategy "
        "produced 0 SIGNALs in 24h (5,476 squeeze_not_held_min_bars events)."
    )
    assert cfg["enabled"] is True, (
        "compression_breakout was un-retired 2026-05-15 with relaxed "
        "params. If you re-disable, update the deep-dive notes in "
        "config/strategies.py and the retired-set test."
    )
    assert cfg.get("validated") is False, (
        "Stay validated=False until n=30 sim trades + post-tune review."
    )


def test_compression_strategy_reads_config_value():
    """Strategy must default to the config value, not a hardcoded 5."""
    import importlib.resources
    src = importlib.resources.files("strategies").joinpath("compression_breakout.py").read_text(encoding="utf-8")
    assert 'self.config.get("min_squeeze_bars"' in src


def test_compression_has_per_condition_instrumentation():
    """The 2026-05-15 deep-dive added NOT_COMPRESSED logging so the
    operator can see WHICH of the 4 stage-1 conditions is the bottleneck.
    Pin the log line so a future refactor doesn't silently strip it."""
    import importlib.resources
    src = importlib.resources.files("strategies").joinpath("compression_breakout.py").read_text(encoding="utf-8")
    assert "NOT_COMPRESSED" in src, (
        "Per-condition instrumentation missing. Without this the operator "
        "is blind to which of TTM/ATR/Volume/Range is gating signals — "
        "exactly the visibility gap that prompted the 2026-05-15 deep-dive."
    )
    # Each of the 4 stage-1 condition labels should appear in failure flags
    for label in ("ttm(", "atr(", "vol(", "range("):
        assert label in src, f"NOT_COMPRESSED diagnostic missing label: {label}"


def test_compression_relaxed_atr_and_range_thresholds():
    """The 2026-05-15 tuning relaxed two of the four stage-1 conditions
    to match MNQ vol profile (not equity-ETF profile). If we tighten them
    back, expect the strategy to go silent again."""
    cfg = STRATEGIES["compression_breakout"]
    assert cfg.get("atr_compression_ratio") == 0.65, (
        "ATR ratio was 0.5 — too strict for MNQ. 0.65 is the operator-"
        "approved tuning."
    )
    assert cfg.get("range_atr_ratio") == 1.8, (
        "Range/ATR ratio was 1.5 — too tight on MNQ. 1.8 broadens it."
    )
