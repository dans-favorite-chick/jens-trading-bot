"""
Phase 4C Commit 1 — resolver-level tests for config/account_routing.py
and the oif_writer account-required guard.

Full signal→OIF integration tests are added in Commit 2 after the
bridge handler and base_bot callers are updated.

Run: pytest tests/test_account_routing.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.account_routing import (
    STRATEGY_ACCOUNT_MAP,
    get_account_for_signal,
    validate_account_map,
)


# ═══════════════════════════════════════════════════════════════════
# opening_session sub-strategy routing (7)
# ═══════════════════════════════════════════════════════════════════
class TestOpeningSessionRouting:
    # Account names are BYTE-EXACT NT8 display-name literals (incl.
    # spaces and mixed case). See config/account_routing.py commit
    # f5ee73f for the authoritative mapping.
    def test_open_drive_routes_to_simopendrive(self):
        assert get_account_for_signal("opening_session", "open_drive") == "SimOpenDrive"

    def test_open_test_drive_routes_to_own_account(self):
        assert get_account_for_signal("opening_session", "open_test_drive") == "SimOpen Test Drive"

    def test_orb_routes_to_simorb(self):
        assert get_account_for_signal("opening_session", "orb") == "SimORB"

    def test_premarket_breakout_routes_to_correct_account(self):
        assert get_account_for_signal("opening_session", "premarket_breakout") == "SimPremarket Breakout"

    def test_open_auction_in_routes_to_correct_account(self):
        assert get_account_for_signal("opening_session", "open_auction_in") == "SimOpen Auction In Range"

    def test_open_auction_out_routes_to_correct_account(self):
        assert get_account_for_signal("opening_session", "open_auction_out") == "SimOpen Auction Out of Range"

    @pytest.mark.parametrize("sub,expected", [
        ("open_drive",           "SimOpenDrive"),
        ("open_test_drive",      "SimOpen Test Drive"),
        ("open_auction_in",      "SimOpen Auction In Range"),
        ("open_auction_out",     "SimOpen Auction Out of Range"),
        ("premarket_breakout",   "SimPremarket Breakout"),
        ("orb",                  "SimORB"),
    ])
    def test_each_opening_sub_strategy_has_account_assigned(self, sub, expected):
        assert get_account_for_signal("opening_session", sub) == expected


# ═══════════════════════════════════════════════════════════════════
# Flat (single-account) strategy routing (3)
# ═══════════════════════════════════════════════════════════════════
class TestFlatStrategyRouting:
    # Byte-exact NT8 display names (f5ee73f).
    def test_bias_momentum_routes_to_correct_account(self):
        assert get_account_for_signal("bias_momentum") == "SimBias Momentum"

    def test_spring_setup_routes_to_correct_account(self):
        assert get_account_for_signal("spring_setup") == "SimSpring Setup"

    def test_vwap_pullback_and_band_pullback_route_to_distinct_accounts(self):
        # Post-f5ee73f these route to DIFFERENT accounts (not shared).
        assert get_account_for_signal("vwap_pullback") == "SimVWapp Pullback"
        assert get_account_for_signal("vwap_band_pullback") == "SimVwap Band Pullback"

    def test_dom_pullback_routes_to_correct_account(self):
        assert get_account_for_signal("dom_pullback") == "SimDom Pull Back"

    def test_ib_breakout_routes_to_correct_account(self):
        assert get_account_for_signal("ib_breakout") == "SimIB Breakout"

    def test_compression_breakout_splits_by_timeframe(self):
        assert get_account_for_signal("compression_breakout_15m") == "SimCompression Breakout"
        assert get_account_for_signal("compression_breakout_30m") == "SimCompression Break out 30 MIN"

    def test_noise_area_routes_to_correct_account(self):
        assert get_account_for_signal("noise_area") == "SimNoise Area"

    def test_top_level_orb_routes_to_standalone_account(self):
        # Top-level `orb` is distinct from opening_session.orb.
        assert get_account_for_signal("orb") == "SimStand alone ORB"


# ═══════════════════════════════════════════════════════════════════
# Fallback + edge cases (4)
# ═══════════════════════════════════════════════════════════════════
class TestFallbackAndEdges:
    def test_unknown_strategy_falls_back_to_sim101(self):
        assert get_account_for_signal("not_a_strategy") == "Sim101"

    def test_opening_session_without_sub_strategy_falls_back_to_sim101(self):
        # Nested strategies require sub_strategy — missing falls back.
        assert get_account_for_signal("opening_session") == "Sim101"
        assert get_account_for_signal("opening_session", None) == "Sim101"

    def test_opening_session_with_unknown_sub_strategy_falls_back(self):
        assert get_account_for_signal("opening_session", "made_up_sub") == "Sim101"

    def test_validate_account_map_returns_expected_count(self):
        # Post-f5ee73f (byte-exact + compression split + top-level orb):
        #
        # opening_session (6 subs, all distinct accounts):
        #   SimOpenDrive, SimOpen Test Drive, SimOpen Auction In Range,
        #   SimOpen Auction Out of Range, SimPremarket Breakout, SimORB
        # Flat strategies (10, all distinct):
        #   SimBias Momentum, SimSpring Setup, SimVWapp Pullback,
        #   SimVwap Band Pullback, SimDom Pull Back, SimIB Breakout,
        #   SimCompression Breakout, SimCompression Break out 30 MIN,
        #   SimNoise Area, SimStand alone ORB
        # + Sim101 default = 17 unique NT8 accounts.
        accounts = validate_account_map()
        assert len(accounts) == 17
        # Spot-check a few known members.
        assert "Sim101" in accounts
        assert "SimOpenDrive" in accounts
        assert "SimORB" in accounts
        assert "SimStand alone ORB" in accounts
        assert "SimCompression Break out 30 MIN" in accounts


# ═══════════════════════════════════════════════════════════════════
# oif_writer account-required guard (plumbing in place, raises on None)
# ═══════════════════════════════════════════════════════════════════
class TestOIFWriterAccountGuard:
    def test_build_entry_line_raises_without_account(self):
        from bridge.oif_writer import _build_entry_line
        with pytest.raises(ValueError, match="account is required"):
            _build_entry_line("BUY", 1, "MARKET", 0.0, 0.0)

    def test_build_stop_line_raises_without_account(self):
        from bridge.oif_writer import _build_stop_line
        with pytest.raises(ValueError, match="account is required"):
            _build_stop_line("SELL", 1, 22000.0)

    def test_build_target_line_raises_without_account(self):
        from bridge.oif_writer import _build_target_line
        with pytest.raises(ValueError, match="account is required"):
            _build_target_line("SELL", 1, 22100.0)

    def test_build_entry_line_with_account_lands_at_position_2(self):
        from bridge.oif_writer import _build_entry_line
        line = _build_entry_line("BUY", 1, "MARKET", 0.0, 0.0, account="SimOpenDrive")
        fields = line.split(";")
        assert fields[0] == "PLACE"
        assert fields[1] == "SimOpenDrive"

    def test_build_stop_line_with_account_lands_at_position_2(self):
        from bridge.oif_writer import _build_stop_line
        line = _build_stop_line("SELL", 1, 22000.0, account="SimORB")
        fields = line.split(";")
        assert fields[1] == "SimORB"

    def test_write_oif_place_paths_raise_without_account(self):
        # ENTER_LONG, EXIT, PARTIAL_EXIT, PLACE_STOP — every PLACE/EXIT action.
        from bridge.oif_writer import write_oif
        for action in ("ENTER_LONG", "ENTER_SHORT", "EXIT",
                       "PARTIAL_EXIT_LONG", "PARTIAL_EXIT_SHORT",
                       "PLACE_STOP_SELL", "PLACE_STOP_BUY"):
            with pytest.raises(ValueError, match="account is required"):
                write_oif(action, qty=1, stop_price=22000.0, trade_id="test")

    def test_write_oif_cancel_single_does_not_require_account(self):
        # Single-order CANCEL is scoped by order_id, not by account.
        from bridge.oif_writer import write_oif
        import bridge.oif_writer as oif
        import tempfile, os as _os
        tmpdir = tempfile.mkdtemp()
        _orig = oif.OIF_INCOMING
        oif.OIF_INCOMING = tmpdir
        try:
            paths = write_oif("CANCEL", trade_id="cancel_me")
            assert len(paths) == 1
        finally:
            oif.OIF_INCOMING = _orig
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# Map structure invariants
# ═══════════════════════════════════════════════════════════════════
class TestMapInvariants:
    def test_default_is_sim101(self):
        assert STRATEGY_ACCOUNT_MAP["_default"] == "Sim101"

    def test_opening_session_has_all_six_sub_strategies(self):
        nested = STRATEGY_ACCOUNT_MAP["opening_session"]
        assert set(nested.keys()) == {
            "open_drive", "open_test_drive",
            "open_auction_in", "open_auction_out",
            "premarket_breakout", "orb",
        }

    def test_every_mapped_value_is_non_empty_string(self):
        for key, value in STRATEGY_ACCOUNT_MAP.items():
            if isinstance(value, str):
                assert value and value.strip(), f"{key} has empty account"
            elif isinstance(value, dict):
                for sub, acct in value.items():
                    assert acct and acct.strip(), f"{key}/{sub} has empty account"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
