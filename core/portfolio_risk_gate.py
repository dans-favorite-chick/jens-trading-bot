"""Portfolio risk gate — F-07 + F-20 mitigation (P1-3).

Runtime gate BEFORE OIF writes that checks:
- Total directional dollar exposure across all open positions in a
  rolling window (default last 60s) does not exceed PORTFOLIO_DIRECTIONAL_CAP
- Pairwise co-fire Jaccard from tools/strategy_correlation_audit.py
  cached output is below CORRELATION_THRESHOLD when a new entry would
  add to existing directional exposure (same direction as N open positions
  with Jaccard > threshold → halve size or reject)

Two enforcement modes:
- WARN (default): log violations, don't block. Operator sees the data
  flow; nothing changes in behavior until they're confident.
- BLOCK (PHOENIX_PORTFOLIO_CAP_BLOCK=1): refuse OIF #N+1 with REFUSE
  response shape compatible with the existing sink protocol.

The gate is intentionally self-contained: it tracks recent entries in an
in-memory rolling window rather than reaching into PositionManager. This
keeps the OIF write-path side-effect-free if the gate ever throws and
makes the unit tests trivially deterministic (no fixture wiring into bot
positions / NT8 state needed).

Wiring:
- One instance per bot, attached as `bot._portfolio_risk_gate` during
  BaseBot.__init__.
- Bot calls `gate.check_entry(strategy, direction, contracts, signal_price)`
  in `_enter_trade` AFTER sizing/budget/stop-sanity gates pass and BEFORE
  the `await ws.send(json.dumps({...trade...}))` OIF dispatch.
- Result dict: {"decision": "ACCEPT"|"REDUCE"|"REFUSE",
                 "contracts": int, "reason": str}
- After a successful submit, bot calls `gate.record_entry(...)` so the
  rolling window reflects newly placed risk. (See base_bot patch below.)

F-07: per-trade risk-fraction sizing has no portfolio view.
F-20: 11 strategies × $200/strategy/day = $2.2K theoretical daily
      exposure — only the global cap catches it, and only after the fact.
This gate adds a prospective check before the OIF write.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque

try:
    from config.settings import (
        PORTFOLIO_DIRECTIONAL_CAP,
        PORTFOLIO_CORRELATION_THRESHOLD,
    )
except Exception:  # pragma: no cover - safe defaults if settings missing
    PORTFOLIO_DIRECTIONAL_CAP = 5
    PORTFOLIO_CORRELATION_THRESHOLD = 0.7

# Rolling window for "recent" exposure (seconds). Anything older falls
# out of consideration for the directional-cap math. 60s matches the
# typical co-fire window operators have been informally tracking.
ROLLING_WINDOW_S = 60

# Correlation cache (produced by tools/strategy_correlation_audit.py).
# Optional — absent file → empty matrix → correlation gate is a no-op.
_DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "data" / "strategy_correlation_cache.json"

logger = logging.getLogger("PortfolioRiskGate")


def _is_block_mode() -> bool:
    """BLOCK mode is opt-in via env. Default = WARN (log-only)."""
    return os.environ.get("PHOENIX_PORTFOLIO_CAP_BLOCK", "").strip() in (
        "1", "true", "True", "yes",
    )


class PortfolioRiskGate:
    """Singleton-per-bot portfolio exposure + correlation gate.

    All public methods are sync and exception-safe — the gate must never
    crash the entry path; if anything goes wrong we log and ACCEPT.
    """

    def __init__(
        self,
        bot: Any,
        *,
        directional_cap: int | None = None,
        correlation_threshold: float | None = None,
        rolling_window_s: int = ROLLING_WINDOW_S,
        cache_path: Path | str | None = None,
    ):
        self.bot = bot
        self.directional_cap = int(
            directional_cap
            if directional_cap is not None
            else PORTFOLIO_DIRECTIONAL_CAP
        )
        self.correlation_threshold = float(
            correlation_threshold
            if correlation_threshold is not None
            else PORTFOLIO_CORRELATION_THRESHOLD
        )
        self.rolling_window_s = int(rolling_window_s)
        # (timestamp, strategy, direction, contracts, signal_price, trade_id)
        # trade_id is optional (empty string when caller didn't supply one) and
        # used by record_exit() to free up rolling-window capacity early.
        self._recent: Deque[tuple[float, str, str, int, float, str]] = deque()
        self._cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE
        self._corr_matrix: dict[tuple[str, str], float] = {}
        self._corr_loaded_at: float = 0.0
        self._load_correlation_cache()

    # ─────────────────────────── correlation cache ────────────────────

    def _load_correlation_cache(self) -> None:
        """Read tools/strategy_correlation_audit.py cached output.

        Expected JSON shape (matches that tool's output):
            {
              "generated_at": <unix ts>,
              "pairs": [
                {"a": "bias_momentum", "b": "vwap_pullback",
                 "jaccard": 0.42, ...},
                ...
              ]
            }

        Missing or malformed → empty matrix (correlation gate becomes
        a no-op; directional cap still runs).
        """
        try:
            if not self._cache_path.exists():
                logger.debug(
                    f"[corr-cache] {self._cache_path} absent — "
                    f"correlation gate inert"
                )
                self._corr_matrix = {}
                return
            with open(self._cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            pairs = payload.get("pairs", []) or []
            matrix: dict[tuple[str, str], float] = {}
            for p in pairs:
                a = p.get("a") or p.get("strategy_a")
                b = p.get("b") or p.get("strategy_b")
                j = p.get("jaccard")
                if not a or not b or j is None:
                    continue
                key = tuple(sorted([str(a), str(b)]))
                matrix[key] = float(j)
            self._corr_matrix = matrix
            self._corr_loaded_at = time.time()
            logger.info(
                f"[corr-cache] loaded {len(matrix)} pairs from "
                f"{self._cache_path}"
            )
        except Exception as e:
            logger.warning(
                f"[corr-cache] failed to load {self._cache_path}: {e!r} "
                f"— correlation gate inert"
            )
            self._corr_matrix = {}

    def _jaccard(self, a: str, b: str) -> float:
        if not a or not b or a == b:
            return 0.0
        key = tuple(sorted([str(a), str(b)]))
        return float(self._corr_matrix.get(key, 0.0))

    # ─────────────────────────── rolling window ───────────────────────

    def _prune(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        cutoff = now - self.rolling_window_s
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

    def record_entry(
        self,
        strategy_name: str,
        direction: str,
        contracts: int,
        signal_price: float,
        *,
        timestamp: float | None = None,
        trade_id: str | None = None,
    ) -> None:
        """Bot calls this AFTER a successful OIF submit so the rolling
        window reflects the new exposure.

        Safe to call from anywhere; never raises.
        """
        try:
            ts = timestamp if timestamp is not None else time.time()
            self._recent.append((
                ts, str(strategy_name), str(direction).upper(),
                int(contracts), float(signal_price),
                str(trade_id) if trade_id is not None else "",
            ))
            self._prune(ts)
        except Exception as e:
            logger.warning(f"[gate] record_entry failed (non-blocking): {e!r}")

    def record_exit(self, trade_id: str) -> None:
        """Bot calls this when a position closes. Drops any rolling-window
        entries whose trade_id matches so the freed capacity is available
        for subsequent entries (rather than waiting for the natural TTL).

        Safe to call from anywhere; never raises. Unknown trade_id is a
        no-op (entry may have already aged out of the window).
        """
        try:
            if not trade_id:
                return
            tid = str(trade_id)
            self._recent = deque(
                row for row in self._recent if row[5] != tid
            )
        except Exception as e:
            logger.warning(f"[gate] record_exit failed (non-blocking): {e!r}")

    def _directional_exposure(self, direction: str) -> int:
        """Sum of contracts in the rolling window for a given direction."""
        self._prune()
        d = direction.upper()
        return sum(c for _, _, dd, c, _, _ in self._recent if dd == d)

    def _correlated_open_count(
        self, strategy_name: str, direction: str,
    ) -> tuple[int, list[str]]:
        """How many open same-direction positions are highly correlated
        (Jaccard > threshold) with this strategy?

        Returns (count, [partner_strategy_names]).
        """
        self._prune()
        d = direction.upper()
        partners: list[str] = []
        for _, strat, dd, _, _, _ in self._recent:
            if dd != d:
                continue
            if strat == strategy_name:
                # Same strategy firing again — that's directional
                # concentration, not cross-strategy correlation. Handled
                # by the directional-cap branch.
                continue
            if self._jaccard(strategy_name, strat) > self.correlation_threshold:
                partners.append(strat)
        return len(partners), partners

    # ─────────────────────────── main entrypoint ──────────────────────

    def check_entry(
        self,
        strategy_name: str,
        direction: str,
        contracts: int,
        signal_price: float,
    ) -> dict:
        """Pre-OIF check. Returns:
            {"decision": "ACCEPT"|"REDUCE"|"REFUSE",
             "contracts": int,
             "reason": str}

        WARN-mode (default): always returns ACCEPT (with original
        contracts) but logs the would-be decision so the operator sees
        the data flow before flipping the env flag.

        BLOCK-mode (PHOENIX_PORTFOLIO_CAP_BLOCK=1): returns the real
        decision and the caller halves/rejects as instructed.
        """
        try:
            return self._check_entry_impl(
                strategy_name, direction, int(contracts), float(signal_price),
            )
        except Exception as e:
            # NEVER crash the entry path — log and pass through.
            logger.error(
                f"[gate] check_entry crashed for {strategy_name} "
                f"{direction} x{contracts}: {e!r} — defaulting to ACCEPT"
            )
            return {
                "decision": "ACCEPT",
                "contracts": int(contracts),
                "reason": f"gate-error-passthrough:{e!r}",
            }

    def _check_entry_impl(
        self,
        strategy_name: str,
        direction: str,
        contracts: int,
        signal_price: float,
    ) -> dict:
        d = direction.upper()
        original = int(contracts)
        block = _is_block_mode()

        # 1. Directional cap — would this entry push total directional
        #    exposure in this window over the cap?
        current_dir = self._directional_exposure(d)
        projected = current_dir + original
        if projected > self.directional_cap:
            reason = (
                f"directional cap {d}: current={current_dir} "
                f"+ new={original} = {projected} > "
                f"cap={self.directional_cap} "
                f"(window={self.rolling_window_s}s)"
            )
            log_msg = f"[PORTFOLIO_CAP:{strategy_name}] would-REFUSE {reason}"
            if block:
                logger.warning(log_msg + " — BLOCK mode active")
                return {
                    "decision": "REFUSE",
                    "contracts": 0,
                    "reason": reason,
                }
            logger.info(log_msg + " — WARN mode (passthrough)")
            return {
                "decision": "ACCEPT",
                "contracts": original,
                "reason": f"WARN: would-REFUSE ({reason})",
            }

        # 2. Correlation check — N open same-direction positions with
        #    Jaccard > threshold → halve size (REDUCE). If reducing to
        #    0 would be needed, REFUSE.
        corr_count, partners = self._correlated_open_count(strategy_name, d)
        if corr_count >= 1:
            partners_str = ",".join(sorted(set(partners)))
            reduced = max(1, original // 2)
            reason = (
                f"correlation {d}: {corr_count} same-direction "
                f"open position(s) with Jaccard>{self.correlation_threshold} "
                f"({partners_str}) — halving {original} -> {reduced}"
            )
            log_msg = f"[PORTFOLIO_CAP:{strategy_name}] would-REDUCE {reason}"
            if block:
                logger.warning(log_msg + " — BLOCK mode active")
                return {
                    "decision": "REDUCE",
                    "contracts": reduced,
                    "reason": reason,
                }
            logger.info(log_msg + " — WARN mode (passthrough)")
            return {
                "decision": "ACCEPT",
                "contracts": original,
                "reason": f"WARN: would-REDUCE ({reason})",
            }

        # 3. All clear.
        return {
            "decision": "ACCEPT",
            "contracts": original,
            "reason": (
                f"under cap (current={current_dir} new={original} "
                f"cap={self.directional_cap}, corr_partners=0)"
            ),
        }

    # ─────────────────────────── introspection helpers ────────────────

    def snapshot(self) -> dict:
        """Diagnostic snapshot for dashboard / logs."""
        self._prune()
        long_exp = self._directional_exposure("LONG")
        short_exp = self._directional_exposure("SHORT")
        return {
            "mode": "BLOCK" if _is_block_mode() else "WARN",
            "directional_cap": self.directional_cap,
            "correlation_threshold": self.correlation_threshold,
            "window_seconds": self.rolling_window_s,
            "long_exposure": long_exp,
            "short_exposure": short_exp,
            "recent_entries": len(self._recent),
            "correlation_pairs_loaded": len(self._corr_matrix),
        }

    def reset(self) -> None:
        """Clear the rolling window. Used by tests / daily reset."""
        self._recent.clear()
