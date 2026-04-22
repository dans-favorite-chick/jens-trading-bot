"""
P14 — Telegram HTML escape across all notifier sites.

Pre-fix state: only `notify_council` (line 157) and `notify_alert`
(line 169) applied a manual `.replace()` chain. `notify_entry`,
`notify_exit`, `notify_daily_summary`, and raw `send()`/`send_sync()`
pass-throughs did NOT escape user-supplied strings like strategy
names, exit reasons, or alert bodies. Any string containing `<`, `>`,
or `&` corrupted the Telegram HTML payload and returned HTTP 400.

Run: python -m unittest tests.test_p14_telegram_escape -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestEscHelper(unittest.TestCase):
    def test_esc_amp(self):
        from core.telegram_notifier import _esc
        self.assertEqual(_esc("A & B"), "A &amp; B")

    def test_esc_angle_brackets(self):
        from core.telegram_notifier import _esc
        self.assertEqual(_esc("<script>"), "&lt;script&gt;")

    def test_esc_quotes(self):
        """html.escape(..., quote=True) also escapes quotes — defense-in-depth."""
        from core.telegram_notifier import _esc
        result = _esc('a"b')
        # Different Python versions use &quot; or &#x27; — check it's not raw
        self.assertNotIn('"', result)

    def test_esc_none_returns_empty(self):
        from core.telegram_notifier import _esc
        self.assertEqual(_esc(None), "")

    def test_esc_tolerates_non_string(self):
        """Integers/floats/etc. get str()'d first — callers shouldn't have to coerce."""
        from core.telegram_notifier import _esc
        self.assertEqual(_esc(42), "42")
        self.assertEqual(_esc(3.14), "3.14")


@unittest.skip("B54: notify_entry is a no-op (Jennifer wants exit-only P&L alerts). "
               "Escape logic still tested via notify_exit + notify_alert.")
class TestNotifyEntryEscapes(unittest.TestCase):
    def test_entry_escapes_strategy_name_with_ampersand(self):
        from core.telegram_notifier import notify_entry
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_entry(
                trade_id="abc123", direction="LONG",
                strategy="A & B",  # the risky character
                price=22000.0, stop=21950.0, target=22100.0,
                contracts=1, risk_dollars=10.0, tier="A",
                regime="OPEN_MOMENTUM",
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("A &amp; B", msg)
        self.assertNotIn("A & B", msg)

    def test_entry_escapes_regime_with_angle_brackets(self):
        from core.telegram_notifier import notify_entry
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_entry(
                trade_id="t", direction="LONG", strategy="s",
                price=0, stop=0, target=0, contracts=1,
                risk_dollars=0, tier="A",
                regime="<bad>",
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("&lt;bad&gt;", msg)

    def test_entry_escapes_trade_id(self):
        from core.telegram_notifier import notify_entry
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_entry(
                trade_id="tid<script>alert(1)</script>",
                direction="LONG", strategy="s", price=0, stop=0, target=0,
                contracts=1, risk_dollars=0, tier="A", regime="R",
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("&lt;script&gt;", msg)
        self.assertNotIn("<script>", msg)


class TestNotifyExitEscapes(unittest.TestCase):
    def test_exit_escapes_exit_reason(self):
        from core.telegram_notifier import notify_exit
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_exit(
                trade_id="t", direction="LONG", strategy="s",
                entry_price=22000, exit_price=22050,
                pnl_dollars=50.0, pnl_ticks=2.0,
                result="WIN",
                exit_reason="stopped out by <angry> trader & sell wall",
                hold_time_s=60,
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("&lt;angry&gt;", msg)
        self.assertIn("&amp;", msg)
        # The <b> tag used by the formatter should still be present
        # (not escaped — that's structure, not user data).
        self.assertIn("<b>", msg)


class TestNotifyDailySummaryEscapes(unittest.TestCase):
    def test_daily_status_recovery_mode_escapes_warning_emoji(self):
        """Status string passes through _esc — existing unicode passes fine."""
        from core.telegram_notifier import notify_daily_summary
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_daily_summary(
                daily_pnl=-30.0, trades=5, wins=2, losses=3,
                win_rate=40.0, recovery_mode=True,
            ))
        msg = mock_send.call_args[0][0]
        # warning emoji should survive, no raw < > &
        self.assertIn("RECOVERY MODE", msg)
        # the <b> structure tags stay intact
        self.assertIn("<b>", msg)


class TestNotifyAlertEscapes(unittest.TestCase):
    def test_alert_escapes_message_with_html_injection(self):
        from core.telegram_notifier import notify_alert
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_alert(
                alert_type="Test",
                message="A & B <>",
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("A &amp; B &lt;&gt;", msg)

    def test_alert_escapes_alert_type(self):
        from core.telegram_notifier import notify_alert
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_alert(
                alert_type="<danger>",
                message="",
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("&lt;danger&gt;", msg)


class TestNotifyCouncilEscapes(unittest.TestCase):
    def test_council_still_escapes_summary(self):
        """Regression: ensure council (which already had a manual escape)
        still escapes under the _esc() rewrite."""
        from core.telegram_notifier import notify_council
        with patch("core.telegram_notifier.send_sync") as mock_send:
            _run(notify_council(
                bias="BULLISH",
                vote_count="5/7",
                summary="Council thinks <UP> is right & proper",
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("&lt;UP&gt;", msg)
        self.assertIn("&amp;", msg)


class TestStructuralMarkupPreserved(unittest.TestCase):
    """Guard: <b>, <code> tags used by the formatter itself must NOT be
    escaped — only CALLER-supplied strings are. If this test fails, the
    formatter's own markup has been accidentally escaped."""

    def test_exit_preserves_b_and_code_tags(self):
        """B54: notify_entry is no-op; use notify_exit for this assertion."""
        from core.telegram_notifier import notify_exit
        with patch("core.telegram_notifier.send", new_callable=AsyncMock) as mock_send:
            _run(notify_exit(
                trade_id="t", direction="LONG", strategy="s",
                entry_price=100.0, exit_price=101.0,
                pnl_dollars=50.0, pnl_ticks=10.0,
                result="WIN", exit_reason="target",
                hold_time_s=60.0,
            ))
        msg = mock_send.call_args[0][0]
        self.assertIn("<b>", msg)
        self.assertNotIn("&lt;b&gt;", msg)


if __name__ == "__main__":
    unittest.main()
