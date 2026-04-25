"""Section 4 stub coverage: Finnhub WS skeleton."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.news.finnhub_ws import (  # noqa: E402
    FINNHUB_API_KEY_ENV,
    REST_CALLS_PER_MINUTE,
    WS_SYMBOL_CAP,
    FinnhubNewsItem,
    FinnhubNewsWS,
)


def test_constants():
    assert FINNHUB_API_KEY_ENV == "FINNHUB_API_KEY"
    assert REST_CALLS_PER_MINUTE == 60
    assert WS_SYMBOL_CAP == 50


def test_news_item_defaults():
    item = FinnhubNewsItem()
    assert item.id == ""
    assert item.symbols == []


def test_unconfigured_skipped(monkeypatch):
    monkeypatch.delenv(FINNHUB_API_KEY_ENV, raising=False)
    ws = FinnhubNewsWS()
    assert ws.configured is False
    # start() should be a no-op (not raise) when unconfigured
    asyncio.run(ws.start())


def test_configured_raises_not_implemented(monkeypatch):
    monkeypatch.setenv(FINNHUB_API_KEY_ENV, "fake-key-for-tests")
    ws = FinnhubNewsWS()
    assert ws.configured is True
    with pytest.raises(NotImplementedError):
        asyncio.run(ws.start())
