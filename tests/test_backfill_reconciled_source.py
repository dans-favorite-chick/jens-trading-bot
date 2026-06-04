"""Regression tests for REDTEAM-2-FIX-SCRIPT (remediation 2026-06-04
round 2) — the canonical-reader + topology-aware migration script.

The script in `tools/backfill_reconciled_source.py` walks the
deduplicated trade_memory view via
`core.trade_memory.load_all_trades()`, plans a topology-aware label
update for every orphan-adoption row, and applies the changes
through `TradeMemory.update_trade()` so JSON + SQLite shadow stay
in lockstep.

Each test runs against a synthetic logs/ dir under tmp_path so the
real trade memory and the running bots are never touched.

Operator-specified case (master prompt 3c-s.6):
    5 RECONCILED + 2 already-migrated + 1 non-orphan
      → dry-run flags exactly the 5
      → --apply migrates them
      → re-run is no-op
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# Resolve the module by file path so we can pass the synthetic logs-dir
# via the public main() argv interface (CLI parity).
TOOL = Path(__file__).resolve().parent.parent / "tools" / "backfill_reconciled_source.py"


def _import_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("backfill_reconciled_source", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── Synthetic data builders ───────────────────────────────────────────


def _trade(
    *,
    trade_id: str,
    bot_id: str = "prod",
    strategy: str,
    account: str,
    source: str | None = None,
    reconciled_from_orphan: bool | None = None,
    strategy_original_attribution: str | None = None,
    entry_time: float = 1_700_000_000.0,
    exit_time: float = 1_700_000_300.0,
    entry_price: float = 30_000.0,
    exit_price: float = 30_020.0,
    pnl_dollars: float = 18.18,
    result: str = "WIN",
    direction: str = "LONG",
) -> dict:
    t: dict = {
        "trade_id": trade_id,
        "bot_id": bot_id,
        "strategy": strategy,
        "account": account,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_dollars": pnl_dollars,
        "pnl_dollars_net": pnl_dollars,
        "result": result,
        "direction": direction,
        "contracts": 1,
        "stop_price": 29_980.0,
        "target_price": 30_040.0,
    }
    if source is not None:
        t["source"] = source
    if reconciled_from_orphan is not None:
        t["reconciled_from_orphan"] = reconciled_from_orphan
    if strategy_original_attribution is not None:
        t["strategy_original_attribution"] = strategy_original_attribution
    return t


def _make_synthetic_logs(tmp_path: Path) -> Path:
    """Build a synthetic logs/ dir with the operator-specified mix:

      - 5 RECONCILED rows on Sim101 (multi-strategy account), labeled
        with the pre-fix `big_move_signal` strategy and no source field.
      - 2 RECONCILED rows already migrated to the canonical state
        (source='manual_reconciled', strategy matches topology rule).
        One on Sim101 with `_reconciled_Sim101`; one on SimBias Momentum
        with `bias_momentum` (single-strategy account → real name per
        topology rule).
      - 1 non-orphan row: an ordinary bot trade with a normal trade_id,
        used to confirm the script does not touch non-matching rows.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    # Prod has 5 unmigrated + 1 already-migrated + 1 non-orphan = 7 rows.
    prod_trades = [
        _trade(
            trade_id=f"RECONCILED_Sim101_unmig_{i}",
            bot_id="prod",
            strategy="big_move_signal",
            account="Sim101",
            pnl_dollars=10.0 + i,
        )
        for i in range(5)
    ] + [
        # already-migrated (Sim101 multi-strategy → pseudo-label).
        _trade(
            trade_id="RECONCILED_Sim101_already_migrated_a",
            bot_id="prod",
            strategy="_reconciled_Sim101",
            account="Sim101",
            source="manual_reconciled",
            reconciled_from_orphan=True,
            strategy_original_attribution="big_move_signal",
        ),
        # non-orphan: real bot trade.
        _trade(
            trade_id="trade_realbot_xyz",
            bot_id="prod",
            strategy="bias_momentum",
            account="SimBias Momentum",
        ),
    ]
    (logs_dir / "trade_memory_prod.json").write_text(
        json.dumps(prod_trades), encoding="utf-8"
    )

    # Sim has 1 already-migrated single-strategy row (SimBias Momentum
    # → bias_momentum per topology rule).
    sim_trades = [
        _trade(
            trade_id="RECONCILED_SimBias Momentum_already_migrated_b",
            bot_id="sim",
            strategy="bias_momentum",
            account="SimBias Momentum",
            source="manual_reconciled",
            reconciled_from_orphan=True,
            strategy_original_attribution="bias_momentum",
        ),
    ]
    (logs_dir / "trade_memory_sim.json").write_text(
        json.dumps(sim_trades), encoding="utf-8"
    )

    # NO legacy file — keep the test scoped to per-bot.
    return logs_dir


