"""2026-05-08 — Gemini 403 circuit breaker.

When the Gemini API is project-disabled at the GCP level, every call
returns 403 PERMISSION_DENIED. Each council vote triggers ~7 parallel
Gemini calls, each emitting:
  - 1 httpx INFO line ("HTTP/1.1 403 Forbidden")
  - 1 AIClient WARNING line ("Tier=fast gemini failed, falling back...")

That's ~50 lines per vote, every minute. The breaker:
  1. Trips on the FIRST 403 detected by ask_gemini.
  2. Short-circuits subsequent calls to None for _GEMINI_CIRCUIT_OPEN_S
     seconds — no API call, no log spam.
  3. Demotes the per-call "falling back" warning to DEBUG while open.
  4. Emits ONE clear ERROR line on the first trip with the GCP fix path.
  5. Resets on any successful call (manual GCP fix recovers automatically).
"""
from __future__ import annotations

import asyncio
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import agents.ai_client as aic


@pytest.fixture
def reset_circuit():
    """Reset breaker state before/after each test."""
    aic._gemini_circuit_close()
    yield
    aic._gemini_circuit_close()


class TestCircuitBreakerLifecycle:
    def test_starts_closed(self, reset_circuit):
        assert aic._gemini_circuit_is_open() is False

    def test_trip_then_open(self, reset_circuit):
        aic._gemini_circuit_trip(reason="403 PERMISSION_DENIED")
        assert aic._gemini_circuit_is_open() is True

    def test_close_resets_state(self, reset_circuit):
        aic._gemini_circuit_trip(reason="test")
        assert aic._gemini_circuit_is_open() is True
        aic._gemini_circuit_close()
        assert aic._gemini_circuit_is_open() is False

    def test_first_trip_logged_only_once(self, reset_circuit, caplog):
        import logging
        caplog.set_level(logging.ERROR, logger="AIClient")
        aic._gemini_circuit_trip(reason="403 PERMISSION_DENIED")
        aic._gemini_circuit_trip(reason="403 PERMISSION_DENIED")
        aic._gemini_circuit_trip(reason="403 PERMISSION_DENIED")
        # Only the FIRST trip should produce an ERROR log line.
        circuit_errors = [r for r in caplog.records
                          if "CIRCUIT OPENED" in r.message]
        assert len(circuit_errors) == 1, (
            f"Expected exactly one CIRCUIT OPENED log line, got "
            f"{len(circuit_errors)}. Multiple trips should NOT spam the log."
        )


class TestAskGeminiShortCircuits:
    def test_returns_none_fast_when_circuit_open(self, reset_circuit):
        aic._gemini_circuit_trip(reason="test trip")
        # No GOOGLE_API_KEY needed — circuit short-circuits before any
        # client lookup. Function must return None synchronously fast.
        result = asyncio.run(aic.ask_gemini("hello", timeout_s=0.1))
        assert result is None

    def test_403_in_exception_trips_circuit(self, reset_circuit, monkeypatch):
        """Simulate a 403 PERMISSION_DENIED exception path."""
        async def _fake_call(*a, **kw):
            raise RuntimeError(
                "403 PERMISSION_DENIED: Gemini API has not been used in "
                "project 390580647026 before or it is disabled."
            )

        # Monkeypatch the inner client so the exception path runs.
        class _BoomClient:
            class models:
                @staticmethod
                def generate_content(**kw):
                    raise RuntimeError("403 PERMISSION_DENIED: API disabled")

        monkeypatch.setattr(aic, "_get_gemini_client", lambda: _BoomClient())

        async def _run():
            return await aic.ask_gemini("hi", timeout_s=2.0)

        result = asyncio.run(_run())
        assert result is None
        assert aic._gemini_circuit_is_open() is True, (
            "A 403 / PERMISSION_DENIED error must trip the circuit."
        )

    def test_unrelated_exception_does_not_trip_circuit(
        self, reset_circuit, monkeypatch,
    ):
        """A generic timeout/parse error must NOT trip the breaker —
        only the sticky-state 403 family should."""
        class _BoomClient:
            class models:
                @staticmethod
                def generate_content(**kw):
                    raise RuntimeError("Connection reset by peer")

        monkeypatch.setattr(aic, "_get_gemini_client", lambda: _BoomClient())

        async def _run():
            return await aic.ask_gemini("hi", timeout_s=2.0)

        result = asyncio.run(_run())
        assert result is None
        assert aic._gemini_circuit_is_open() is False, (
            "Transient errors must NOT trip the circuit — only sticky 403s."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
