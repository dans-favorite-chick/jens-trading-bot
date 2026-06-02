"""Hard $-cap on ``agents.strategy_oracle._run_llm_loop``.

BUG #3 L-3 fix (2026-06-02): when accumulated LLM spend reaches
``settings.ORACLE_HARD_CAP_USD`` the loop must break with a
CRITICAL log, fire Telegram (best-effort), and preserve whatever
``final_text`` we have so far.

The loop is driven by a stub Anthropic client that returns canned
responses with known token usage. The price constants and the cap
constant are both monkey-patched so tests run in a fixed, fast
budget regardless of model-pricing drift.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agents.strategy_oracle as so


def _resp(*, text: str, input_tokens: int, output_tokens: int,
          stop_reason: str = "end_turn"):
    """Build a fake Anthropic Message response with a known usage."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(
        content=[block],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
        ),
    )


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    import core.telegram_notifier as tg
    monkeypatch.setattr(tg, "send_sync", MagicMock(return_value=False))


@pytest.fixture
def _no_audit():
    """Minimal _RunCtx replacement that satisfies the dispatch path."""
    return MagicMock()


# ─── unit: dollar accumulator math ───────────────────────────────────────

def test_hard_cap_accumulator_increments(monkeypatch, _no_audit):
    """The loop must convert input + output tokens to USD at the listed
    rates and exit when the accumulated spend reaches the cap.
    """
    monkeypatch.setattr(so, "PRICE_PER_MTOK_INPUT", 3.00)
    monkeypatch.setattr(so, "PRICE_PER_MTOK_OUTPUT", 15.00)
    from config import settings as _settings
    monkeypatch.setattr(_settings, "ORACLE_HARD_CAP_USD", 1.50, raising=False)

    # Single turn that consumes 200K input + 50K output =
    # 200K × $3/MTok + 50K × $15/MTok = $0.60 + $0.75 = $1.35 (below cap).
    # Second turn another 100K input + 20K output = $0.30 + $0.30 = $0.60
    # → cumulative $1.95 (above cap).
    responses = [
        _resp(text="first partial", input_tokens=200_000,
              output_tokens=50_000, stop_reason="tool_use"),
        _resp(text="second partial", input_tokens=100_000,
              output_tokens=20_000, stop_reason="tool_use"),
        # Should never be consumed — the cap fires before this turn.
        _resp(text="should not see this", input_tokens=10,
              output_tokens=10),
    ]
    # Provide tool_use blocks so the loop wants to dispatch — we patch
    # _extract_tool_uses so the dispatcher doesn't actually run.
    monkeypatch.setattr(so, "_extract_tool_uses",
                        lambda _blocks: [SimpleNamespace(name="think", input={}, id="t1")])
    monkeypatch.setattr(so, "_extract_text_from_blocks",
                        lambda blocks: getattr(blocks[0], "text", ""))
    monkeypatch.setattr(so, "_dispatch_tool",
                        lambda _name, _args, _ctx: {"ok": True, "result": "noop"})
    monkeypatch.setattr(so, "_block_to_message_content", lambda b: {"type": "text", "text": getattr(b, "text", "")})

    client = MagicMock()
    client.messages.create.side_effect = responses

    final_text, total_tokens = so._run_llm_loop(
        client=client,
        system_prompt="test",
        user_msg="go",
        ctx=_no_audit,
        token_budget=10_000_000,  # token soft-cap won't fire
    )

    # The loop must have stopped at turn 2 (cap triggered).
    assert client.messages.create.call_count == 2, (
        "loop must abort after the turn whose tokens push spend over cap"
    )
    assert final_text == "second partial", (
        "partial output from the over-cap turn must be preserved"
    )
    # Total tokens reflects both consumed turns.
    assert total_tokens == 200_000 + 50_000 + 100_000 + 20_000


def test_hard_cap_critical_log_emitted(monkeypatch, _no_audit, caplog):
    monkeypatch.setattr(so, "PRICE_PER_MTOK_INPUT", 3.00)
    monkeypatch.setattr(so, "PRICE_PER_MTOK_OUTPUT", 15.00)
    from config import settings as _settings
    monkeypatch.setattr(_settings, "ORACLE_HARD_CAP_USD", 1.00, raising=False)

    monkeypatch.setattr(so, "_extract_tool_uses",
                        lambda _blocks: [SimpleNamespace(name="think", input={}, id="t1")])
    monkeypatch.setattr(so, "_extract_text_from_blocks",
                        lambda blocks: getattr(blocks[0], "text", ""))
    monkeypatch.setattr(so, "_dispatch_tool",
                        lambda _name, _args, _ctx: {"ok": True})
    monkeypatch.setattr(so, "_block_to_message_content", lambda b: {"type": "text", "text": getattr(b, "text", "")})

    # Single turn that immediately exceeds the $1.00 cap:
    # 300K input × $3/MTok = $0.90, 20K output × $15/MTok = $0.30, total $1.20.
    client = MagicMock()
    client.messages.create.side_effect = [
        _resp(text="big spend", input_tokens=300_000,
              output_tokens=20_000, stop_reason="tool_use"),
    ]

    with caplog.at_level("CRITICAL", logger="agents.strategy_oracle"):
        so._run_llm_loop(client=client, system_prompt="t", user_msg="u",
                         ctx=_no_audit, token_budget=10_000_000)

    crit = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert crit, "expected a CRITICAL log when the hard cap fires"
    assert any("hard cap" in r.getMessage() for r in crit), (
        "CRITICAL must explicitly mention the hard cap"
    )


def test_hard_cap_does_not_fire_below_threshold(monkeypatch, _no_audit):
    """Regression: ordinary runs that stay under the cap must NOT be
    cut short.
    """
    monkeypatch.setattr(so, "PRICE_PER_MTOK_INPUT", 3.00)
    monkeypatch.setattr(so, "PRICE_PER_MTOK_OUTPUT", 15.00)
    from config import settings as _settings
    monkeypatch.setattr(_settings, "ORACLE_HARD_CAP_USD", 100.00, raising=False)

    monkeypatch.setattr(so, "_extract_tool_uses", lambda _blocks: [])
    monkeypatch.setattr(so, "_extract_text_from_blocks",
                        lambda blocks: getattr(blocks[0], "text", ""))
    monkeypatch.setattr(so, "_block_to_message_content", lambda b: {"type": "text", "text": getattr(b, "text", "")})

    client = MagicMock()
    client.messages.create.side_effect = [
        _resp(text="clean exit", input_tokens=1000,
              output_tokens=500, stop_reason="end_turn"),
    ]

    final_text, total_tokens = so._run_llm_loop(
        client=client, system_prompt="t", user_msg="u",
        ctx=_no_audit, token_budget=10_000_000,
    )
    assert final_text == "clean exit"
    assert total_tokens == 1500


# ─── config wiring ───────────────────────────────────────────────────────

def test_settings_exposes_oracle_hard_cap():
    """``config.settings.ORACLE_HARD_CAP_USD`` must exist and default to
    $15.00 per the 2026-06-02 operator decision.
    """
    from config import settings
    assert hasattr(settings, "ORACLE_HARD_CAP_USD")
    assert settings.ORACLE_HARD_CAP_USD == 15.00


def test_pricing_constants_match_sonnet_4_6_list():
    """If the model is swapped (MODEL_ID), these constants must be
    updated together.
    """
    assert so.MODEL_ID == "claude-sonnet-4-6"
    assert so.PRICE_PER_MTOK_INPUT == 3.00
    assert so.PRICE_PER_MTOK_OUTPUT == 15.00
