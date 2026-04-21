"""
Phoenix Bot -- Daily Momentum Score Tracker

Computes a 1-5 daily momentum score from market structure, matching
the concept used by MenthorQ's Quinn AI for multi-day trend assessment.

Score Definitions:
    5 -- INSTITUTIONAL: All 4 TF aligned, strong CVD, price well above/below
         VWAP and HVL, ATR expanding. Full institutional conviction.
    4 -- DEVELOPING: 3+ TF aligned, above/below VWAP, directional CVD.
         Real trend underway but not max strength.
    3 -- TRANSITIONAL: 2 TF aligned, mixed CVD, price near VWAP.
         Early-stage or weakening trend.
    2 -- WEAK: 1 TF aligned or conflicting signals. Low conviction.
    1 -- NEUTRAL/CHOPPY: No TF alignment, oscillating near VWAP. No edge.

How it differs from intraday momentum_score in bias_momentum.py:
    - This is an END-OF-SESSION score saved daily, not a per-bar signal
    - It tracks TRAJECTORY across days (how long has score been sustained)
    - Quinn uses this to assess whether a rally is "institutional grade"
    - We use it to weight C/R (continuation/reversal) assessments

Storage: data/momentum_scores.json
    {
      "2026-04-14": {
        "score": 4,
        "direction": "BULLISH",
        "factors": [...],
        "regime": "MID_MORNING",
        "price": 25344.0,
        "hvl": 25093.38,
        "above_hvl": true,
        "atr_5m": 45.2,
        "tf_bull": 3,
        "tf_bear": 0,
        "vwap": 25200.0,
        "cvd": 1234567
      }
    }
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("MomentumScore")

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "momentum_scores.json")
MAX_HISTORY_DAYS = 60   # Keep 60 days of scores


# ── Score computation ─────────────────────────────────────────────────────────

def compute_score(market: dict, mq_snap=None) -> dict:
    """
    Compute today's momentum score from a market snapshot.

    Args:
        market: Dict from aggregator.snapshot() -- price, tf_bias, cvd, vwap, atr_5m, etc.
        mq_snap: Optional MenthorQSnapshot for HVL context

    Returns dict:
        {
          "score": 1-5,
          "direction": "BULLISH" | "BEARISH" | "NEUTRAL",
          "factors": [list of contributing factor strings],
          "detail": str  -- one-line human summary
        }
    """
    price   = float(market.get("price", 0) or 0)
    vwap    = float(market.get("vwap", 0) or 0)
    cvd     = float(market.get("cvd", 0) or 0)
    atr_5m  = float(market.get("atr_5m", 0) or 0)
    tf_bias = market.get("tf_bias", {})

    # TF vote counts
    tf_bull = sum(1 for v in tf_bias.values() if v == "BULLISH")
    tf_bear = sum(1 for v in tf_bias.values() if v == "BEARISH")
    total_tfs = len(tf_bias) or 4

    # Determine direction from TF alignment
    if tf_bull > tf_bear:
        direction = "BULLISH"
        aligned = tf_bull
    elif tf_bear > tf_bull:
        direction = "BEARISH"
        aligned = tf_bear
    else:
        direction = "NEUTRAL"
        aligned = 0

    factors = []
    points = 0

    # ── Factor 1: TF alignment (0-4 TFs) ─────────────────────────────
    # 4/4 = +4 pts,  3/4 = +3,  2/4 = +2,  1/4 = +1,  0 = 0
    points += aligned
    if aligned == total_tfs:
        factors.append(f"All {total_tfs} TF aligned {direction}")
    elif aligned >= 3:
        factors.append(f"{aligned}/{total_tfs} TF aligned {direction}")
    elif aligned >= 2:
        factors.append(f"{aligned}/{total_tfs} TF aligned (developing)")
    elif aligned == 1:
        factors.append("1 TF aligned (weak)")
    else:
        factors.append("No TF alignment (neutral)")

    # ── Factor 2: Price vs VWAP ───────────────────────────────────────
    if price > 0 and vwap > 0:
        vwap_dist = (price - vwap) / vwap * 100  # % above/below
        if direction == "BULLISH" and price > vwap:
            if vwap_dist > 0.3:
                points += 1
                factors.append(f"Price +{vwap_dist:.2f}% above VWAP (strong)")
            else:
                points += 0.5
                factors.append(f"Price {vwap_dist:.2f}% above VWAP (marginal)")
        elif direction == "BEARISH" and price < vwap:
            if vwap_dist < -0.3:
                points += 1
                factors.append(f"Price {vwap_dist:.2f}% below VWAP (strong)")
            else:
                points += 0.5
                factors.append(f"Price {vwap_dist:.2f}% below VWAP (marginal)")
        else:
            factors.append("Price on wrong side of VWAP (countertrend)")

    # ── Factor 3: CVD direction and magnitude ─────────────────────────
    if cvd != 0:
        if direction == "BULLISH" and cvd > 0:
            if cvd > 500_000:
                points += 1
                factors.append(f"CVD strongly positive (+{cvd/1e6:.1f}M) -- buyer conviction")
            else:
                points += 0.5
                factors.append(f"CVD positive (+{cvd/1e6:.1f}M) -- mild buying")
        elif direction == "BEARISH" and cvd < 0:
            if cvd < -500_000:
                points += 1
                factors.append(f"CVD strongly negative ({cvd/1e6:.1f}M) -- seller conviction")
            else:
                points += 0.5
                factors.append(f"CVD negative ({cvd/1e6:.1f}M) -- mild selling")
        elif direction == "BULLISH" and cvd < 0:
            factors.append(f"CVD negative ({cvd/1e6:.1f}M) vs bullish price -- divergence warning")
        elif direction == "BEARISH" and cvd > 0:
            factors.append(f"CVD positive (+{cvd/1e6:.1f}M) vs bearish price -- divergence warning")

    # ── Factor 4: HVL position (MenthorQ gamma context) ──────────────
    if mq_snap and mq_snap.hvl > 0:
        hvl = mq_snap.hvl
        above_hvl = price >= hvl
        if direction == "BULLISH" and above_hvl:
            dist_pct = (price - hvl) / hvl * 100
            if dist_pct > 1.0:
                points += 1
                factors.append(f"Price {dist_pct:.1f}% above HVL {hvl:.2f} (strong positive gamma)")
            else:
                points += 0.5
                factors.append(f"Price at/just above HVL {hvl:.2f} (positive gamma zone)")
        elif direction == "BEARISH" and not above_hvl:
            dist_pct = (hvl - price) / hvl * 100
            if dist_pct > 1.0:
                points += 1
                factors.append(f"Price {dist_pct:.1f}% below HVL {hvl:.2f} (negative gamma, momentum)")
            else:
                points += 0.5
                factors.append(f"Price just below HVL {hvl:.2f} (negative gamma zone)")
        elif direction == "BULLISH" and not above_hvl:
            factors.append(f"Price BELOW HVL {hvl:.2f} in bullish setup -- negative gamma headwind")
        elif direction == "BEARISH" and above_hvl:
            factors.append(f"Price ABOVE HVL {hvl:.2f} in bearish setup -- positive gamma headwind")

    # ── Normalize to 1-5 scale ────────────────────────────────────────
    # Max possible points: 4 (TF) + 1 (VWAP) + 1 (CVD) + 1 (HVL) = 7
    # Map: 0-1 pts = 1, 1-2 pts = 2, 3 pts = 3, 4-5 pts = 4, 6-7 pts = 5
    raw = points
    if direction == "NEUTRAL" or aligned == 0:
        score = 1
    elif raw <= 1.5:
        score = 2
    elif raw <= 3.0:
        score = 3
    elif raw <= 5.0:
        score = 4
    else:
        score = 5

    labels = {1: "NEUTRAL/CHOPPY", 2: "WEAK", 3: "TRANSITIONAL",
              4: "DEVELOPING", 5: "INSTITUTIONAL"}
    detail = f"Score {score} ({labels[score]}) -- {direction}, {aligned}/{total_tfs} TF"

    return {
        "score": score,
        "direction": direction,
        "factors": factors,
        "detail": detail,
        "raw_points": round(raw, 1),
        "tf_bull": tf_bull,
        "tf_bear": tf_bear,
    }


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_file() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[MomentumScore] Load error: {e}")
        return {}


def _save_file(data: dict):
    Path(DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
    # Trim old entries
    if len(data) > MAX_HISTORY_DAYS:
        keys = sorted(data.keys())
        for old in keys[:len(data) - MAX_HISTORY_DAYS]:
            del data[old]
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"[MomentumScore] Save error: {e}")


def record_daily(market: dict, mq_snap=None, session_date: str = None) -> dict:
    """
    Compute and persist today's momentum score.
    Called once per session (e.g. at market close or end of primary window).

    Returns the computed score dict.
    """
    today = session_date or str(date.today())
    result = compute_score(market, mq_snap)

    # Enrich with context for historical analysis
    hvl = mq_snap.hvl if mq_snap else 0.0
    record = {
        "score":     result["score"],
        "direction": result["direction"],
        "factors":   result["factors"],
        "detail":    result["detail"],
        "price":     market.get("price", 0),
        "vwap":      market.get("vwap", 0),
        "hvl":       hvl,
        "above_hvl": (market.get("price", 0) >= hvl) if hvl > 0 else None,
        "atr_5m":    market.get("atr_5m", 0),
        "cvd":       market.get("cvd", 0),
        "tf_bull":   result["tf_bull"],
        "tf_bear":   result["tf_bear"],
        "recorded_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    data = _load_file()
    data[today] = record
    _save_file(data)

    logger.info(f"[MomentumScore] {today}: {result['detail']}")
    return record


# ── History / trajectory analysis ────────────────────────────────────────────

def get_history(n_days: int = 10) -> list[dict]:
    """
    Return last N days of momentum scores, sorted newest first.
    Each entry includes the date key.
    """
    data = _load_file()
    sorted_dates = sorted(data.keys(), reverse=True)[:n_days]
    result = []
    for d in sorted_dates:
        entry = dict(data[d])
        entry["date"] = d
        result.append(entry)
    return result


def get_trajectory(n_days: int = 5) -> dict:
    """
    Analyze momentum score trajectory over the last N days.

    Returns:
        {
          "current_score": int,
          "current_direction": str,
          "consecutive_days": int,     -- days at current score
          "trend": "RISING"|"FALLING"|"STABLE"|"UNKNOWN",
          "exhaustion_warning": bool,  -- True if score hit 5 recently
          "reversal_risk": str,        -- "LOW"|"MEDIUM"|"HIGH"
          "history": [...]             -- raw history list
        }
    """
    history = get_history(n_days)
    if not history:
        return {
            "current_score": 0,
            "current_direction": "UNKNOWN",
            "consecutive_days": 0,
            "trend": "UNKNOWN",
            "exhaustion_warning": False,
            "reversal_risk": "UNKNOWN",
            "history": [],
        }

    current = history[0]
    current_score = current["score"]
    current_dir = current["direction"]

    # Count consecutive days at current score
    consecutive = 1
    for entry in history[1:]:
        if entry["score"] == current_score and entry["direction"] == current_dir:
            consecutive += 1
        else:
            break

    # Score trend over available history
    scores = [e["score"] for e in history]
    if len(scores) >= 2:
        recent_avg = sum(scores[:3]) / min(3, len(scores))
        older_avg  = sum(scores[3:6]) / max(1, len(scores[3:6]))
        if recent_avg > older_avg + 0.4:
            trend = "RISING"
        elif recent_avg < older_avg - 0.4:
            trend = "FALLING"
        else:
            trend = "STABLE"
    else:
        trend = "UNKNOWN"

    # Exhaustion warning: score reached 5 in last 3 days
    exhaustion_warning = any(e["score"] == 5 for e in history[:3])

    # Reversal risk assessment
    # HIGH: score dropping from recent high, OR at 5 for 2+ days
    # MEDIUM: score at 4 for 3+ days without hitting 5
    # LOW: score rising or transitioning
    if trend == "FALLING" and current_score <= 3:
        reversal_risk = "HIGH"
    elif exhaustion_warning and consecutive >= 2:
        reversal_risk = "HIGH"
    elif current_score == 4 and consecutive >= 3 and trend == "STABLE":
        reversal_risk = "MEDIUM"
    elif trend == "RISING":
        reversal_risk = "LOW"
    else:
        reversal_risk = "MEDIUM"

    return {
        "current_score": current_score,
        "current_direction": current_dir,
        "consecutive_days": consecutive,
        "trend": trend,
        "exhaustion_warning": exhaustion_warning,
        "reversal_risk": reversal_risk,
        "history": history,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    traj = get_trajectory(10)
    print("\nMomentum Score History (last 10 days):")
    print(f"{'Date':<12} {'Score':<7} {'Dir':<10} {'Detail'}")
    print("-" * 70)
    for e in reversed(traj["history"]):
        print(f"  {e['date']:<10} {e['score']:<7} {e['direction']:<10} {e.get('detail','')}")
    print()
    print(f"Current:      Score {traj['current_score']} ({traj['current_direction']})")
    print(f"Consecutive:  {traj['consecutive_days']} days at this score")
    print(f"Trend:        {traj['trend']}")
    print(f"Exhaustion:   {'YES -- watch for reversal' if traj['exhaustion_warning'] else 'No'}")
    print(f"Reversal risk: {traj['reversal_risk']}")
