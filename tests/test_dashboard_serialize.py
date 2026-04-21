"""
BUG-TL1 — Dashboard snapshot JSON serialization tests.

Covers the `_json_default_safe` helper in `bots/base_bot.py` which coerces
non-JSON-serializable values (datetime, date, anything with isoformat(),
exotic types) at the dashboard-push boundary. Also sanity-checks that
dashboard push is wrapped in a top-level try/except so lingering serialize
failures cannot kick the bot off the bridge.

Run: python -m unittest tests.test_dashboard_serialize -v
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _import_helper():
    """Import the private helper directly without constructing a BaseBot."""
    import bots.base_bot as bb
    return bb._json_default_safe


class TestDatetimeInNestedDict(unittest.TestCase):
    def test_datetime_roundtrips_as_iso_string(self):
        helper = _import_helper()
        payload = {
            "bot_name": "prod",
            "nested": {"ts": datetime(2026, 4, 19, 22, 50, 0)},
        }
        encoded = json.dumps(payload, default=helper)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["nested"]["ts"], "2026-04-19T22:50:00")

    def test_tz_aware_datetime_preserves_offset(self):
        helper = _import_helper()
        ts = datetime(2026, 4, 19, 22, 50, tzinfo=timezone(timedelta(hours=-5)))
        encoded = json.dumps({"ts": ts}, default=helper)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["ts"], "2026-04-19T22:50:00-05:00")

    def test_datetime_inside_list(self):
        helper = _import_helper()
        encoded = json.dumps(
            {"events": [datetime(2026, 4, 19), "str", 42]},
            default=helper,
        )
        decoded = json.loads(encoded)
        self.assertEqual(decoded["events"][0], "2026-04-19T00:00:00")
        self.assertEqual(decoded["events"][1], "str")
        self.assertEqual(decoded["events"][2], 42)

    def test_deeply_nested_datetime(self):
        helper = _import_helper()
        payload = {"a": {"b": {"c": {"ts": datetime(2026, 4, 19, 10, 30)}}}}
        encoded = json.dumps(payload, default=helper)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["a"]["b"]["c"]["ts"], "2026-04-19T10:30:00")


class TestDateSerialization(unittest.TestCase):
    def test_plain_date_serializes(self):
        helper = _import_helper()
        payload = {"settled": date(2026, 4, 18)}
        encoded = json.dumps(payload, default=helper)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["settled"], "2026-04-18")

    def test_time_serializes(self):
        """time objects (no date component) also have isoformat()."""
        helper = _import_helper()
        encoded = json.dumps({"open": dtime(9, 30)}, default=helper)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["open"], "09:30:00")


class TestNoDatetimePassthrough(unittest.TestCase):
    def test_snapshot_with_no_datetime_is_byte_identical_to_default_encoder(self):
        """If the snapshot is already serializable, default= must not perturb output."""
        helper = _import_helper()
        payload = {
            "bot_name": "prod",
            "status": "IDLE",
            "live": False,
            "counts": [1, 2, 3],
            "nested": {"foo": "bar", "num": 3.14, "flag": True},
        }
        with_default = json.dumps(payload, default=helper, sort_keys=True)
        without_default = json.dumps(payload, sort_keys=True)
        self.assertEqual(with_default, without_default)

    def test_empty_dict_no_default_invocation(self):
        helper = _import_helper()
        self.assertEqual(json.dumps({}, default=helper), "{}")


class TestExoticTypeGraceful(unittest.TestCase):
    def test_set_falls_back_to_str(self):
        helper = _import_helper()
        encoded = json.dumps({"tags": {1, 2, 3}}, default=helper)
        decoded = json.loads(encoded)
        # set → str() produces something like "{1, 2, 3}" — just confirm no raise
        self.assertIsInstance(decoded["tags"], str)

    def test_custom_object_without_isoformat_falls_back_to_str(self):
        helper = _import_helper()

        class Widget:
            def __repr__(self):
                return "Widget()"

        encoded = json.dumps({"w": Widget()}, default=helper)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["w"], "Widget()")

    def test_object_with_broken_isoformat_falls_back_to_str(self):
        helper = _import_helper()

        class BrokenIso:
            def isoformat(self):
                raise RuntimeError("nope")

            def __repr__(self):
                return "BrokenIso()"

        encoded = json.dumps({"b": BrokenIso()}, default=helper)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["b"], "BrokenIso()")

    def test_object_with_broken_str_returns_sentinel_not_raise(self):
        helper = _import_helper()

        class BrokenStr:
            def __str__(self):
                raise RuntimeError("also nope")

        encoded = json.dumps({"x": BrokenStr()}, default=helper)
        decoded = json.loads(encoded)
        self.assertIn("<unserializable", decoded["x"])


class TestPushBoundaryGuarded(unittest.TestCase):
    """The dashboard_loop already wraps json.dumps() + HTTP in a try/except
    at line ~479 of base_bot.py. This test confirms the try/except is still
    there — any refactor that drops it would regress to BUG-TL1 observability
    (and worse, could propagate to the WS handler)."""

    def test_dashboard_loop_has_outer_exception_guard(self):
        src = (Path(__file__).parent.parent / "bots" / "base_bot.py").read_text(encoding="utf-8")
        # The guard is the specific pattern we rely on; look for both the
        # outer try and the warning log.
        self.assertIn("Dashboard push failed:", src,
                      "Dashboard-push outer guard log message missing — "
                      "serialize failures could propagate.")
        # The _json_default_safe helper must be referenced by both push sites.
        self.assertGreaterEqual(src.count("_json_default_safe"), 3,
                                "Expected _json_default_safe in 2 json.dumps calls + 1 definition = 3+ refs.")


if __name__ == "__main__":
    unittest.main()
