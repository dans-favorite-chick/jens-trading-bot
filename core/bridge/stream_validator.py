"""
Phoenix Bot - StreamValidator (Phase B+ Section 1)

Sanity-checks ticks at the bridge fanout before they're broadcast to
bots. Added 2026-04-25 after the 2026-04-24 incident where two NT8
TickStreamer indicators (one on the live MNQM6 chart, one on a stale
chart at ~7,150) connected to bridge :8765 simultaneously and both
claimed `instrument=MNQM6`. The bridge happily fanned both streams to
sim_bot which then booked five $40,000-loss phantom trades before the
operator noticed.

The defensive `core/price_sanity.py` layer downstream catches the bad
ticks. This module's job is one level up: identify and quarantine the
bad CLIENT (the source NT8 port) at the bridge fanout layer, so we can
emit a recovery hint and stop the rogue stream at the source.

Three orthogonal signals, evaluated in cheap-to-expensive order:

  1. **Static price band** — `instrument_price_bands.yaml` defines a
     plausible MNQ/ES/etc. price band. Anything outside is rejected
     immediately. O(1).

  2. **Cross-client median absolute deviation (MAD)** — if more than
     one client port is connected, what's the cross-client median
     price for this instrument over the last-N tick window? A tick
     whose contributing client median diverges from the cross-client
     median by more than `mad_threshold_pct` (default 5%) is rejected.
     The single-client case (no peers) always passes this signal.

  3. **Tick-grid alignment** — every futures contract has a fixed
     tick increment (MNQ = 0.25 pts). A tick whose price is not on a
     multiple of `tick_size` (within `tick_size / 100` tolerance) is
     structurally invalid (the exchange can't quote it). O(1).

Public surface (matches Phase B+ Section §1 spec):

  - on_tick(port, instrument, price, tick_size=0.25) -> bool
  - is_quarantined(port) -> bool
  - quarantine_reason(port) -> str | None
  - health_snapshot() -> dict
  - module-level get_validator() -> singleton StreamValidator

Hot-path discipline: no I/O, no logging in the inner loop. The caller
decides whether to drop or pass-through; this module just answers
True/False and updates internal counters.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ───────── Tunables (defaults; configurable via constructor) ─────────

DEFAULT_BANDS_PATH: str = "config/instrument_price_bands.yaml"
DEFAULT_WINDOW_N: int = 30              # MAD computed over last 30 ticks per port
DEFAULT_MAD_THRESHOLD_PCT: float = 0.05  # 5% deviation triggers reject
DEFAULT_TICK_SIZE: float = 0.25          # MNQ/NQ default; override per-call
DEFAULT_QUARANTINE_AFTER: int = 5        # consecutive rejects before quarantine


@dataclass
class _PortStats:
    """Per-port (per-NT8-client) tick stream metrics. Internal state."""
    instrument: str = ""
    last_price: float = 0.0
    last_tick_ts: float = 0.0
    accepted: int = 0
    rejected: int = 0
    quarantined: bool = False
    quarantined_since_ts: Optional[float] = None
    reason: str = ""
    # Ring buffer of recent accepted prices (for per-port median)
    recent_prices: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_WINDOW_N))


class StreamValidator:
    """Fanout-side multi-stream tick validator.

    Designed to be called from a single asyncio loop (the bridge's),
    so we don't add explicit locking — concurrent ``on_tick()`` across
    threads is NOT safe. If a multi-threaded fanout ever arrives, add
    a Lock around the per-port dict mutations.

    Construction loads instrument bands from YAML; if the file is
    missing or unparseable, the validator runs with an empty band
    table — static-band signal becomes a pass-through and the other
    two signals (MAD + tick grid) still apply.

    Per spec: ``on_tick(...)`` returns a plain ``bool``. Rich detail
    (last reason, peer median, drift) is exposed via ``health_snapshot()``.
    """

    def __init__(
        self,
        instrument_bands_path: str = DEFAULT_BANDS_PATH,
        *,
        window_n: int = DEFAULT_WINDOW_N,
        mad_threshold_pct: float = DEFAULT_MAD_THRESHOLD_PCT,
        default_tick_size: float = DEFAULT_TICK_SIZE,
        quarantine_after_n_rejects: int = DEFAULT_QUARANTINE_AFTER,
    ) -> None:
        self.bands: dict[str, dict[str, float]] = {}
        self._bands_path = instrument_bands_path
        self._load_bands(instrument_bands_path)
        self.window_n = window_n
        self.mad_threshold_pct = mad_threshold_pct
        self.default_tick_size = default_tick_size
        self.quarantine_after_n_rejects = quarantine_after_n_rejects
        self._ports: dict[int, _PortStats] = {}

    # ── config ────────────────────────────────────────────────────
    def _load_bands(self, path_str: str) -> None:
        """Load instrument price bands. Uses PyYAML; falls back to a
        tiny line-based parser if PyYAML is unavailable for any reason
        (parser is sufficient for the simple ``KEY: {min: A, max: B}``
        form used in the YAML file).
        """
        path = Path(path_str)
        if not path.is_absolute():
            # Resolve relative to project root (parent of core/).
            project_root = Path(__file__).resolve().parent.parent.parent
            path = project_root / path_str
        if not path.exists():
            self.bands = {}
            return
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(text) or {}
            self.bands = {
                k: {"min": float(v["min"]), "max": float(v["max"])}
                for k, v in loaded.items()
                if isinstance(v, dict) and "min" in v and "max" in v
            }
            return
        except Exception:
            pass
        # Fallback parser
        import re as _re
        out: dict[str, dict[str, float]] = {}
        for line in text.splitlines():
            m = _re.match(
                r"^([A-Z0-9_]+)\s*:\s*\{\s*min\s*:\s*([\d.]+)\s*,\s*max\s*:\s*([\d.]+)",
                line.strip(),
            )
            if m:
                out[m.group(1)] = {"min": float(m.group(2)), "max": float(m.group(3))}
        self.bands = out

    # ── helpers ───────────────────────────────────────────────────
    def _instrument_root(self, instrument: str) -> str:
        """Strip month/year suffix to find the instrument family.

        ``MNQM6`` -> ``MNQ``; ``ESH26`` -> ``ES``. Tries 2-, 3-, and
        4-char suffixes in order; first match in the bands table wins.
        Falls back to the literal instrument string if nothing matches.
        """
        for suffix_len in (2, 3, 4):
            if len(instrument) > suffix_len:
                candidate = instrument[:-suffix_len]
                if candidate in self.bands:
                    return candidate
        return instrument

    # ── signal 1: static price band ───────────────────────────────
    def _check_static_band(
        self, instrument: str, price: float
    ) -> tuple[bool, str]:
        root = self._instrument_root(instrument)
        band = self.bands.get(root) or self.bands.get(instrument)
        if not band:
            # Unknown instrument: pass static-band check (other signals still apply)
            return True, ""
        if price < band["min"]:
            return False, f"static band (price < min for {root})"
        if price > band["max"]:
            return False, f"static band (price > max for {root})"
        return True, ""

    # ── signal 2: cross-client MAD ────────────────────────────────
    def _peer_median(
        self, instrument: str, exclude_port: int
    ) -> tuple[Optional[float], int]:
        """Cross-client median over peer ports' per-port medians.

        Returns ``(median, n_peers)``. ``median`` is None if there are
        fewer than 1 peer with enough data — single-client case
        bootstraps as "always passes MAD".
        """
        peer_medians: list[float] = []
        for p, stats in self._ports.items():
            if p == exclude_port:
                continue
            if stats.instrument != instrument:
                continue
            if not stats.recent_prices:
                continue
            peer_medians.append(statistics.median(stats.recent_prices))
        if not peer_medians:
            return None, 0
        return statistics.median(peer_medians), len(peer_medians)

    def _check_peer_mad(
        self, instrument: str, port: int, price: float
    ) -> tuple[bool, str]:
        peer_med, n_peers = self._peer_median(instrument, port)
        if peer_med is None or peer_med <= 0 or n_peers < 1:
            # Single-client: no peers — always pass
            return True, ""
        # Use this client's running median (or current price if empty)
        stats = self._ports.get(port)
        own_recent = list(stats.recent_prices) if stats and stats.recent_prices else []
        own_median = statistics.median(own_recent + [price]) if own_recent else price
        dev = abs(own_median - peer_med) / peer_med
        if dev > self.mad_threshold_pct:
            return False, (
                f"cross-client MAD (own_med={own_median:.2f} vs "
                f"peer_med={peer_med:.2f}, drift={dev*100:.2f}%)"
            )
        return True, ""

    # ── signal 3: tick-grid alignment ─────────────────────────────
    @staticmethod
    def _check_tick_grid(price: float, tick_size: float) -> tuple[bool, str]:
        if tick_size <= 0:
            return True, ""
        # Tolerance per spec: < tick_size / 100
        tol = tick_size / 100.0
        multiplier = price / tick_size
        nearest = round(multiplier)
        rem = abs(multiplier - nearest) * tick_size
        if rem >= tol:
            return False, f"tick-grid (price {price} not multiple of {tick_size})"
        return True, ""

    # ── public API ────────────────────────────────────────────────
    def on_tick(
        self,
        port: int,
        instrument: str,
        price: float,
        tick_size: float = DEFAULT_TICK_SIZE,
    ) -> bool:
        """Validate one tick from a specific source port.

        Args:
            port: source NT8 client port (the discriminator across streams).
            instrument: tick instrument label, e.g. ``"MNQM6"``.
            price: tick price.
            tick_size: minimum tick increment. Defaults to 0.25 (MNQ/NQ).

        Returns:
            ``True`` if all three signals pass, ``False`` otherwise.

        Side effects:
            Updates per-port stats (accepted/rejected counters, last_price,
            recent_prices ring buffer). Promotes a port to quarantined
            after ``quarantine_after_n_rejects`` consecutive rejects.
        """
        now = time.time()
        stats = self._ports.get(port)
        if stats is None:
            stats = _PortStats(instrument=instrument)
            self._ports[port] = stats
        stats.instrument = instrument or stats.instrument
        stats.last_tick_ts = now

        # Already quarantined? Reject immediately. Operator must clear.
        if stats.quarantined:
            stats.rejected += 1
            return False

        # Signal 1: static price band (cheapest, do it first)
        ok, why = self._check_static_band(instrument, price)
        if not ok:
            self._record_reject(stats, why)
            return False

        # Signal 3: tick-grid (cheap, before MAD)
        ok, why = self._check_tick_grid(price, tick_size)
        if not ok:
            self._record_reject(stats, why)
            return False

        # Signal 2: cross-client MAD (most expensive, last)
        ok, why = self._check_peer_mad(instrument, port, price)
        if not ok:
            self._record_reject(stats, why)
            return False

        # All three signals pass — accept
        stats.accepted += 1
        stats.last_price = price
        stats.recent_prices.append(price)
        stats.reason = "ok"
        return True

    # ── internal state mutators ───────────────────────────────────
    def _record_reject(self, stats: _PortStats, reason: str) -> None:
        stats.rejected += 1
        stats.reason = reason
        if (
            not stats.quarantined
            and stats.rejected >= self.quarantine_after_n_rejects
        ):
            stats.quarantined = True
            stats.quarantined_since_ts = time.time()

    # ── public accessors ──────────────────────────────────────────
    def is_quarantined(self, port: int) -> bool:
        """Return True iff this port is currently quarantined."""
        stats = self._ports.get(port)
        return bool(stats and stats.quarantined)

    def quarantine_reason(self, port: int) -> Optional[str]:
        """Return the latest rejection reason for this port, or None."""
        stats = self._ports.get(port)
        if stats is None:
            return None
        return stats.reason or None

    def quarantine(self, port: int, reason: str = "manual") -> None:
        """Force a port into quarantine immediately (operator hook)."""
        stats = self._ports.get(port)
        if stats is None:
            stats = _PortStats()
            self._ports[port] = stats
        stats.quarantined = True
        stats.quarantined_since_ts = time.time()
        stats.reason = reason

    def unquarantine(self, port: int) -> None:
        """Clear quarantine on a port (operator hook)."""
        stats = self._ports.get(port)
        if stats is None:
            return
        stats.quarantined = False
        stats.quarantined_since_ts = None
        stats.reason = ""
        stats.rejected = 0

    def health_snapshot(self) -> dict:
        """Read-only per-port snapshot for dashboard / quarantine tool.

        Returns a dict mapping ``port -> {last_price, median_30,
        ticks_accepted, ticks_rejected, quarantined, quarantined_since_ts,
        reason, instrument}`` plus a top-level ``bands_loaded`` list
        and tunables for visibility.
        """
        out: dict = {
            "window_n": self.window_n,
            "mad_threshold_pct": self.mad_threshold_pct,
            "bands_loaded": sorted(self.bands.keys()),
            "ports": {},
        }
        for port, s in self._ports.items():
            med = (
                statistics.median(s.recent_prices)
                if s.recent_prices
                else 0.0
            )
            out["ports"][port] = {
                "instrument": s.instrument,
                "last_price": s.last_price,
                "median_30": med,
                "ticks_accepted": s.accepted,
                "ticks_rejected": s.rejected,
                "quarantined": s.quarantined,
                "quarantined_since_ts": s.quarantined_since_ts,
                "reason": s.reason,
            }
        return out


# ──────────────────────────────────────────────────────────────────
# Module-level singleton + accessor
# ──────────────────────────────────────────────────────────────────

_validator: StreamValidator = StreamValidator()


def get_validator() -> StreamValidator:
    """Return the process-wide singleton StreamValidator.

    The bridge and the live monitor share this instance so the monitor
    sees the same per-port stats as the bridge fanout.
    """
    return _validator
