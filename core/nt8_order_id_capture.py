"""NT8 stop-order ID capture (P1-8).

After Phoenix writes a stop OIF, NT8 ATI eventually writes a 'WORKING'
file in the outgoing folder containing the assigned order ID. This
module polls for that file (default: 200ms x 25 = 5s max wait) and
returns the captured ID -- or None if it didn't appear.

The captured ID is persisted to data/active_stops.json (trade_id -> stop_order_id)
so a bot restart doesn't lose the mapping. When a strategy later wants
to move the stop, base_bot._move_nt8_stop reads from this file and
issues a cancel-and-replace via the OIF modify_stop writer.

Why this matters: without the ID, dynamic stop moves silently no-op
(see [STOP_MOVE_NO_ID] log signature). Strategies with managed exits
(noise_area, vwap_pullback_v2 trail, etc.) cannot actually trail.

NT8 ATI emits one file per working order into the configured outgoing/
folder, named ``{account}_{order_id}.txt`` whose first line is
``WORKING;0;<price>`` (the same shape consumed by
``bridge.oif_writer.scan_outgoing_for_order_id``). The polling routine
here watches for *new* WORKING files appearing inside the wait window
and returns the order_id portion of the filename.

Owned files (per P1-8 contract):
    - core/nt8_order_id_capture.py            (this module)
    - tests/test_nt8_order_id_capture.py      (round-trip + timeout tests)

Patches to bots/base_bot.py and bridge/oif_writer.py are documented in
the P1-8 task summary -- this module does NOT mutate either file
directly.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nt8_order_id_capture")

# Defaults wired to mirror existing oif_writer scan loop cadence (~150ms)
# and the spec's 5s ceiling. 200ms x 25 = 5.0s.
_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_POLL_INTERVAL_S = 0.2

# Persistence default. Resolved relative to the phoenix_bot project root
# so callers don't have to think about cwd. Override via the `path` kwarg.
_DEFAULT_STATE_PATH = "data/active_stops.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    """Return the phoenix_bot project root.

    This module sits at ``phoenix_bot/core/nt8_order_id_capture.py`` so
    the project root is one directory up from this file's parent.
    """
    return Path(__file__).resolve().parent.parent


def _resolve_state_path(path: str) -> Path:
    """Resolve a state-file path. Absolute paths pass through; relative
    paths are anchored at the project root so callers from any cwd
    converge on the same file."""
    p = Path(path)
    if p.is_absolute():
        return p
    return _project_root() / p


def _load_state(path: str) -> dict:
    """Return the persisted trade_id -> stop_order_id map.

    Returns an empty dict when the file is missing or unreadable -- this
    matches the spec contract that ``load_stop_id`` returns None on
    miss rather than raising.
    """
    sp = _resolve_state_path(path)
    if not sp.exists():
        return {}
    try:
        with sp.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "[NT8_ID_CAPTURE] %s did not contain a dict (got %s) -- "
                "treating as empty.", sp, type(data).__name__,
            )
            return {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "[NT8_ID_CAPTURE] could not read state file %s: %s -- "
            "treating as empty.", sp, e,
        )
        return {}


def _atomic_write(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` atomically (tmp + os.replace).

    Mirrors the pattern used in core/tier_sizer.py: write to a sibling
    tmp file in the same directory (so os.replace is a same-volume
    rename) then atomically swap. Crash-safe: a partial write leaves
    the previous file intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile + delete=False so we can rename it ourselves.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # Some filesystems (or pytest tmpfs on Windows) reject fsync;
                # the rename is still the atomicity guarantee we need.
                pass
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup so failed writes don't leak tmp files.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _resolve_outgoing_dir(outgoing_dir: Optional[str]) -> str:
    """Resolve the outgoing folder path.

    When the caller passes None, fall back to ``config.settings.OIF_OUTGOING``
    -- the spec's "Polling MUST use the OIF_OUTGOING constant" constraint.
    Importing it lazily avoids forcing every importer of this module to
    pay the settings-load cost.
    """
    if outgoing_dir:
        return outgoing_dir
    try:
        # Ensure project root on sys.path for direct-script callers.
        root = str(_project_root())
        if root not in sys.path:
            sys.path.insert(0, root)
        from config.settings import OIF_OUTGOING  # type: ignore
        return OIF_OUTGOING
    except Exception as e:  # pragma: no cover - settings import edge
        raise RuntimeError(
            "nt8_order_id_capture: outgoing_dir not supplied and "
            f"config.settings.OIF_OUTGOING could not be imported: {e}"
        )


def _scan_working_files(
    outgoing_dir: str,
    since_mtime: float,
    seen: set,
) -> Optional[str]:
    """Scan ``outgoing_dir`` for a NEW file whose contents start with
    'WORKING'. Returns the parsed order_id (filename component after the
    first underscore, sans .txt) or None if no new WORKING file is
    visible yet.

    ``seen`` tracks filenames already inspected so we don't keep
    re-reading files that pre-existed the wait. ``since_mtime`` is the
    monotonic-ish threshold (mtime in epoch seconds) below which files
    are ignored as historical.
    """
    try:
        entries = list(os.scandir(outgoing_dir))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        logger.debug("[NT8_ID_CAPTURE] scandir(%s) failed: %s", outgoing_dir, e)
        return None

    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        name = entry.name
        if not name.endswith(".txt"):
            continue
        if name in seen:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < since_mtime:
            # Existed before we started polling -- ignore so we don't
            # adopt a stale order_id for a previous trade with the same
            # account.
            seen.add(name)
            continue
        try:
            with open(entry.path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read().strip()
        except OSError:
            continue
        seen.add(name)
        if not content.upper().startswith("WORKING"):
            continue
        # Filename shape per NT8 ATI: ``{account}_{order_id}.txt``.
        stem = name[:-4]  # strip .txt
        if "_" in stem:
            order_id = stem.split("_", 1)[1]
        else:
            order_id = stem
        if order_id:
            return order_id
    return None


# ---------------------------------------------------------------------------
# Public API (P1-8 contract)
# ---------------------------------------------------------------------------

def wait_for_stop_id(
    trade_id: str,
    outgoing_dir: Optional[str] = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> Optional[str]:
    """Poll the NT8 outgoing folder for a fresh 'WORKING' file and return
    the order_id NT8 assigned.

    Args:
        trade_id: Phoenix-side trade identifier. Used only for log
            tagging -- NT8 ATI does not embed trade_id in its outgoing
            file names; the WORKING file is correlated by being the
            next ``{account}_{order_id}.txt`` to appear after the OIF
            was written.
        outgoing_dir: Folder to poll. When None, resolves to
            ``config.settings.OIF_OUTGOING``.
        timeout_s: Maximum wall-clock seconds to wait before giving up.
        poll_interval_s: Sleep between scans.

    Returns:
        The NT8 order_id string on success, or None if no WORKING file
        appeared inside the wait window.
    """
    resolved = _resolve_outgoing_dir(outgoing_dir)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    # Anchor the "new file" check to wall time just before we start
    # polling. NT8 emits the WORKING file *after* it accepts our OIF,
    # so anything older than this is a previous order's file.
    since_mtime = time.time() - 0.5  # 500ms grace for filesystem latency
    seen: set = set()

    # Prime ``seen`` with pre-existing files older than the grace window
    # so we don't keep re-reading them on every tick of the poll loop.
    try:
        for entry in os.scandir(resolved):
            try:
                if entry.is_file() and entry.stat().st_mtime < since_mtime:
                    seen.add(entry.name)
            except OSError:
                continue
    except (FileNotFoundError, NotADirectoryError):
        logger.debug(
            "[NT8_ID_CAPTURE:%s] outgoing dir does not exist yet: %s",
            trade_id, resolved,
        )

    # First scan immediately so a zero-or-tiny timeout still inspects the
    # folder once before returning.
    while True:
        oid = _scan_working_files(resolved, since_mtime, seen)
        if oid:
            logger.info(
                "[NT8_ID_CAPTURE:%s] captured stop_order_id=%s",
                trade_id, oid,
            )
            return oid
        if time.monotonic() >= deadline:
            logger.warning(
                "[NT8_ID_CAPTURE:%s] no WORKING file appeared in %ss "
                "(outgoing=%s)", trade_id, timeout_s, resolved,
            )
            return None
        time.sleep(max(0.001, float(poll_interval_s)))


def save_stop_id(
    trade_id: str,
    stop_order_id: str,
    path: str = _DEFAULT_STATE_PATH,
) -> None:
    """Persist a (trade_id -> stop_order_id) mapping atomically.

    The state file is the single source of truth that lets a bot
    restart recover the order_id needed to issue a cancel-and-replace
    when a strategy decides to move its stop.
    """
    if not trade_id or not str(trade_id).strip():
        raise ValueError("save_stop_id: trade_id must be non-empty")
    if not stop_order_id or not str(stop_order_id).strip():
        raise ValueError("save_stop_id: stop_order_id must be non-empty")

    sp = _resolve_state_path(path)
    state = _load_state(path)
    state[str(trade_id)] = str(stop_order_id)
    _atomic_write(sp, state)
    logger.debug(
        "[NT8_ID_CAPTURE] saved trade_id=%s -> stop_order_id=%s (file=%s)",
        trade_id, stop_order_id, sp,
    )


def load_stop_id(
    trade_id: str,
    path: str = _DEFAULT_STATE_PATH,
) -> Optional[str]:
    """Return the previously-saved stop_order_id for ``trade_id``,
    or None if absent."""
    if not trade_id:
        return None
    state = _load_state(path)
    val = state.get(str(trade_id))
    if val is None:
        return None
    return str(val) or None


def clear_stop_id(
    trade_id: str,
    path: str = _DEFAULT_STATE_PATH,
) -> None:
    """Remove the (trade_id -> stop_order_id) mapping.

    Called on position close so the state file doesn't grow unbounded
    and stale IDs from closed trades can't be cancel-replaced by
    accident if a trade_id is somehow reused.
    """
    if not trade_id:
        return
    sp = _resolve_state_path(path)
    state = _load_state(path)
    if str(trade_id) not in state:
        return
    state.pop(str(trade_id), None)
    _atomic_write(sp, state)
    logger.debug(
        "[NT8_ID_CAPTURE] cleared trade_id=%s (file=%s)", trade_id, sp,
    )
