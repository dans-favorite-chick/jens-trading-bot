# Sprint M Tier 2 — Scheduled for 2026-05-19 (Tuesday)

**Status**: Scheduling deferred. The Claude session that planned this (2026-05-12 evening) was flagged as a scheduled-task session, and the harness rejects task creation from within such sessions.

**How to register**: ask Claude in a normal (non-scheduled) chat to run:
```
mcp__scheduled-tasks__create_scheduled_task
  taskId: phoenix-sprint-m-tier-2-may-19
  fireAt: 2026-05-19T18:00:00-05:00
  description: Sprint M Tier 2 — DOM event-stream, OFI, iceberg/refill, tape large-trade, VPIN toxicity gate
  prompt: (use the prompt text below verbatim)
```

Or trigger via the `/schedule` slash command using the prompt text below.

---

## Prompt text (use verbatim when registering)

Sprint M Tier 2 implementation — order-flow microstructure upgrades.

### CONTEXT (locked in commits from 2026-05-12)

Tier 1 already shipped (commit `a4ab967`):
- Adaptive imbalance ratio in TickStreamer.cs (per-bar, scaled by bar range)
- Context-alignment IQS bonuses in `footprint_cvd_reversal` (+20 cap):
  - `structural_bias_aligned` (+5)
  - `sweep_aligned` (+5)
  - `multi_tf_cvd_aligned` (+5)
  - `poc_migration_aligned` (+5)
- 26 new tests, 1,681 total passing

### TIER 2 SCOPE (in priority order)

#### 2.1 — DOM event-stream + Order Flow Imbalance (OFI) — 1 day

Currently `TickStreamer.cs` emits DOM totals (bid_stack, ask_stack) on a snapshot basis. To compute OFI per Cont/Kukanov/Stoikov 2014, we need DOM **changes** (additions / cancellations / fills at each level), not totals.

**NT8 side**:
- Subscribe to `OnMarketDepth` events
- For each level update, emit a `dom_event` message with:
  - `type`: `"add" | "cancel" | "fill"`
  - `side`: `"bid" | "ask"`
  - `price`: level price
  - `size`: change in volume at this level
  - `ts`: timestamp

**Python side**:
- `bridge_server.py`: handle `dom_event` messages, forward to bots
- `core/ofi.py`: new module computing OFI from the event stream
- OFI formula: sum of signed-volume changes at top N levels over a rolling window
- Add `ofi_short` (5s window) and `ofi_medium` (30s window) to the market snapshot
- Hook into `footprint_cvd_reversal` as a new IQS bonus (`ofi_aligned`, +5). Either extend the context-bonus cap from 20 → 25, or add a new column.

#### 2.2 — Iceberg / refill detection — 1 day

Identifies hidden institutional orders by tracking when the same price level keeps getting filled even though displayed size is small. Strong signal for true support/resistance.

**Approach**:
- `core/iceberg_detector.py`: rolling track of `(price_level, displayed_size_at_arrival, total_filled, fills_count)`
- Iceberg fires when `fills_count >= 3 AND total_filled > displayed_size * 5`
- Surface as `market["iceberg_levels"]` list of `{price, total_filled, fills_count}`
- In `footprint_cvd_reversal`: bonus when entry price within `level_buffer_ticks` of an iceberg level matching direction

#### 2.3 — Tape large-trade detection — half day

Single large trades (sweeps, institutional aggression) are leading indicators. Already partly present via tape_read_event but not used as IQS input.

**Approach**:
- `core/tape_reader.py`: track tick stream, flag any tick with `size >= N` contracts (N = adaptive based on session-average tick size; suggest 50 for MNQ during RTH)
- Emit `market["large_prints"]` = last 20 large trades with `{ts, price, size, side}`
- In `footprint_cvd_reversal`: bonus when recent large prints align with direction

#### 2.4 — VPIN toxicity gate — 1 day

Easley/Lopez de Prado/O'Hara 2012 toxicity metric. Don't ENTER when informed flow is dominant (high VPIN); market-makers will pull liquidity, fills degrade.

**Approach**:
- `core/vpin.py`: bucketed-volume VPIN with 50 buckets, volume bucket size = avg_daily_volume / 50
- Output VPIN value 0–1; >0.7 = toxic
- In `footprint_cvd_reversal`: if `VPIN > 0.7`, **skip entry regardless of IQS** (hard gate, not bonus)

### PRE-FLIGHT CHECKS (run before starting any implementation)

1. **Tier 1 actually accumulated data**:
   ```
   cd 'C:\Trading Project\phoenix_bot'
   python tools/validation_tracker.py --post-b13-only
   ```
   Look for `footprint_cvd_reversal` in output. If `n=0`, the strategy still hasn't fired since Tier 1 shipped — **STOP and investigate** before building more.

2. **TickStreamer.cs recompiled** (Tier 1.1 NT8 side):
   ```powershell
   (Get-Content 'C:\Trading Project\phoenix_bot\data\volumetric_latest.json' | ConvertFrom-Json).imbalance_ratio
   ```
   Should return a number. If null/missing, the C# recompile didn't happen yet — ask operator before stacking Tier 2 NT8 changes on top.

3. **Volumetric feed healthy**:
   ```powershell
   $age = ((Get-Date) - (Get-Item 'C:\Trading Project\phoenix_bot\data\volumetric_latest.json').LastWriteTime).TotalSeconds
   ```
   <300s during RTH, <600s overnight. If much higher, feed died — fix first.

4. **Git on a clean branch with Tier 1 as base**:
   ```
   git status
   git log --oneline -5
   ```
   Expected: `a4ab967` (Sprint M Tier 1) at or near HEAD.

### DEVELOPMENT DISCIPLINE

- Each Tier 2 item = its own commit. Don't bundle.
- Tests required per item (unit + static checks).
- Full suite stays green (1,681 baseline).
- C# changes require operator recompile — call out explicitly in commit message.
- Do **NOT** raise the 70-IQS entry threshold or change tier classification. New bonuses go IN the existing arithmetic.

### REPORT WHEN DONE

After all 4 items ship + tests green, write at `memory/context/SPRINT_M_TIER_2_COMPLETE.md`:
- Commits shipped
- Test count delta
- Any blockers
- Next-tier candidates

If you can't finish all 4 in one session, stop after the last clean commit and note remaining items in `OPEN_QUESTIONS.md`. Better to ship 2 solid than 4 half-done.
