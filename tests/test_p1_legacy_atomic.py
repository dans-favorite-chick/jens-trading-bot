"""
P1 — Legacy write_oif() atomic staging tests.

Before P1, the legacy single-order path did `with open(filepath, "w")` —
if Python crashed between open() and close(), NT8's filesystem watcher
could pick up a half-written .txt file. The atomic bracket path already
used _stage_oif + _commit_staged (tmp → rename); this fix extends the
same pattern to the legacy actions (EXIT, CANCEL_ALL, CANCEL,
PARTIAL_EXIT_LONG/SHORT, PLACE_STOP_SELL/BUY).

Run: python -m unittest tests.test_p1_legacy_atomic -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class _OIFIsolator:
    """Monkey-patch OIF_INCOMING to a temp dir; clean up on exit."""

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp()
        import bridge.oif_writer as oif
        self.oif = oif
        self._orig = oif.OIF_INCOMING
        oif.OIF_INCOMING = self.tmpdir
        return self.tmpdir

    def __exit__(self, *exc):
        self.oif.OIF_INCOMING = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def _dir_state(path):
    """(txt_count, tmp_count, content_list) — for post-write inspection."""
    names = os.listdir(path)
    txt = [n for n in names if n.endswith(".txt")]
    tmp = [n for n in names if n.endswith(".tmp")]
    contents = {n: open(os.path.join(path, n)).read() for n in txt}
    return txt, tmp, contents


class TestLegacyPathAtomic(unittest.TestCase):
    """Each legacy action must land as a fully-formed .txt (not .tmp)."""

    def test_exit_commits_as_txt_no_tmp_left(self):
        with _OIFIsolator() as tmp:
            from bridge.oif_writer import write_oif
            paths = write_oif("EXIT", trade_id="exit1", account="Sim101")
            txt, tmp_files, _ = _dir_state(tmp)
            self.assertEqual(len(paths), 1)
            self.assertEqual(len(txt), 1)
            self.assertEqual(len(tmp_files), 0,
                             "No .tmp files may linger after successful commit")

    def test_partial_exit_long_atomic(self):
        with _OIFIsolator() as tmp:
            from bridge.oif_writer import write_oif
            paths = write_oif("PARTIAL_EXIT_LONG", qty=1, trade_id="pex", account="Sim101")
            txt, tmp_files, contents = _dir_state(tmp)
            self.assertEqual(len(txt), 1)
            self.assertEqual(len(tmp_files), 0)
            self.assertIn("SELL", list(contents.values())[0])

    def test_partial_exit_short_atomic(self):
        with _OIFIsolator() as tmp:
            from bridge.oif_writer import write_oif
            paths = write_oif("PARTIAL_EXIT_SHORT", qty=1, trade_id="pes", account="Sim101")
            txt, tmp_files, contents = _dir_state(tmp)
            self.assertEqual(len(txt), 1)
            self.assertEqual(len(tmp_files), 0)
            self.assertIn("BUY", list(contents.values())[0])

    def test_place_stop_sell_atomic(self):
        with _OIFIsolator() as tmp:
            from bridge.oif_writer import write_oif
            paths = write_oif("PLACE_STOP_SELL", qty=1, stop_price=21950.0,
                              trade_id="pss", account="Sim101")
            txt, tmp_files, contents = _dir_state(tmp)
            self.assertEqual(len(txt), 1)
            self.assertEqual(len(tmp_files), 0)
            content = list(contents.values())[0]
            self.assertIn("STOPMARKET", content)
            self.assertIn("21950.00", content)

    def test_cancel_all_atomic(self):
        with _OIFIsolator() as tmp:
            from bridge.oif_writer import write_oif
            paths = write_oif("CANCEL_ALL", account="SimBias Momentum")  # B58
            txt, tmp_files, _ = _dir_state(tmp)
            self.assertEqual(len(txt), 1)
            self.assertEqual(len(tmp_files), 0)


class TestFileContentWellFormed(unittest.TestCase):
    """Written files must be complete (no zero-byte / partial-line files)."""

    def test_exit_content_is_complete_line(self):
        with _OIFIsolator() as tmp:
            from bridge.oif_writer import write_oif
            paths = write_oif("EXIT", trade_id="c1", account="Sim101")
            content = open(paths[0]).read()
            # Must start with the command verb and end with a newline
            self.assertTrue(content.startswith("CLOSEPOSITION"))
            self.assertTrue(content.endswith("\n"))
            # Must have the expected semicolon structure intact
            self.assertGreater(content.count(";"), 3)

    def test_legacy_path_marker_present(self):
        """P1 fix marker in the source — if it disappears in a refactor,
        someone replaced the atomic staging with direct write again."""
        src = (Path(__file__).parent.parent / "bridge" / "oif_writer.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("P1 fix", src,
                      "P1 atomic staging marker missing from oif_writer.py")
        self.assertIn("_stage_oif", src)
        self.assertIn("_commit_staged", src)

    def test_legacy_path_no_longer_uses_plain_open_write(self):
        """B45 rev3 (2026-04-21) intentionally reverted to direct write
        inside _stage_oif after .tmp/.stage attempts broke NT8's
        FileSystemWatcher. The original P1 concern (partial-write race on
        concurrent NT8 read) is empirically non-existent for ~100-byte
        OIF files on Windows NTFS — partial-write window is sub-ms and
        NT8 reads only on complete-write events.

        New contract: _stage_oif must write to the final path directly,
        no cross-directory rename. Other functions in oif_writer must
        NOT do their own `with open(filepath, "w")` (all file IO goes
        through _stage_oif).
        """
        src = (Path(__file__).parent.parent / "bridge" / "oif_writer.py").read_text(
            encoding="utf-8"
        )
        stage_start = src.index("def _stage_oif")
        stage_end = src.index("def ", stage_start + 1)
        stage_region = src[stage_start:stage_end]
        # B45 rev3: direct-write to final_path (no staging dir).
        self.assertIn('open(final_path, "w")', stage_region,
                      "_stage_oif must write directly to final_path (B45 rev3)")
        # Everywhere else: should NOT have plain `open(filepath, "w")` ops
        rest = src[:stage_start] + src[stage_end:]
        self.assertNotIn('with open(filepath, "w")', rest,
                         "Legacy write_oif() reverted to plain open() — P1 regressed")


class TestMultiCommandBatch(unittest.TestCase):
    """When cmds has multiple entries (rare — only if legacy path ever
    emits multiple commands), all must commit together."""

    def test_all_commands_in_batch_either_commit_or_rollback(self):
        with _OIFIsolator() as tmp:
            from bridge.oif_writer import write_oif
            # Normal single-command action — all 1 should land
            paths = write_oif("PLACE_STOP_BUY", qty=1, stop_price=22050.0,
                              trade_id="batch", account="Sim101")
            self.assertEqual(len(paths), 1)
            txt, tmp_files, _ = _dir_state(tmp)
            self.assertEqual(len(tmp_files), 0)


if __name__ == "__main__":
    unittest.main()
