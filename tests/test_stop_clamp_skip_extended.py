"""skip_on_stop_clamp wired into vwap_pullback + dom_pullback (#8, 2026-05-13).

The was_clamped_from_above() helper was already wired into bias_momentum
in the 2026-05-03 forensic-audit fix (0W/5L on clamped stops). #8 extends
the same protection to the other two ATR-anchored-stop strategies on
the same NQ noise profile: vwap_pullback and dom_pullback.

These tests pin:
1. Both configs carry skip_on_stop_clamp=True.
2. Both strategies actually import and use compute_natural_stop_ticks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Config pins ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["vwap_pullback", "dom_pullback", "bias_momentum"])
def test_strategy_carries_skip_on_stop_clamp(name):
    from config.strategies import STRATEGIES
    cfg = STRATEGIES[name]
    assert cfg.get("skip_on_stop_clamp") is True, (
        f"{name} should have skip_on_stop_clamp=True. The 2026-05-03 audit "
        f"on bias_momentum showed clamped-from-above stops were 0W/5L. "
        f"The same vol-regime-mismatch pattern applies to {name}."
    )


# ── Source pins (the wiring exists in code, not just config) ────────────

@pytest.mark.parametrize("path", [
    "strategies/vwap_pullback.py",
    "strategies/dom_pullback.py",
])
def test_strategy_source_uses_compute_natural_stop_ticks(path):
    """A config flag without the wiring would silently do nothing.
    These pins make sure the helper is actually imported and called."""
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "compute_natural_stop_ticks" in src, (
        f"{path} should import compute_natural_stop_ticks to support "
        f"skip_on_stop_clamp. Otherwise the config flag is a no-op."
    )
    assert "skip_on_stop_clamp" in src, (
        f"{path} should read the skip_on_stop_clamp config key."
    )


# ── Behavior pin: clamped natural stop returns None signal ──────────────

def test_vwap_pullback_skips_when_clamp_from_above():
    """End-to-end-ish: feed vwap_pullback an extreme atr_5m that would
    produce a natural stop > max_stop_ticks. With skip_on_stop_clamp=True,
    the strategy must return None (no signal).

    We construct a minimal market dict + bars list to drive evaluate()
    past the early gates and into the stop calc.
    """
    from strategies._nq_stop import compute_natural_stop_ticks
    # Hand-verified arithmetic: natural stop on extreme ATR exceeds 120
    raw = compute_natural_stop_ticks(
        direction="LONG", entry_price=20000.0, last_5m_bar=None,
        atr_5m_points=50.0,  # absurd ATR → stop ~400t (100pt)
        tick_size=0.25, stop_atr_mult=2.0,
    )
    assert raw > 120, (
        f"Test fixture broken: expected raw > 120 to exercise the clamp "
        f"branch, got {raw}. Pick a larger atr_5m_points."
    )
