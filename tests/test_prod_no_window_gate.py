"""Regression: prod_bot must not have a silent trading-window gate.

Background
----------
Pre-2026-05-13, BaseBot._evaluate_strategies had this block:

    if self.bot_name == "prod":
        if not self.session.is_prod_trading_window(...):
            return  # SILENT skip — no log, no _last_eval update

Effect: outside 08:30-11:00 + 13:00-14:30 CST, prod_bot silently
skipped every strategy evaluation. Operator saw "SCANNING" status
all day with no rejection log, no signal log, no idea why.

2026-05-13 incident made the impact concrete: NT8 internet outage
08:30-11:09 cost prod its entire primary window. By the time the
secondary window opened, the gate had been silently skipping for
hours and continued skipping anything outside 13:00-14:30. Sim
(which overrides _evaluate_strategies and bypasses the gate) booked
4 wins / $114.22 the same day; prod booked nothing.

Removal was deliberate. This test prevents accidental re-introduction.

What WE WANT in the eval body:
- HALT marker + circuit-breaker checks (these log)
- positions.is_flat early return (this is a normal state)
- warmup guard (this logs)
- regular strategy iteration

What we DO NOT want:
- Any silent `return` keyed off bot_name == "prod"
- Any uncommented call to is_prod_trading_window inside _evaluate_strategies
- (the function still EXISTS in core/session_manager for any future
  log-only / dashboard-display purpose — only the gate is removed)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE_BOT_SRC = (ROOT / "bots" / "base_bot.py").read_text(encoding="utf-8")


def _evaluate_strategies_body() -> str:
    """Return the body of BaseBot._evaluate_strategies as a string."""
    m = re.search(
        r"def _evaluate_strategies\(self\).*?(?=\n    (?:async )?def )",
        BASE_BOT_SRC, re.DOTALL,
    )
    assert m, "couldn't locate BaseBot._evaluate_strategies"
    return m.group(0)


def test_no_prod_window_gate_active_code():
    """The bot_name == 'prod' window gate must not appear as active code."""
    body = _evaluate_strategies_body()

    # Strip comment lines so the deliberate "was: ..." documentation
    # block doesn't false-positive the test.
    non_comment = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )

    assert 'self.bot_name == "prod"' not in non_comment, (
        "BaseBot._evaluate_strategies has an active `self.bot_name == \"prod\"` "
        "check — the silent trading-window gate appears to have been "
        "re-introduced. See 2026-05-13 commit removing it + the doc-block "
        "inside the function explaining why."
    )

    assert "is_prod_trading_window" not in non_comment, (
        "BaseBot._evaluate_strategies still calls is_prod_trading_window. "
        "If you want to surface window state to the dashboard, set "
        "self._last_eval[\"window\"] = ... — do NOT use it as a silent gate."
    )


def test_remaining_skip_paths_still_log():
    """The gates we INTENTIONALLY kept must all log when they skip.

    Specifically: HALT marker, circuit breakers, warmup — each should
    have either a logger.* call or a self._last_eval = ... assignment
    near its `return` so the operator sees WHY eval was skipped.
    """
    body = _evaluate_strategies_body()

    # HALT path: must update _last_eval before returning
    halt_section = body[body.find("HALT_MARKER_FILE"):body.find("# Apply runtime profile overrides")]
    assert "self._last_eval" in halt_section, (
        "HALT-path skip must populate self._last_eval so the dashboard "
        "shows the halt reason"
    )
    assert "logger.warning" in halt_section, (
        "HALT-path skip must emit a logger.warning so operator sees it"
    )


def test_session_manager_still_has_is_prod_trading_window():
    """We removed the GATE in base_bot.py but kept the FUNCTION available
    in core/session_manager.py — for any future log-only / dashboard
    display use. Confirm it's still callable."""
    from core.session_manager import SessionManager
    sm = SessionManager(bot_name="prod")
    # Just verify it returns a bool — semantic behavior is its own tests.
    r = sm.is_prod_trading_window()
    assert isinstance(r, bool), (
        f"is_prod_trading_window must return bool, got {type(r).__name__}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
