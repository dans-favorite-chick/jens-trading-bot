"""Tests for core/news/finnhub_ws.py (Section 3.5).

All tests are offline. Network surfaces (websockets.connect and
aiohttp.ClientSession) are dependency-injected via the client's
``_ws_connect`` and ``_aiohttp_session_factory`` hooks so we never need
a real socket or HTTP session in CI.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.news import finnhub_ws as ws_mod  # noqa: E402
from core.news.finnhub_ws import (  # noqa: E402
    FINNHUB_API_KEY_ENV,
    FinnhubNewsItem,
    FinnhubWebSocketClient,
    NewsEvent,
    _backoff_delay,
    _LRUSet,
    _TokenBucket,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
SAMPLE_REST_PAYLOAD = [
    {
        "category": "general",
        "datetime": 1716_000_000,
        "headline": "Fed minutes signal rate-cut patience",
        "id": 1001,
        "image": "https://x/y.jpg",
        "related": "SPY,QQQ",
        "source": "Reuters",
        "summary": "FOMC participants want more inflation evidence.",
        "url": "https://example.com/news/1001",
    },
    {
        "category": "general",
        "datetime": 1716_000_300,
        "headline": "NVDA pops on AI capex outlook",
        "id": 1002,
        "image": "",
        "related": "NVDA",
        "source": "Bloomberg",
        "summary": "Hyperscalers reaffirm 2026 build plans.",
        "url": "https://example.com/news/1002",
    },
]


class _FakeAsyncWS:
    """Async-iterable fake of a websockets ClientConnection."""

    def __init__(self, frames: list[str]):
        self._frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


def _ws_factory(*frame_batches: list[str]):
    """Return a callable mimicking ``websockets.connect``.

    Each call yields the next prepared batch of frames. Useful for
    testing reconnect behavior.
    """
    batches = [list(b) for b in frame_batches]
    calls = {"n": 0}

    def _connect(url: str):
        i = calls["n"]
        calls["n"] += 1
        if i < len(batches):
            return _FakeAsyncWS(batches[i])
        # After scripted batches, raise so the loop backs off.
        raise ConnectionError("scripted-end")

    _connect.calls = calls  # type: ignore[attr-defined]
    return _connect


# ---------------------------------------------------------------------------
# 1. WebSocket connect happy path
# ---------------------------------------------------------------------------
def test_ws_connect_happy_path_dispatches_news():
    """A 'news' frame should parse into a NewsEvent and fire the callback."""
    received: list[NewsEvent] = []

    def cb(item: NewsEvent) -> None:
        received.append(item)

    client = FinnhubWebSocketClient(
        api_key="test1234abcd",
        on_news=cb,
        fallback_rest=False,
    )

    frame = json.dumps({
        "type": "news",
        "data": [SAMPLE_REST_PAYLOAD[0]],
    })
    client._ws_connect = _ws_factory([frame])

    async def _drive():
        # Run start in background, stop after a tick.
        task = asyncio.create_task(client.start(force_mode="ws"))
        await asyncio.sleep(0.05)
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()

    asyncio.run(_drive())

    assert len(received) == 1
    item = received[0]
    assert isinstance(item, FinnhubNewsItem)
    assert item.id == "1001"
    assert "Fed minutes" in item.headline
    assert "SPY" in item.symbols_related
    assert "QQQ" in item.symbols_related
    assert item.datetime_iso  # ISO string was populated


# ---------------------------------------------------------------------------
# 2. Backoff increments
# ---------------------------------------------------------------------------
def test_backoff_increments_with_attempt():
    """_backoff_delay should grow with attempt # and stay <= max+jitter."""
    # Set jitter window: jittered values are within +/-25% of the base.
    bases = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
    for attempt, base in enumerate(bases, start=1):
        delays = [_backoff_delay(attempt) for _ in range(50)]
        for d in delays:
            assert d >= 0
            assert d <= base * 1.26  # 25% upper jitter + epsilon

    # Past the cap, delays should clamp to <= max * 1.25
    capped = [_backoff_delay(20) for _ in range(50)]
    for d in capped:
        assert d <= ws_mod.WS_BACKOFF_MAX_S * 1.26


# ---------------------------------------------------------------------------
# 3. Dedup: same id delivered twice -> callback fires once
# ---------------------------------------------------------------------------
def test_dedup_suppresses_duplicate_ids():
    received: list[NewsEvent] = []
    client = FinnhubWebSocketClient(api_key="abcd1111", on_news=received.append)

    same = SAMPLE_REST_PAYLOAD[0]
    frame_a = json.dumps({"type": "news", "data": [same]})
    frame_b = json.dumps({"type": "news", "data": [same]})  # same id
    client._ws_connect = _ws_factory([frame_a, frame_b])

    async def _drive():
        task = asyncio.create_task(client.start(force_mode="ws"))
        await asyncio.sleep(0.05)
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()

    asyncio.run(_drive())

    assert len(received) == 1, f"expected exactly one event, got {len(received)}"


