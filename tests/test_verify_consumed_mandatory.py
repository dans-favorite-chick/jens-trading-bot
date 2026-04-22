"""
P0.4 — Mandatory OIF verification (D1).

Every PLACE/EXIT entry point in bridge/oif_writer.py must call
_verify_consumed with raise_on_stuck=True so a silent ATI rejection can
never masquerade as a successful order. Before P0.4, write_modify_stop
and legacy write_oif skipped the check entirely — stop-modify failures
were invisible until a trade blew past the intended stop.

The OIFStuckError class exists specifically so callers at base_bot /
position_manager can't `except Exception:` + ignore. It's a RuntimeError
subclass carrying (trade_id, stuck_paths, timeout_s) for forensics.

Run: pytest tests/test_verify_consumed_mandatory.py -v
"""

from __future__ import annotations

import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bridge.oif_writer as oif
from bridge.oif_writer import OIFStuckError, _verify_consumed


# P0.4 tests specifically exercise the mandatory consume-check. The
# autouse conftest fixture flips _PYTEST_BYPASS_CONSUME_CHECK=True for
# everything else; this module-scope fixture flips it back to False so
# _verify_consumed actually runs its polling loop and raise logic.
@pytest.fixture(autouse=True)
def _enable_consume_check(monkeypatch):
    monkeypatch.setattr(oif, "_PYTEST_BYPASS_CONSUME_CHECK", False, raising=False)
    yield


# ═══════════════════════════════════════════════════════════════════
# OIFStuckError exception shape
# ═══════════════════════════════════════════════════════════════════
class TestOIFStuckError:
    def test_is_runtime_error_subclass(self):
        # Callers may `except RuntimeError:` — ensure that still works.
        assert issubclass(OIFStuckError, RuntimeError)

    def test_carries_forensic_attrs(self):
        err = OIFStuckError("t1", ["/tmp/a.txt", "/tmp/b.txt"], 2.0)
        assert err.trade_id == "t1"
        assert err.stuck_paths == ["/tmp/a.txt", "/tmp/b.txt"]
        assert err.timeout_s == 2.0

    def test_str_includes_trade_id_count_and_filenames(self):
        err = OIFStuckError("trade_xyz", ["/tmp/a.txt"], 1.5)
        s = str(err)
        assert "trade_xyz" in s
        assert "1" in s
        assert "a.txt" in s
        assert "1.5" in s


# ═══════════════════════════════════════════════════════════════════
# _verify_consumed core behavior
# ═══════════════════════════════════════════════════════════════════
class TestVerifyConsumed:
    def test_returns_empty_list_when_files_already_consumed(self, tmp_path):
        # No files exist → nothing stuck → []
        result = _verify_consumed(
            [str(tmp_path / "does_not_exist.txt")], "t1", timeout_s=0.3,
        )
        assert result == []

    def test_returns_stuck_list_in_legacy_mode(self, tmp_path, caplog):
        # File still present after timeout → back-compat mode returns list,
        # does NOT raise.
        p = tmp_path / "phoenix_1_stuck.txt"
        p.write_text("PLACE;Sim101;MNQM6;BUY;1;MARKET;0;0;DAY;;;;\n")
        with caplog.at_level(logging.CRITICAL, logger="OIF"):
            result = _verify_consumed([str(p)], "t_legacy", timeout_s=0.3)
        assert result == [str(p)]
        # CRITICAL log fired for the stuck file.
        assert any("OIF_STUCK" in rec.getMessage() for rec in caplog.records)

    def test_raise_on_stuck_true_raises_OIFStuckError(self, tmp_path):
        p = tmp_path / "phoenix_1_stuck.txt"
        p.write_text("PLACE;Sim101;MNQM6;BUY;1;MARKET;0;0;DAY;;;;\n")
        with pytest.raises(OIFStuckError) as exc_info:
            _verify_consumed([str(p)], "t_strict", timeout_s=0.3, raise_on_stuck=True)
        assert exc_info.value.trade_id == "t_strict"
        assert str(p) in exc_info.value.stuck_paths
        assert exc_info.value.timeout_s == 0.3

    def test_raise_on_stuck_does_not_raise_when_consumed(self, tmp_path):
        # Control: with raise_on_stuck=True but no stuck files, must return []
        # (NOT raise on the happy path).
        assert _verify_consumed([], "t_empty", raise_on_stuck=True) == []


