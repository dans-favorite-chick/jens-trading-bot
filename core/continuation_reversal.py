"""
Phoenix Bot -- Continuation/Reversal Assessment Engine

Mirrors the analysis Quinn (MenthorQ AI) produces:
  - Where is price relative to the expected daily range (1D Min/Max)?
  - Is the momentum score rising, stable, or fading?
  - Does the gamma regime support continuation or resistance?
  - What is the net verdict: CONTINUATION, REVERSAL, or CONTESTED?

This runs INTRADAY on every strategy evaluation, not just at EOD.
The result feeds:
  - Pre-trade filter (AI prompt context)
  - Strategy confidence scoring
  - Dashboard display
  - Telegram alerts at key transitions

Quinn Factor Table (replicated):
    Factor                  Signal          Implication
    Momentum Score          1-5             Trend strength
    Score duration          N days          Sustainability
    Price @ 1D Max/Min      True/False      Natural mean-reversion zone
    Gamma regime            POS/NEG         Dealer mechanics
    IV / ATR                Low/High        Volatility expectation
    Call/Put resistance     Distance        Mechanical ceiling/floor
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("CR_Assessment")

# Proximity thresholds (as fraction of ATR)
LEVEL_NEAR_ATR_MULT = 1.5   # "near" a level = within 1.5x the 1m ATR
DAY_RANGE_NEAR_PCT  = 0.002  # "near" day max/min = within 0.2% of price


@dataclass
class CRVerdict:
    """
    Full continuation/reversal assessment for current market state.
    Mirrors Quinn's output structure.
    """
    verdict: str = "CONTESTED"         # "CONTINUATION" | "REVERSAL" | "CONTESTED" | "WAIT"
    confidence: str = "LOW"            # "LOW" | "MEDIUM" | "HIGH"
    direction_bias: str = "NEUTRAL"    # "LONG" | "SHORT" | "NEUTRAL"

    # Factor signals (matches Quinn's table)
    momentum_score: int = 0
    momentum_direction: str = "NEUTRAL"
    consecutive_days: int = 0
    momentum_trend: str = "UNKNOWN"
    exhaustion_warning: bool = False

    at_day_max: bool = False
    at_day_min: bool = False
    at_call_resistance: bool = False
    at_put_support: bool = False

    gamma_regime: str = "UNKNOWN"       # POSITIVE = dealers resist move, NEGATIVE = dealers amplify
    above_hvl: bool = True
    iv_regime: str = "NORMAL"           # "LOW" | "NORMAL" | "HIGH" (from ATR vs historical)

    # Human-readable factor list (for AI prompt and dashboard)
    factors: list = field(default_factory=list)
    reversal_factors: list = field(default_factory=list)
    continuation_factors: list = field(default_factory=list)

    # Full Quinn-style analysis text
    analysis_text: str = ""
    summary_table: str = ""


def assess(market: dict, mq_snap=None, trajectory: dict = None) -> CRVerdict:
    """
    Run continuation/reversal assessment.

    Args:
        market:     Dict from aggregator.snapshot()
        mq_snap:    MenthorQSnapshot (from menthorq_feed.get_snapshot())
        trajectory: Dict from momentum_score.get_trajectory()

    Returns CRVerdict with full analysis.
    """
    price   = float(market.get("price", 0) or 0)
    vwap    = float(market.get("vwap", 0) or 0)
    atr_1m  = float(market.get("atr_1m", 0) or 0)
    atr_5m  = float(market.get("atr_5m", 0) or 0)
    cvd     = float(market.get("cvd", 0) or 0)
    tf_bias = market.get("tf_bias", {})

    # Use 1m ATR for proximity thresholds; fallback to 5m ATR / 4
    atr_ref = atr_1m if atr_1m > 0 else (atr_5m / 4 if atr_5m > 0 else 5.0)
    near_thresh = atr_ref * LEVEL_NEAR_ATR_MULT

    # MQ levels
    hvl         = mq_snap.hvl if mq_snap else 0.0
    day_max     = mq_snap.day_max if mq_snap else 0.0
    day_min     = mq_snap.day_min if mq_snap else 0.0
    call_res    = mq_snap.call_resistance_all if mq_snap else 0.0
    put_sup     = mq_snap.put_support_all if mq_snap else 0.0
    call_res_0d = mq_snap.call_resistance_0dte if mq_snap else 0.0
    put_sup_0d  = mq_snap.put_support_0dte if mq_snap else 0.0
    gamma_regime = market.get("gamma_regime", "UNKNOWN")
    above_hvl    = market.get("above_hvl", True)

    # Trajectory / momentum score
    if trajectory is None:
        from core.momentum_score import get_trajectory
        trajectory = get_trajectory(10)

    mom_score  = trajectory.get("current_score", 0)
    mom_dir    = trajectory.get("current_direction", "NEUTRAL")
    consec     = trajectory.get("consecutive_days", 0)
    mom_trend  = trajectory.get("trend", "UNKNOWN")
    exhaustion = trajectory.get("exhaustion_warning", False)

    # TF alignment
    tf_bull = sum(1 for v in tf_bias.values() if v == "BULLISH")
    tf_bear = sum(1 for v in tf_bias.values() if v == "BEARISH")
    total_tfs = len(tf_bias) or 4

    # ── IV proxy from ATR ─────────────────────────────────────────────
    # ATR_5m thresholds for MNQ (calibrated from historical):
    # <80 = LOW vol, 80-150 = NORMAL, >150 = HIGH
    if atr_5m > 0:
        if atr_5m < 80:
            iv_regime = "LOW"
        elif atr_5m < 150:
            iv_regime = "NORMAL"
        else:
            iv_regime = "HIGH"
    else:
        iv_regime = "UNKNOWN"

    # ── Level position classification ─────────────────────────────────
    # Levels are CONTEXT, never signals by themselves.
    # A level only contributes to reversal scoring when CONFIRMED by
    # price action (rejection close) + CVD evidence.
    # Breaking through a level WITH momentum = continuation.
    #
    # Three states per level:
    #   BROKEN_ABOVE  price > level + break_buffer  → continuation signal
    #   APPROACHING   price within near_thresh below → note only, no score
    #   REJECTED_AT   price was at level, now pulled back with neg bar_delta → reversal signal
    #   HOLDING_BELOW price below level, outside near range → neutral
    #
    # We detect rejection via: price within range AND bar_delta < 0 (sellers in control)
    # We detect breakout via: price clearly above the level

    break_buffer = near_thresh * 0.5   # "clearly above" = 0.5x ATR above level
    bar_delta = float(market.get("bar_delta", 0) or 0)

    def level_state(level: float, is_resistance: bool) -> str:
        """Classify price position relative to a level."""
        if level <= 0 or price <= 0:
            return "NEUTRAL"
        diff = price - level
        if is_resistance:
            if diff > break_buffer:
                return "BROKEN_ABOVE"    # Price convincingly above — resistance cleared
            elif diff > -near_thresh:
                # Price is within near_thresh below the level — approaching or testing
                # Only mark as REJECTED if sellers are in control at the test
                if bar_delta < -200 and diff < near_thresh * 0.3:
                    return "REJECTED_AT"
                return "APPROACHING"
            else:
                return "HOLDING_BELOW"
        else:  # support
            if diff < -break_buffer:
                return "BROKEN_BELOW"
            elif diff < near_thresh:
                if bar_delta > 200 and abs(diff) < near_thresh * 0.3:
                    return "BOUNCING_AT"
                return "APPROACHING_FROM_ABOVE"
            else:
                return "HOLDING_ABOVE"

    state_day_max   = level_state(day_max,     is_resistance=True)
    state_day_min   = level_state(day_min,     is_resistance=False)
    state_call_res  = level_state(call_res,    is_resistance=True)
    state_put_sup   = level_state(put_sup,     is_resistance=False)
    state_call_res_0d = level_state(call_res_0d, is_resistance=True)
    state_put_sup_0d  = level_state(put_sup_0d,  is_resistance=False)

    # Simplified booleans for downstream code and dashboard
    at_day_max    = state_day_max in ("APPROACHING", "REJECTED_AT")
    above_day_max = state_day_max == "BROKEN_ABOVE"
    at_day_min    = state_day_min in ("APPROACHING_FROM_ABOVE", "BROKEN_BELOW", "BOUNCING_AT")
    at_call_res   = state_call_res in ("APPROACHING", "REJECTED_AT")
    at_put_sup    = state_put_sup in ("APPROACHING_FROM_ABOVE", "BOUNCING_AT")

    # ── Score continuation and reversal factors ───────────────────────
    cont_score = 0
    rev_score  = 0
    cont_factors = []
    rev_factors  = []

    # --- Momentum score (primary driver) ---
    score_labels = {1:"NEUTRAL/CHOPPY", 2:"WEAK", 3:"TRANSITIONAL",
                    4:"DEVELOPING", 5:"INSTITUTIONAL"}
    mom_label = score_labels.get(mom_score, "?")

    if mom_score >= 5:
        cont_score += 3
        cont_factors.append(
            f"Momentum Score {mom_score} (INSTITUTIONAL) for {consec} day(s) -- "
            f"full institutional conviction, highest continuation edge"
        )
    elif mom_score == 4:
        cont_score += 2
        cont_factors.append(
            f"Momentum Score {mom_score} ({mom_label}) sustained "
            f"{consec} consecutive day(s) -- trend has structural support"
        )
    elif mom_score == 3:
        cont_score += 1
        cont_factors.append(f"Momentum Score {mom_score} (transitional) -- developing strength")
    elif mom_score <= 2 and mom_score > 0:
        rev_factors.append(f"Momentum Score {mom_score} (weak/choppy) -- low trend conviction")

    if exhaustion:
        rev_score += 1
        rev_factors.append("Score 5 reached recently -- possible exhaustion, watch for score drop")

    if mom_trend == "FALLING":
        rev_score += 2
        rev_factors.append("Momentum trend FALLING -- fading, reversal thesis strengthening")
    elif mom_trend == "RISING":
        cont_score += 1
        cont_factors.append("Momentum trend RISING -- building conviction")

    # --- Level position: BROKEN levels are continuation signals ------
    # Levels are context. Breaking them WITH momentum = no resistance.
    # Only score reversal when price shows actual rejection evidence.

    if above_day_max:
        cont_score += 1
        cont_factors.append(
            f"Price BROKEN ABOVE 1D Max {day_max:.2f} -- "
            f"gamma model ceiling cleared, extension possible"
        )
    elif state_day_max == "REJECTED_AT":
        rev_score += 2
        rev_factors.append(
            f"REJECTED at 1D Max {day_max:.2f} -- "
            f"price tested level, sellers taking control (bar_delta={bar_delta:.0f}). "
            f"Confirmation required: watch for close back below level."
        )
    elif state_day_max == "APPROACHING":
        # Approaching only — note as watch zone, zero score impact
        cont_factors.append(
            f"Approaching 1D Max {day_max:.2f} -- "
            f"watch for rejection vs breakout; no reversal bias until confirmed"
        )

    if state_call_res == "BROKEN_ABOVE":
        cont_score += 1
        cont_factors.append(
            f"Call Resistance {call_res:.2f} CLEARED -- "
            f"dealers forced to buy calls to hedge, adds momentum above this level"
        )
    elif state_call_res == "REJECTED_AT":
        rev_score += 2
        rev_factors.append(
            f"REJECTED at Call Resistance {call_res:.2f} -- "
            f"dealer selling confirmed by negative bar delta. Reversal setup forming."
        )
    elif state_call_res == "APPROACHING":
        cont_factors.append(
            f"Approaching Call Resistance {call_res:.2f} -- "
            f"watch for acceptance vs rejection; no score until confirmed"
        )

    if state_call_res_0d == "BROKEN_ABOVE":
        cont_score += 1
        cont_factors.append(f"0DTE Call Resistance {call_res_0d:.2f} cleared -- same-day gamma ceiling removed")
    elif state_call_res_0d == "REJECTED_AT":
        rev_score += 1
        rev_factors.append(f"0DTE Call Resistance {call_res_0d:.2f} rejecting -- intraday gamma ceiling holding")

    if state_put_sup == "BROKEN_BELOW":
        rev_score += 2
        rev_factors.append(
            f"Put Support {put_sup:.2f} BROKEN -- "
            f"dealer buying exhausted, momentum likely accelerating lower"
        )
    elif state_put_sup == "BOUNCING_AT":
        cont_score += 1
        cont_factors.append(f"Bouncing at Put Support {put_sup:.2f} -- dealer buying zone holding")

    # --- Gamma regime (context, not signal) ---
    if gamma_regime == "POSITIVE":
        # Positive gamma = dealers suppress extremes. Context only -- doesn't score reversal alone.
        # Only adds reversal weight when COMBINED WITH confirmed rejection evidence.
        if state_day_max == "REJECTED_AT" or state_call_res == "REJECTED_AT":
            rev_score += 1
            rev_factors.append(
                "Positive GEX + confirmed rejection = strong mean-reversion setup. "
                "Dealers hedging by selling at resistance."
            )
        else:
            # Just note it as context
            cont_factors.append(
                f"Positive GEX regime -- dealers suppress volatility. "
                f"{'Level cleared = forced hedge buying adds fuel.' if above_day_max else 'Watch for rejection at key levels.'}"
            )
    elif gamma_regime == "NEGATIVE":
        cont_score += 1
        cont_factors.append(
            "Negative GEX regime -- dealer short gamma amplifies moves. "
            "Breakouts more likely to extend."
        )

    # --- CVD confirmation (strongest intraday reversal signal) -------
    # CVD divergence at a new price high = institutional sellers distributing.
    # This is the primary confirmation we need before calling reversal.
    if cvd != 0:
        if mom_dir == "BULLISH" and cvd > 500_000:
            cont_score += 1
            cont_factors.append(f"CVD bullishly aligned (+{cvd/1e6:.1f}M) -- buyer conviction confirmed")
        elif mom_dir == "BULLISH" and cvd > 0:
            cont_score += 0.5
            cont_factors.append(f"CVD positive (+{cvd/1e6:.1f}M) -- mild buying support")
        elif mom_dir == "BEARISH" and cvd < -500_000:
            cont_score += 1
            cont_factors.append(f"CVD bearishly aligned ({cvd/1e6:.1f}M) -- seller conviction confirmed")
        elif mom_dir == "BULLISH" and cvd < -500_000:
            # Strong negative CVD while price bullish = major divergence warning
            rev_score += 2
            rev_factors.append(
                f"CVD BEARISH DIVERGENCE ({cvd/1e6:.1f}M) while price rallying -- "
                f"institutional sellers are distributing into the rally. "
                f"High-conviction reversal warning."
            )
        elif mom_dir == "BULLISH" and cvd < -200_000:
            rev_score += 1
            rev_factors.append(
                f"CVD divergence ({cvd/1e6:.1f}M vs bullish price) -- "
                f"flow not confirming price strength"
            )
        elif mom_dir == "BEARISH" and cvd > 200_000:
            rev_score += 1
            rev_factors.append(
                f"CVD divergence (+{cvd/1e6:.1f}M vs bearish price) -- "
                f"flow not confirming price weakness"
            )

    # --- IV proxy (context only) ---
    if iv_regime == "HIGH":
        cont_score += 1
        cont_factors.append(f"High volatility (ATR_5m={atr_5m:.1f}) -- trending/breakout environment")
    elif iv_regime == "LOW" and (state_day_max == "REJECTED_AT" or state_call_res == "REJECTED_AT"):
        # Low vol + confirmed rejection = range-bound, mean-reversion favored
        rev_score += 1
        rev_factors.append(
            f"Low volatility (ATR_5m={atr_5m:.1f}) + level rejection -- "
            f"compressed range likely to contain the move"
        )

    # ── Verdict (confirmation-required model) ─────────────────────────
    # REVERSAL requires EVIDENCE, not just proximity to levels.
    # Minimum reversal criteria:
    #   - At least 1 confirmed rejection signal (REJECTED_AT or CVD divergence)
    #   - OR momentum score dropping (trend = FALLING)
    # Without that, we stay CONTINUATION or CONTESTED.
    has_rejection_evidence = (
        state_day_max == "REJECTED_AT"
        or state_call_res == "REJECTED_AT"
        or state_call_res_0d == "REJECTED_AT"
        or state_put_sup == "BROKEN_BELOW"
        or (mom_dir == "BULLISH" and cvd < -500_000)
        or mom_trend == "FALLING"
    )

    total = cont_score + rev_score
    if total == 0:
        verdict = "WAIT"
        confidence = "LOW"
    elif not has_rejection_evidence and rev_score > cont_score:
        # Rev factors exist but no confirmation — stay at CONTESTED, not REVERSAL
        verdict = "CONTESTED"
        confidence = "LOW"
    else:
        cont_pct = cont_score / total
        if cont_pct >= 0.65:
            verdict = "CONTINUATION"
            confidence = "HIGH" if cont_pct >= 0.80 else "MEDIUM"
        elif cont_pct <= 0.35 and has_rejection_evidence:
            verdict = "REVERSAL"
            confidence = "HIGH" if cont_pct <= 0.20 else "MEDIUM"
        else:
            verdict = "CONTESTED"
            confidence = "LOW"

    # Direction bias for trades
    if verdict == "CONTINUATION":
        direction_bias = mom_dir if mom_dir != "NEUTRAL" else "NEUTRAL"
    elif verdict == "REVERSAL":
        direction_bias = "SHORT" if mom_dir == "BULLISH" else ("LONG" if mom_dir == "BEARISH" else "NEUTRAL")
    else:
        direction_bias = "NEUTRAL"

    # ── Build factor table (Quinn format) ────────────────────────────
    rows = []
    rows.append(f"{'Factor':<32} {'Signal':<22} {'Implication'}")
    rows.append("-" * 85)

    rows.append(f"{'Momentum Score (' + str(mom_score) + ')':<32} "
                f"{mom_label:<22} "
                f"{'Continuation potential' if mom_score >= 4 else 'Weak/transitional'}")
    rows.append(f"{'Score Duration':<32} "
                f"{str(consec) + ' day(s) at score ' + str(mom_score):<22} "
                f"{'Established' if consec >= 3 else 'Early-stage, reversal risk if drops'}")
    rows.append(f"{'Momentum Trend':<32} "
                f"{mom_trend:<22} "
                f"{'Fading -- reversal thesis gaining' if mom_trend == 'FALLING' else 'Sustained' if mom_trend == 'STABLE' else 'Building'}")
    day_max_signal = (
        "BROKEN -- level cleared" if above_day_max
        else "REJECTED -- reversal evidence" if state_day_max == "REJECTED_AT"
        else "Approaching -- watch" if at_day_max
        else "Below"
    )
    rows.append(f"{'1D Max (' + str(round(day_max,0)) + ')':<32} "
                f"{day_max_signal:<22} "
                f"{'Continuation above cleared level' if above_day_max else 'Rejection CONFIRMS reversal setup' if state_day_max == 'REJECTED_AT' else 'No signal until accepted or rejected'}")
    call_res_signal = (
        "BROKEN -- cleared" if state_call_res == "BROKEN_ABOVE"
        else "REJECTED" if state_call_res == "REJECTED_AT"
        else "Approaching" if at_call_res
        else "Below"
    )
    rows.append(f"{'Call Resistance (' + str(round(call_res,0)) + ')':<32} "
                f"{call_res_signal:<22} "
                f"{'Forced dealer hedge buying above' if state_call_res == 'BROKEN_ABOVE' else 'Dealer selling confirmed' if state_call_res == 'REJECTED_AT' else 'Watch for accept vs reject'}")
    rows.append(f"{'Gamma Regime':<32} "
                f"{gamma_regime + ' GEX':<22} "
                f"{'Dealer selling at resistance (mean-revert)' if gamma_regime == 'POSITIVE' else 'Dealer amplifying moves (momentum)' if gamma_regime == 'NEGATIVE' else 'Unknown'}")
    rows.append(f"{'Volatility (ATR proxy)':<32} "
                f"{iv_regime + ' (ATR=' + str(round(atr_5m, 1)) + ')':<22} "
                f"{'Compressed -- breakout is surprise' if iv_regime == 'LOW' else 'Elevated -- trending moves likely' if iv_regime == 'HIGH' else 'Normal'}")
    if call_res > 0:
        rows.append(f"{'Call Resistance Level':<32} "
                    f"{str(round(call_res, 2)):<22} "
                    f"{'First tactical rejection zone' if at_call_res else 'Overhead target/resistance'}")

    summary_table = "\n".join(rows)

    # ── Build full analysis text ──────────────────────────────────────
    lines = []
    lines.append(f"Continuation vs. Reversal Assessment -- {__import__('datetime').date.today()}")
    lines.append("")
    lines.append(f"Momentum Score Analysis")
    lines.append(f"Current Signal: {mom_label} (Score {mom_score})")
    lines.append("")
    if trajectory.get("history"):
        lines.append("Momentum Trajectory:")
        for e in reversed(trajectory["history"][:6]):
            lines.append(f"  {e['date']}: Score {e['score']} ({e['direction']})")
    lines.append("")
    lines.append(f"  Score sustained for {consec} consecutive day(s).")
    if exhaustion:
        lines.append("  WARNING: Score 5 reached recently -- exhaustion signal active.")
    lines.append("")

    lines.append("Analytical Verdict:")
    lines.append(summary_table)
    lines.append("")
    lines.append(f"Continuation factors ({cont_score} pts):")
    for f_ in cont_factors:
        lines.append(f"  + {f_}")
    lines.append("")
    lines.append(f"Reversal factors ({rev_score} pts):")
    for f_ in rev_factors:
        lines.append(f"  - {f_}")
    lines.append("")

    if mom_score <= 4 and not exhaustion:
        lines.append("Notable: Score never reached 5 in this cycle -- no exhaustion signal yet, "
                      "but also no institutional-grade conviction.")
    lines.append("")
    lines.append(f"Conclusion: {verdict} ({confidence} confidence)")
    if direction_bias != "NEUTRAL":
        lines.append(f"Direction Bias: {direction_bias}")
    lines.append("")

    # Key watch condition
    if above_day_max:
        lines.append(
            f"Price has BROKEN ABOVE 1D Max {day_max:.2f} -- "
            f"no reversal signal yet. Reversal requires: "
            f"(1) price close back below {day_max:.2f}, or "
            f"(2) momentum score drops to {mom_score-1} or lower next session, or "
            f"(3) CVD turns strongly negative while price stalls."
        )
    elif verdict in ("REVERSAL", "CONTESTED"):
        if mom_score >= 4:
            lines.append(
                f"Key Risk: Momentum Score {mom_score} still intact -- "
                f"reversal thesis needs confirmation: score drop to {mom_score-1}+ "
                f"OR CVD divergence OR rejection close at a key level."
            )
        if at_day_max or at_call_res:
            lines.append(
                "Level context: Price near resistance -- WAIT for rejection close or "
                "CVD divergence before treating as reversal. "
                "Clean break above with positive CVD = continuation."
            )

    analysis_text = "\n".join(lines)

    return CRVerdict(
        verdict=verdict,
        confidence=confidence,
        direction_bias=direction_bias,
        momentum_score=mom_score,
        momentum_direction=mom_dir,
        consecutive_days=consec,
        momentum_trend=mom_trend,
        exhaustion_warning=exhaustion,
        at_day_max=at_day_max,
        at_day_min=at_day_min,
        at_call_resistance=at_call_res or (state_call_res_0d in ("APPROACHING", "REJECTED_AT")),
        at_put_support=at_put_sup or (state_put_sup_0d in ("APPROACHING_FROM_ABOVE", "BOUNCING_AT")),
        gamma_regime=gamma_regime,
        above_hvl=above_hvl,
        iv_regime=iv_regime,
        factors=cont_factors + rev_factors,
        continuation_factors=cont_factors,
        reversal_factors=rev_factors,
        analysis_text=analysis_text,
        summary_table=summary_table,
    )


def to_prompt_context(cr: CRVerdict) -> str:
    """Format CRVerdict as a compact block for AI prompt injection."""
    at_resistance = []
    if cr.at_day_max:
        at_resistance.append("AT 1D Max (gamma ceiling)")
    if cr.at_call_resistance:
        at_resistance.append("AT Call Resistance (dealer selling zone)")
    if cr.at_put_support:
        at_resistance.append("AT Put Support (dealer buying zone)")

    res_str = " | ".join(at_resistance) if at_resistance else "Not at key level"
    conf_str = f"{cr.verdict} ({cr.confidence} confidence)"

    lines = [
        "## Continuation/Reversal Assessment (Quinn-style)",
        f"Verdict: {conf_str}  |  Direction Bias: {cr.direction_bias}",
        f"Momentum: Score {cr.momentum_score} ({cr.momentum_direction}), "
        f"{cr.consecutive_days} day(s) sustained, trend={cr.momentum_trend}",
        f"Price Location: {res_str}",
        f"Gamma: {cr.gamma_regime}  |  {'ABOVE HVL' if cr.above_hvl else 'BELOW HVL'}  |  "
        f"Vol={cr.iv_regime}",
    ]
    if cr.reversal_factors:
        lines.append("Reversal signals: " + " | ".join(cr.reversal_factors[:2]))
    if cr.continuation_factors:
        lines.append("Continuation signals: " + " | ".join(cr.continuation_factors[:2]))

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # Simulate with sample market data from bridged file
    sample_market = {
        "price": 25344.5,
        "vwap":  25204.5,
        "atr_5m": 45.2,
        "atr_1m": 11.3,
        "cvd": -211470126,
        "tf_bias": {"1m": "BULLISH", "5m": "BULLISH", "15m": "BULLISH", "60m": "BULLISH"},
        "gamma_regime": "POSITIVE",
        "above_hvl": True,
        "mq_day_min": 25382.76,
        "mq_day_max": 25998.73,
    }

    try:
        from core.menthorq_feed import get_snapshot
        mq_snap = get_snapshot()
        sample_market["gamma_regime"] = "POSITIVE" if sample_market["price"] >= mq_snap.hvl else "NEGATIVE"
        sample_market["above_hvl"] = sample_market["price"] >= mq_snap.hvl
    except Exception:
        mq_snap = None

    from core.momentum_score import get_trajectory
    traj = get_trajectory(10)

    cr = assess(sample_market, mq_snap, traj)

    print("\n" + "=" * 85)
    print(cr.analysis_text)
    print("=" * 85)
    print("\nPrompt context block:")
    print(to_prompt_context(cr))
