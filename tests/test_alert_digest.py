"""Telegram alert digest — Sprint D F4 tests.

Verifies:
  - real-time categories bypass the digest (immediate send)
  - digest categories accumulate in the buffer
  - size threshold (default 10) flushes the buffer
  - time threshold (default 4h) flushes the buffer
  - unknown categories default to real-time (fail-safe)
  - flush message groups by category with truncation + counts
"""
from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


# ─── classify_alert() ────────────────────────────────────────────────

def test_classify_real_time():
    from core.telegram_digest import classify_alert
    for cat in ("HALT", "KILL_SWITCH", "EXIT_TIMEOUT", "BOT_FAILED",
                "RECOVERY_MODE", "BRIDGE_DOWN", "DESYNC"):
        assert classify_alert(cat) == "real_time", f"{cat} must be real-time"


def test_classify_digest():
    from core.telegram_digest import classify_alert
    for cat in ("NEWS_EVENT", "SCALE_OUT", "STRATEGY_DECAY_WARNING",
                "BREAKER_OBSERVE"):
        assert classify_alert(cat) == "digest", f"{cat} must be digest"


def test_classify_unknown_is_unknown():
    from core.telegram_digest import classify_alert
    assert classify_alert("RANDOM_NEW_CATEGORY") == "unknown"
    assert classify_alert("") == "unknown"


# ─── AlertDigest queueing + flushing ─────────────────────────────────

def test_under_threshold_no_flush():
    from core.telegram_digest import AlertDigest
    digest = AlertDigest(flush_interval_s=999999, flush_size=10)
    for i in range(9):
        flush = digest.queue_alert("NEWS_EVENT", f"msg {i}")
        assert flush is None, f"flush triggered too early at i={i}"
    assert digest.queue_size() == 9


def test_size_threshold_triggers_flush():
    from core.telegram_digest import AlertDigest
    digest = AlertDigest(flush_interval_s=999999, flush_size=10)
    for i in range(9):
        assert digest.queue_alert("NEWS_EVENT", f"msg {i}") is None
    flush = digest.queue_alert("NEWS_EVENT", "10th msg — flush trigger")
    assert flush is not None
    assert "Phoenix digest" in flush
    assert "10 alerts" in flush
    # Buffer cleared after flush
    assert digest.queue_size() == 0


def test_time_threshold_triggers_flush():
    """If `flush_interval_s` has elapsed since last flush, the next
    queue_alert flushes regardless of size."""
    from core.telegram_digest import AlertDigest
    digest = AlertDigest(flush_interval_s=0.05, flush_size=999999)
    # Queue one — fires immediately because last_flush_ts at construction
    # was set to now() and we're inside flush_interval_s window
    flush_a = digest.queue_alert("NEWS_EVENT", "first")
    assert flush_a is None  # within interval
    # Advance past the interval
    time.sleep(0.1)
    flush_b = digest.queue_alert("NEWS_EVENT", "second")
    assert flush_b is not None
    assert "Phoenix digest" in flush_b


def test_force_flush_with_pending():
    from core.telegram_digest import AlertDigest
    digest = AlertDigest()
    digest.queue_alert("NEWS_EVENT", "x")
    digest.queue_alert("SCALE_OUT", "y")
    out = digest.force_flush()
    assert out is not None
    assert "NEWS_EVENT" in out
    assert "SCALE_OUT" in out
    assert digest.queue_size() == 0


def test_force_flush_with_empty_returns_none():
    from core.telegram_digest import AlertDigest
    digest = AlertDigest()
    assert digest.force_flush() is None


# ─── digest format: groups by category, truncates, shows count ───────

def test_format_groups_by_category():
    from core.telegram_digest import AlertDigest
    digest = AlertDigest(flush_size=2)
    digest.queue_alert("NEWS_EVENT", "fed event")
    out = digest.queue_alert("SCALE_OUT", "MNQ +1 scale")
    assert out is not None
    assert "NEWS_EVENT: 1" in out
    assert "SCALE_OUT: 1" in out


def test_format_caps_at_3_examples_per_category():
    from core.telegram_digest import AlertDigest
    digest = AlertDigest(flush_size=10)
    for i in range(8):
        digest.queue_alert("NEWS_EVENT", f"event #{i}")
    digest.queue_alert("SCALE_OUT", "single")
    out = digest.queue_alert("SCALE_OUT", "trigger")  # 10th
    assert out is not None
    assert "NEWS_EVENT: 8" in out
    # Should show 3 examples + "...and N more"
    assert "and 5 more" in out