# ── End-to-end dry-run / apply / re-run cycle ─────────────────────────


def test_dryrun_flags_exactly_the_unmigrated_rows(tmp_path):
    """Per master prompt 3c-s.6: 5 RECONCILED + 2 already + 1 non-orphan
    → dry-run plans exactly 5 migrations."""
    logs_dir = _make_synthetic_logs(tmp_path)
    out_path = tmp_path / "dryrun.md"
    tool = _import_tool()
    rc = tool.main([
        "--logs-dir", str(logs_dir),
        "--out", str(out_path),
    ])
    assert rc == 0
    text = out_path.read_text(encoding="utf-8")
    # The headline summary line.
    assert "Rows to migrate (`--apply` would change): **5**" in text
    # 2 already-canonical rows skipped idempotently.
    assert "Rows already canonical (skip): **2**" in text


def test_apply_migrates_only_the_unmigrated_rows(tmp_path):
    """`--apply` writes the canonical fields to the 5 unmigrated rows,
    leaves the 2 already-migrated rows untouched, and ignores the
    non-orphan bot trade.
    """
    logs_dir = _make_synthetic_logs(tmp_path)

    # CRITICAL: the canonical TradeMemory.update_trade resolves its
    # per-bot file via CWD when no override is given. Our --logs-dir
    # plumbing in _apply_one explicitly constructs `filepath=` when
    # logs_dir != DEFAULT_LOGS_DIR, so no CWD juggling is required.
    out_path = tmp_path / "applied.md"
    tool = _import_tool()
    rc = tool.main([
        "--apply",
        "--logs-dir", str(logs_dir),
        "--out", str(out_path),
    ])
    assert rc == 0

    # Read the JSON files back directly to confirm the on-disk state.
    prod = json.loads(
        (logs_dir / "trade_memory_prod.json").read_text(encoding="utf-8")
    )
    sim = json.loads(
        (logs_dir / "trade_memory_sim.json").read_text(encoding="utf-8")
    )
    prod_by_id = {t["trade_id"]: t for t in prod}
    sim_by_id = {t["trade_id"]: t for t in sim}

    # All 5 unmigrated rows now carry the canonical fields. They're on
    # Sim101 (multi-strategy) so target strategy is the pseudo-label.
    for i in range(5):
        t = prod_by_id[f"RECONCILED_Sim101_unmig_{i}"]
        assert t["source"] == "manual_reconciled", (
            f"unmig_{i} should now carry source='manual_reconciled'"
        )
        assert t["reconciled_from_orphan"] is True
        assert t["strategy"] == "_reconciled_Sim101"
        # Original attribution is the pre-fix mislabel (big_move_signal),
        # preserved for forensic audit.
        assert t["strategy_original_attribution"] == "big_move_signal"

    # Already-migrated Sim101 row unchanged in source/flag/strategy.
    a = prod_by_id["RECONCILED_Sim101_already_migrated_a"]
    assert a["source"] == "manual_reconciled"
    assert a["strategy"] == "_reconciled_Sim101"
    assert a["strategy_original_attribution"] == "big_move_signal"

    # Already-migrated SimBias Momentum row unchanged.
    b = sim_by_id["RECONCILED_SimBias Momentum_already_migrated_b"]
    assert b["source"] == "manual_reconciled"
    assert b["strategy"] == "bias_momentum"

    # Non-orphan row untouched — no source field added.
    real = prod_by_id["trade_realbot_xyz"]
    assert "source" not in real, (
        "Non-matching trade_id should NOT be modified by the migration"
    )
    assert real["strategy"] == "bias_momentum"  # unchanged


def test_rerun_after_apply_is_a_noop(tmp_path):
    """Strict idempotency: after `--apply`, the next dry-run reports
    0 rows to migrate."""
    logs_dir = _make_synthetic_logs(tmp_path)
    tool = _import_tool()
    # First apply.
    rc1 = tool.main([
        "--apply",
        "--logs-dir", str(logs_dir),
        "--out", str(tmp_path / "applied1.md"),
    ])
    assert rc1 == 0
    # Second dry-run.
    rc2 = tool.main([
        "--logs-dir", str(logs_dir),
        "--out", str(tmp_path / "dryrun2.md"),
    ])
    assert rc2 == 0
    text = (tmp_path / "dryrun2.md").read_text(encoding="utf-8")
    assert "Rows to migrate (`--apply` would change): **0**" in text


