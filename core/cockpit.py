"""
Phoenix Bot -- Cockpit 12-Layer Grading System

OBSERVATION ONLY -- scores 12 layers but NEVER blocks trades.
Philosophy: observe and inform so the AI can learn from outcomes.
During golden trade windows (OPEN_MOMENTUM, MID_MORNING), the bot
should be AGGRESSIVE, not defensive.

Each layer is GREEN / YELLOW / RED.
Final recommendation: AGGRESSIVE / NORMAL / CAUTIOUS / DEFENSIVE.
"""

import logging
from datetime import datetime

logger = logging.getLogger("Cockpit")


# Regimes considered "golden windows" for aggressive trading
_GOLDEN_REGIMES = {"OPEN_MOMENTUM", "MID_MORNING"}
_MODERATE_REGIMES = {"LATE_AFTERNOON"}
_WEAK_REGIMES = {"AFTERNOON_CHOP", "OVERNIGHT_RANGE", "CLOSE_CHOP", "PREMARKET_DRIFT"}


def _status(condition_green, condition_yellow=True):
    """Helper: return GREEN / YELLOW / RED based on two boolean conditions."""
    if condition_green:
        return "GREEN"
    if condition_yellow:
        return "YELLOW"
    return "RED"


class Cockpit:
    """12-layer market grading system. Observation only -- never blocks trades."""

    def grade(self, market: dict, session_info: dict, intel: dict,
              council_result: dict = None) -> dict:
        """
        Score 12 layers, each GREEN / YELLOW / RED.

        Args:
            market: aggregator.snapshot() dict
            session_info: session_manager.to_dict() dict
            intel: market_intel.get_full_intel() dict (may be empty/partial)
            council_result: council_to_dict() dict or None

        Returns:
            {
                layers: [{name, status, detail}],
                green_count: int,
                score: "9/12 GREEN -- AGGRESSIVE",
                recommendation: str,
                timestamp: str,
            }
        """
        layers = []

        # --- 1. Regime (time window) ---
        regime = session_info.get("regime", "UNKNOWN")
        if regime in _GOLDEN_REGIMES:
            layers.append({"name": "Regime", "status": "GREEN",
                           "detail": f"{regime} -- golden window"})
        elif regime in _MODERATE_REGIMES:
            layers.append({"name": "Regime", "status": "YELLOW",
                           "detail": f"{regime} -- moderate edge"})
        else:
            layers.append({"name": "Regime", "status": "RED",
                           "detail": f"{regime} -- low edge window"})

        # --- 2. TF Alignment (multi-timeframe bias) ---
        tf_bull = market.get("tf_votes_bullish", 0)
        tf_bear = market.get("tf_votes_bearish", 0)
        tf_agree = max(tf_bull, tf_bear)
        if tf_agree >= 3:
            layers.append({"name": "TF Alignment", "status": "GREEN",
                           "detail": f"{tf_agree} TF agree (bull={tf_bull} bear={tf_bear})"})
        elif tf_agree >= 2:
            layers.append({"name": "TF Alignment", "status": "YELLOW",
                           "detail": f"{tf_agree} TF agree (bull={tf_bull} bear={tf_bear})"})
        else:
            layers.append({"name": "TF Alignment", "status": "RED",
                           "detail": f"No alignment (bull={tf_bull} bear={tf_bear})"})

        # --- 3. VWAP Position ---
        price = market.get("price", 0)
        vwap = market.get("vwap", 0)
        if vwap > 0 and price > 0:
            vwap_dist = (price - vwap) / vwap * 100 if vwap else 0
            # GREEN if price clearly on one side (>0.05%), YELLOW if near, RED if no data
            if abs(vwap_dist) > 0.05:
                layers.append({"name": "VWAP Position", "status": "GREEN",
                               "detail": f"Price {'above' if vwap_dist > 0 else 'below'} VWAP by {abs(vwap_dist):.2f}%"})
            else:
                layers.append({"name": "VWAP Position", "status": "YELLOW",
                               "detail": f"Price near VWAP ({vwap_dist:+.2f}%)"})
        else:
            layers.append({"name": "VWAP Position", "status": "RED",
                           "detail": "No VWAP data"})

        # --- 4. CVD Direction ---
        cvd = market.get("cvd", 0)
        bar_delta = market.get("bar_delta", 0)
        if abs(cvd) > 50 and abs(bar_delta) > 10:
            # CVD and bar delta both non-trivial
            same_dir = (cvd > 0 and bar_delta > 0) or (cvd < 0 and bar_delta < 0)
            if same_dir:
                layers.append({"name": "CVD Direction", "status": "GREEN",
                               "detail": f"CVD={cvd:.0f} delta={bar_delta:.0f} aligned"})
            else:
                layers.append({"name": "CVD Direction", "status": "YELLOW",
                               "detail": f"CVD={cvd:.0f} delta={bar_delta:.0f} diverging"})
        elif abs(cvd) < 20:
            layers.append({"name": "CVD Direction", "status": "YELLOW",
                           "detail": f"CVD flat ({cvd:.0f})"})
        else:
            layers.append({"name": "CVD Direction", "status": "RED",
                           "detail": f"CVD={cvd:.0f} delta={bar_delta:.0f} weak/divergent"})

        # --- 5. ATR Regime ---
        atr_5m = market.get("atr_5m", 0)
        if 50 < atr_5m < 200:
            layers.append({"name": "ATR Regime", "status": "GREEN",
                           "detail": f"ATR(5m)={atr_5m:.1f} -- normal range"})
        elif atr_5m <= 50:
            layers.append({"name": "ATR Regime", "status": "YELLOW",
                           "detail": f"ATR(5m)={atr_5m:.1f} -- low/choppy"})
        else:
            layers.append({"name": "ATR Regime", "status": "RED",
                           "detail": f"ATR(5m)={atr_5m:.1f} -- extreme volatility"})

        # --- 6. VIX Level ---
        vix_data = intel.get("vix", {}) if intel else {}
        vix_val = vix_data.get("value", 0) if isinstance(vix_data, dict) else 0
        if vix_val > 0:
            if vix_val < 20:
                layers.append({"name": "VIX Level", "status": "GREEN",
                               "detail": f"VIX={vix_val:.1f} -- calm"})
            elif vix_val < 30:
                layers.append({"name": "VIX Level", "status": "YELLOW",
                               "detail": f"VIX={vix_val:.1f} -- elevated"})
            else:
                layers.append({"name": "VIX Level", "status": "RED",
                               "detail": f"VIX={vix_val:.1f} -- fear"})
        else:
            # No VIX data -- default to YELLOW (unknown)
            layers.append({"name": "VIX Level", "status": "YELLOW",
                           "detail": "VIX data unavailable"})

        # --- 7. News Tier ---
        news = intel.get("news", {}) if intel else {}
        tier1 = news.get("tier1_active", False)
        tier2 = news.get("tier2_active", False)
        highest = intel.get("highest_tier", None) if intel else None
        if tier1:
            layers.append({"name": "News Tier", "status": "RED",
                           "detail": f"Tier 1 news active"})
        elif tier2:
            layers.append({"name": "News Tier", "status": "YELLOW",
                           "detail": f"Tier 2 news active"})
        else:
            layers.append({"name": "News Tier", "status": "GREEN",
                           "detail": "No high-impact news"})

        # --- 8. Trump Sentiment ---
        trump = intel.get("trump", {}) if intel else {}
        trump_score = trump.get("score", 0) if isinstance(trump, dict) else 0
        tariff = trump.get("tariff_mentioned", False) if isinstance(trump, dict) else False
        if tariff and trump_score < -0.3:
            layers.append({"name": "Trump Sentiment", "status": "RED",
                           "detail": f"Tariff mention + negative ({trump_score:.2f})"})
        elif trump_score < -0.2:
            layers.append({"name": "Trump Sentiment", "status": "YELLOW",
                           "detail": f"Negative sentiment ({trump_score:.2f})"})
        else:
            layers.append({"name": "Trump Sentiment", "status": "GREEN",
                           "detail": f"Neutral/positive ({trump_score:.2f})" if trump_score else "No signal"})

        # --- 9. DXY Direction ---
        dxy = intel.get("dxy", {}) if intel else {}
        dxy_change = dxy.get("change_pct", 0) if isinstance(dxy, dict) else 0
        if abs(dxy_change) < 0.1:
            layers.append({"name": "DXY Direction", "status": "GREEN",
                           "detail": f"DXY flat ({dxy_change:+.2f}%)"})
        elif dxy_change < -0.1:
            # DXY down is generally good for equities (longs)
            layers.append({"name": "DXY Direction", "status": "GREEN",
                           "detail": f"DXY falling ({dxy_change:+.2f}%) -- equity supportive"})
        else:
            layers.append({"name": "DXY Direction", "status": "YELLOW",
                           "detail": f"DXY rising ({dxy_change:+.2f}%) -- headwind"})

        # --- 10. Bond Yields ---
        bonds = intel.get("bond_yields", {}) if intel else {}
        yield_change = bonds.get("change_bps", 0) if isinstance(bonds, dict) else 0
        if yield_change <= 2:
            layers.append({"name": "Bond Yields", "status": "GREEN",
                           "detail": f"Yields stable/falling ({yield_change:+.1f} bps)"})
        elif yield_change <= 5:
            layers.append({"name": "Bond Yields", "status": "YELLOW",
                           "detail": f"Yields slightly rising ({yield_change:+.1f} bps)"})
        else:
            layers.append({"name": "Bond Yields", "status": "RED",
                           "detail": f"Yields spiking ({yield_change:+.1f} bps)"})

        # --- 11. Intermarket (risk-on/off) ---
        im = intel.get("intermarket", {}) if intel else {}
        if isinstance(im, dict) and not im.get("error"):
            # Check if ES, RTY, BTC are positive
            tickers = im.get("tickers", {})
            up_count = 0
            checked = 0
            for sym in ("ES=F", "RTY=F", "BTC-USD", "^GSPC"):
                t = tickers.get(sym, {})
                if isinstance(t, dict) and "change_pct" in t:
                    checked += 1
                    if t["change_pct"] > 0:
                        up_count += 1
            if checked == 0:
                layers.append({"name": "Intermarket", "status": "YELLOW",
                               "detail": "No intermarket data"})
            elif up_count >= 3:
                layers.append({"name": "Intermarket", "status": "GREEN",
                               "detail": f"Risk-on ({up_count}/{checked} up)"})
            elif up_count >= 2:
                layers.append({"name": "Intermarket", "status": "YELLOW",
                               "detail": f"Mixed ({up_count}/{checked} up)"})
            else:
                layers.append({"name": "Intermarket", "status": "RED",
                               "detail": f"Risk-off ({up_count}/{checked} up)"})
        else:
            layers.append({"name": "Intermarket", "status": "YELLOW",
                           "detail": "No intermarket data"})

        # --- 12. Council Bias ---
        if council_result and isinstance(council_result, dict):
            bias = council_result.get("bias", "NEUTRAL")
            vote = council_result.get("vote_count", "?")
            if bias in ("BULLISH", "BEARISH"):
                layers.append({"name": "Council Bias", "status": "GREEN",
                               "detail": f"Council: {bias} ({vote})"})
            elif bias == "NEUTRAL":
                layers.append({"name": "Council Bias", "status": "YELLOW",
                               "detail": f"Council: NEUTRAL ({vote})"})
            else:
                layers.append({"name": "Council Bias", "status": "RED",
                               "detail": f"Council: {bias} ({vote})"})
        else:
            layers.append({"name": "Council Bias", "status": "YELLOW",
                           "detail": "Council not yet run"})

        # --- Score and Recommendation ---
        green_count = sum(1 for l in layers if l["status"] == "GREEN")
        yellow_count = sum(1 for l in layers if l["status"] == "YELLOW")
        red_count = sum(1 for l in layers if l["status"] == "RED")

        if green_count >= 10:
            recommendation = "AGGRESSIVE"
        elif green_count >= 7:
            recommendation = "NORMAL"
        elif green_count >= 4:
            recommendation = "CAUTIOUS"
        else:
            recommendation = "DEFENSIVE"

        score_str = f"{green_count}/12 GREEN -- {recommendation}"

        result = {
            "layers": layers,
            "green_count": green_count,
            "yellow_count": yellow_count,
            "red_count": red_count,
            "score": score_str,
            "recommendation": recommendation,
            "timestamp": datetime.now().isoformat(),
        }

        logger.info(f"[COCKPIT] {score_str} "
                     f"(G={green_count} Y={yellow_count} R={red_count})")

        return result

    def to_dict(self, last_grade: dict = None) -> dict:
        """Return last grading result for dashboard."""
        if last_grade:
            return last_grade
        return {"score": "Not graded yet", "layers": [], "green_count": 0,
                "recommendation": "UNKNOWN"}
