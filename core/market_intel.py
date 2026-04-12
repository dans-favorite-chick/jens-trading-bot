"""
Market Intelligence Module for Phoenix Trading Bot.

Provides real-time market context from multiple free data sources:
  - VIX monitoring (Alpaca -> yfinance -> cache fallback)
  - News scanning via Finnhub (headline classification)
  - Trump Truth Social sentiment (CNN archive + VADER)
  - Economic calendar via Finnhub
  - Reddit/WSB momentum via ApeWisdom API
  - FRED macro data (Fed Funds Rate, CPI trend)
  - Market regime context via yfinance + Alpaca

All calls are async with 5-second timeouts and safe defaults on failure.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as _requests

import finnhub
import yfinance as yf

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
except ImportError:
    _vader = None

logger = logging.getLogger("MarketIntel")

# ---------------------------------------------------------------------------
# Load .env from project root (if not already loaded by parent process)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# API clients (lazy-init)
# ---------------------------------------------------------------------------
_finnhub_client: finnhub.Client | None = None
_alpaca_api = None


def _get_finnhub() -> finnhub.Client | None:
    global _finnhub_client
    if _finnhub_client is None:
        key = os.environ.get("FINNHUB_API_KEY")
        if key:
            _finnhub_client = finnhub.Client(api_key=key)
    return _finnhub_client


def _get_alpaca():
    global _alpaca_api
    if _alpaca_api is None:
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if key and secret:
            import alpaca_trade_api as tradeapi
            _alpaca_api = tradeapi.REST(
                key, secret, base_url="https://paper-api.alpaca.markets", api_version="v2"
            )
    return _alpaca_api


# ---------------------------------------------------------------------------
# Simple TTL cache
# ---------------------------------------------------------------------------
class _Cache:
    def __init__(self):
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str, max_age: float) -> object | None:
        if key in self._store:
            ts, val = self._store[key]
            if time.time() - ts < max_age:
                return val
        return None

    def put(self, key: str, val: object):
        self._store[key] = (time.time(), val)


_cache = _Cache()

# ---------------------------------------------------------------------------
# News headline classification
# ---------------------------------------------------------------------------
_TIER1_KEYWORDS = [
    "fomc", "federal reserve rate", "interest rate decision",
    "cpi release", "cpi report", "consumer price index",
    "ppi release", "ppi report", "producer price index",
    "nonfarm payroll", "non-farm payroll", "nfp report",
    "gdp release", "gdp report",
]
_TIER2_KEYWORDS = [
    "earnings", "tariff", "trade war", "geopolitical",
    "sanctions", "war", "invasion", "missile", "nuclear",
    "default", "debt ceiling", "government shutdown",
    "recession", "downturn", "bank failure",
]
_HEAVY_TICKERS = {"NVDA", "AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMZN", "TSLA"}
_HIGH_IMPACT_EVENTS = {
    "fomc", "cpi", "ppi", "nfp", "gdp", "jobless claims",
    "pce", "non-farm", "nonfarm", "federal funds rate",
}


def _classify_headline(headline: str) -> int:
    """Return tier: 1 (hard stop), 2 (caution), 3 (info)."""
    lower = headline.lower()
    for kw in _TIER1_KEYWORDS:
        if kw in lower:
            return 1
    for kw in _TIER2_KEYWORDS:
        if kw in lower:
            return 2
    # Check if headline mentions a NQ heavy-weight ticker
    upper = headline.upper()
    for ticker in _HEAVY_TICKERS:
        if ticker in upper:
            return 2
    return 3


# ---------------------------------------------------------------------------
# VIX Monitor (3-tier fallback)
# ---------------------------------------------------------------------------
async def get_vix() -> dict:
    """
    Fetch VIX with 3-tier fallback:
      1. Alpaca real-time quote for UVXY (VIX proxy)
      2. yfinance ^VIX (15-min delay)
      3. Cached value or 0
    Cache: 60 seconds.
    """
    cached = _cache.get("vix", 60)
    if cached is not None:
        cached["age_s"] = round(time.time() - _cache._store["vix"][0], 1)
        return cached

    # Tier 1: Alpaca
    try:
        api = _get_alpaca()
        if api:
            quote = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, lambda: api.get_latest_quote("UVXY")),
                timeout=5.0,
            )
            vix_proxy = float(quote.ap) if hasattr(quote, "ap") and quote.ap else 0
            if vix_proxy > 0:
                result = {"vix_proxy": round(vix_proxy, 2), "source": "alpaca_UVXY", "age_s": 0}
                _cache.put("vix", result)
                logger.info(f"VIX proxy (UVXY) from Alpaca: {vix_proxy}")
                return result
    except Exception as e:
        logger.debug(f"Alpaca VIX fallback: {e}")

    # Tier 2: yfinance
    try:
        def _yf_vix():
            ticker = yf.Ticker("^VIX")
            hist = ticker.history(period="1d", interval="1m")
            if hist is not None and len(hist) > 0:
                return float(hist["Close"].iloc[-1])
            return None

        vix_val = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _yf_vix),
            timeout=5.0,
        )
        if vix_val and vix_val > 0:
            result = {"vix": round(vix_val, 2), "source": "yfinance", "age_s": 0}
            _cache.put("vix", result)
            logger.info(f"VIX from yfinance: {vix_val}")
            return result
    except Exception as e:
        logger.debug(f"yfinance VIX fallback: {e}")

    # Tier 3: cached or zero
    stale = _cache.get("vix", 3600)  # accept up to 1 hour stale
    if stale is not None:
        stale["source"] = "cache_stale"
        stale["age_s"] = round(time.time() - _cache._store["vix"][0], 1)
        return stale

    logger.warning("VIX unavailable from all sources, returning 0")
    return {"vix": 0, "source": "unavailable", "age_s": -1}


# ---------------------------------------------------------------------------
# News Scanner
# ---------------------------------------------------------------------------
async def get_market_news() -> dict:
    """
    Fetch market news from Finnhub (last 2 hours).
    Classify each headline into tiers.
    Cache: 120 seconds.
    """
    cached = _cache.get("news", 120)
    if cached is not None:
        return cached

    headlines = []
    tier1_active = False
    tier2_active = False
    highest_tier = 3
    summary_parts = []

    try:
        fc = _get_finnhub()
        if fc:
            def _fetch_news():
                return fc.general_news("general", min_id=0)

            raw = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _fetch_news),
                timeout=5.0,
            )
            cutoff = time.time() - 14400  # 4 hours ago
            for item in (raw or []):
                pub_time = item.get("datetime", 0)
                if pub_time < cutoff:
                    continue
                headline_text = item.get("headline", "")
                if not headline_text:
                    continue
                tier = _classify_headline(headline_text)
                entry = {
                    "headline": headline_text,
                    "source": item.get("source", ""),
                    "tier": tier,
                    "time": datetime.fromtimestamp(pub_time, tz=timezone.utc).isoformat(),
                    "url": item.get("url", ""),
                }
                headlines.append(entry)
                if tier == 1:
                    tier1_active = True
                    summary_parts.append(f"[TIER1] {headline_text[:80]}")
                elif tier == 2:
                    tier2_active = True
                    summary_parts.append(f"[TIER2] {headline_text[:80]}")
                highest_tier = min(highest_tier, tier)

            # Sort by tier (most important first), then by time
            headlines.sort(key=lambda h: (h["tier"], h["time"]), reverse=False)
            # Keep top 20
            headlines = headlines[:20]
    except Exception as e:
        logger.warning(f"Finnhub news fetch failed: {e}")

    result = {
        "headlines": headlines,
        "tier1_active": tier1_active,
        "tier2_active": tier2_active,
        "highest_tier": highest_tier if headlines else 3,
        "summary": "; ".join(summary_parts[:5]) if summary_parts else "No significant news",
        "count": len(headlines),
    }
    _cache.put("news", result)
    return result


# ---------------------------------------------------------------------------
# Economic Calendar
# ---------------------------------------------------------------------------
async def get_economic_calendar() -> dict:
    """
    Fetch today's economic calendar from Finnhub.
    Flag upcoming high-impact events within 30 minutes.
    Cache: 300 seconds.
    """
    cached = _cache.get("calendar", 300)
    if cached is not None:
        return cached

    events_today = []
    next_event = None
    trade_restricted = False
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    try:
        fc = _get_finnhub()
        if fc:
            def _fetch_cal():
                return fc.calendar_economic(
                    _from=today_str, to=today_str
                )

            raw = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _fetch_cal),
                timeout=5.0,
            )
            cal_events = []
            if raw and isinstance(raw, dict):
                cal_events = raw.get("economicCalendar", [])
            elif raw and isinstance(raw, list):
                cal_events = raw

            for ev in cal_events:
                event_name = ev.get("event", "")
                impact = ev.get("impact", "low")
                event_time_str = ev.get("time", "")
                country = ev.get("country", "")

                # Only US events
                if country and country.upper() != "US":
                    continue

                # Parse event time
                minutes_until = None
                if event_time_str:
                    try:
                        event_dt = datetime.strptime(
                            f"{today_str} {event_time_str}", "%Y-%m-%d %H:%M"
                        ).replace(tzinfo=timezone.utc)
                        minutes_until = (event_dt - now).total_seconds() / 60
                    except ValueError:
                        pass

                # Check if this is a high-impact event we track
                is_high = False
                lower_name = event_name.lower()
                for kw in _HIGH_IMPACT_EVENTS:
                    if kw in lower_name:
                        is_high = True
                        break
                if impact and impact.lower() == "high":
                    is_high = True

                entry = {
                    "name": event_name,
                    "time": event_time_str,
                    "impact": "HIGH" if is_high else impact.upper() if impact else "LOW",
                    "minutes_until": round(minutes_until, 1) if minutes_until is not None else None,
                }
                events_today.append(entry)

                # Track the next upcoming high-impact event
                if is_high and minutes_until is not None and minutes_until > -10:
                    if next_event is None or (minutes_until < next_event.get("minutes_until", 9999)):
                        next_event = entry

                    # Restrict trading if high-impact event within 5 minutes
                    if -5 <= minutes_until <= 5:
                        trade_restricted = True

    except Exception as e:
        logger.warning(f"Finnhub calendar fetch failed: {e}")

    result = {
        "events_today": events_today,
        "next_event": next_event,
        "trade_restricted": trade_restricted,
        "count": len(events_today),
    }
    _cache.put("calendar", result)
    return result


# ---------------------------------------------------------------------------
# Market Regime Context
# ---------------------------------------------------------------------------
async def get_market_context() -> dict:
    """
    Build market regime context from yfinance NQ=F bars.
    Includes overnight range, gap %, trend classification.
    Cache: 120 seconds.
    """
    cached = _cache.get("context", 120)
    if cached is not None:
        return cached

    result = {
        "overnight_range": {"high": 0, "low": 0, "range_ticks": 0},
        "gap_pct": 0.0,
        "premarket_volume_ratio": 0.0,
        "trend_5m": "UNKNOWN",
    }

    try:
        def _yf_nq():
            ticker = yf.Ticker("NQ=F")
            # 2 hours of 5-min bars
            hist = ticker.history(period="2d", interval="5m")
            return hist

        hist = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _yf_nq),
            timeout=5.0,
        )

        if hist is not None and len(hist) > 10:
            # Overnight range: bars from today before 9:30 ET (14:30 UTC)
            now_utc = datetime.now(timezone.utc)
            today_open_utc = now_utc.replace(hour=14, minute=30, second=0, microsecond=0)

            # Use last 2 hours of data for range
            recent = hist.tail(24)  # ~2 hours of 5-min bars
            high = float(recent["High"].max())
            low = float(recent["Low"].min())
            range_ticks = int((high - low) / 0.25)  # MNQ tick = 0.25

            result["overnight_range"] = {
                "high": round(high, 2),
                "low": round(low, 2),
                "range_ticks": range_ticks,
            }

            # Gap calculation: compare today's first bar open vs yesterday's last bar close
            if len(hist) > 24:
                yesterday_close = float(hist["Close"].iloc[-25])
                today_open = float(hist["Open"].iloc[-24])
                if yesterday_close > 0:
                    result["gap_pct"] = round(
                        ((today_open - yesterday_close) / yesterday_close) * 100, 3
                    )

            # Trend: simple — are last 12 bars trending up or down?
            closes = recent["Close"].values
            if len(closes) >= 12:
                first_half = float(closes[:6].mean())
                second_half = float(closes[6:12].mean())
                diff_pct = ((second_half - first_half) / first_half) * 100
                if diff_pct > 0.05:
                    result["trend_5m"] = "BULLISH"
                elif diff_pct < -0.05:
                    result["trend_5m"] = "BEARISH"
                else:
                    result["trend_5m"] = "NEUTRAL"

            # Volume ratio: last 6 bars avg vs prior 18 bars avg
            if len(recent) >= 24:
                recent_vol = float(recent["Volume"].tail(6).mean())
                prior_vol = float(recent["Volume"].head(18).mean())
                if prior_vol > 0:
                    result["premarket_volume_ratio"] = round(recent_vol / prior_vol, 2)

    except Exception as e:
        logger.warning(f"Market context fetch failed: {e}")

    _cache.put("context", result)
    return result


# ---------------------------------------------------------------------------
# Trump / Truth Social Sentiment (CNN Archive + VADER)
# ---------------------------------------------------------------------------
_TRUTH_ARCHIVE_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"


async def get_trump_sentiment() -> dict:
    """
    Fetch Trump's recent Truth Social posts via CNN archive.
    Run VADER sentiment analysis. Cache: 120 seconds.

    Returns: {score: float [-1,1], posts: int, latest: str, source: str}
    """
    cached = _cache.get("trump", 120)
    if cached is not None:
        return cached

    result = {"score": 0.0, "posts": 0, "latest": "", "source": "unavailable",
              "tariff_mentioned": False, "market_keywords": []}

    try:
        def _fetch():
            session = _requests.Session()
            session.trust_env = False
            resp = session.get(_TRUTH_ARCHIVE_URL, timeout=5)
            resp.raise_for_status()
            return resp.json()

        data = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _fetch),
            timeout=5.0,
        )

        if isinstance(data, list) and data:
            # Take last 5 posts
            posts = data[:5]
            texts = [str(p.get("content", "")) for p in posts]
            combined = " ".join(texts).strip()

            if combined and _vader:
                sentiment = _vader.polarity_scores(combined)
                result["score"] = round(sentiment["compound"], 3)

            result["posts"] = len(posts)
            result["latest"] = texts[0][:150] if texts else ""
            result["source"] = "cnn_archive"

            # Check for market-moving keywords
            lower = combined.lower()
            keywords_found = []
            for kw in ["tariff", "trade deal", "china", "fed", "interest rate",
                        "stock market", "economy", "tax", "sanction", "trade war",
                        "nasdaq", "dow", "crash", "boom", "billion", "trillion"]:
                if kw in lower:
                    keywords_found.append(kw)

            result["tariff_mentioned"] = "tariff" in lower or "trade war" in lower
            result["market_keywords"] = keywords_found

            if keywords_found:
                logger.info(f"Trump sentiment: {result['score']:.2f}, "
                             f"keywords: {keywords_found}")

    except Exception as e:
        logger.debug(f"Trump sentiment fetch failed: {e}")

    _cache.put("trump", result)
    return result


# ---------------------------------------------------------------------------
# Reddit / WSB Momentum via ApeWisdom API
# ---------------------------------------------------------------------------
_APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"


async def get_reddit_momentum() -> dict:
    """
    Fetch top mentioned tickers from Reddit (WSB, stocks, etc.)
    via ApeWisdom free API. Cache: 300 seconds.

    Returns: {top_mentions: [{ticker, mentions, rank}], nq_relevant: [...]}
    """
    cached = _cache.get("reddit", 300)
    if cached is not None:
        return cached

    result = {"top_mentions": [], "nq_relevant": [], "source": "unavailable"}

    # NQ-heavy tickers to watch
    nq_tickers = {"NVDA", "AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMZN",
                   "TSLA", "NFLX", "AMD", "AVGO", "CRM", "COST", "ADBE",
                   "QQQ", "TQQQ", "SQQQ"}

    try:
        def _fetch():
            session = _requests.Session()
            session.trust_env = False
            resp = session.get(_APEWISDOM_URL, timeout=5)
            resp.raise_for_status()
            return resp.json()

        data = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _fetch),
            timeout=5.0,
        )

        results_list = data.get("results", [])
        top = []
        nq_relevant = []

        for i, item in enumerate(results_list[:30]):
            ticker = item.get("ticker", "")
            mentions = item.get("mentions", 0)
            upvotes = item.get("upvotes", 0)
            entry = {"ticker": ticker, "mentions": mentions,
                     "upvotes": upvotes, "rank": i + 1}
            top.append(entry)

            if ticker in nq_tickers:
                nq_relevant.append(entry)

        result["top_mentions"] = top[:10]
        result["nq_relevant"] = nq_relevant
        result["source"] = "apewisdom"

        if nq_relevant:
            logger.info(f"Reddit NQ mentions: {[t['ticker'] for t in nq_relevant[:5]]}")

    except Exception as e:
        logger.debug(f"ApeWisdom fetch failed: {e}")

    _cache.put("reddit", result)
    return result


# ---------------------------------------------------------------------------
# FRED Macro Data (Federal Reserve Economic Data)
# ---------------------------------------------------------------------------

async def get_fred_macro() -> dict:
    """
    Fetch key macro indicators from FRED API.
    Cache: 3600 seconds (1 hour — macro data changes slowly).

    Returns: {fed_funds_rate, cpi_yoy, unemployment, source}
    """
    cached = _cache.get("fred", 3600)
    if cached is not None:
        return cached

    result = {"fed_funds_rate": None, "cpi_yoy": None,
              "unemployment": None, "source": "unavailable"}

    try:
        # FRED API is free, no key needed for basic series
        def _fetch_fred():
            out = {}
            base = "https://api.stlouisfed.org/fred/series/observations"
            # Fed Funds Rate (DFF)
            try:
                resp = _requests.get(base, params={
                    "series_id": "DFF", "sort_order": "desc",
                    "limit": "1", "file_type": "json",
                    "api_key": os.environ.get("FRED_API_KEY", ""),
                }, timeout=5)
                if resp.status_code == 200:
                    obs = resp.json().get("observations", [])
                    if obs:
                        out["fed_funds_rate"] = float(obs[0]["value"])
            except Exception:
                pass

            # CPI Year-over-Year (CPIAUCSL)
            try:
                resp = _requests.get(base, params={
                    "series_id": "CPIAUCSL", "sort_order": "desc",
                    "limit": "13", "file_type": "json",
                    "api_key": os.environ.get("FRED_API_KEY", ""),
                }, timeout=5)
                if resp.status_code == 200:
                    obs = resp.json().get("observations", [])
                    if len(obs) >= 13:
                        latest = float(obs[0]["value"])
                        year_ago = float(obs[12]["value"])
                        out["cpi_yoy"] = round((latest - year_ago) / year_ago * 100, 2)
            except Exception:
                pass

            # Unemployment Rate (UNRATE)
            try:
                resp = _requests.get(base, params={
                    "series_id": "UNRATE", "sort_order": "desc",
                    "limit": "1", "file_type": "json",
                    "api_key": os.environ.get("FRED_API_KEY", ""),
                }, timeout=5)
                if resp.status_code == 200:
                    obs = resp.json().get("observations", [])
                    if obs:
                        out["unemployment"] = float(obs[0]["value"])
            except Exception:
                pass

            return out

        fred_data = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _fetch_fred),
            timeout=10.0,
        )

        result.update(fred_data)
        if any(v is not None for k, v in fred_data.items()):
            result["source"] = "fred"
            logger.info(f"FRED macro: FFR={fred_data.get('fed_funds_rate')}, "
                         f"CPI={fred_data.get('cpi_yoy')}%, "
                         f"Unemp={fred_data.get('unemployment')}%")

    except Exception as e:
        logger.debug(f"FRED fetch failed: {e}")

    _cache.put("fred", result)
    return result


# ---------------------------------------------------------------------------
# Master Intelligence Function
# ---------------------------------------------------------------------------
async def get_full_intel() -> dict:
    """
    Run all intelligence gathering concurrently.
    Each sub-call has its own 5-second timeout.
    Failures return safe defaults -- never blocks trading.
    """
    start = time.time()

    # Run ALL intelligence sources concurrently
    vix_task = asyncio.create_task(_safe_call(get_vix, "vix"))
    news_task = asyncio.create_task(_safe_call(get_market_news, "news"))
    cal_task = asyncio.create_task(_safe_call(get_economic_calendar, "calendar"))
    ctx_task = asyncio.create_task(_safe_call(get_market_context, "context"))
    trump_task = asyncio.create_task(_safe_call(get_trump_sentiment, "trump"))
    reddit_task = asyncio.create_task(_safe_call(get_reddit_momentum, "reddit"))
    fred_task = asyncio.create_task(_safe_call(get_fred_macro, "fred"))

    vix, news, calendar, context, trump, reddit, fred = await asyncio.gather(
        vix_task, news_task, cal_task, ctx_task, trump_task, reddit_task, fred_task
    )

    elapsed = round(time.time() - start, 2)

    # Determine overall trade restriction
    trade_ok = True
    restriction_reason = None
    if calendar.get("trade_restricted"):
        trade_ok = False
        evt = calendar.get("next_event", {})
        restriction_reason = f"High-impact event: {evt.get('name', 'unknown')} in {evt.get('minutes_until', '?')} min"
    if news.get("tier1_active"):
        trade_ok = False
        restriction_reason = f"Tier-1 news active: {news.get('summary', '')[:100]}"

    # Trump tariff warning (don't block, but flag)
    trump_warning = None
    if trump.get("tariff_mentioned") and abs(trump.get("score", 0)) > 0.3:
        trump_warning = f"Trump tariff post detected (sentiment: {trump.get('score', 0):.2f})"

    return {
        "vix": vix,
        "news": news,
        "calendar": calendar,
        "market_context": context,
        "trump": trump,
        "reddit": reddit,
        "fred": fred,
        "trade_ok": trade_ok,
        "restriction_reason": restriction_reason,
        "trump_warning": trump_warning,
        "fetch_time_s": elapsed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _safe_call(fn, label: str) -> dict:
    """Wrap an intel function with safe default on any failure."""
    try:
        return await fn()
    except Exception as e:
        logger.error(f"Intel sub-call [{label}] failed: {e}")
        return {"error": str(e), "source": f"{label}_failed"}


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
async def _test():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    print("=" * 60)
    print("Market Intelligence Module -- Standalone Test")
    print("=" * 60)

    print("\n--- VIX ---")
    vix = await get_vix()
    print(f"  Result: {vix}")

    print("\n--- Market News ---")
    news = await get_market_news()
    print(f"  Headlines: {news['count']}")
    print(f"  Tier1 active: {news['tier1_active']}")
    print(f"  Tier2 active: {news['tier2_active']}")
    print(f"  Summary: {news['summary'][:120]}")
    for h in news["headlines"][:3]:
        print(f"    [{h['tier']}] {h['headline'][:80]}")

    print("\n--- Economic Calendar ---")
    cal = await get_economic_calendar()
    print(f"  Events today: {cal['count']}")
    print(f"  Next event: {cal['next_event']}")
    print(f"  Trade restricted: {cal['trade_restricted']}")

    print("\n--- Market Context ---")
    ctx = await get_market_context()
    print(f"  Overnight range: {ctx['overnight_range']}")
    print(f"  Gap: {ctx['gap_pct']}%")
    print(f"  Trend (5m): {ctx['trend_5m']}")

    print("\n--- Trump Sentiment ---")
    trump = await get_trump_sentiment()
    print(f"  Score: {trump.get('score', 'N/A')}")
    print(f"  Posts: {trump.get('posts', 0)}")
    print(f"  Keywords: {trump.get('market_keywords', [])}")
    print(f"  Tariff: {trump.get('tariff_mentioned', False)}")
    if trump.get("latest"):
        print(f"  Latest: {trump['latest'][:100]}...")

    print("\n--- Reddit / WSB Momentum ---")
    reddit = await get_reddit_momentum()
    print(f"  Source: {reddit.get('source', 'N/A')}")
    print(f"  Top mentions: {[t['ticker'] for t in reddit.get('top_mentions', [])[:5]]}")
    print(f"  NQ relevant: {[t['ticker'] for t in reddit.get('nq_relevant', [])[:5]]}")

    print("\n--- FRED Macro ---")
    fred = await get_fred_macro()
    print(f"  Fed Funds Rate: {fred.get('fed_funds_rate', 'N/A')}%")
    print(f"  CPI YoY: {fred.get('cpi_yoy', 'N/A')}%")
    print(f"  Unemployment: {fred.get('unemployment', 'N/A')}%")

    print("\n--- Full Intel ---")
    intel = await get_full_intel()
    print(f"  Trade OK: {intel['trade_ok']}")
    print(f"  Restriction: {intel['restriction_reason']}")
    print(f"  Trump warning: {intel.get('trump_warning', 'None')}")
    print(f"  Sources: VIX + News + Calendar + Context + Trump + Reddit + FRED")
    print(f"  Fetch time: {intel['fetch_time_s']}s")

    print("\n" + "=" * 60)
    print("Test complete.")


if __name__ == "__main__":
    asyncio.run(_test())
