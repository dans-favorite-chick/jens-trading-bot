"""
Proactive News & Momentum Scanner for Phoenix Trading Bot.

Runs as a background task, periodically checking multiple data sources
for market-moving events. Produces classified alerts consumed by the
AI Council and Pre-Trade Filter.

Alert types:
  MOMENTUM  - large NQ price move detected
  NEWS      - market-moving headline from Finnhub
  ECONOMIC  - upcoming high-impact calendar event
  VIX_SPIKE - volatility regime change
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import finnhub
import yfinance as yf

logger = logging.getLogger("NewsScanner")

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path, override=True)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Shared state with market_intel (lazy clients)
# ---------------------------------------------------------------------------
_finnhub_client: finnhub.Client | None = None


def _get_finnhub() -> finnhub.Client | None:
    global _finnhub_client
    if _finnhub_client is None:
        key = os.environ.get("FINNHUB_API_KEY")
        if key:
            _finnhub_client = finnhub.Client(api_key=key)
    return _finnhub_client


# ---------------------------------------------------------------------------
# Alert classification keywords (shared with market_intel)
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
    lower = headline.lower()
    for kw in _TIER1_KEYWORDS:
        if kw in lower:
            return 1
    for kw in _TIER2_KEYWORDS:
        if kw in lower:
            return 2
    upper = headline.upper()
    for ticker in _HEAVY_TICKERS:
        if ticker in upper:
            return 2
    return 3


# ---------------------------------------------------------------------------
# NewsScanner class
# ---------------------------------------------------------------------------
class NewsScanner:
    """
    Background scanner that periodically checks news, momentum, and
    volatility for market-moving events.

    Usage:
        scanner = NewsScanner()
        alerts = await scanner.scan()  # manual trigger
        # -- or --
        await scanner.run_loop()       # background loop
    """

    def __init__(self, scan_interval: int = 120):
        self.alerts: list[dict] = []
        self._last_check: float = 0
        self.scan_interval: int = scan_interval  # seconds
        self._prev_nq_close: float | None = None
        self._prev_vix: float | None = None
        self._seen_headlines: set[str] = set()
        self._running = False

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------
    async def scan(self) -> list[dict]:
        """Run all scanner checks and return new alerts."""
        new_alerts: list[dict] = []
        now = time.time()
        self._last_check = now
        ts = datetime.now(timezone.utc).isoformat()

        # Run all checks concurrently
        results = await asyncio.gather(
            self._check_general_news(),
            self._check_company_news(),
            self._check_economic_calendar(),
            self._check_nq_momentum(),
            self._check_vix_spike(),
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Scanner sub-check failed: {r}")
                continue
            if isinstance(r, list):
                new_alerts.extend(r)

        # Tag all alerts with timestamp
        for a in new_alerts:
            a["timestamp"] = ts

        # Sort by severity (lower tier = more severe)
        new_alerts.sort(key=lambda a: a.get("severity", 3))

        # Prepend to rolling alert buffer (keep last 50)
        self.alerts = new_alerts + self.alerts
        self.alerts = self.alerts[:50]

        if new_alerts:
            logger.info(f"NewsScanner produced {len(new_alerts)} new alert(s)")
        else:
            logger.debug("NewsScanner: no new alerts")

        return new_alerts

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------
    async def run_loop(self):
        """Run scanner in a continuous loop. Call as a background task."""
        self._running = True
        logger.info(f"NewsScanner background loop started (interval={self.scan_interval}s)")
        while self._running:
            try:
                await self.scan()
            except Exception as e:
                logger.error(f"NewsScanner loop error: {e}")
            await asyncio.sleep(self.scan_interval)

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Check: General market news (Finnhub)
    # ------------------------------------------------------------------
    async def _check_general_news(self) -> list[dict]:
        alerts = []
        try:
            fc = _get_finnhub()
            if not fc:
                return alerts

            def _fetch():
                return fc.general_news("general", min_id=0)

            raw = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _fetch),
                timeout=5.0,
            )
            cutoff = time.time() - 7200  # last 2 hours (Finnhub free tier has delay)
            for item in (raw or []):
                pub_time = item.get("datetime", 0)
                if pub_time < cutoff:
                    continue
                headline = item.get("headline", "")
                if not headline or headline in self._seen_headlines:
                    continue
                self._seen_headlines.add(headline)
                tier = _classify_headline(headline)
                if tier <= 2:
                    alerts.append({
                        "type": "NEWS",
                        "severity": tier,
                        "headline": headline,
                        "source": item.get("source", ""),
                        "url": item.get("url", ""),
                    })
        except Exception as e:
            logger.debug(f"General news check failed: {e}")
        return alerts

    # ------------------------------------------------------------------
    # Check: Company news for NQ heavyweights
    # ------------------------------------------------------------------
    async def _check_company_news(self) -> list[dict]:
        alerts = []
        try:
            fc = _get_finnhub()
            if not fc:
                return alerts

            now = datetime.now(timezone.utc)
            from_date = (now - timedelta(minutes=30)).strftime("%Y-%m-%d")
            to_date = now.strftime("%Y-%m-%d")

            # Check top 3 heavy tickers to stay within rate limits
            tickers_to_check = ["NVDA", "AAPL", "MSFT"]
            for ticker in tickers_to_check:
                try:
                    def _fetch(t=ticker):
                        return fc.company_news(t, _from=from_date, to=to_date)

                    raw = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, _fetch),
                        timeout=5.0,
                    )
                    cutoff = time.time() - 1800
                    for item in (raw or [])[:5]:  # limit per ticker
                        pub_time = item.get("datetime", 0)
                        if pub_time < cutoff:
                            continue
                        headline = item.get("headline", "")
                        if not headline or headline in self._seen_headlines:
                            continue
                        self._seen_headlines.add(headline)
                        tier = _classify_headline(headline)
                        if tier <= 2:
                            alerts.append({
                                "type": "NEWS",
                                "severity": tier,
                                "headline": headline,
                                "ticker": ticker,
                                "source": item.get("source", ""),
                            })
                except Exception as e:
                    logger.debug(f"Company news check for {ticker} failed: {e}")
        except Exception as e:
            logger.debug(f"Company news check failed: {e}")
        return alerts

    # ------------------------------------------------------------------
    # Check: Economic calendar (next 30 min)
    # ------------------------------------------------------------------
    async def _check_economic_calendar(self) -> list[dict]:
        alerts = []
        try:
            fc = _get_finnhub()
            if not fc:
                return alerts

            now = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")

            def _fetch():
                return fc.calendar_economic(_from=today_str, to=today_str)

            raw = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _fetch),
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

                if country and country.upper() != "US":
                    continue

                # Check if high impact
                is_high = False
                lower_name = event_name.lower()
                for kw in _HIGH_IMPACT_EVENTS:
                    if kw in lower_name:
                        is_high = True
                        break
                if impact and impact.lower() == "high":
                    is_high = True

                if not is_high:
                    continue

                # Parse time
                if event_time_str:
                    try:
                        event_dt = datetime.strptime(
                            f"{today_str} {event_time_str}", "%Y-%m-%d %H:%M"
                        ).replace(tzinfo=timezone.utc)
                        minutes_until = (event_dt - now).total_seconds() / 60
                        if 0 < minutes_until <= 30:
                            alerts.append({
                                "type": "ECONOMIC",
                                "severity": 1,
                                "event": event_name,
                                "time": event_time_str,
                                "minutes_until": round(minutes_until, 1),
                            })
                    except ValueError:
                        pass
        except Exception as e:
            logger.debug(f"Economic calendar check failed: {e}")
        return alerts

    # ------------------------------------------------------------------
    # Check: NQ momentum (>1% move in 30 min)
    # ------------------------------------------------------------------
    async def _check_nq_momentum(self) -> list[dict]:
        alerts = []
        try:
            def _fetch():
                ticker = yf.Ticker("NQ=F")
                hist = ticker.history(period="1d", interval="5m")
                return hist

            hist = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _fetch),
                timeout=5.0,
            )

            if hist is not None and len(hist) >= 6:
                # Compare current close to 30 min ago (6 x 5-min bars)
                current_close = float(hist["Close"].iloc[-1])
                past_close = float(hist["Close"].iloc[-7]) if len(hist) >= 7 else float(hist["Close"].iloc[0])

                if past_close > 0:
                    move_pct = ((current_close - past_close) / past_close) * 100
                    if abs(move_pct) >= 1.0:
                        direction = "UP" if move_pct > 0 else "DOWN"
                        alerts.append({
                            "type": "MOMENTUM",
                            "severity": 2,
                            "direction": direction,
                            "move_pct": round(move_pct, 2),
                            "current_price": round(current_close, 2),
                            "message": f"NQ moved {move_pct:+.2f}% in last 30 min",
                        })
                        logger.info(f"Momentum alert: NQ {direction} {abs(move_pct):.2f}%")

                self._prev_nq_close = current_close
        except Exception as e:
            logger.debug(f"NQ momentum check failed: {e}")
        return alerts

    # ------------------------------------------------------------------
    # Check: VIX spike (>5% move in last hour)
    # ------------------------------------------------------------------
    async def _check_vix_spike(self) -> list[dict]:
        alerts = []
        try:
            def _fetch():
                ticker = yf.Ticker("^VIX")
                hist = ticker.history(period="1d", interval="5m")
                return hist

            hist = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _fetch),
                timeout=5.0,
            )

            if hist is not None and len(hist) >= 12:
                current_vix = float(hist["Close"].iloc[-1])
                hour_ago_vix = float(hist["Close"].iloc[-13]) if len(hist) >= 13 else float(hist["Close"].iloc[0])

                if hour_ago_vix > 0:
                    move_pct = ((current_vix - hour_ago_vix) / hour_ago_vix) * 100
                    if abs(move_pct) >= 5.0:
                        direction = "SPIKE" if move_pct > 0 else "CRUSH"
                        alerts.append({
                            "type": "VIX_SPIKE",
                            "severity": 2 if abs(move_pct) < 10 else 1,
                            "direction": direction,
                            "move_pct": round(move_pct, 2),
                            "current_vix": round(current_vix, 2),
                            "message": f"VIX {direction} {abs(move_pct):.1f}% in last hour (now {current_vix:.1f})",
                        })
                        logger.info(f"VIX alert: {direction} {abs(move_pct):.1f}%")

                self._prev_vix = current_vix
        except Exception as e:
            logger.debug(f"VIX spike check failed: {e}")
        return alerts

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    def get_recent_alerts(self, max_age_s: int = 600, max_count: int = 10) -> list[dict]:
        """Return recent alerts within max_age_s, up to max_count."""
        now = datetime.now(timezone.utc)
        recent = []
        for a in self.alerts:
            ts_str = a.get("timestamp", "")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    age = (now - ts).total_seconds()
                    if age <= max_age_s:
                        recent.append(a)
                except ValueError:
                    pass
            if len(recent) >= max_count:
                break
        return recent

    def clear_seen(self):
        """Reset seen headlines (useful at session start)."""
        self._seen_headlines.clear()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
async def _test():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    print("=" * 60)
    print("News Scanner -- Standalone Test")
    print("=" * 60)

    scanner = NewsScanner(scan_interval=120)

    print("\nRunning scan...")
    alerts = await scanner.scan()

    print(f"\nTotal alerts: {len(alerts)}")
    for a in alerts:
        sev = a.get("severity", "?")
        atype = a.get("type", "?")
        msg = a.get("headline", a.get("message", a.get("event", "no detail")))
        print(f"  [{atype}] severity={sev}: {msg[:80]}")

    if not alerts:
        print("  (no alerts -- market is calm or outside trading hours)")

    print(f"\nAll alerts in buffer: {len(scanner.alerts)}")
    print("\n" + "=" * 60)
    print("Test complete.")


if __name__ == "__main__":
    asyncio.run(_test())
