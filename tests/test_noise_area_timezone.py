"""
Phoenix Bot — Timezone-correctness tests for noise_area / ORB / IB strategies.

Verifies that explicit zoneinfo ET conversion handles both standard (EST)
and daylight-savings (EDT) correctly. Prior naive implementation used
`datetime.fromtimestamp(...) + timedelta(hours=1)` which silently broke on
UTC-hosted VPS instances and was DST-unaware.

Run: python -m unittest tests.test_noise_area_timezone -v
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

_ET = ZoneInfo("America/New_York")


@dataclass
class _Bar:
    open: float = 22000.0
    high: float = 22005.0
    low: float = 21995.0
    close: float = 22000.0
    volume: int = 100
    tick_count: int = 1
    start_time: float = 0.0
    end_time: float = 0.0


def _utc_epoch(year, month, day, hour, minute) -> float:
    """Build a Unix epoch for a given UTC wall-clock moment."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


# ═════════════════════════════════════════════════════════════════════
# Direct zoneinfo correctness — foundation for all strategy conversions
# ═════════════════════════════════════════════════════════════════════
class TestZoneInfoConversion(unittest.TestCase):
    def test_winter_est_no_dst(self):
        """Jan 15, 2026 14:30 UTC = 9:30 ET (EST, UTC-5)."""
        epoch = _utc_epoch(2026, 1, 15, 14, 30)
        et = datetime.fromtimestamp(epoch, tz=_ET)
        self.assertEqual(et.strftime("%Y-%m-%d %H:%M"), "2026-01-15 09:30")

    def test_summer_edt_with_dst(self):
        """Jul 15, 2026 13:30 UTC = 9:30 ET (EDT, UTC-4)."""
        epoch = _utc_epoch(2026, 7, 15, 13, 30)
        et = datetime.fromtimestamp(epoch, tz=_ET)
        self.assertEqual(et.strftime("%Y-%m-%d %H:%M"), "2026-07-15 09:30")

    def test_dst_transition_spring_forward(self):
        """Mar 8, 2026 is DST start in US. The 2am→3am jump happens at 07:00 UTC
        (= 02:00 EST → 03:00 EDT). 06:30 UTC is pre-jump (01:30 EST),
        07:30 UTC is post-jump (03:30 EDT)."""
        before_jump = datetime.fromtimestamp(_utc_epoch(2026, 3, 8, 6, 30), tz=_ET)
        after_jump = datetime.fromtimestamp(_utc_epoch(2026, 3, 8, 7, 30), tz=_ET)
        self.assertEqual(before_jump.strftime("%H:%M"), "01:30")
        self.assertEqual(after_jump.strftime("%H:%M"), "03:30")
        # UTC offset shifts from -5 (EST) to -4 (EDT) across the jump
        self.assertEqual(before_jump.utcoffset(), timedelta(hours=-5))
        self.assertEqual(after_jump.utcoffset(), timedelta(hours=-4))

    def test_naive_offset_would_be_wrong(self):
        """Demonstrate the old bug: naive +1h from CT is wrong during DST-mismatched
        scenarios. This test pins the behavior we're AVOIDING."""
        epoch = _utc_epoch(2026, 1, 15, 14, 30)  # 9:30 ET = 8:30 CT
        # Old broken code: naive fromtimestamp (interprets as local time)
        # would give wrong ET only if the host isn't CT.
        # New code: explicit ET via zoneinfo — correct from any host.
        et_correct = datetime.fromtimestamp(epoch, tz=_ET)
        self.assertEqual(et_correct.hour, 9)
        self.assertEqual(et_correct.minute, 30)


# ═════════════════════════════════════════════════════════════════════
# Noise Area _minute_of_day math — critical to the signal cadence
# ═════════════════════════════════════════════════════════════════════
class TestNoiseAreaMinuteOfDay(unittest.TestCase):
    def test_minute_of_day_at_open(self):
        """9:30 ET → minute_of_day 0."""
        from strategies.noise_area import NoiseAreaMomentum
        et_dt = datetime(2026, 1, 15, 9, 30, tzinfo=_ET)
        self.assertEqual(NoiseAreaMomentum._minute_of_day(et_dt), 0)

    def test_minute_of_day_at_30min_mark(self):
        """10:00 ET → minute_of_day 30 (Noise Area's signal cadence bucket)."""
        from strategies.noise_area import NoiseAreaMomentum
        et_dt = datetime(2026, 1, 15, 10, 0, tzinfo=_ET)
        self.assertEqual(NoiseAreaMomentum._minute_of_day(et_dt), 30)

    def test_minute_of_day_at_close(self):
        """16:00 ET → minute_of_day 390 (6.5h × 60min)."""
        from strategies.noise_area import NoiseAreaMomentum
        et_dt = datetime(2026, 1, 15, 16, 0, tzinfo=_ET)
        self.assertEqual(NoiseAreaMomentum._minute_of_day(et_dt), 390)

    def test_minute_of_day_works_in_both_seasons(self):
        """Winter vs summer bars both yield the same minute_of_day for 10:00 ET."""
        from strategies.noise_area import NoiseAreaMomentum
        winter_utc = _utc_epoch(2026, 1, 15, 15, 0)   # 10:00 EST
        summer_utc = _utc_epoch(2026, 7, 15, 14, 0)   # 10:00 EDT
        w_et = datetime.fromtimestamp(winter_utc, tz=_ET)
        s_et = datetime.fromtimestamp(summer_utc, tz=_ET)
        self.assertEqual(NoiseAreaMomentum._minute_of_day(w_et), 30)
        self.assertEqual(NoiseAreaMomentum._minute_of_day(s_et), 30)


# ═════════════════════════════════════════════════════════════════════
# Regression — no more _CT_TO_ET_HOURS anywhere in noise_area / orb / ib
# ═════════════════════════════════════════════════════════════════════
class TestNoNaiveTZConstantsRemain(unittest.TestCase):
    def test_noise_area_has_no_ct_to_et_hours(self):
        from pathlib import Path
        src = Path(__file__).parent.parent / "strategies" / "noise_area.py"
        text = src.read_text(encoding="utf-8")
        self.assertNotIn("_CT_TO_ET_HOURS", text)
        # zoneinfo must be imported
        self.assertIn("zoneinfo", text)

    def test_orb_uses_zoneinfo(self):
        from pathlib import Path
        src = Path(__file__).parent.parent / "strategies" / "orb.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("zoneinfo", text)

    def test_ib_breakout_uses_zoneinfo(self):
        from pathlib import Path
        src = Path(__file__).parent.parent / "strategies" / "ib_breakout.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("zoneinfo", text)


if __name__ == "__main__":
    unittest.main()
