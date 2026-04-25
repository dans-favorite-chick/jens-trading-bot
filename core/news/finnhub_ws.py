"""
Phoenix Bot - Finnhub News Client (Section 3.5)

Real implementation: WebSocket primary path + REST polling fallback.

Design notes
------------
Finnhub's WebSocket news subscription (``subscribe-news``) is a paid-tier
feature on most accounts. The free tier still has REST access to
``/api/v1/news?category=general``. To support both worlds we:

  1. Prefer WS. ``start()`` opens ``wss://ws.finnhub.io?token=<key>`` with
     exponential backoff (1s, 2s, 4s, ... up to 60s, +/-25% jitter) and
     dispatches news frames through the dedup LRU into ``on_news``.
  2. If the broker rejects the news subscription (paid-tier required) or
     WS is otherwise unreachable, ``start()`` flips to REST polling at
     ``rest_poll_interval_s`` (default 60s). REST mode tracks the
     newest seen ``id`` so each call only emits novel items.

Hard rules
----------
* API key never appears in logs (truncated form ``abcd***`` only).
* REST cap: 60 calls/min global. Token bucket gates this; a violation
  refuses to issue the call rather than spamming Finnhub.
* WS subscription cap: 50 symbols. Excess symbols are dropped with WARN.
* Dedup: in-memory LRU of the last 1000 news ``id`` values.
* All public coroutines fail-soft: missing key = WARN + clean return.

Public surfaces (kept stable for callers / tests)
--------------------------------------------------
* ``FinnhubWebSocketClient`` - new spec API: ``start``, ``subscribe``,
  ``stop``, plus ``on_news`` callback property.
* ``FinnhubNewsItem`` / ``NewsEvent`` - dataclass; ``NewsEvent`` is the
  spec-named alias used by the SentimentFlowAgent wiring.
* ``FinnhubNewsWS`` - legacy alias retained for the
  ``tests/test_sentiment/test_finnhub_stub.py`` baseline. ``start()`` on
  the legacy class still raises ``NotImplementedError`` when configured
  so the existing test passes unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger("FinnhubWS")

# Public env var name (DO NOT put the key in source).
FINNHUB_API_KEY_ENV = "FINNHUB_API_KEY"

# Hard rate-limit constants per Finnhub docs.
REST_CALLS_PER_MINUTE = 60
WS_SYMBOL_CAP = 50

# Reconnect backoff bounds for the WS loop.
WS_BACKOFF_MIN_S = 1.0
WS_BACKOFF_MAX_S = 60.0
WS_BACKOFF_JITTER = 0.25  # +/-25%

# Dedup LRU size.
NEWS_DEDUP_CAPACITY = 1000

# Endpoints
WS_ENDPOINT = "wss://ws.finnhub.io"
REST_NEWS_ENDPOINT = "https://finnhub.io/api/v1/news"


# ----------------------------------------------------------------------
# Data class
# ----------------------------------------------------------------------
@dataclass
class FinnhubNewsItem:
    """Normalized news payload, parsed from either WS frames or REST JSON.

    Field naming matches the Phase B+ spec (``NewsEvent`` is an alias).
    The legacy ``timestamp`` (epoch seconds) is preserved for back-compat
    while ``datetime_iso`` is the canonical ISO-8601 form for new code.
    """
    id: str = ""
    headline: str = ""
    summary: str = ""
    source: str = ""
    url: str = ""
    category: str = ""
    datetime_iso: str = ""
    symbols_related: list[str] = field(default_factory=list)
    # Legacy fields preserved (still populated for existing tests).
    symbols: list[str] = field(default_factory=list)
    timestamp: float = 0.0


# Spec-named alias.
NewsEvent = FinnhubNewsItem

# Callback may be sync or async.
NewsCallback = Callable[[FinnhubNewsItem], Union[None, Awaitable[None]]]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _redact_key(api_key: Optional[str]) -> str:
    """Return a log-safe form: first 4 chars + '***'. Empty if absent."""
    if not api_key:
        return "<unset>"
    if len(api_key) <= 4:
        return api_key[:1] + "***"
    return api_key[:4] + "***"


def _backoff_delay(attempt: int) -> float:
    """Return jittered exponential backoff in seconds for attempt >= 1."""
    base = min(WS_BACKOFF_MIN_S * (2 ** max(0, attempt - 1)), WS_BACKOFF_MAX_S)
    jitter = base * WS_BACKOFF_JITTER * (2.0 * random.random() - 1.0)
    return max(0.0, base + jitter)


class _LRUSet:
    """Tiny insertion-ordered dedup set capped at ``capacity`` entries."""

    def __init__(self, capacity: int) -> None:
        self._cap = max(1, int(capacity))
        self._items: "OrderedDict[str, None]" = OrderedDict()

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def add(self, key: str) -> bool:
        """Add ``key``. Return True if newly inserted, False if duplicate."""
        if key in self._items:
            # Refresh recency.
            self._items.move_to_end(key, last=True)
            return False
        self._items[key] = None
        if len(self._items) > self._cap:
            self._items.popitem(last=False)
        return True


class _TokenBucket:
    """Simple per-minute token bucket gating REST calls.

    ``capacity`` tokens refill at a steady rate over a 60s window. We use
    a 60-tokens-per-60-seconds default (one-per-second average), matching
    Finnhub's published REST cap.
    """

    def __init__(self, capacity: int = REST_CALLS_PER_MINUTE,
                 window_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = float(capacity)
        self.window_s = float(window_s)
        self.tokens = float(capacity)
        self._last = clock()
        self._clock = clock

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        if elapsed <= 0.0:
            return
        rate = self.capacity / self.window_s  # tokens per second
        self.tokens = min(self.capacity, self.tokens + elapsed * rate)
        self._last = now

    def try_consume(self, n: float = 1.0) -> bool:
        """Return True if ``n`` tokens were available and consumed."""
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# ----------------------------------------------------------------------
# Main client
# ----------------------------------------------------------------------
class FinnhubWebSocketClient:
    """News client with WebSocket primary + REST fallback.

    Construct with an optional callback. Call ``await start()`` to begin
    the read loop. Use ``stop()`` for graceful shutdown.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        on_news: Optional[NewsCallback] = None,
        fallback_rest: bool = True,
        rest_poll_interval_s: int = 60,
        symbols: Optional[list[str]] = None,
        category: str = "general",
    ) -> None:
        # Resolution order: explicit arg, then env var.
        self.api_key: Optional[str] = api_key or os.environ.get(FINNHUB_API_KEY_ENV)
        self._callback: Optional[NewsCallback] = on_news
        self.fallback_rest: bool = bool(fallback_rest)
        self.rest_poll_interval_s: int = max(1, int(rest_poll_interval_s))
        self.category: str = category
        self.symbols: list[str] = list(symbols or [])

        # Runtime state.
        self._mode: str = "idle"        # idle | ws | rest
        self._connected: bool = False
        self._stop_event: Optional[asyncio.Event] = None
        self._dedup: _LRUSet = _LRUSet(NEWS_DEDUP_CAPACITY)
        self._rest_bucket = _TokenBucket(REST_CALLS_PER_MINUTE)
        self._latest_news_id: Optional[int] = None
        self._task: Optional[asyncio.Task[Any]] = None

        # Hooks for tests / DI.
        self._ws_connect = None  # type: Optional[Callable[..., Any]]
        self._aiohttp_session_factory = None  # type: Optional[Callable[..., Any]]

    # ------------------------------------------------------------------
    # Public properties / setters
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def connected(self) -> bool:
        return self._connected

    def on_news(self, callback: NewsCallback) -> None:
        """Register / replace the news callback."""
        self._callback = callback

    async def subscribe(self, symbols: list[str]) -> None:
        """Cap at 50 and update the symbol filter for both modes."""
        if not isinstance(symbols, list):
            symbols = list(symbols)
        if len(symbols) > WS_SYMBOL_CAP:
            logger.warning(
                "subscribe(): truncating %d symbols to cap=%d",
                len(symbols), WS_SYMBOL_CAP,
            )
            symbols = symbols[:WS_SYMBOL_CAP]
        self.symbols = list(symbols)
        logger.info("subscribed symbols=%d mode=%s", len(self.symbols), self._mode)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, force_mode: Optional[str] = None) -> None:
        """Run the news loop until ``stop()`` is called.

        ``force_mode``: ``"ws"`` or ``"rest"`` to bypass auto-detect.
        Defaults to None (probe WS first, fall back to REST).
        """
        if not self.configured:
            logger.warning(
                "start(): no %s configured - returning without connecting",
                FINNHUB_API_KEY_ENV,
            )
            return

        self._stop_event = asyncio.Event()
        logger.info(
            "start(): key=%s mode_request=%s symbols=%d poll_s=%d",
            _redact_key(self.api_key),
            force_mode or "auto",
            len(self.symbols),
            self.rest_poll_interval_s,
        )

        try:
            if force_mode == "rest":
                await self._run_rest()
                return
            if force_mode == "ws":
                await self._run_ws(allow_fallback=False)
                return
            # Auto: try WS, fall back to REST on upgrade-required / probe-fail.
            await self._run_ws(allow_fallback=self.fallback_rest)
        finally:
            self._connected = False
            logger.info("start(): exit (mode=%s)", self._mode)
            self._mode = "idle"

    async def stop(self) -> None:
        """Signal the read loop to exit and await teardown."""
        logger.info("stop(): requested")
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("stop(): task did not exit within 5s")
            except Exception as e:
                logger.warning("stop(): task raised on shutdown: %s", e)
        self._connected = False

    # ------------------------------------------------------------------
    # WS path
    # ------------------------------------------------------------------
    async def _run_ws(self, allow_fallback: bool) -> None:
        """Inner WS loop with reconnect/backoff. Falls back to REST if
        ``allow_fallback`` is True and the broker rejects the news
        subscription (paid-tier required) or the URL is unreachable.
        """
        self._mode = "ws"
        attempt = 0
        upgrade_required = False

        # Resolve websockets.connect lazily so test mocks can override.
        if self._ws_connect is None:
            try:
                import websockets  # type: ignore
                self._ws_connect = websockets.connect
            except Exception as e:
                logger.warning("websockets import failed: %s", e)
                if allow_fallback:
                    logger.info("fallback_to_rest: websockets unavailable")
                    await self._run_rest()
                return

        url = f"{WS_ENDPOINT}?token={self.api_key}"
        log_url = f"{WS_ENDPOINT}?token={_redact_key(self.api_key)}"

        while self._stop_event is not None and not self._stop_event.is_set():
            attempt += 1
            logger.info("connecting ws=%s attempt=%d", log_url, attempt)
            try:
                ws_ctx = self._ws_connect(url)
                # websockets returns an async context manager.
                async with ws_ctx as ws:
                    self._connected = True
                    logger.info("connected ws ok")
                    attempt = 0  # reset backoff on success

                    # Send news subscription(s).
                    sub_targets = self.symbols if self.symbols else ["*"]
                    for sym in sub_targets:
                        msg = json.dumps({"type": "subscribe-news", "symbol": sym})
                        await ws.send(msg)
                    logger.info("subscribed targets=%d", len(sub_targets))

                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        upgrade_required = await self._handle_ws_frame(raw)
                        if upgrade_required:
                            logger.warning("ws upgrade-required; halting WS loop")
                            break

                self._connected = False
                if upgrade_required:
                    break  # leave reconnect loop, possibly enter fallback

            except Exception as e:
                self._connected = False
                logger.warning("ws connect/read failed: %s", e)
                # If it's the very first attempt and we have fallback enabled,
                # bail out fast so the REST path can take over.
                if attempt >= 1 and allow_fallback and not self.symbols:
                    # General-news subscription: REST is the canonical path,
                    # so we hand off rather than spinning.
                    logger.info("fallback_to_rest: ws error on first attempt")
                    await self._run_rest()
                    return

            if self._stop_event.is_set():
                break

            delay = _backoff_delay(attempt)
            logger.info("reconnecting in %.2fs (attempt=%d)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                # If wait returns without timeout, stop was requested.
                break
            except asyncio.TimeoutError:
                pass

        if upgrade_required and allow_fallback:
            logger.info("fallback_to_rest: ws subscription requires paid tier")
            await self._run_rest()

        logger.info("disconnected ws")

    async def _handle_ws_frame(self, raw: Union[str, bytes]) -> bool:
        """Parse a WS frame and dispatch news. Return True if the frame
        signals 'upgrade required' / paid-tier-needed."""
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            data = json.loads(text)
        except Exception as e:
            logger.warning("ws frame parse failed: %s", e)
            return False

        if not isinstance(data, dict):
            return False

        msg_type = str(data.get("type", "")).lower()

        if msg_type == "error":
            err_msg = str(data.get("msg", ""))
            logger.warning("ws error frame: %s", err_msg)
            lower = err_msg.lower()
            # Heuristic for "you need a paid plan" responses.
            if any(token in lower for token in (
                "paid", "premium", "upgrade", "not authorized", "forbidden",
            )):
                return True
            return False

        if msg_type == "ping":
            return False

        if msg_type == "news":
            items = data.get("data") or []
            if not isinstance(items, list):
                items = [items]
            for raw_item in items:
                if isinstance(raw_item, dict):
                    await self._emit_item(raw_item)
        # Unknown types: ignore.
        return False

    # ------------------------------------------------------------------
    # REST path
    # ------------------------------------------------------------------
    async def _run_rest(self) -> None:
        """Poll Finnhub REST ``/news`` every ``rest_poll_interval_s``."""
        self._mode = "rest"
        self._connected = True
        logger.info(
            "rest mode active key=%s poll_s=%d category=%s",
            _redact_key(self.api_key),
            self.rest_poll_interval_s,
            self.category,
        )

        # Resolve aiohttp factory lazily so tests can DI a mock.
        session_factory = self._aiohttp_session_factory
        if session_factory is None:
            try:
                import aiohttp  # type: ignore
                session_factory = aiohttp.ClientSession
            except Exception as e:
                logger.warning("aiohttp import failed: %s", e)
                self._connected = False
                return

        try:
            async with session_factory() as session:
                while self._stop_event is not None and not self._stop_event.is_set():
                    await self._rest_poll_once(session)
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=float(self.rest_poll_interval_s),
                        )
                        break  # stop_event fired
                    except asyncio.TimeoutError:
                        continue
        finally:
            self._connected = False
            logger.info("rest mode exit")

    async def _rest_poll_once(self, session: Any) -> None:
        """Issue one REST call (gated by token bucket). Best-effort, no raise."""
        if not self._rest_bucket.try_consume(1):
            logger.warning("rest poll skipped: token bucket empty")
            return

        params = {"category": self.category, "token": self.api_key}
        try:
            resp_ctx = session.get(REST_NEWS_ENDPOINT, params=params)
            async with resp_ctx as resp:
                status = getattr(resp, "status", 200)
                if status != 200:
                    logger.warning("rest poll: status=%s", status)
                    return
                payload = await resp.json()
        except Exception as e:
            logger.warning("rest poll error: %s", e)
            return

        if not isinstance(payload, list):
            return

        # Sort by id ascending so we emit in chronological order.
        try:
            payload.sort(key=lambda r: int(r.get("id", 0)))
        except Exception:
            pass

        emitted = 0
        max_seen = self._latest_news_id or 0
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            try:
                item_id = int(raw_item.get("id", 0))
            except Exception:
                item_id = 0
            if self._latest_news_id is not None and item_id <= self._latest_news_id:
                continue
            await self._emit_item(raw_item)
            emitted += 1
            if item_id > max_seen:
                max_seen = item_id

        if max_seen > (self._latest_news_id or 0):
            self._latest_news_id = max_seen
        if emitted:
            logger.info("rest poll: emitted=%d latest_id=%s", emitted, self._latest_news_id)

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------
    async def _emit_item(self, raw_item: dict) -> None:
        """Parse a raw news dict, dedup on id, dispatch to callback."""
        item = self._parse_item(raw_item)
        if not item.id:
            # Fall back to a synthetic key to avoid dropping every entry.
            item.id = f"{item.source}|{item.headline}|{item.timestamp}"
        if not self._dedup.add(item.id):
            return  # duplicate, skip
        if self._callback is None:
            return
        try:
            result = self._callback(item)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning("on_news callback raised: %s", e)

    @staticmethod
    def _parse_item(raw: dict) -> FinnhubNewsItem:
        """Map a Finnhub news dict (REST or WS) into ``FinnhubNewsItem``."""
        # ID can be int or string in different responses.
        raw_id = raw.get("id", "")
        try:
            id_str = str(int(raw_id)) if raw_id != "" else ""
        except Exception:
            id_str = str(raw_id)

        # datetime: REST returns int epoch seconds; WS uses 'datetime' too.
        dt_raw = raw.get("datetime", 0)
        try:
            ts = float(dt_raw)
        except Exception:
            ts = 0.0
        if ts > 1e12:  # ms epoch
            ts = ts / 1000.0
        try:
            from datetime import datetime, timezone
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts > 0 else ""
        except Exception:
            iso = ""

        related = raw.get("related") or ""
        if isinstance(related, str):
            symbols_related = [s.strip() for s in related.split(",") if s.strip()]
        elif isinstance(related, list):
            symbols_related = [str(s) for s in related if s]
        else:
            symbols_related = []

        return FinnhubNewsItem(
            id=id_str,
            headline=str(raw.get("headline", "") or ""),
            summary=str(raw.get("summary", "") or ""),
            source=str(raw.get("source", "") or ""),
            url=str(raw.get("url", "") or ""),
            category=str(raw.get("category", "") or ""),
            datetime_iso=iso,
            symbols_related=symbols_related,
            symbols=list(symbols_related),
            timestamp=ts,
        )


