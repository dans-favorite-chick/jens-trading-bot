"""HistoryLogger tests.

⚠️  2026-05-06 Sprint J cleanup ⚠️
The original four tests in this module asserted that log_eval surfaced
`gamma_regime` as a first-class snapshot field (S1 phase-eh feature).
With the MenthorQ subscription retired, log_eval no longer writes
`gamma_regime` — those tests have been replaced with a single sanity
test that confirms the field is absent (i.e. removal succeeded).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestLogEvalNoGammaRegime(unittest.TestCase):
    """Sprint J cleanup: gamma_regime field must NOT appear in eval logs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from core import history_logger
        self._patcher = patch.object(history_logger, "HISTORY_DIR", self.tmpdir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _read_events(self, hl):
        hl.close()
        files = [f for f in os.listdir(self.tmpdir) if f.endswith(".jsonl")]
        self.assertEqual(len(files), 1, f"expected 1 jsonl file, got {files}")
        with open(os.path.join(self.tmpdir, files[0]), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_gamma_regime_absent_from_eval_record(self):
        """Even if the caller passes gamma_regime in eval_record / market,
        log_eval should NOT write it (MQ subscription retired)."""
        from core.history_logger import HistoryLogger
        hl = HistoryLogger(bot_name="test")
        hl.log_eval(
            eval_record={"regime": "OPENING", "gamma_regime": "NEGATIVE"},
            market={"price": 25210.0, "gamma_regime": "POSITIVE"},
        )
        events = self._read_events(hl)
        self.assertNotIn("gamma_regime", events[0])
        # Other fields still present (sanity)
        self.assertEqual(events[0]["regime"], "OPENING")
        self.assertEqual(events[0]["price"], 25210.0)


if __name__ == "__main__":
    unittest.main()
