"""
Phase 4C Integration (G-B37) — end-to-end account routing through OIF writer.

Validates that the account string resolved by config/account_routing.py
survives end-to-end: through oif_writer serialization, on-disk OIF file,
and back via read. Also exercises the _require_account guard and the
Sim101 default-fallback path.

Uses the real STRATEGY_ACCOUNT_MAP (no patching). Only filesystem IO is
redirected — OIF_INCOMING is pointed at a pytest tmp_path so we don't
touch the real NT8 incoming folder.

Run: pytest tests/test_4c_integration.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# B40: force multi-account routing ON so tests validate map logic, not
# the runtime kill-switch. Production default is False until NT8 ATI is
# configured for multi-account execution.
import config.settings as _s
_s.MULTI_ACCOUNT_ROUTING_ENABLED = True

from config.account_routing import (
    STRATEGY_ACCOUNT_MAP,
    get_account_for_signal,
)
from bridge import oif_writer


# ─── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def tmp_oif_dir(tmp_path, monkeypatch):
    """Redirect OIF_INCOMING to a tmp dir so we never touch real NT8."""
    monkeypatch.setattr(oif_writer, "OIF_INCOMING", str(tmp_path))
    return tmp_path


# ─── _require_account guard ─────────────────────────────────────────

class TestRequireAccountGuard:
    def test_guard_rejects_none(self):
        with pytest.raises(ValueError, match="account is required"):
            oif_writer._require_account(None, "test")

    def test_guard_rejects_empty_string(self):
        with pytest.raises(ValueError, match="account is required"):
            oif_writer._require_account("", "test")

    def test_guard_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="account is required"):
            oif_writer._require_account("   ", "test")

    def test_guard_accepts_real_routed_account(self):
        # Real routed account for an unmapped strategy falls back to Sim101,
        # which _require_account must accept as a valid non-empty string.
        acct = get_account_for_signal("totally_unmapped_strategy_xyz")
        assert oif_writer._require_account(acct, "test") == acct

    def test_bracket_order_raises_without_account(self, tmp_oif_dir):
        with pytest.raises(ValueError, match="account is required"):
            oif_writer.write_bracket_order(
                direction="LONG", qty=1, entry_type="MARKET",
                entry_price=18500.0, stop_price=18495.0,
                target_price=18510.0, trade_id="test_noacct",
                account=None,
            )
        # And nothing was written to disk.
        assert list(tmp_oif_dir.iterdir()) == []


# ─── End-to-end: routing → OIF write → read back ────────────────────

class TestRoutingEndToEnd:
    def test_bias_momentum_account_survives_to_disk(self, tmp_oif_dir):
        # Resolve using the REAL map (no patching).
        account = get_account_for_signal("bias_momentum")
        assert account == "SimBias Momentum"  # sanity — the map value

        written = oif_writer.write_bracket_order(
            direction="LONG", qty=1, entry_type="MARKET",
            entry_price=18500.0, stop_price=18495.0,
            target_price=18510.0, trade_id="bm_int_001",
            account=account,
        )
        assert len(written) == 3, "expected entry+stop+target files"

        # Read every committed OIF and assert the byte-exact account string
        # survived serialization — spaces, mixed case, all of it.
        for path in written:
            content = open(path).read()
            assert account in content, (
                f"account '{account}' missing in {path}: {content!r}"
            )

    def test_opening_session_sub_strategy_end_to_end(self, tmp_oif_dir):
        # Nested strategy routing — sub_strategy resolves to its own account.
        account = get_account_for_signal("opening_session", "open_drive")
        assert account == "SimOpenDrive"

        written = oif_writer.write_bracket_order(
            direction="SHORT", qty=1, entry_type="MARKET",
            entry_price=18500.0, stop_price=18505.0,
            target_price=18490.0, trade_id="od_int_002",
            account=account,
        )
        assert len(written) == 3
        for path in written:
            assert account in open(path).read()

    def test_account_string_byte_exact_in_semicolon_format(self, tmp_oif_dir):
        # Accounts with spaces must land between semicolons without escaping
        # or quoting — NT8 parses them as literal fields.
        account = get_account_for_signal("compression_breakout_30m")
        assert account == "SimCompression Break out 30 MIN"

        written = oif_writer.write_bracket_order(
            direction="LONG", qty=1, entry_type="MARKET",
            entry_price=18500.0, stop_price=18495.0,
            target_price=18510.0, trade_id="cb30_int_003",
            account=account,
        )
        # Every OIF line uses semicolon-delimited fields: verify the account
        # appears as a stand-alone field, bracketed by semicolons.
        for path in written:
            line = open(path).read().strip()
            assert f";{account};" in line, (
                f"account not a discrete field in {line!r}"
            )


# ─── Default fallback path (Sim101) ────────────────────────────────

class TestDefaultFallback:
    def test_unmapped_strategy_falls_back_to_sim101(self):
        assert STRATEGY_ACCOUNT_MAP["_default"] == "Sim101"
        assert get_account_for_signal("this_strategy_is_not_in_the_map") == "Sim101"

    def test_nested_strategy_without_sub_falls_back_to_sim101(self):
        # opening_session is nested; without a sub_strategy, fall through.
        assert get_account_for_signal("opening_session") == "Sim101"

    def test_nested_strategy_unknown_sub_falls_back_to_sim101(self):
        assert get_account_for_signal("opening_session", "made_up_sub") == "Sim101"

    def test_sim101_fallback_writes_oif_end_to_end(self, tmp_oif_dir):
        # An unmapped strategy must still produce a valid OIF — the fallback
        # lands on Sim101 and the guard accepts it.
        account = get_account_for_signal("no_such_strategy")
        assert account == "Sim101"

        written = oif_writer.write_bracket_order(
            direction="LONG", qty=1, entry_type="MARKET",
            entry_price=18500.0, stop_price=18495.0,
            target_price=18510.0, trade_id="fallback_int_004",
            account=account,
        )
        assert len(written) == 3
        for path in written:
            assert "Sim101" in open(path).read()
