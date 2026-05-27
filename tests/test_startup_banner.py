"""Startup banner — bots/base_bot.py:_print_startup_banner.

Per operator request (2026-05-25): every prod_bot / sim_bot process must
emit a multi-line safety-configuration snapshot as the FIRST thing in
the log, so the operator can verify the loaded config matches their
expectations after a restart. If LIVE_TRADING / FREEZE_ACTIVE /
LIVE_STRATEGY_ALLOWLIST / walk-forward gates don't match, they kill the
bot before it sends a tick.

This test:
  - captures log output produced by BaseBot.__init__,
  - asserts the banner is present with the required fields,
  - asserts the banner is printed BEFORE any strategy evaluation
    (i.e. before load_strategies() / _on_bar() can fire).

Run: pytest tests/test_startup_banner.py -v
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def captured_logs(caplog):
    """Capture every log record at INFO+ from the 'Bot' logger."""
    caplog.set_level(logging.INFO, logger="Bot")
    return caplog


def _banner_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_banner_appears_on_bot_init(captured_logs):
    """Banner emits on BaseBot() construction — before run() is awaited."""
    from bots.base_bot import BaseBot
    _ = BaseBot()
    text = _banner_text(captured_logs)
    assert "Phoenix Bot starting" in text, (
        f"Expected banner header in log output. Got:\n{text}"
    )
    assert "safety configuration snapshot" in text


def test_banner_has_required_fields(captured_logs):
    """All operator-verifiable safety fields must be present in the banner."""
    from bots.base_bot import BaseBot
    _ = BaseBot()
    text = _banner_text(captured_logs)

    required = [
        "LIVE_TRADING:",
        "LIVE_STRATEGY_ALLOWLIST:",
        "FREEZE_ACTIVE:",
        "Strategies loading:",
        "Walk-forward gates:",
        "WEEKLY_LOSS_LIMIT:",
        "Daily loss cap:",
        "AI agents:",
        "Trace IDs:",
        "Process:",
        "Started at:",
    ]
    missing = [f for f in required if f not in text]
    assert not missing, (
        f"Banner missing required fields: {missing}\nFull banner:\n{text}"
    )


def test_banner_lists_allowlist_and_freeze_active(captured_logs):
    """The actually-loaded LIVE_STRATEGY_ALLOWLIST and FREEZE_ACTIVE
    values must appear in the banner — not hardcoded strings."""
    from config.settings import LIVE_STRATEGY_ALLOWLIST
    from config.strategies import FREEZE_ACTIVE
    from bots.base_bot import BaseBot

    _ = BaseBot()
    text = _banner_text(captured_logs)

    # The repr/str of the actual allowlist must be in the banner.
    # We check for at least one allowlisted strategy name OR the empty
    # tuple repr — covers any future config change.
    if LIVE_STRATEGY_ALLOWLIST:
        for name in LIVE_STRATEGY_ALLOWLIST:
            assert name in text, (
                f"Banner should list allowlist strategy {name!r}. Got:\n{text}"
            )
    assert f"FREEZE_ACTIVE:     {FREEZE_ACTIVE}" in text or str(FREEZE_ACTIVE) in text


def test_banner_shows_walk_forward_gates(captured_logs):
    """The banner must surface walk-forward gates — hard_block strategies
    are named explicitly; informational strategies are collapsed to a
    count (operator only needs to act on hard_block items)."""
    from bots.base_bot import BaseBot
    _ = BaseBot()
    text = _banner_text(captured_logs)

    # Spec requires "hard_block" for bias_momentum and an "informational"
    # bucket for the rest. The current config has at least bias_momentum
    # at hard_block.
    assert "hard_block" in text, f"Expected 'hard_block' gate in banner:\n{text}"
    assert "informational" in text, f"Expected 'informational' gate in banner:\n{text}"


def test_banner_printed_before_strategy_evaluation(captured_logs):
    """The banner MUST appear in the log stream before any strategy
    evaluation could fire. We assert this structurally: BaseBot()
    finishes construction (during which the banner emits) WITHOUT
    triggering load_strategies() / _on_bar() / any strategy code.

    The check is: the 'Phoenix Bot starting' banner header appears in
    the captured records, AND no record from any strategy logger
    (loggers whose name starts with 'strategies' or 'Strategy') has
    been emitted by the time __init__ returns.
    """
    from bots.base_bot import BaseBot
    bot = BaseBot()

    # Find the banner header record index.
    banner_idx = None
    for i, r in enumerate(captured_logs.records):
        if "Phoenix Bot starting" in r.getMessage():
            banner_idx = i
            break
    assert banner_idx is not None, "Banner header not found in log output"

    # No strategy-evaluation log records should precede the banner.
    # (More importantly, no strategies have even been *loaded* — this
    # is the structural invariant.)
    assert bot.strategies == [], (
        f"Strategies were loaded during __init__ — banner can no longer "
        f"be guaranteed to precede strategy evaluation. Got: "
        f"{[type(s).__name__ for s in bot.strategies]}"
    )

    # Also: no record before the banner should originate from a
    # strategy/eval logger. Allow framework loggers (websockets, etc.)
    # to pre-empt the banner — only strategy code is the concern.
    forbidden_prefixes = ("strategies.", "Strategy.")
    for r in captured_logs.records[:banner_idx]:
        assert not r.name.startswith(forbidden_prefixes), (
            f"Strategy log record {r.name!r}: {r.getMessage()!r} "
            f"appeared before the startup banner."
        )


def test_banner_handles_missing_config_gracefully(monkeypatch, captured_logs):
    """If a config attribute is missing, the banner must print
    '<unset>' rather than crashing the bot at startup."""
    import config.strategies as cfg_strats
    # Temporarily hide FREEZE_ACTIVE so the lazy import in the banner
    # raises AttributeError. The banner should swallow it and print
    # '<unset>' for that field, leaving all other fields intact.
    monkeypatch.delattr(cfg_strats, "FREEZE_ACTIVE", raising=False)

    from bots.base_bot import BaseBot
    _ = BaseBot()  # must NOT raise

    text = _banner_text(captured_logs)
    assert "Phoenix Bot starting" in text
    assert "FREEZE_ACTIVE:" in text  # the line is still emitted
    assert "<unset>" in text, (
        f"Expected '<unset>' fallback for missing FREEZE_ACTIVE. Got:\n{text}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
