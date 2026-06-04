# Phoenix Findings Tracker

Living ledger of every audit finding. Update on every fix-it prompt
(Phase 0 verification + final RESOLVED phase). IDs are stable forever
— never reuse, never delete rows, only mark superseded.

Status codes:
- OPEN          — defect confirmed in HEAD, not yet fixed
- IN-PROGRESS   — a fix-it session is actively addressing it
- RESOLVED      — fixed in the commit listed
- STALE         — was OPEN, found ALREADY-FIXED off-prompt; credited commit listed
- SUPERSEDED    — replaced by a newer finding with broader scope

| ID                       | Date       | Source                       | Location                                 | Description                            | Status      | Fix commit  | Notes                                                                                              |
|--------------------------|------------|------------------------------|------------------------------------------|----------------------------------------|-------------|-------------|----------------------------------------------------------------------------------------------------|
| FINDING-2026-06-02-C     | 2026-06-02 | dashboard-rebuild audit      | dashboard/server.py /api/equity-curve    | r_multiple field-order bug             | RESOLVED    | cb04e61     | shipped same session                                                                               |
| FINDING-2026-06-02-D     | 2026-06-02 | dashboard-rebuild audit      | core/position_manager.py:737, :937       | initial_stop_price not serialized      | RESOLVED    | 280f76e     | OPERATOR-APPROVED 2026-06-02 batch (re-run)                                                        |
| FINDING-2026-06-02-A     | 2026-06-02 | dashboard-rebuild audit      | core/trade_memory.py TradeMemory.save()  | non-atomic save → truncation race      | RESOLVED    | 1df1aa2     | atomic write shipped 1df1aa2; root-cause race fixed (reader mitigation 2698323 retained)           |
| FINDING-2026-06-02-E     | 2026-06-02 | dashboard-rebuild audit      | bots/_pending_entry_sweeper.py:132       | ISO exit_time instead of float epoch   | RESOLVED    | 3842e87     | unprotected, normal commit, shipped 2026-06-02; ISO preserved under exit_time_iso                  |
| FINDING-2026-06-02-NOISE | 2026-06-02 | DuckDB schema-split task     | tests/test_adaptive.py:530               | 4 pre-existing ParserException errors  | RESOLVED    | 0a22cd5     | baseline noise floor cleared                                                                       |
| FINDING-2026-06-02-BP1   | 2026-06-02 | 0a22cd5 bug sweep            | core/startup_reconciliation.py:142       | blind parts[2] indexing                | STALE       | ccb29b2     | found already-guarded by Phase 4 sweep 2026-06-02                                                  |
| FINDING-2026-06-02-BP2   | 2026-06-02 | 0a22cd5 bug sweep            | tools/strategy_backtest_es_nq_v2.py:65   | blind parts[2] indexing                | STALE       | b498512     | found already-guarded by Phase 4 sweep 2026-06-02                                                  |
| FINDING-2026-06-02-BP3   | 2026-06-02 | 0a22cd5 bug sweep            | bridge/oif_writer.py:882                 | blind parts[2] indexing in scan        | STALE       | 8f0ffc9     | found already-guarded by Phase 4 sweep 2026-06-02                                                  |
| FINDING-2026-06-03-T1    | 2026-06-03 | D/A/E batch Subagent B       | tools/mark_position_flat.py:190          | ISO exit_time (Finding E sibling)      | RESOLVED    | 726edb8     | tonight's sibling sprint; unprotected                                                              |
| FINDING-2026-06-03-T2    | 2026-06-03 | D/A/E batch Subagent B       | tools/backfill_bot_id.py:80-81           | non-atomic save (Finding A sibling)    | RESOLVED    | 3234c25     | tonight's sibling sprint; unprotected                                                              |
| FINDING-2026-06-03-T3    | 2026-06-03 | D/A/E batch Subagent B       | tools/mark_position_flat.py:220-223      | shared .tmp suffix (concurrency)       | RESOLVED    | 5409f32     | tonight's sibling sprint; unprotected                                                              |
| FINDING-2026-06-03-D-SQL | 2026-06-03 | D/A/E batch Phase 4 chip     | core/trade_memory_db.py                  | initial_stop_price not column-mapped   | RESOLVED    | 4b5afa3     | schema bump 1→2; ALTER migration added                                                             |
