"""2026-05-08 — fmp_sanity.poll_loop legacy-kwarg compatibility.

base_bot.py used to call poll_loop(halt_on_divergence_pct=0.015) but the
function's signature was poll_loop(interval_s=, divergence_threshold_pct=,
_unused_halt=). Python doesn't auto-route keyword args by docstring claim;
the call raised TypeError which killed the FMP loop on every bot start
(observed continuously from at least 2026-05-04 to 2026-05-08).

Fix: poll_loop signature now accepts **legacy_kwargs to absorb renamed
keywords cleanly so callers ahead of a coordinated rename don't crash
the loop start. The base_bot caller was also updated to use the correct
name `divergence_threshold_pct=`. Both layers are defended.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import fmp_sanity


class TestPollLoopAcceptsLegacyKwargs:
    def test_signature_has_legacy_kwargs_absorber(self):
        sig = inspect.signature(fmp_sanity.poll_loop)
        # Need a VAR_KEYWORD param somewhere in the signature.
        kinds = [p.kind for p in sig.parameters.values()]
        assert inspect.Parameter.VAR_KEYWORD in kinds, (
            "poll_loop must declare **kwargs (or named **legacy_kwargs) so "
            "callers passing renamed/legacy keyword args don't TypeError."
        )

    def test_call_with_legacy_halt_on_divergence_pct_does_not_raise(self):
        """The exact call site that broke production for a week."""
        # Prevent the body from doing real work — _api_key() returns None
        # without FMP_API_KEY in env, so the coroutine returns early.
        old_key = os.environ.pop("FMP_API_KEY", None)
        try:
            coro = fmp_sanity.poll_loop(
                interval_s=60.0,
                halt_on_divergence_pct=0.015,  # legacy name
            )
            assert asyncio.iscoroutine(coro)
            asyncio.run(coro)
        finally:
            if old_key is not None:
                os.environ["FMP_API_KEY"] = old_key

    def test_call_with_modern_kwarg_works(self):
        old_key = os.environ.pop("FMP_API_KEY", None)
        try:
            coro = fmp_sanity.poll_loop(
                interval_s=60.0,
                divergence_threshold_pct=0.015,  # modern name
            )
            assert asyncio.iscoroutine(coro)
            asyncio.run(coro)
        finally:
            if old_key is not None:
                os.environ["FMP_API_KEY"] = old_key

    def test_base_bot_caller_uses_modern_name(self):
        """Source-level guard: if someone re-introduces the legacy name,
        catch it at test time before it ships."""
        from bots import base_bot
        src = inspect.getsource(base_bot.BaseBot.run)
        assert "divergence_threshold_pct=" in src, (
            "BaseBot.run must call fmp_sanity.poll_loop with the modern "
            "kwarg `divergence_threshold_pct=`. The legacy `halt_on_divergence_pct=` "
            "would be absorbed by **legacy_kwargs but should be cleaned up."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
