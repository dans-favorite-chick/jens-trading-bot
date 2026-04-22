"""
Phase C — Per-strategy Telegram routing + tagging.

Verifies:
  - notify_* routes to TELEGRAM_CHAT_ID default when no override
  - routes to override chat_id when strategy has one
  - prepends [strategy] tag when TELEGRAM_TAG_STRATEGY=True
  - omits tag when flag is False
  - sub-strategy keys (e.g., "opening_session.orb") resolve correctly
  - unknown strategy falls through to default

Run: python -m pytest tests/test_telegram_routing.py -v
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


class TelegramRoutingTests(unittest.TestCase):
    DEFAULT_CHAT = "-1001111111111"

    def _patch_config(self, overrides: dict, tag: bool = True):
        """Return list of patchers — caller must start/stop."""
        import core.telegram_notifier as tn
        return [
            patch.object(tn, "CHAT_ID", self.DEFAULT_CHAT),
            patch.object(tn, "TOKEN", "fake-token"),
            patch.object(tn, "TELEGRAM_STRATEGY_CHAT_OVERRIDES", overrides),
            patch.object(tn, "TELEGRAM_TAG_STRATEGY", tag),
        ]

    def _call_entry(self, strategy: str):
        """B54: notify_entry is a no-op. Route tests now use notify_exit
        (real trade-close path) which IS the user-visible send for each
        strategy."""
        from core.telegram_notifier import notify_exit
        with patch("core.telegram_notifier.send", new_callable=AsyncMock) as mock_send:
            _run(notify_exit(
                trade_id="t1", direction="LONG", strategy=strategy,
                entry_price=22000.0, exit_price=22050.0,
                pnl_dollars=50.0, pnl_ticks=20.0,
                result="WIN", exit_reason="target",
                hold_time_s=120.0,
            ))
        args, kwargs = mock_send.call_args
        text = args[0]
        chat_id = kwargs.get("chat_id")
        return text, chat_id

    def test_default_channel_when_no_override(self):
        patchers = self._patch_config(overrides={}, tag=True)
        for p in patchers:
            p.start()
        try:
            text, chat_id = self._call_entry("bias_momentum")
            self.assertEqual(chat_id, self.DEFAULT_CHAT)
        finally:
            for p in patchers:
                p.stop()

    def test_override_channel_used(self):
        override_chat = "-1009999999999"
        patchers = self._patch_config(
            overrides={"bias_momentum": override_chat}, tag=True
        )
        for p in patchers:
            p.start()
        try:
            text, chat_id = self._call_entry("bias_momentum")
            self.assertEqual(chat_id, override_chat)
        finally:
            for p in patchers:
                p.stop()

    def test_tag_prefix_present_when_flag_true(self):
        patchers = self._patch_config(overrides={}, tag=True)
        for p in patchers:
            p.start()
        try:
            text, _ = self._call_entry("bias_momentum")
            self.assertTrue(text.startswith("[bias_momentum] "),
                            f"Expected tag prefix, got: {text[:40]}")
        finally:
            for p in patchers:
                p.stop()

    def test_tag_absent_when_flag_false(self):
        patchers = self._patch_config(overrides={}, tag=False)
        for p in patchers:
            p.start()
        try:
            text, _ = self._call_entry("bias_momentum")
            self.assertFalse(text.startswith("[bias_momentum]"),
                             f"Tag should be absent, got: {text[:40]}")
        finally:
            for p in patchers:
                p.stop()

    def test_sub_strategy_key_resolves(self):
        override_chat = "-1008888888888"
        patchers = self._patch_config(
            overrides={"opening_session.orb": override_chat}, tag=True
        )
        for p in patchers:
            p.start()
        try:
            text, chat_id = self._call_entry("opening_session.orb")
            self.assertEqual(chat_id, override_chat)
            self.assertTrue(text.startswith("[opening_session.orb] "))
        finally:
            for p in patchers:
                p.stop()

    def test_unknown_strategy_falls_through_to_default(self):
        patchers = self._patch_config(
            overrides={"bias_momentum": "-100111"}, tag=True
        )
        for p in patchers:
            p.start()
        try:
            text, chat_id = self._call_entry("no_such_strategy")
            self.assertEqual(chat_id, self.DEFAULT_CHAT)
        finally:
            for p in patchers:
                p.stop()

    def test_notify_alert_routes_with_strategy(self):
        override_chat = "-1007777777777"
        patchers = self._patch_config(
            overrides={"spring_setup": override_chat}, tag=True
        )
        for p in patchers:
            p.start()
        try:
            from core.telegram_notifier import notify_alert
            # B54: notify_alert now calls send_sync directly via executor
            # (for dedup support) instead of the old `send` async wrapper.
            with patch("core.telegram_notifier.send_sync") as mock_send:
                _run(notify_alert("KILL_SWITCH", "halted",
                                  strategy="spring_setup"))
            args, kwargs = mock_send.call_args
            self.assertEqual(kwargs.get("chat_id"), override_chat)
            self.assertTrue(args[0].startswith("[spring_setup] "))
        finally:
            for p in patchers:
                p.stop()

    def test_notify_alert_without_strategy_default(self):
        """Backward compat: calling notify_alert with no strategy still works."""
        patchers = self._patch_config(overrides={}, tag=True)
        for p in patchers:
            p.start()
        try:
            from core.telegram_notifier import notify_alert
            # B54: notify_alert now calls send_sync directly via executor
            # (for dedup support) instead of the old `send` async wrapper.
            with patch("core.telegram_notifier.send_sync") as mock_send:
                _run(notify_alert("RECOVERY", "entering recovery"))
            args, kwargs = mock_send.call_args
            self.assertEqual(kwargs.get("chat_id"), self.DEFAULT_CHAT)
            # No tag when strategy not provided
            self.assertFalse(args[0].startswith("["))
        finally:
            for p in patchers:
                p.stop()


if __name__ == "__main__":
    unittest.main()