# ----------------------------------------------------------------------
# Legacy alias kept for back-compat with existing test_finnhub_stub.py.
# That test asserts ``start()`` raises NotImplementedError when an API
# key is configured, so we preserve that behavior here. New callers
# should use ``FinnhubWebSocketClient``.
# ----------------------------------------------------------------------
class FinnhubNewsWS:
    """Legacy stub kept for back-compat with pre-Section-4 tests."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get(FINNHUB_API_KEY_ENV)
        self._running = False

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def start(self) -> None:
        if not self.configured:
            logger.warning(
                "[FinnhubNewsWS] no %s set - skeleton refusing to start",
                FINNHUB_API_KEY_ENV,
            )
            return
        raise NotImplementedError(
            "FinnhubNewsWS is the legacy alias - use FinnhubWebSocketClient"
        )

    async def stop(self) -> None:
        self._running = False


__all__ = [
    "FINNHUB_API_KEY_ENV",
    "REST_CALLS_PER_MINUTE",
    "WS_SYMBOL_CAP",
    "WS_BACKOFF_MIN_S",
    "WS_BACKOFF_MAX_S",
    "WS_BACKOFF_JITTER",
    "NEWS_DEDUP_CAPACITY",
    "FinnhubNewsItem",
    "NewsEvent",
    "FinnhubWebSocketClient",
    "FinnhubNewsWS",
    "NewsCallback",
]