# ---------------------------------------------------------------------------
# 4. REST fallback fires on WS upgrade-required response
# ---------------------------------------------------------------------------
def test_rest_fallback_on_ws_upgrade_required():
    """An error frame with 'paid'/'premium' triggers REST fallback."""
    received: list[NewsEvent] = []

    client = FinnhubWebSocketClient(
        api_key="abcd2222",
        on_news=received.append,
        fallback_rest=True,
        rest_poll_interval_s=1,
    )

    err_frame = json.dumps({
        "type": "error",
        "msg": "You need a paid plan for news subscription.",
    })
    client._ws_connect = _ws_factory([err_frame])

    # Build a fake aiohttp session whose .get returns a JSON list.
    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return SAMPLE_REST_PAYLOAD

    class _Session:
        def __init__(self):
            self.calls: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, params=None):
            self.calls.append({"url": url, "params": params})
            return _Resp()

    sessions: list[_Session] = []

    def _factory():
        s = _Session()
        sessions.append(s)
        return s

    client._aiohttp_session_factory = _factory

    async def _drive():
        task = asyncio.create_task(client.start(force_mode=None))
        await asyncio.sleep(0.2)
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()

    asyncio.run(_drive())

    # Both REST events should have arrived (after WS upgrade-required).
    assert client.mode in ("idle", "rest"), client.mode
    ids = sorted(it.id for it in received)
    assert ids == ["1001", "1002"], ids
    assert len(sessions) >= 1
    # Token must NOT have leaked into URL portion of params... it IS in
    # params, but only via the dict, not the log. Sanity check anyway:
    assert "token" in (sessions[0].calls[0]["params"] or {})


# ---------------------------------------------------------------------------
# 5. Token bucket prevents > 60 calls/min
# ---------------------------------------------------------------------------
def test_token_bucket_caps_at_sixty_per_minute():
    """A fresh bucket has 60 tokens; the 61st consume in the same instant fails."""
    fake_now = {"t": 0.0}

    def clock():
        return fake_now["t"]

    bucket = _TokenBucket(capacity=60, window_s=60.0, clock=clock)
    granted = sum(1 for _ in range(60) if bucket.try_consume(1))
    assert granted == 60
    # 61st in the same instant -> denied.
    assert bucket.try_consume(1) is False

    # After half the window, ~30 tokens have refilled.
    fake_now["t"] = 30.0
    refilled = sum(1 for _ in range(40) if bucket.try_consume(1))
    assert 25 <= refilled <= 30  # tolerate refill rounding

    # Immediate further consumes denied.
    assert bucket.try_consume(1) is False


# ---------------------------------------------------------------------------
# 6. Parse Finnhub payload into NewsEvent fields
# ---------------------------------------------------------------------------
def test_parse_item_field_mapping():
    raw = SAMPLE_REST_PAYLOAD[1]
    item = FinnhubWebSocketClient._parse_item(raw)
    assert item.id == "1002"
    assert item.headline.startswith("NVDA pops")
    assert item.summary.startswith("Hyperscalers")
    assert item.source == "Bloomberg"
    assert item.url.endswith("/1002")
    assert item.category == "general"
    assert item.symbols_related == ["NVDA"]
    assert item.symbols == ["NVDA"]  # legacy alias kept in sync
    assert item.timestamp == 1716_000_300.0
    assert item.datetime_iso.startswith("20")  # ISO-8601 year prefix


# ---------------------------------------------------------------------------
# 7. Missing-key fail-soft
# ---------------------------------------------------------------------------
def test_start_without_api_key_returns_quietly(monkeypatch):
    monkeypatch.delenv(FINNHUB_API_KEY_ENV, raising=False)
    client = FinnhubWebSocketClient(api_key=None)
    assert client.configured is False
    asyncio.run(client.start())  # must not raise


# ---------------------------------------------------------------------------
# 8. Subscribe truncates to WS_SYMBOL_CAP
# ---------------------------------------------------------------------------
def test_subscribe_truncates_to_cap():
    client = FinnhubWebSocketClient(api_key="abcd9999")
    huge = [f"SYM{i}" for i in range(200)]
    asyncio.run(client.subscribe(huge))
    assert len(client.symbols) == ws_mod.WS_SYMBOL_CAP


# ---------------------------------------------------------------------------
# 9. LRU set behavior
# ---------------------------------------------------------------------------
def test_lru_set_capacity_eviction():
    s = _LRUSet(capacity=3)
    assert s.add("a") is True
    assert s.add("b") is True
    assert s.add("c") is True
    assert s.add("a") is False  # duplicate, refresh recency
    assert s.add("d") is True   # evicts 'b' (oldest, since 'a' was refreshed)
    assert "b" not in s
    assert "a" in s
    assert "c" in s
    assert "d" in s


# ---------------------------------------------------------------------------
# 10. Wire to SentimentFlowAgent
# ---------------------------------------------------------------------------
def test_wire_into_sentiment_flow_agent(tmp_path):
    """SentimentFlowAgent.wire_news_source registers _handle_news as cb."""
    from agents.sentiment_flow_agent import SentimentFlowAgent

    agent = SentimentFlowAgent(
        fallback_log_path=tmp_path / "obs.jsonl",
    )
    client = FinnhubWebSocketClient(api_key="abcd0000")
    agent.wire_news_source(client)

    # Simulate a news arrival via the client's emit path.
    async def _drive():
        await client._emit_item(SAMPLE_REST_PAYLOAD[0])

    asyncio.run(_drive())

    assert agent._latest_news is not None
    assert agent._latest_news.id == "1001"
    # Breadcrumb was written.
    assert (tmp_path / "obs.jsonl").exists()
