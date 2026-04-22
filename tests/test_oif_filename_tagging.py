"""
P0.2 — OIF filename author-tagging (D8).

Every OIF filename emitted by Phoenix must start with `phoenix_<pid>_` so
the NT8-side PhoenixOIFGuard AddOn can quarantine anything that doesn't.
Any rogue process (another bot, a manual script, a misconfigured tool)
writing into incoming/ without the prefix will be diverted to quarantine/
before NT8's ATI parser sees it.

Background: on 2026-04-22 pytest leaked literal stop prices (100.00, then
21000.00) into real OIFs that NT8 placed on Jennifer's live chart. B81
isolated OIF_INCOMING in the test conftest — but that only stops pytest.
P0.2 closes the remaining surface area (any other process that could
write into the real incoming folder).

Run: pytest tests/test_oif_filename_tagging.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bridge.oif_writer as oif


_PID = os.getpid()
_PREFIX = f"phoenix_{_PID}_"


@pytest.fixture
def isolated_incoming(monkeypatch, tmp_path):
    """Redirect OIF_INCOMING to a temp dir so writes never hit real NT8."""
    monkeypatch.setattr(oif, "OIF_INCOMING", str(tmp_path))
    return tmp_path


def _files(incoming):
    """All .txt files in the incoming dir (what NT8 would see)."""
    return sorted(p.name for p in incoming.iterdir() if p.suffix == ".txt")


# ═══════════════════════════════════════════════════════════════════
# Every OIF entry point emits files with the phoenix_<pid>_ prefix
# ═══════════════════════════════════════════════════════════════════
class TestFilenamePrefix:
    def test_write_oif_enter_long_bracket_all_three_files_tagged(self, isolated_incoming):
        paths = oif.write_oif(
            "ENTER_LONG", qty=1,
            stop_price=22000.0, target_price=22100.0,
            trade_id="t1", order_type="LIMIT", limit_price=22050.0,
            account="Sim101",
        )
        assert paths, "bracket order should write >=2 files"
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX), (
                f"Bracket leg filename missing phoenix_<pid>_ prefix: {p}"
            )

    def test_write_oif_exit_is_tagged(self, isolated_incoming):
        paths = oif.write_oif("EXIT", qty=1, trade_id="exit1", account="Sim101")
        assert paths
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)

    def test_write_oif_partial_exit_long_is_tagged(self, isolated_incoming):
        paths = oif.write_oif(
            "PARTIAL_EXIT_LONG", qty=1, trade_id="pex", account="Sim101",
        )
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)

    def test_write_oif_place_stop_sell_is_tagged(self, isolated_incoming):
        paths = oif.write_oif(
            "PLACE_STOP_SELL", qty=1, stop_price=22000.0,
            trade_id="pss", account="Sim101",
        )
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)

    def test_write_oif_cancel_all_is_tagged(self, isolated_incoming):
        paths = oif.write_oif("CANCEL_ALL", qty=0, trade_id="cancel")
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)

    def test_write_oif_cancel_single_is_tagged(self, isolated_incoming):
        paths = oif.write_oif("CANCEL", trade_id="cancel_me")
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)

    def test_write_bracket_order_all_legs_tagged(self, isolated_incoming):
        paths = oif.write_bracket_order(
            direction="LONG", qty=1, entry_type="LIMIT",
            entry_price=22000.0, stop_price=21950.0, target_price=22100.0,
            trade_id="bracket1", account="Sim101",
        )
        assert len(paths) == 3, "bracket = entry + stop + target"
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)

    def test_write_partial_exit_helper_is_tagged(self, isolated_incoming):
        paths = oif.write_partial_exit(
            "LONG", n_contracts=1, trade_id="partial", account="Sim101",
        )
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)

    def test_write_be_stop_helper_is_tagged(self, isolated_incoming):
        paths = oif.write_be_stop(
            "LONG", stop_price=22010.0, n_contracts=1,
            trade_id="be", account="Sim101",
        )
        for p in paths:
            assert os.path.basename(p).startswith(_PREFIX)


# ═══════════════════════════════════════════════════════════════════
# Rogue (un-tagged) filenames are exactly what OIFGuard must catch
# ═══════════════════════════════════════════════════════════════════
class TestRogueFilenameShape:
    """The AddOn's quarantine rule is: filename does NOT start with
    `phoenix_<pid>_` for ANY integer pid. Verify that a bare `oif_*.txt`
    or any external-tool filename would fail that check."""

    def test_bare_oif_filename_is_rogue(self):
        # The pre-P0.2 filename shape — must now be flagged as rogue
        # because it has no phoenix_<pid>_ author tag.
        name = "oif12345_trade_abc_entry.txt"
        assert not _looks_phoenix_tagged(name)

    def test_external_tool_filename_is_rogue(self):
        name = "manual_test_order.txt"
        assert not _looks_phoenix_tagged(name)

    def test_phoenix_tagged_filename_is_accepted(self):
        name = f"phoenix_{_PID}_oif12345_trade_abc.txt"
        assert _looks_phoenix_tagged(name)

    def test_phoenix_prefix_with_any_pid_is_accepted(self):
        # The guard AddOn trusts ANY phoenix_<int>_ prefix, not just
        # this test process's pid — multiple bot processes can write.
        name = "phoenix_99999_oif1_trade_x_entry.txt"
        assert _looks_phoenix_tagged(name)

    def test_prefix_without_pid_is_rogue(self):
        # Just "phoenix_" with no pid doesn't prove Phoenix origin —
        # reject. Matches PhoenixOIFGuard.cs regex logic.
        name = "phoenix_oif1_trade.txt"
        assert not _looks_phoenix_tagged(name)


def _looks_phoenix_tagged(name: str) -> bool:
    """
    Python-side mirror of the C# PhoenixOIFGuard regex: the filename
    must start with literal 'phoenix_', then a run of digits, then '_'.
    Used here only to lock the naming convention into a spec tests can
    assert against — the real check runs inside the NT8 AddOn.
    """
    import re
    return bool(re.match(r"^phoenix_\d+_", name))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
