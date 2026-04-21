"""
S1 phase-eh — HistoryLogger.log_eval must surface gamma_regime as a
first-class snapshot field (mirrored from log_entry).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class _FakeGammaEnum(Enum):
    NEGATIVE = "NEGATIVE"
    POSITIVE = "POSITIVE"


class TestLogEvalGammaRegime(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Patch HISTORY_DIR so we don't pollute logs/history/.
        from core import history_logger
        self._patcher = patch.object(history_logger, "HISTORY_DIR", self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        # Don't bother cleaning — tmpdir cleanup is OS-level best-effort.

    def _read_events(self, hl):
        hl.close()
        files = [f for f in os.listdir(self.tmpdir) if f.endswith(".jsonl")]
        self.assertEqual(len(files), 1, f"expected 1 jsonl file, got {files}")
        with open(os.path.join(self.tmpdir, files[0]), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_gamma_regime_from_eval_record_string(self):
        from core.history_logger import HistoryLogger
        hl = HistoryLogger(bot_name="test")
        hl.log_eval(
            eval_record={"regime": "OPENING", "gamma_regime": "NEGATIVE"},
            market={"price": 25210.0},
        )
        events = self._read_events(hl)
        self.assertEqual(events[0]["gamma_regime"], "NEGATIVE")

    def test_gamma_regime_from_eval_record_enum(self):
        from core.history_logger import HistoryLogger
        hl = HistoryLogger(bot_name="test")
        hl.log_eval(
            eval_record={"gamma_regime": _FakeGammaEnum.NEGATIVE},
            market={"price": 25210.0},
        )
        events = self._read_events(hl)
        self.assertEqual(events[0]["gamma_regime"], "NEGATIVE")

    def test_gamma_regime_falls_back_to_market(self):
        from core.history_logger import HistoryLogger
        hl = HistoryLogger(bot_name="test")
        hl.log_eval(
            eval_record={"regime": "PRIMARY"},
            market={"price": 25210.0, "gamma_regime": _FakeGammaEnum.POSITIVE},
        )
        events = self._read_events(hl)
        self.assertEqual(events[0]["gamma_regime"], "POSITIVE")

    def test_gamma_regime_absent_is_none(self):
        from core.history_logger import HistoryLogger
        hl = HistoryLogger(bot_name="test")
        hl.log_eval(
            eval_record={"regime": "PRIMARY"},
            market={"price": 25210.0},
        )
        events = self._read_events(hl)
        self.assertIn("gamma_regime", events[0])
        self.assertIsNone(events[0]["gamma_regime"])


if __name__ == "__main__":
    unittest.main()
