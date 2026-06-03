"""
Finding A — TradeMemory.save() atomic-write invariants.

Pins the 2026-06-02 fix: save() now writes to filepath + ".tmp", fsyncs,
then os.replace()s into the final path. The destination is therefore
never observably half-written.

Three invariants:
  1. The two-step (write tmp, replace) pattern is actually called.
  2. A mid-write failure leaves the destination file byte-for-byte intact.
  3. Concurrent saves never produce a corrupt file (os.replace atomicity).

Run: pytest tests/test_trade_memory_atomic_save.py -v
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from core import trade_memory as tm_module
from core.trade_memory import TradeMemory


# ─── Finding A / test 1: write-tmp-then-replace pattern is actually used ──────

class TestSaveWritesTempThenRenames:

    def test_save_writes_temp_then_renames(self, tmp_path: Path, monkeypatch):
        """save() must write to a sibling .tmp.<pid>.<tid> then os.replace into final.

        We wrap the real os.replace so the on-disk post-condition still
        holds naturally: after save() returns, the .tmp file must be gone
        because os.replace consumed it. The .tmp filename embeds pid +
        thread id so concurrent writers don't collide on Windows (see
        Finding A doc-comment in core/trade_memory.py).
        """
        final_path = str(tmp_path / "trade_memory_test.json")
        expected_tmp_prefix = final_path + ".tmp."

        tm = TradeMemory(filepath=final_path)
        tm.trades = [
            {"trade_id": "t1", "bot_id": "test", "pnl_dollars": 5.0},
            {"trade_id": "t2", "bot_id": "test", "pnl_dollars": -3.0},
        ]

        # Wrap the real os.replace so we capture the call args but still
        # let the atomic rename actually happen.
        real_replace = os.replace
        captured_calls: list[tuple[str, str]] = []

        def capturing_replace(src, dst):
            captured_calls.append((src, dst))
            return real_replace(src, dst)

        monkeypatch.setattr("core.trade_memory.os.replace", capturing_replace)

        tm.save()

        # Invariant 1a: os.replace was called exactly once.
        assert len(captured_calls) == 1, (
            f"expected exactly 1 os.replace call, got {len(captured_calls)}"
        )

        # Invariant 1b: it was called with (tmp, final), where tmp starts
        # with `{final}.tmp.` and dst is the final path.
        src, dst = captured_calls[0]
        assert src.startswith(expected_tmp_prefix), (
            f"os.replace src arg should start with '{expected_tmp_prefix}' "
            f"(filepath.tmp.<pid>.<tid> per Finding A); got '{src}'"
        )
        assert dst == final_path, (
            f"os.replace dst arg should be '{final_path}', got '{dst}'"
        )

        # Invariant 1c: the captured tmp file does NOT exist afterward
        # (real replace consumed it).
        assert not os.path.exists(src), (
            f"sibling .tmp file '{src}' must NOT linger after a "
            f"successful save() — os.replace should have consumed it"
        )

        # Invariant 1d: no stray sibling .tmp.* lingers in the directory.
        leftovers = [
            p for p in os.listdir(str(tmp_path))
            if p.startswith(os.path.basename(final_path) + ".tmp.")
        ]
        assert not leftovers, (
            f"unexpected sibling .tmp.* files lingering: {leftovers}"
        )

        # Invariant 1e: final file exists and contains the expected JSON.
        assert os.path.exists(final_path), "final destination file should exist after save()"
        with open(final_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == tm.trades, "saved JSON must round-trip equal to in-memory trades"


# ─── Finding A / test 2: failed write must NOT truncate destination ───────────

class TestSaveDoesNotTruncateOnFailedWrite:

    def test_save_does_not_truncate_on_failed_write(self, tmp_path: Path, monkeypatch):
        """The destination file must remain byte-for-byte intact across a
        simulated mid-write crash.

        This is THE invariant that motivated Finding A — the pre-fix code
        truncated the file with open(..., "w") BEFORE any new bytes
        landed, so a crash mid-write left an empty/half-written file on
        disk. With the atomic-tmp-then-replace pattern, the destination
        is never touched if json.dump raises.
        """
        final_path = str(tmp_path / "trade_memory_test.json")

        tm = TradeMemory(filepath=final_path)
        tm.trades = [
            {"trade_id": "t1", "bot_id": "test", "pnl_dollars": 5.0},
            {"trade_id": "t2", "bot_id": "test", "pnl_dollars": -3.0},
        ]

        # First save lands cleanly via the real path.
        tm.save()
        assert os.path.exists(final_path), "first save() must land the file on disk"

        # Snapshot the on-disk bytes — this is the contract we're pinning.
        with open(final_path, "rb") as f:
            snapshot_bytes = f.read()
        assert snapshot_bytes, "snapshot bytes should be non-empty"

        # Now simulate a mid-write crash by making json.dump raise. The
        # implementation's except-Exception block will swallow it.
        def boom(*args, **kwargs):
            raise RuntimeError("simulated mid-write crash")

        monkeypatch.setattr("core.trade_memory.json.dump", boom)

        # Mutate in-memory trades so a successful write WOULD have changed bytes.
        tm.trades.append(
            {"trade_id": "t3", "bot_id": "test", "pnl_dollars": 42.0}
        )

        # This save() must swallow the error and leave the destination alone.
        tm.save()

        # Invariant 2a: the destination file still exists.
        assert os.path.exists(final_path), (
            "destination file must still exist after failed save() — "
            "atomic write means the original is never touched on failure"
        )

        # Invariant 2b: byte-for-byte equal to the snapshot.
        with open(final_path, "rb") as f:
            current_bytes = f.read()
        assert current_bytes == snapshot_bytes, (
            "destination file changed during a failed save() — the atomic "
            "write contract is BROKEN. Pre-fix bug behavior detected."
        )

        # Invariant 2c: no sibling .tmp.* must linger as garbage in the
        # directory (best-effort cleanup in the except block). Glob the
        # parent dir to catch the pid/tid-suffixed tmp filename.
        parent = os.path.dirname(final_path)
        leftovers = [
            p for p in os.listdir(parent)
            if p.startswith(os.path.basename(final_path) + ".tmp.")
        ]
        assert not leftovers, (
            f"sibling .tmp.* files should be cleaned up on failure; "
            f"found leftovers: {leftovers}"
        )


# ─── Finding A / test 3: concurrent saves never produce a corrupt file ────────

class TestConcurrentSaveDoesNotCorrupt:

    def test_concurrent_save_does_not_corrupt(self, tmp_path: Path):
        """Two threads saving to the same path must NEVER leave a corrupt file.

        os.replace is atomic on both POSIX and Windows (Py3.3+), so the
        final on-disk file must equal exactly ONE of the two thread
        snapshots — last-replacer-wins, no interleaving, no partial write.

        We do NOT pin which thread wins (that's a race we don't care
        about); we ONLY pin "no corruption".
        """
        final_path = str(tmp_path / "trade_memory_test.json")

        trades_a = [
            {"trade_id": f"a{i}", "bot_id": "thread_a", "pnl_dollars": float(i)}
            for i in range(20)
        ]
        trades_b = [
            {"trade_id": f"b{i}", "bot_id": "thread_b", "pnl_dollars": float(-i)}
            for i in range(20)
        ]

        tm_a = TradeMemory(filepath=final_path)
        tm_a.trades = list(trades_a)

        tm_b = TradeMemory(filepath=final_path)
        tm_b.trades = list(trades_b)

        # Force both threads to hit save() at roughly the same instant.
        barrier = threading.Barrier(2)

        def run_save(tm: TradeMemory):
            barrier.wait()
            tm.save()

        t_a = threading.Thread(target=run_save, args=(tm_a,))
        t_b = threading.Thread(target=run_save, args=(tm_b,))

        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert not t_a.is_alive(), "thread A failed to complete save() within 10s"
        assert not t_b.is_alive(), "thread B failed to complete save() within 10s"

        # Invariant 3a: file exists.
        assert os.path.exists(final_path), "final file must exist after concurrent saves"

        # Invariant 3b: parses as valid JSON. (Pre-fix code could have left
        # a half-written file here — that would raise here.)
        with open(final_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        # Invariant 3c: top-level is a list.
        assert isinstance(loaded, list), (
            f"final file top-level should be a list, got {type(loaded).__name__}"
        )

        # Invariant 3d: equals one of the two snapshots exactly. os.replace is
        # atomic, so the loser's bytes are replaced wholesale by the winner's.
        # No partial / interleaved content is permitted.
        assert loaded == trades_a or loaded == trades_b, (
            "final file content does not match EITHER thread's snapshot — "
            "this means the writes interleaved on disk, which violates "
            "the os.replace atomicity contract Finding A relies on."
        )

        # Invariant 3e: no .tmp.* garbage lingers from either thread.
        # Glob the parent because each thread uses a pid/tid-suffixed tmp.
        parent = os.path.dirname(final_path)
        leftovers = [
            p for p in os.listdir(parent)
            if p.startswith(os.path.basename(final_path) + ".tmp.")
        ]
        assert not leftovers, (
            f"sibling .tmp.* files should not linger after concurrent saves "
            f"complete — both threads should have consumed their own .tmp via "
            f"os.replace. Found leftovers: {leftovers}"
        )