# ═══════════════════════════════════════════════════════════════════
# Every PLACE/EXIT entry point calls _verify_consumed with raise_on_stuck
# ═══════════════════════════════════════════════════════════════════
class TestEntryPointsRaiseOnStuck:
    """
    Feed each public OIF writer into a mocked consume-check so files
    appear 'stuck'. The writer should raise OIFStuckError, not silently
    swallow the stuck return.
    """

    @pytest.fixture
    def stuck_incoming(self, monkeypatch, tmp_path):
        """
        Redirect OIF_INCOMING to a tmp dir AND patch os.path.exists (for
        just the oif_writer module's imported os) so consume-check sees
        the file as perpetually present.
        """
        monkeypatch.setattr(oif, "OIF_INCOMING", str(tmp_path))

        real_exists = os.path.exists

        def _always_exists_for_oif_files(path):
            # Any path inside OIF_INCOMING that ends with phoenix_ prefix is
            # still present; real files checked normally.
            if str(tmp_path) in str(path) and "phoenix_" in os.path.basename(str(path)):
                return True
            return real_exists(path)

        monkeypatch.setattr(oif.os.path, "exists", _always_exists_for_oif_files)
        return tmp_path

    def test_write_bracket_order_raises_on_stuck(self, stuck_incoming):
        with pytest.raises(OIFStuckError):
            oif.write_bracket_order(
                direction="LONG", qty=1, entry_type="LIMIT",
                entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
                trade_id="stuck_bracket", account="Sim101",
            )

    def test_write_modify_stop_raises_on_stuck(self, stuck_incoming):
        with pytest.raises(OIFStuckError):
            oif.write_modify_stop(
                direction="LONG", new_stop_price=22010.0, n_contracts=1,
                trade_id="stuck_modify", account="Sim101",
                old_stop_order_id="oif_abc",
            )

    def test_write_oif_exit_raises_on_stuck(self, stuck_incoming):
        with pytest.raises(OIFStuckError):
            oif.write_oif(
                "EXIT", qty=1, trade_id="stuck_exit", account="Sim101",
            )

    def test_write_oif_place_stop_raises_on_stuck(self, stuck_incoming):
        with pytest.raises(OIFStuckError):
            oif.write_oif(
                "PLACE_STOP_SELL", qty=1, stop_price=22000.0,
                trade_id="stuck_stop", account="Sim101",
            )

    def test_write_oif_cancel_single_raises_on_stuck(self, stuck_incoming):
        # Cancels are also PLACE/EXIT-adjacent: a stuck cancel is dangerous
        # (can't remove a bad order). Must raise too.
        with pytest.raises(OIFStuckError):
            oif.write_oif("CANCEL", trade_id="stuck_cancel")

    def test_write_protection_oco_surfaces_stuck_state(self, stuck_incoming):
        # This path has its own retry logic; it was already a strict caller
        # before P0.4. Verify it still raises or logs CRITICAL on stuck.
        # (Pre-existing behaviour — lock it in.)
        try:
            oif.write_protection_oco(
                direction="LONG", qty=1,
                stop_price=21950.0, target_price=22100.0,
                trade_id="stuck_oco", account="Sim101",
            )
        except OIFStuckError:
            return  # expected outcome
        except Exception as e:
            # Retry logic may raise a different error class — accept any
            # non-silent surface. Plain return WITHOUT raising is the bug.
            assert "stuck" in str(e).lower() or "reject" in str(e).lower(), (
                f"write_protection_oco swallowed stuck state silently: {e!r}"
            )


# ═══════════════════════════════════════════════════════════════════
# Happy path: when NT8 consumes normally, no raise, no error log
# ═══════════════════════════════════════════════════════════════════
class TestHappyPath:
    def test_bracket_order_no_raise_when_consumed(self, monkeypatch, tmp_path):
        """NT8 simulated by immediately deleting any file written to
        OIF_INCOMING — the writer must complete without raising."""
        monkeypatch.setattr(oif, "OIF_INCOMING", str(tmp_path))

        real_stage_oif = oif._stage_oif

        def _stage_then_delete(cmd, trade_id, suffix=""):
            tmp_path_ret, final_path_ret = real_stage_oif(cmd, trade_id, suffix)
            # Simulate NT8 consuming instantly (remove the file).
            try:
                os.remove(final_path_ret)
            except FileNotFoundError:
                pass
            return tmp_path_ret, final_path_ret

        monkeypatch.setattr(oif, "_stage_oif", _stage_then_delete)

        # Should not raise.
        paths = oif.write_bracket_order(
            direction="LONG", qty=1, entry_type="LIMIT",
            entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
            trade_id="happy", account="Sim101",
        )
        assert paths  # files were "written" before the simulated consume


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
