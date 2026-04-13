"""
Phoenix Bot — COT (Commitments of Traders) Data Feed

Weekly CFTC institutional positioning data for NQ/MNQ futures.
Shows what big money (leveraged funds, asset managers) is doing:
- Net long = institutions bullish
- Net short = institutions bearish
- Extreme positioning (>90th or <10th percentile) = potential reversal

Updated weekly (Tuesday data, released Friday). We cache locally
and only fetch once per day.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger("COTFeed")


@dataclass
class COTSignal:
    leveraged_fund_net: int = 0       # Net contracts (long - short)
    asset_manager_net: int = 0        # Asset manager net position
    percentile_rank: float = 0.5      # 0-1, vs last 52 weeks
    extreme_signal: str = "NEUTRAL"   # BULLISH_EXTREME / BEARISH_EXTREME / NEUTRAL
    trend: str = "FLAT"               # INCREASING / DECREASING / FLAT
    last_updated: str = ""
    weeks_of_data: int = 0


class COTFeed:
    """Fetches and caches COT data for NQ futures."""

    def __init__(self, cache_dir: str = None):
        self._cache_dir = cache_dir or os.path.join(
            os.path.dirname(__file__), "..", "data"
        )
        self._cache_path = os.path.join(self._cache_dir, "cot_cache.json")
        os.makedirs(self._cache_dir, exist_ok=True)
        self._signal = COTSignal()
        self._last_fetch: float = 0
        self._fetch_interval = 86400  # Once per day

        # Try to load from cache on init
        self._load_cache()

    def _load_cache(self):
        """Load cached COT data."""
        try:
            if os.path.exists(self._cache_path):
                with open(self._cache_path, "r") as f:
                    data = json.load(f)
                self._signal = COTSignal(**data.get("signal", {}))
                self._last_fetch = data.get("fetch_time", 0)
                logger.info(f"[COT] Loaded cache: net={self._signal.leveraged_fund_net} "
                           f"rank={self._signal.percentile_rank:.2f} "
                           f"signal={self._signal.extreme_signal}")
        except Exception as e:
            logger.debug(f"[COT] Cache load error: {e}")

    def _save_cache(self, raw_data: dict = None):
        """Save COT data to cache."""
        try:
            cache = {
                "signal": {
                    "leveraged_fund_net": self._signal.leveraged_fund_net,
                    "asset_manager_net": self._signal.asset_manager_net,
                    "percentile_rank": self._signal.percentile_rank,
                    "extreme_signal": self._signal.extreme_signal,
                    "trend": self._signal.trend,
                    "last_updated": self._signal.last_updated,
                    "weeks_of_data": self._signal.weeks_of_data,
                },
                "fetch_time": time.time(),
                "raw_data": raw_data,
            }
            with open(self._cache_path, "w") as f:
                json.dump(cache, f, indent=2)
        except Exception as e:
            logger.debug(f"[COT] Cache save error: {e}")

    async def refresh(self):
        """Fetch latest COT data. Call once per day."""
        if time.time() - self._last_fetch < self._fetch_interval:
            return  # Already fresh

        try:
            # Try pycot-reports first
            await self._fetch_via_pycot()
        except ImportError:
            logger.info("[COT] pycot-reports not installed, trying CFTC API directly")
            try:
                await self._fetch_via_cftc_api()
            except Exception as e:
                logger.warning(f"[COT] All fetch methods failed: {e}")

    async def _fetch_via_pycot(self):
        """Fetch using pycot-reports library."""
        import asyncio
        loop = asyncio.get_event_loop()

        def _fetch():
            from pycot import reports
            # NASDAQ MINI contract code in CFTC
            df = reports.legacy_report(
                contract_name="NASDAQ MINI",
                report_type="futures_only",
            )
            return df

        df = await loop.run_in_executor(None, _fetch)

        if df is not None and len(df) > 0:
            # Extract leveraged fund positioning
            recent = df.tail(52)  # Last year
            latest = df.iloc[-1]

            lev_long = latest.get("Lev_Money_Positions_Long_All", 0)
            lev_short = latest.get("Lev_Money_Positions_Short_All", 0)
            net = int(lev_long - lev_short)

            # Compute percentile rank
            net_history = (recent.get("Lev_Money_Positions_Long_All", 0) -
                          recent.get("Lev_Money_Positions_Short_All", 0))
            if len(net_history) > 0:
                rank = float((net_history < net).mean())
            else:
                rank = 0.5

            # Trend: compare to 4 weeks ago
            if len(df) >= 5:
                prev_net = int(df.iloc[-5].get("Lev_Money_Positions_Long_All", 0) -
                              df.iloc[-5].get("Lev_Money_Positions_Short_All", 0))
                trend = "INCREASING" if net > prev_net + 1000 else "DECREASING" if net < prev_net - 1000 else "FLAT"
            else:
                trend = "FLAT"

            self._signal = COTSignal(
                leveraged_fund_net=net,
                percentile_rank=round(rank, 3),
                extreme_signal=(
                    "BULLISH_EXTREME" if rank > 0.9 else
                    "BEARISH_EXTREME" if rank < 0.1 else "NEUTRAL"
                ),
                trend=trend,
                last_updated=datetime.now().isoformat(),
                weeks_of_data=len(recent),
            )
            self._last_fetch = time.time()
            self._save_cache()
            logger.info(f"[COT] Updated: net={net} rank={rank:.2f} signal={self._signal.extreme_signal}")

    async def _fetch_via_cftc_api(self):
        """Fallback: fetch directly from CFTC EDGAR API."""
        import asyncio
        import urllib.request

        def _fetch():
            # CFTC Socrata API for disaggregated futures
            url = ("https://publicreporting.cftc.gov/resource/jun7-fc8e.json?"
                   "$where=market_and_exchange_names like '%NASDAQ MINI%'"
                   "&$order=report_date_as_yyyy_mm_dd DESC"
                   "&$limit=52")
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read().decode())

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, _fetch)
        except Exception as e:
            logger.warning(f"[COT] CFTC API failed: {e}")
            return

        if not data:
            return

        # Parse: leveraged funds = "lev_money_positions_long_all" / "lev_money_positions_short_all"
        nets = []
        for row in data:
            try:
                long_pos = int(row.get("lev_money_positions_long_all", 0))
                short_pos = int(row.get("lev_money_positions_short_all", 0))
                nets.append(long_pos - short_pos)
            except (ValueError, TypeError):
                continue

        if not nets:
            return

        latest_net = nets[0]
        rank = sum(1 for n in nets if n < latest_net) / max(1, len(nets))
        trend = "INCREASING" if len(nets) >= 5 and latest_net > nets[4] + 1000 else \
                "DECREASING" if len(nets) >= 5 and latest_net < nets[4] - 1000 else "FLAT"

        self._signal = COTSignal(
            leveraged_fund_net=latest_net,
            percentile_rank=round(rank, 3),
            extreme_signal=(
                "BULLISH_EXTREME" if rank > 0.9 else
                "BEARISH_EXTREME" if rank < 0.1 else "NEUTRAL"
            ),
            trend=trend,
            last_updated=datetime.now().isoformat(),
            weeks_of_data=len(nets),
        )
        self._last_fetch = time.time()
        self._save_cache(raw_data=data[:5])
        logger.info(f"[COT] Updated via CFTC: net={latest_net} rank={rank:.2f}")

    def get_signal(self) -> dict:
        """Get current COT signal for strategy consumption."""
        return {
            "leveraged_fund_net": self._signal.leveraged_fund_net,
            "percentile_rank": self._signal.percentile_rank,
            "extreme_signal": self._signal.extreme_signal,
            "trend": self._signal.trend,
            "last_updated": self._signal.last_updated,
            "weeks_of_data": self._signal.weeks_of_data,
            "is_stale": (time.time() - self._last_fetch > 86400 * 7),  # Stale if > 1 week
        }

    def to_dict(self) -> dict:
        return self.get_signal()
