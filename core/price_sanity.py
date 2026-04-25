"""
Phoenix Bot — Price Sanity Guard

Central defensive layer against corrupted price data. Added 2026-04-24 after
an incident where a mixed NT8 tick stream (two TickStreamer clients on the
bridge publishing different instruments under the same "MNQM6" label) fed
prices oscillating between ~27,175 (real MNQ) and ~7,175 (wrong contract)
to the strategies. The ~20,000-point delta translated into exactly $40,000
P&L per trade at MNQ's $2/point, destroying 5 trades on 2026-04-24 for
-$40,313 × cumulative, including wiping spring_setup below its $1,500 floor.

Two checks:
  1. Tick ingress: rejects a tick whose price deviates from a rolling median
     of recent accepted prices by more than `tick_tolerance_pct` (default
     10%). The median requires a warmup of `warmup_n` ticks; during warmup
     everything is accepted and used to seed the window.
  2. Outbound order: rejects an OIF bracket whose entry_price deviates from
     the current accepted-tick reference by more than `order_tolerance_pct`
     (default 2% — tighter, because if we're about to commit real money
     to an order we'd better be within 2% of the last known market).

Both checks log CRITICAL on rejection and maintain counters exposed via
snapshot() for dashboard surfacing. On rejection the caller decides
what to do: tick_aggregator drops the tick, OIF writer returns [] and
refuses the bracket.

Module-level singleton rather than per-instance. Both the aggregator
(which runs in the bot process) and oif_writer (same process) share
the same reference. FMP-aware overrides can be injected by calling
`set_external_reference(price, source)` — the FMP sanity module does
this on successful fetches so local-only drift is cross-checked.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("PriceSanity")


@dataclass
class _SanityState:
    # Rolling window of accepted tick prices (most recent first).
    window: deque = field(default_factory=lambda: deque(maxlen=30))
    # Last accepted price (fast path — avoids statistics.median on every OIF).
    last_accepted: float = 0.0
    last_accepted_ts: float = 0.0
    # External reference (e.g. FMP NDX converted to MNQ-equivalent). Optional.
    external_ref_price: float = 0.0
    external_ref_source: str = ""
    external_ref_ts: float = 0.0
    # Counters for dashboard.
    ticks_accepted: int = 0
    ticks_rejected: int = 0
    orders_rejected: int = 0
    last_rejection_reason: str = ""
    # 2026-04-24 ADDED: FMP-takeover / fallback mode. When the local tick
    # stream drifts too far from the external reference on consecutive
    # polls, we flip `mode` to "fmp_primary". In that mode new entries
    # are soft-blocked (check_order_price returns False with reason
    # fmp_primary_mode) but existing positions keep being managed. Ticks
    # that agree with FMP within tolerance are still accepted so bars /
    # VWAP / CVD can resume once the stream heals. When `consecutive_fmp_agree`
    # hits the self-heal threshold we flip back to "local_primary".
    mode: str = "local_primary"                  # "local_primary" | "fmp_primary"
    mode_since_ts: float = 0.0
    consecutive_fmp_divergent: int = 0           # Incremented by fmp_sanity poll_loop on bad ticks
    consecutive_fmp_agree: int = 0               # Incremented when local tick matches FMP
    mode_flips: int = 0                           # Counter for dashboard


_state = _SanityState()
_lock = threading.Lock()

# Tunables — conservative defaults. Flip via set_thresholds() if needed.
_tick_tolerance_pct: float = 0.10     # 10% — catches the 20k-point / 75% delta bug
_order_tolerance_pct: float = 0.02    # 2% — tight; an order this far off-market is almost certainly wrong
_warmup_n: int = 10                    # Ticks accepted unconditionally at startup to seed window
_external_max_age_s: float = 90.0     # External reference older than this is ignored


# ═══════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════

def set_thresholds(
    tick_pct: Optional[float] = None,
    order_pct: Optional[float] = None,
    warmup_n: Optional[int] = None,
) -> None:
    """Adjust the guard thresholds. Only override what you mean to change."""
    global _tick_tolerance_pct, _order_tolerance_pct, _warmup_n
    if tick_pct is not None:
        _tick_tolerance_pct = float(tick_pct)
    if order_pct is not None:
        _order_tolerance_pct = float(order_pct)
    if warmup_n is not None:
        _warmup_n = int(warmup_n)


def set_external_reference(price: float, source: str = "fmp") -> None:
    """Record a trusted external reference price (e.g. FMP NDX)."""
    if price and price > 0:
        with _lock:
            _state.external_ref_price = float(price)
            _state.external_ref_source = source
            _state.external_ref_ts = time.time()


# ═══════════════════════════════════════════════════════════════════════
# Mode management (FMP-primary fallback, per Jennifer 2026-04-24)
# ═══════════════════════════════════════════════════════════════════════

# How many consecutive FMP-divergent polls before we flip to fmp_primary.
_DIVERGENT_POLLS_TO_FLIP = 2
# How many consecutive local-agrees-with-FMP polls before we flip back.
_AGREE_POLLS_TO_HEAL = 5


def current_mode() -> str:
    """Return 'local_primary' or 'fmp_primary'."""
    with _lock:
        return _state.mode


def _set_mode(new_mode: str, reason: str) -> bool:
    """Internal: flip the mode and log. Returns True if mode actually changed."""
    with _lock:
        if _state.mode == new_mode:
            return False
        _state.mode = new_mode
        _state.mode_since_ts = time.time()
        _state.mode_flips += 1
        _state.consecutive_fmp_divergent = 0
        _state.consecutive_fmp_agree = 0
    logger.critical(
        f"[PRICE_SANITY] MODE FLIP -> {new_mode}: {reason} (flip #{_state.mode_flips})"
    )
    return True


def record_fmp_check(local_price: float, fmp_price: float,
                     deviation_pct: float, threshold_pct: float) -> Optional[str]:
    """Called by fmp_sanity.poll_loop on every successful cross-check.

    Increments the agree / divergent counters and flips the mode when
    the appropriate threshold is crossed. Returns the new mode if a
    flip happened, else None. 2026-04-24 Jennifer: "rather than producing
    a halt, lets have it flip to fmp temporarily" — so divergence moves
    us to `fmp_primary`, not into the HALT marker.
    """
    if fmp_price <= 0 or local_price <= 0:
        return None
    with _lock:
        if deviation_pct > threshold_pct:
            _state.consecutive_fmp_divergent += 1
            _state.consecutive_fmp_agree = 0
            should_flip_to_fmp = (
                _state.mode == "local_primary"
                and _state.consecutive_fmp_divergent >= _DIVERGENT_POLLS_TO_FLIP
            )
        else:
            _state.consecutive_fmp_agree += 1
            _state.consecutive_fmp_divergent = 0
            should_flip_to_fmp = False
        should_heal = (
            _state.mode == "fmp_primary"
            and _state.consecutive_fmp_agree >= _AGREE_POLLS_TO_HEAL
        )
    if should_flip_to_fmp:
        _set_mode(
            "fmp_primary",
            f"local {local_price:.2f} diverged from FMP {fmp_price:.2f} "
            f"({deviation_pct*100:.2f}% > {threshold_pct*100:.2f}%) for "
            f"{_DIVERGENT_POLLS_TO_FLIP} consecutive polls — soft-blocking "
            f"new entries until stream heals"
        )
        return "fmp_primary"
    if should_heal:
        _set_mode(
            "local_primary",
            f"local agrees with FMP for {_AGREE_POLLS_TO_HEAL} consecutive "
            f"polls — resuming normal entries"
        )
        return "local_primary"
    return None


# ═══════════════════════════════════════════════════════════════════════
# Tick ingress check
# ═══════════════════════════════════════════════════════════════════════

def check_tick(price: float) -> tuple[bool, str]:
    """Decide whether to accept an inbound tick.

    Returns (accepted, reason). If accepted, caller MUST call
    `record_accepted(price)` after its own bookkeeping to keep the
    rolling window current. The accept/record split exists because
    tick_aggregator also has its own price<=0 short-circuit; we want
    this module to be the sole decider for out-of-range tick values
    without duplicating the zero-check.
    """
    if price is None or price <= 0:
        return False, "non_positive_price"
    with _lock:
        n = len(_state.window)
        if n < _warmup_n:
            return True, "warmup"
        median = statistics.median(_state.window)
    if median <= 0:
        return True, "median_zero_accept"
    dev = abs(price - median) / median
    if dev > _tick_tolerance_pct:
        return False, (
            f"deviation {dev*100:.2f}% > {_tick_tolerance_pct*100:.1f}% "
            f"(price={price:.2f}, median={median:.2f})"
        )
    return True, "ok"


def record_accepted(price: float) -> None:
    """Mark a tick as accepted — window and last_accepted updated."""
    if price is None or price <= 0:
        return
    with _lock:
        _state.window.append(float(price))
        _state.last_accepted = float(price)
        _state.last_accepted_ts = time.time()
        _state.ticks_accepted += 1


_LOG_THROTTLE_INTERVAL_S: float = 10.0  # One tick-rejection log at most every 10s
_LOG_THROTTLE_EVERY_N: int = 500         # …plus one per 500 rejections as a progress signal
_last_rejection_log_ts: float = 0.0


def record_rejected(price: float, reason: str) -> None:
    """Mark a tick as rejected — increments counters and logs CRITICAL.

    The log is throttled because a mixed-instrument NT8 stream can spam
    3+ rejections per second (the 2026-04-24 incident generated 340
    rejections in 2 minutes). Unthrottled CRITICAL output would balloon
    sim_bot_stdout.log the way the pre-fix EXIT_PENDING flood did. We
    emit one message at most every 10 seconds, plus a checkpoint log
    every 500 rejections so the counter stays visible over long sessions.
    """
    global _last_rejection_log_ts
    with _lock:
        _state.ticks_rejected += 1
        _state.last_rejection_reason = reason
        rej_count = _state.ticks_rejected
        acc_count = _state.ticks_accepted

    now = time.time()
    should_log = (
        (now - _last_rejection_log_ts) >= _LOG_THROTTLE_INTERVAL_S
        or (rej_count % _LOG_THROTTLE_EVERY_N == 0)
    )
    if should_log:
        _last_rejection_log_ts = now
        logger.critical(
            f"[PRICE_SANITY] TICK REJECTED price={price} reason={reason} "
            f"accepted_total={acc_count} rejected_total={rej_count} "
            f"(log throttled; see snapshot for running counts)"
        )


# ═══════════════════════════════════════════════════════════════════════
# Outbound order check
# ═══════════════════════════════════════════════════════════════════════

def check_order_price(price: float, label: str = "entry") -> tuple[bool, str]:
    """Decide whether an outbound OIF price is sane.

    Tighter than check_tick: an order price must be within 2% of the
    most recent accepted tick. If the tick stream has been silent for
    longer than 30s we fall back to the external reference (FMP). If
    neither is available, we conservatively REJECT.

    2026-04-24: when mode == "fmp_primary" (local tick stream is
    diverging from FMP), new entries are soft-blocked regardless of
    price — we don't trust the local stream enough to open a position,
    but existing positions keep being managed (the caller's side) via
    managed-exit paths that don't call this guard.
    """
    if price is None or price <= 0:
        return False, "non_positive_price"

    with _lock:
        mode = _state.mode
        ref = _state.last_accepted
        ref_age = time.time() - _state.last_accepted_ts if _state.last_accepted_ts else float("inf")
        ext = _state.external_ref_price
        ext_source = _state.external_ref_source
        ext_age = time.time() - _state.external_ref_ts if _state.external_ref_ts else float("inf")

    # Soft-block all new entries while FMP is primary. label="entry" is
    # the tightest block; stop/target on existing positions bypass this
    # check because callers invoke close_position / _exit_trade, not
    # write_bracket_order, when managing open trades.
    if mode == "fmp_primary" and label == "entry":
        return False, (
            f"fmp_primary_mode — local tick stream diverged from FMP; "
            f"new entries soft-blocked until stream heals"
        )

    # In fmp_primary mode, still allow stop/target checks to use FMP as the
    # trusted source. In local_primary mode, prefer the fresh local tick.
    if mode == "fmp_primary" and ext > 0 and ext_age <= _external_max_age_s:
        dev = abs(price - ext) / ext
        if dev > _order_tolerance_pct:
            return False, (
                f"{label} deviation {dev*100:.2f}% > {_order_tolerance_pct*100:.1f}% "
                f"vs FMP ref {ext:.2f} ({ext_source}, age {ext_age:.1f}s)"
            )
        return True, f"ok_vs_fmp ref={ext:.2f}"

    if ref > 0 and ref_age <= 30.0:
        dev = abs(price - ref) / ref
        if dev > _order_tolerance_pct:
            return False, (
                f"{label} deviation {dev*100:.2f}% > {_order_tolerance_pct*100:.1f}% "
                f"vs last tick {ref:.2f} (age {ref_age:.1f}s)"
            )
        return True, f"ok_vs_tick ref={ref:.2f}"

    if ext > 0 and ext_age <= _external_max_age_s:
        dev = abs(price - ext) / ext
        if dev > _order_tolerance_pct:
            return False, (
                f"{label} deviation {dev*100:.2f}% > {_order_tolerance_pct*100:.1f}% "
                f"vs external ref {ext:.2f} ({ext_source}, age {ext_age:.1f}s)"
            )
        return True, f"ok_vs_ext ref={ext:.2f}"

    return False, (
        f"no_usable_reference (last_tick_age={ref_age:.1f}s, "
        f"ext_age={ext_age:.1f}s) — rejecting conservatively"
    )


def record_order_rejected(price: float, reason: str, label: str = "entry") -> None:
    with _lock:
        _state.orders_rejected += 1
        _state.last_rejection_reason = f"order_{label}: {reason}"
    logger.critical(
        f"[PRICE_SANITY] ORDER REJECTED {label}={price} reason={reason} "
        f"orders_rejected_total={_state.orders_rejected}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Observability
# ═══════════════════════════════════════════════════════════════════════

def snapshot() -> dict:
    """Small read-only view for dashboard / logs."""
    with _lock:
        window_copy = list(_state.window)
        median = statistics.median(window_copy) if window_copy else 0.0
        return {
            "last_accepted_price": _state.last_accepted,
            "last_accepted_age_s": (time.time() - _state.last_accepted_ts) if _state.last_accepted_ts else None,
            "window_len": len(window_copy),
            "window_median": median,
            "external_ref_price": _state.external_ref_price,
            "external_ref_source": _state.external_ref_source,
            "external_ref_age_s": (time.time() - _state.external_ref_ts) if _state.external_ref_ts else None,
            "ticks_accepted": _state.ticks_accepted,
            "ticks_rejected": _state.ticks_rejected,
            "orders_rejected": _state.orders_rejected,
            "last_rejection_reason": _state.last_rejection_reason,
            "tick_tolerance_pct": _tick_tolerance_pct,
            "order_tolerance_pct": _order_tolerance_pct,
            # 2026-04-24 mode state
            "mode": _state.mode,
            "mode_since_s": (time.time() - _state.mode_since_ts) if _state.mode_since_ts else None,
            "mode_flips": _state.mode_flips,
            "consecutive_fmp_divergent": _state.consecutive_fmp_divergent,
            "consecutive_fmp_agree": _state.consecutive_fmp_agree,
        }


def reset() -> None:
    """Clear all state. For tests; do not call in production."""
    global _state
    with _lock:
        _state = _SanityState()
