"""
P0.5 — Scale-out race fix (D4 orphan-stop).

Before P0.5, `_scale_out_trade` placed a NEW break-even stop via
`write_be_stop` without cancelling the original OCO stop. Result: two
stops on the same position — the BE stop fires first on adverse move,
closing the position; if price bounces and hits the old stop, NT8
places a REVERSAL fill (orphan phantom SHORT/LONG).

P0.5 routes scale-out through `_move_nt8_stop` → `write_modify_stop`,
which stages a PLACE-new-stop + CANCEL-old-stop sequence atomically.
Critical ordering constraint: PLACE-new commits BEFORE CANCEL-old so we
never have a no-stop window. CANCEL-then-PLACE is FORBIDDEN.

Run: pytest tests/test_scale_out_no_race.py -v
"""

from __future__ import annotations

import os
import sys
import inspect

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bridge.oif_writer as oif


# ═══════════════════════════════════════════════════════════════════
# write_modify_stop commit order: PLACE first, CANCEL second
# ═══════════════════════════════════════════════════════════════════
class TestWriteModifyStopOrder:
    def test_new_stop_is_staged_before_cancel(self, tmp_path):
        """
        After staging, list order is [new_stop, cancel_old]. The commit
        loop walks the list in order so new_stop lands on disk first —
        NT8 sees the new stop before the cancel.
        """
        paths = oif.write_modify_stop(
            direction="LONG", new_stop_price=22010.0, n_contracts=1,
            trade_id="order_test", account="Sim101",
            old_stop_order_id="oif_old_xyz",
        )
        assert len(paths) == 2

        # Filenames carry the suffix we used in _stage_oif — stop_replace
        # must appear in the FIRST committed path, stop_cancel SECOND.
        first = os.path.basename(paths[0])
        second = os.path.basename(paths[1])
        assert "stop_replace" in first, (
            f"P0.5 violated: first committed OIF is '{first}' — must be the "
            f"new-stop PLACE, not the cancel. CANCEL-before-PLACE leaves a "
            f"no-stop window open to adverse price moves."
        )
        assert "stop_cancel" in second, (
            f"P0.5 violated: second committed OIF is '{second}' — must be "
            f"the cancel of the old stop."
        )

    def test_new_stop_file_contains_stopmarket(self, tmp_path):
        """Sanity: the first file is a PLACE STOPMARKET, not a CANCEL."""
        paths = oif.write_modify_stop(
            direction="SHORT", new_stop_price=21990.0, n_contracts=1,
            trade_id="content_test", account="Sim101",
            old_stop_order_id="oif_old_sh",
        )
        assert len(paths) == 2
        first_body = open(paths[0]).read().strip()
        second_body = open(paths[1]).read().strip()
        assert first_body.startswith("PLACE"), (
            f"First file must PLACE new stop: {first_body!r}"
        )
        assert "STOPMARKET" in first_body
        assert second_body.startswith("CANCEL"), (
            f"Second file must CANCEL old stop: {second_body!r}"
        )


# ═══════════════════════════════════════════════════════════════════
# Scale-out uses _move_nt8_stop, never bare write_be_stop
# ═══════════════════════════════════════════════════════════════════
class TestScaleOutRouting:
    """
    The scale-out path in base_bot.py must call _move_nt8_stop (which
    threads through write_modify_stop cancel+replace) rather than
    write_be_stop (which only PLACEs and leaves the old stop dangling).
    """

    def test_scale_out_source_does_not_call_write_be_stop(self):
        """
        Grep the source of _scale_out_trade for write_be_stop CALLS. After
        P0.5 fix it must be absent as a callable. (String appearances in
        comments explaining why we DON'T use it are fine.) A regression
        would reintroduce the orphan-stop bug.
        """
        from bots import base_bot

        src = inspect.getsource(base_bot.BaseBot._scale_out_trade)
        # Strip comment lines so docstrings/explanatory comments that
        # legitimately mention 'write_be_stop' (the bad-pattern note)
        # don't trigger the grep.
        code_only = "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith("#")
        )
        # Check for actual function-call patterns: `write_be_stop(` or
        # `import write_be_stop`. Either indicates the bug is back.
        assert "write_be_stop(" not in code_only, (
            "P0.5 regression: _scale_out_trade is calling write_be_stop again. "
            "This reintroduces the orphan-stop bug — use _move_nt8_stop instead."
        )
        assert "import write_be_stop" not in code_only, (
            "P0.5 regression: _scale_out_trade is importing write_be_stop."
        )

    def test_scale_out_source_calls_move_nt8_stop(self):
        """After fix, _scale_out_trade must use _move_nt8_stop."""
        from bots import base_bot

        src = inspect.getsource(base_bot.BaseBot._scale_out_trade)
        assert "_move_nt8_stop" in src, (
            "_scale_out_trade must call _move_nt8_stop to ensure the BE "
            "move goes through the cancel+replace (P0.5) path."
        )

    def test_move_nt8_stop_uses_write_modify_stop(self):
        """_move_nt8_stop must route through write_modify_stop — NOT
        through write_be_stop or a bare _build_stop_line."""
        from bots import base_bot

        src = inspect.getsource(base_bot._move_nt8_stop)
        assert "write_modify_stop" in src, (
            "_move_nt8_stop must use write_modify_stop to enforce "
            "PLACE-before-CANCEL semantics."
        )


# ═══════════════════════════════════════════════════════════════════
# Forbidden pattern never appears anywhere in the codebase
# ═══════════════════════════════════════════════════════════════════
class TestForbiddenSequenceAbsent:
    """
    The FORBIDDEN sequence is: cancel an existing stop, THEN place a
    replacement. Leaves a no-stop window. No code path in oif_writer.py
    should end up staging in that order.
    """

    def test_write_modify_stop_stages_place_before_cancel_only(self):
        """
        Source-level guard: in write_modify_stop, the _stage_oif calls
        must have 'stop_replace' (PLACE new) appear before 'stop_cancel'
        (CANCEL old) in source order. Flipping them reintroduces the
        no-stop window.
        """
        src = inspect.getsource(oif.write_modify_stop)
        replace_idx = src.find('suffix="stop_replace"')
        cancel_idx = src.find('suffix="stop_cancel"')
        assert replace_idx != -1, "stop_replace stage missing"
        assert cancel_idx != -1, "stop_cancel stage missing"
        assert replace_idx < cancel_idx, (
            "P0.5 violated: in write_modify_stop, 'stop_cancel' is staged "
            "BEFORE 'stop_replace'. That's the FORBIDDEN cancel-first order. "
            "Commit order must be PLACE new stop, then CANCEL old."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
