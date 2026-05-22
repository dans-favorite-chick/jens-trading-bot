"""
core/confluence_gates.py — shared confluence-voter gate helpers
================================================================

Created 2026-05-22 (ship pt6) from the agent `a16cf0ef28426d5fe`
per-strategy confluence research. The research found a universal alpha
pattern across 6 RTH directional strategies: requiring `tf_60m` and
`es_correlation` agreement with trade direction lifts WR from
~38-47% baseline up to 51-58%.

Centralizing the gate logic here so:
1. The exact same gate runs on every strategy (no copy-paste drift).
2. Adding a new voter or tweaking the agreement rule is one edit.
3. Tests can pin the canonical behavior in one place.

Each helper returns `(passed: bool, reject_reason: Optional[str])` —
callers pattern-match on `not passed` to short-circuit + log.

Usage in a strategy's evaluate():

    from core.confluence_gates import tf60m_es_gate
    passed, reason = tf60m_es_gate(
        market, direction, strategy_name=self.name,
        config=self.config, logger=logger,
    )
    if not passed:
        return None  # rejection already logged by the helper
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

_DEFAULT_LOG = logging.getLogger(__name__)


def _direction_to_sign(direction: str) -> int:
    """LONG -> +1, SHORT -> -1, anything else -> 0."""
    if direction == "LONG":
        return 1
    if direction == "SHORT":
        return -1
    return 0


def _tf_bias_dir(market: dict, tf: str) -> Optional[str]:
    """Extract tf_bias for a specific timeframe ('1m', '5m', '15m', '60m').

    Tries both market["tf_bias"]["60m"] dict-style and the flat
    market["tf_bias_60m"] string-style — both are written by
    different code paths over the lifetime of the bot.

    Returns "LONG" / "SHORT" / None (when bias is NEUTRAL or missing).
    """
    bias_val = None
    tf_obj = market.get("tf_bias")
    if isinstance(tf_obj, dict):
        bias_val = tf_obj.get(tf)
    if bias_val is None:
        bias_val = market.get(f"tf_bias_{tf}")
    if bias_val == "BULL":
        return "LONG"
    if bias_val == "BEAR":
        return "SHORT"
    return None


def _es_correlation_sign(market: dict) -> Optional[int]:
    """Extract ES/NQ relative-strength sign.

    Returns +1 (NQ outperforming → bullish), -1 (NQ underperforming
    → bearish), or None (data unavailable — graceful degrade).
    """
    rs = market.get("es_nq_rs")
    if rs is None:
        im = market.get("intermarket") or {}
        if isinstance(im, dict):
            rs = im.get("nq_es_relative_strength")
    if rs is None:
        return None
    if not isinstance(rs, (int, float)):
        return None
    if rs > 0:
        return 1
    if rs < 0:
        return -1
    return None  # exact zero = neutral, treat as unavailable


def tf60m_es_gate(
    market: dict,
    direction: str,
    *,
    strategy_name: str = "unknown",
    config: Optional[dict] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[bool, Optional[str]]:
    """Universal `tf_60m + es_correlation` confluence gate.

    Per a16cf0ef research: WR lifts from baseline 38-47% to 51-58% on
    6 RTH directional strategies (bias_momentum, spring_setup,
    vwap_pullback_v2, opening_session.orb, opening_session.open_drive,
    ib_breakout) when both voters agree.

    Returns:
        (True, None) — gate passed (direction is supported by both voters,
            OR one voter is unavailable and we graceful-degrade through).
        (False, reason) — at least one voter actively DISAGREES with
            direction. Caller should return None from evaluate(). The
            rejection is logged at INFO inside this helper.

    Args:
        market: the market-dict passed to evaluate()
        direction: "LONG" or "SHORT"
        strategy_name: for log clarity
        config: strategy's config dict. Honors:
            require_tf60m_es_gate (default True) — flip False to disable
                this gate per-strategy without redeploying.
        logger: optional logger; uses module logger if not supplied.
    """
    cfg = config or {}
    log = logger or _DEFAULT_LOG

    if not cfg.get("require_tf60m_es_gate", True):
        return True, None

    dir_sign = _direction_to_sign(direction)
    if dir_sign == 0:
        return True, None  # unknown direction — let downstream handle it

    # tf_60m check
    tf_60m_dir = _tf_bias_dir(market, "60m")
    if tf_60m_dir is not None:
        if tf_60m_dir != direction:
            reason = (
                f"TF60M_GATE: tf_bias_60m={tf_60m_dir} disagrees with "
                f"{direction} (research +$9.91/trade avg edge when agrees)"
            )
            log.info(
                f"[EVAL] {strategy_name}: NO_SIGNAL tf60m_disagree "
                f"({direction} vs {tf_60m_dir})"
            )
            return False, reason

    # ES correlation check
    es_sign = _es_correlation_sign(market)
    if es_sign is not None:
        if es_sign != dir_sign:
            reason = (
                f"ES_GATE: ES/NQ relative-strength sign disagrees with "
                f"{direction} (research +$4.05/trade avg edge when agrees)"
            )
            log.info(
                f"[EVAL] {strategy_name}: NO_SIGNAL es_disagree "
                f"({direction} vs RS sign={es_sign:+d})"
            )
            return False, reason

    return True, None


def tf5m_es_gate(
    market: dict,
    direction: str,
    *,
    strategy_name: str = "unknown",
    config: Optional[dict] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[bool, Optional[str]]:
    """`tf_5m + es_correlation` gate — best for breakout strategies.

    Per research: on e_multi_day_breakout, this combo lifts WR from
    77.8% to 95.97% (n=273), the largest WR improvement of any gate
    in the per-strategy analysis. Use on multi-day / inside-bar
    breakouts where 5m structure aligns with the breakout direction
    and ES confirms the broader risk environment.
    """
    cfg = config or {}
    log = logger or _DEFAULT_LOG

    if not cfg.get("require_tf5m_es_gate", True):
        return True, None

    dir_sign = _direction_to_sign(direction)
    if dir_sign == 0:
        return True, None

    tf_5m_dir = _tf_bias_dir(market, "5m")
    if tf_5m_dir is not None and tf_5m_dir != direction:
        reason = (
            f"TF5M_GATE: tf_bias_5m={tf_5m_dir} disagrees with "
            f"{direction} (research +$4.80/trade avg edge on breakouts)"
        )
        log.info(
            f"[EVAL] {strategy_name}: NO_SIGNAL tf5m_disagree "
            f"({direction} vs {tf_5m_dir})"
        )
        return False, reason

    es_sign = _es_correlation_sign(market)
    if es_sign is not None and es_sign != dir_sign:
        reason = (
            f"ES_GATE: ES/NQ RS sign disagrees with {direction} "
            f"(research +$4.05/trade avg edge when agrees)"
        )
        log.info(
            f"[EVAL] {strategy_name}: NO_SIGNAL es_disagree "
            f"({direction} vs RS sign={es_sign:+d})"
        )
        return False, reason

    return True, None


def regime_veto(
    market: dict,
    veto_regimes: tuple,
    *,
    strategy_name: str = "unknown",
    config: Optional[dict] = None,
    logger: Optional[logging.Logger] = None,
    config_key: str = "veto_regimes_enabled",
) -> Tuple[bool, Optional[str]]:
    """Reject signal if current regime is in the veto list.

    Per a16cf0ef research:
    - bias_momentum: veto OVERNIGHT_RANGE (-$2.55/trade drag)
    - raschke_baseline: veto OPEN_MOMENTUM (-$7.43/trade)
    - vwap_band_pullback: veto OPEN_MOMENTUM (-$35.95/trade — most extreme!)
    - opening_session.orb: veto AFTERNOON_CHOP (-$9.40), LATE_AFTERNOON (-$3.04)

    Args:
        veto_regimes: tuple of regime names that trigger rejection
            (e.g., ("OPEN_MOMENTUM",) or ("AFTERNOON_CHOP", "LATE_AFTERNOON"))
        config_key: per-strategy config flag name to allow disabling
    """
    cfg = config or {}
    log = logger or _DEFAULT_LOG

    if not cfg.get(config_key, True):
        return True, None

    regime = market.get("regime")
    if regime is None:
        # Fall back to session_info would require passing it; skip.
        return True, None

    if regime in veto_regimes:
        reason = (
            f"REGIME_VETO: {regime} is in veto list {veto_regimes} "
            f"(research showed negative edge)"
        )
        log.info(
            f"[EVAL] {strategy_name}: NO_SIGNAL regime_veto={regime}"
        )
        return False, reason

    return True, None
