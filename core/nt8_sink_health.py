"""NT8 sink-health state owner.

Owns one piece of state per bot:
  "Is this bot's NT8 sink currently paused because of a PROTECT failure?"

If yes, every subsequent entry attempt is gated upstream
(see ``bots/_trade_entry.py::enter_trade``) until the operator
clears the pause via the dashboard or the ``/nt8_clear`` slash command.

Design source: ``docs/superpowers/specs/2026-06-02-nt8-sink-auto-pause-design.md``.
Incident driver:
``logs/oracle/research/2026-06-02_chart_orders_root_cause.md``.

The module is intentionally narrow:
- no knowledge of strategies, signals, or OIF wire format,
- no imports from ``bots/`` or ``strategies/`` (this module sits
  downstream of both — pulling them in would create a circular import
  at startup),
- telegram failures never block the state update.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Optional

logger = logging.getLogger("NT8SinkHealth")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_DIR = _REPO_ROOT / "runtime"


def _state_path(bot_name: str) -> Path:
    return _RUNTIME_DIR / f"nt8_sink_state_{bot_name}.json"


# ─── State model ──────────────────────────────────────────────────────────

@dataclass
class NT8SinkState:
    """Per-bot snapshot of NT8 sink health.

    Persisted to ``runtime/nt8_sink_state_{bot_name}.json`` so a bot
    restart inherits the paused state rather than racing back into
    submitting OIFs while NT8 ATI is still dead.
    """
    paused: bool = False
    paused_at: Optional[str] = None          # ISO local timestamp
    paused_reason: Optional[str] = None      # human-readable
    paused_trade_id: Optional[str] = None
    paused_strategy: Optional[str] = None
    paused_account: Optional[str] = None
    cleared_at: Optional[str] = None
    cleared_by: Optional[str] = None         # "dashboard" | "slash" | …


# ─── Atomic write helper ─────────────────────────────────────────────────

def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` atomically (tmp + os.replace).

    Mirrors ``core/nt8_order_id_capture.py::_atomic_write``: tmp file in
    the same directory so os.replace is a same-volume rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ─── Owner ────────────────────────────────────────────────────────────────

class NT8SinkHealth:
    """Per-bot owner of the NT8 sink-health state.

    Use ``get_sink_health(bot_name)`` to obtain the singleton instance —
    do not instantiate directly outside of tests.
    """

    def __init__(self, bot_name: str) -> None:
        self.bot_name = bot_name
        self._path = _state_path(bot_name)
        self._lock = RLock()
        self.state: NT8SinkState = self._load()
        if self.state.paused:
            logger.critical(
                "[NT8_SINK] booting paused from %s (%s). "
                "/nt8_clear required to resume.",
                self.state.paused_at, self.state.paused_reason,
            )

    # ─── public API ───
    def is_paused(self) -> bool:
        with self._lock:
            return self.state.paused

    def record_protect_failed(
        self, trade_id: str, strategy: str,
        direction: str, account: Optional[str],
        reason: Optional[str] = None,
    ) -> None:
        """Trip the pause after a PROTECT-all-3-retries-failed event.

        Idempotent: if the bot is already paused, the call is a no-op
        (no field re-write, no second Telegram fire).
        """
        with self._lock:
            if self.state.paused:
                return
            self.state.paused = True
            self.state.paused_at = datetime.now().isoformat(timespec="seconds")
            self.state.paused_reason = (
                reason
                or f"PROTECT FAILED on {trade_id} ({strategy} {direction} on {account})"
            )
            self.state.paused_trade_id = trade_id
            self.state.paused_strategy = strategy
            self.state.paused_account = account
            # Reset cleared_* so the snapshot doesn't carry stale
            # "cleared at …" alongside an active pause.
            self.state.cleared_at = None
            self.state.cleared_by = None
            self._persist()
        self._fire_telegram_pause()

    def clear(self, by: str = "unknown") -> None:
        """Clear the pause. ``by`` records who cleared (audit trail).

        No-op (no Telegram) if the sink is not currently paused — this
        prevents spurious "cleared" alerts on bot boot when state has
        been clean all along.
        """
        with self._lock:
            if not self.state.paused:
                return
            paused_at = self.state.paused_at
            self.state.paused = False
            self.state.cleared_at = datetime.now().isoformat(timespec="seconds")
            self.state.cleared_by = by
            self._persist()
        self._fire_telegram_cleared(paused_at)

    # ─── persistence ───
    def _load(self) -> NT8SinkState:
        if not self._path.exists():
            fresh = NT8SinkState()
            # Don't persist on first boot — let the first real event
            # create the file. Reduces noise on fresh installs.
            return fresh
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[NT8_SINK] could not read %s: %s — treating as fresh state",
                self._path, e,
            )
            return NT8SinkState()
        # Drop unknown keys; tolerate missing ones.
        valid_keys = {f for f in NT8SinkState.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in valid_keys}
        return NT8SinkState(**clean)

    def _persist(self) -> None:
        _atomic_write_json(self._path, asdict(self.state))

    # ─── telegram (best-effort) ───
    def _fire_telegram_pause(self) -> None:
        try:
            from core.telegram_notifier import send_sync
            send_sync(
                f"🛑 NT8 SINK PAUSED — {self.bot_name} bot. "
                f"Cause: {self.state.paused_reason} at {self.state.paused_at}. "
                f"Resume with /nt8_clear or via dashboard.",
                dedup_key=f"nt8_sink_paused:{self.bot_name}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[NT8_SINK] telegram pause alert failed: %s", e)

    def _fire_telegram_cleared(self, paused_at: Optional[str]) -> None:
        try:
            duration_str = ""
            if paused_at:
                try:
                    start = datetime.fromisoformat(paused_at)
                    end = datetime.fromisoformat(self.state.cleared_at)  # type: ignore[arg-type]
                    minutes = (end - start).total_seconds() / 60.0
                    duration_str = f" after {minutes:.1f} min"
                except Exception:  # noqa: BLE001
                    pass
            from core.telegram_notifier import send_sync
            send_sync(
                f"✅ NT8 SINK CLEARED — {self.bot_name} bot resuming. "
                f"Cleared by {self.state.cleared_by}{duration_str}.",
                dedup_key=f"nt8_sink_cleared:{self.bot_name}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[NT8_SINK] telegram clear alert failed: %s", e)


# ─── singleton accessor ─────────────────────────────────────────────────

_INSTANCES: dict[str, NT8SinkHealth] = {}
_INSTANCE_LOCK = RLock()


def get_sink_health(bot_name: str) -> NT8SinkHealth:
    """Return the per-bot singleton.

    Tests can reset the cache via ``reset_sink_health_cache()``.
    """
    with _INSTANCE_LOCK:
        inst = _INSTANCES.get(bot_name)
        if inst is None:
            inst = NT8SinkHealth(bot_name)
            _INSTANCES[bot_name] = inst
        return inst


def reset_sink_health_cache() -> None:
    """Clear the singleton cache. Test helper only."""
    with _INSTANCE_LOCK:
        _INSTANCES.clear()