def test_topology_relabels_old_pseudo_label_on_single_strategy_account(tmp_path):
    """A row migrated by an EARLIER round (pre-topology-awareness) on a
    single-strategy account carries `_reconciled_<account>` as strategy
    but the topology rule says it should be the real strategy name.
    The script must re-migrate it to canonical state, preserving
    `strategy_original_attribution`.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    old_migrated = _trade(
        trade_id="RECONCILED_SimBias Momentum_old_migrated",
        bot_id="sim",
        strategy="_reconciled_SimBias Momentum",  # pre-topology label
        account="SimBias Momentum",
        source="manual_reconciled",
        reconciled_from_orphan=True,
        strategy_original_attribution="bias_momentum",
    )
    (logs_dir / "trade_memory_sim.json").write_text(
        json.dumps([old_migrated]), encoding="utf-8"
    )
    out_path = tmp_path / "applied.md"
    tool = _import_tool()
    rc = tool.main([
        "--apply",
        "--logs-dir", str(logs_dir),
        "--out", str(out_path),
    ])
    assert rc == 0
    sim = json.loads((logs_dir / "trade_memory_sim.json").read_text(encoding="utf-8"))
    by_id = {r["trade_id"]: r for r in sim}
    t = by_id["RECONCILED_SimBias Momentum_old_migrated"]
    # Topology rule: SimBias Momentum is single-strategy → strategy is
    # the real name.
    assert t["strategy"] == "bias_momentum"
    # Original audit trail preserved.
    assert t["strategy_original_attribution"] == "bias_momentum"
    # Canonical fields still set.
    assert t["source"] == "manual_reconciled"
    assert t["reconciled_from_orphan"] is True


def test_empty_account_is_skipped_with_reason(tmp_path):
    """Defensive: a RECONCILED_ row whose account field is missing or
    empty must NOT be fabricated into a label. The script surfaces
    it via `_skip_reason` and leaves the row untouched on --apply."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    t = _trade(
        trade_id="RECONCILED_BadRow_xxx",
        bot_id="prod",
        strategy="something_old",
        account="",  # bad data
    )
    (logs_dir / "trade_memory_prod.json").write_text(
        json.dumps([t]), encoding="utf-8"
    )
    out_path = tmp_path / "applied.md"
    tool = _import_tool()
    rc = tool.main([
        "--apply",
        "--logs-dir", str(logs_dir),
        "--out", str(out_path),
    ])
    # rc=1 because the bad row counts as a fail (the script's
    # contract: _skip_reason rows are reported but not silently OKd).
    assert rc == 1
    prod = json.loads((logs_dir / "trade_memory_prod.json").read_text(encoding="utf-8"))
    # Row is untouched — no source field added.
    assert "source" not in prod[0]
    assert prod[0]["strategy"] == "something_old"


def test_custom_pattern_matches_only_specified_prefixes(tmp_path):
    """`--patterns` extends the matcher. Default matches RECONCILED_ only;
    a custom regex picks up an alternative format without altering the
    default behavior on RECONCILED_ rows."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    rows = [
        _trade(
            trade_id="ORPHAN_Sim101_zzz",
            bot_id="prod",
            strategy="big_move_signal",
            account="Sim101",
        ),
        _trade(
            trade_id="RECONCILED_Sim101_yyy",
            bot_id="prod",
            strategy="big_move_signal",
            account="Sim101",
        ),
        _trade(
            trade_id="trade_realbot_aaa",
            bot_id="prod",
            strategy="bias_momentum",
            account="SimBias Momentum",
        ),
    ]
    (logs_dir / "trade_memory_prod.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )
    out_path = tmp_path / "dryrun.md"
    tool = _import_tool()
    # Default pattern: matches only RECONCILED_*
    rc = tool.main([
        "--logs-dir", str(logs_dir),
        "--out", str(out_path),
    ])
    assert rc == 0
    text = out_path.read_text(encoding="utf-8")
    assert "Rows to migrate (`--apply` would change): **1**" in text
    # Custom pattern: both ORPHAN_ and RECONCILED_ matched
    rc2 = tool.main([
        "--logs-dir", str(logs_dir),
        "--patterns", "^RECONCILED_,^ORPHAN_",
        "--out", str(out_path),
    ])
    assert rc2 == 0
    text2 = out_path.read_text(encoding="utf-8")
    assert "Rows to migrate (`--apply` would change): **2**" in text2