def test_format_truncates_long_messages():
    from core.telegram_digest import AlertDigest
    digest = AlertDigest(flush_size=1)
    long_msg = "x" * 200
    out = digest.queue_alert("NEWS_EVENT", long_msg)
    assert out is not None
    # Default truncation at 80 chars + ellipsis
    assert "x" * 80 in out
    assert "x" * 200 not in out


# ─── notify_alert wiring: real-time bypass + digest queue ────────────

def test_notify_alert_real_time_bypasses_digest(monkeypatch):
    """A HALT (real-time) alert must call send_sync immediately, not
    queue into the digest."""
    import asyncio
    from unittest.mock import MagicMock
    from core import telegram_digest as td
    from core import telegram_notifier as tn

    # Reset digest singleton
    td.reset_digest_for_tests()
    sent = MagicMock()
    monkeypatch.setattr(tn, "send_sync", sent)
    asyncio.run(tn.notify_alert("HALT", "kill switch engaged"))
    assert sent.called, "real-time alert must send immediately"
    # Digest stays empty
    assert td.get_digest().queue_size() == 0


def test_notify_alert_digest_category_queues(monkeypatch):
    """A NEWS_EVENT (digest) alert must NOT call send_sync (until flush)
    and must accumulate in the buffer."""
    import asyncio
    from unittest.mock import MagicMock
    from core import telegram_digest as td
    from core import telegram_notifier as tn

    td.reset_digest_for_tests()
    sent = MagicMock()
    monkeypatch.setattr(tn, "send_sync", sent)
    # Queue 3 — under threshold, no send
    for i in range(3):
        asyncio.run(tn.notify_alert("NEWS_EVENT", f"event {i}"))
    assert not sent.called
    assert td.get_digest().queue_size() == 3


def test_notify_alert_digest_flush_triggers_send(monkeypatch):
    """When size threshold hits, the FORMATTED DIGEST is sent, not the
    individual messages."""
    import asyncio
    from unittest.mock import MagicMock
    from core import telegram_digest as td
    from core import telegram_notifier as tn

    td.reset_digest_for_tests()
    # Force a small flush size so we don't need 10 messages
    td._DIGEST = td.AlertDigest(flush_interval_s=999999, flush_size=3)
    sent = MagicMock()
    monkeypatch.setattr(tn, "send_sync", sent)
    for i in range(3):
        asyncio.run(tn.notify_alert("NEWS_EVENT", f"event {i}"))
    # 3rd one triggered flush
    assert sent.call_count == 1
    flush_msg = sent.call_args.args[0] if sent.call_args.args \
                else sent.call_args.kwargs.get("message", "")
    assert "Phoenix digest" in flush_msg
    assert "3 alerts" in flush_msg


def test_notify_alert_unknown_defaults_to_real_time(monkeypatch):
    """An unclassified category falls through to immediate send."""
    import asyncio
    from unittest.mock import MagicMock
    from core import telegram_digest as td
    from core import telegram_notifier as tn

    td.reset_digest_for_tests()
    sent = MagicMock()
    monkeypatch.setattr(tn, "send_sync", sent)
    asyncio.run(tn.notify_alert("BRAND_NEW_CATEGORY", "first time"))
    assert sent.called, "unknown category must default to real-time send"


# ─── thread safety ──────────────────────────────────────────────────

def test_concurrent_queue_threadsafe():
    """Hammer queue_alert from many threads — final size must equal
    total enqueues (no lost updates) and exactly N//flush_size flushes
    happened."""
    import threading
    from core.telegram_digest import AlertDigest
    digest = AlertDigest(flush_interval_s=999999, flush_size=10)
    flush_count = [0]
    flush_lock = threading.Lock()

    def worker(n):
        for i in range(n):
            r = digest.queue_alert("NEWS_EVENT", f"t{i}")
            if r is not None:
                with flush_lock:
                    flush_count[0] += 1

    threads = [threading.Thread(target=worker, args=(50,)) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    # 200 total enqueues / 10 flush_size = 20 flushes; remainder in buffer is 0
    assert flush_count[0] == 20
    assert digest.queue_size() == 0
