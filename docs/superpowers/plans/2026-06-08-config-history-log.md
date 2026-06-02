# Config History Log — Design (Monday 2026-06-08 Review)

_Design-only artifact. No code changes tonight. Bundles with the
`2026-06-08-validated-strategies-refactor.md` workstream — both
ship together Monday if operator approves._

## 1. Problem

When the reconciliation harness in `tools/reconcile_sim_vs_backtest.py`
replays a sim trade from N days ago, it needs to know **what the
`STRATEGIES` config looked like on the day that trade was recorded.**

Today there is no clean answer. Two ways the historical config drifts
out of reach:

1. **Git-tracked edits.** `config/strategies.py` is committed, so
   `git log -- config/strategies.py` *eventually* gets you there —
   but only after `git show <sha>:config/strategies.py`, parsing the
   module out of that snapshot, and reconstructing the `STRATEGIES`
   dict. Fragile, slow, and breaks if the dataclass schema evolved
   between then and now.
2. **Runtime dashboard slider edits.** The dashboard's "save to
   config" route writes new values to `config/strategies.py` in place.
   The intermediate runtime slider drags between save events are
   never persisted. If a sim trade fired between two slider drags,
   the live config at trade time differs from any committed snapshot.

The 2026-06-02 smoke (`logs/oracle/research/2026-06-02_recon_smoke_results.md`)
showed 54/55 REPLAYED rows outside tolerance, with bar-clock
divergence as the leading hypothesis. The *second* hypothesis we
can't currently rule out: the backtester instantiated `bias_momentum`
with a different config than sim had at trade time. We need a way
to make that question answerable in seconds.

Antigravity's existing recon harness includes a conditional
"legacy override" branch for handling older trades whose schema
predates current `STRATEGIES`. That mechanism is brittle: it's a
hardcoded list of fields to override at specific timestamps, and
it grows monotonically as the config evolves. The history log
proposed here replaces it with a uniform mechanism: ask the log
"what was the config at timestamp T?" and trust the answer.

## 2. Proposal

Append-only JSONL log at `logs/config_history.jsonl`. One row per
config change. Schema:

```json
{
  "ts": "2026-06-02T01:23:45+00:00",
  "source": "startup",
  "trigger": "base_bot.startup",
  "config": { /* full STRATEGIES dict serialized */ },
  "hash": "sha256:8f2a...7c1"
}
```

Field semantics:

- `ts`: ISO-8601 UTC timestamp with offset. Always UTC. Format must
  round-trip through `datetime.fromisoformat`.
- `source`: one of `"startup"`, `"dashboard"`, `"migration"`. Tells
  the consumer whether this row reflects a live runtime edit or a
  process-boundary snapshot.
- `trigger`: free-form string naming the call site that wrote the
  row. Examples: `"base_bot.startup"`, `"dashboard.save_to_config"`,
  `"manual.config_history_seed"`.
- `config`: the full `STRATEGIES` dict serialized to JSON. Dict
  keys sorted alphabetically (so the byte representation is
  deterministic given the same logical content — see §5 stable
  ordering test).
- `hash`: `"sha256:"` + hex digest of the canonical JSON-serialized
  `config` field (with sorted keys, no whitespace). Used for
  deduplication and as the `config_hash` value referenced by the
  VALIDATED_STRATEGIES refactor (§8.1 of that doc).

## 3. Hook points

Only two code sites write to the log.

### 3.1 `bots/base_bot.py` startup

On bot launch, after `STRATEGIES` is loaded but before the first
trade evaluation:

1. Compute `hash` of the current `STRATEGIES` dict.
2. Read the last line of `logs/config_history.jsonl` (cheap: file
   is small, but a tail-only read is fine).
3. If the last line's `hash` matches → skip (config unchanged
   since last startup).
4. Else → append a new row with `source="startup"`,
   `trigger="base_bot.startup"`.

This makes a startup row only when the config has actually changed
since the previous run. Bots that restart frequently don't spam
the log.

### 3.2 `dashboard/server.py` save-to-config route

After the dashboard successfully writes new slider values to
`config/strategies.py`:

1. Reload the module to pick up the new values.
2. Compute `hash` of the post-save `STRATEGIES`.
3. Compare against the last line's `hash` — if identical (e.g., the
   operator clicked save without changing anything), skip.
4. Else append with `source="dashboard"`,
   `trigger="dashboard.save_to_config"`.

The slider-drag itself (the WebSocket-driven runtime parameter
updates that don't write to disk) is **not** logged. See §9 for
why.

## 4. Harness consumption

The recon harness loads the config-as-of(timestamp) by binary search.

```python
def config_as_of(ts: datetime) -> dict:
    """Return the STRATEGIES dict that was live at `ts`. Reads
    logs/config_history.jsonl, returns the `config` field of the
    most recent row with row['ts'] <= ts."""
```

Implementation notes:
- Load the log lazily, cache it for the harness run.
- Rows are guaranteed sorted by `ts` (append-only, monotonic
  writer-side clock — see §9 for the slider-tick concern).
- Binary search by `ts` (`bisect_right` on a list of timestamps,
  return `rows[idx - 1]`).
- If no row precedes `ts`, fall back to the earliest row with a
  log warning. The first time the log is created on a running
  system, in-flight trades from before the log's first row are in
  this regime — the warning surfaces it.

This replaces the conditional legacy override mechanism Antigravity
added to the recon harness. The recon code path becomes:

```python
config = config_as_of(sim_trade.entry_ts)
strategy_cfg = config[strategy_name]
# proceed with backtester replay using strategy_cfg
```

No more per-field legacy mappings. The log is the single source
of historical truth.

## 5. Tests — `tests/test_config_history.py`

1. **Round-trip.** Write a row with a known config dict; read it
   back; assert the recovered `config` equals the original and
   the recorded `hash` matches a hand-computed sha256 of the
   canonical JSON.
2. **Stable ordering / canonical JSON.** Two dicts with the same
   keys/values in different insertion orders produce identical
   `hash` values. Implemented via `json.dumps(config, sort_keys=True,
   separators=(",", ":"))` before hashing.
3. **Deduplication on identical hash.** Write the same config
   twice in a row; assert only one row was appended.
4. **As-of lookup (binary search).** Write rows at t1, t2, t3.
   `config_as_of(t2 + 0.5s)` returns t2's config.
   `config_as_of(t1 - 1s)` returns t1's config with a warning.
   `config_as_of(t3 + 1day)` returns t3's config.
5. **Append-only invariant.** The writer never overwrites or
   truncates. A separate test asserts that after `N` appends the
   file has exactly `N` lines (plus whatever was there before).
6. **Concurrent-writer safety.** Two simulated concurrent writers
   appending different configs don't corrupt the file. Use file
   locking via `portalocker` (already a transitive dep via the
   data pipeline) or OS-level `O_APPEND` semantics — pick one in
   the implementation PR, but the test pins the contract.
7. **Schema completeness.** Every row has all 5 fields. Missing
   field → `ConfigHistoryError` on read.

## 6. Estimated effort

| Phase | Hours |
|---|---|
| Writer module (`tools/config_history.py` or similar — `append()` + `config_as_of()`) | 3-4 |
| Hook into `bots/base_bot.py` startup | 1 |
| Hook into `dashboard/server.py` save route | 1-2 |
| Recon harness consumer wiring | 1-2 |
| Tests (7 cases) | 2-3 |
| **Total** | **~0.5 day code + tests** |

Small enough to bundle into the same Monday workstream as the
`VALIDATED_STRATEGIES` refactor.

## 7. Storage and rotation

Per-row size estimate: `STRATEGIES` has ~15 strategies each with
~10-30 numeric fields plus the global defaults block. Serialized,
roughly **5-10 KB per row** uncompressed.

Volume scenarios:
- Quiet day (1 startup, no dashboard saves): 1 row, ~10 KB.
- Active tuning day (1 startup + 20 dashboard saves): 21 rows,
  ~210 KB.
- 1 year of 100 changes/day (high estimate): ~365 MB
  uncompressed.

Rotation policy for v1: **none.** The file is fine on disk through
the next year. When we cross 100 MB or 100k lines, rotate
annually to `logs/config_history.<year>.jsonl` and keep the active
file as `logs/config_history.jsonl`. The `config_as_of()` reader
must then merge the rotated archives — straightforward but
not needed yet.

Compression: jsonl gzips beautifully (~10x). If disk pressure
emerges, gzip rotated archives in-place. The active file stays
uncompressed for cheap appends.

## 8. Privacy and gitignore

The `STRATEGIES` config contains no secrets — no API keys, no
account numbers, no personal data. Only thresholds, multipliers,
booleans. Safe to commit if the operator wants reproducibility,
though `logs/` is currently in `.gitignore` and that's fine —
the recon report (which embeds the relevant row) is the
operator-facing artifact, not the raw log.

Recommendation: keep `logs/` in `.gitignore`. The history log
lives on the operator's workstation. The recon report references
specific rows by `ts` + `hash` for traceability without dragging
the entire log into git.

## 9. Open question for operator

**Dashboard slider behavior: per-tick vs per-save?**

The dashboard exposes sliders for thresholds (`min_confluence`,
`min_momentum_confidence`, etc.). Two implementation choices for
the slider:

- **(A) Per-save only** (proposal): the slider drag updates the
  bot's runtime state immediately via WebSocket, but **only writes
  a history row** when the operator clicks "save to config." The
  intermediate drag positions are ephemeral.
- **(B) Per-tick**: every slider tick writes a row. Drift between
  the live runtime state and the persisted config is impossible
  because every state is logged.

(B) is more honest about runtime state but explodes the log during
operator tuning sessions: 50 ticks of a single slider drag = 50
rows, each ~10 KB. A 10-minute tuning session could write 500+
rows.

Default proposal: **(A) per-save.** The recon harness primarily
cares about "what config did the bot use when it fired this trade"
— and trades fire on the runtime state, which after a slider tick
matches the slider's current position. If the operator wants
full fidelity, we can revisit and add per-tick logging later.

The downside of (A): if a sim trade fires *during* an unsaved
slider tune, the harness will read the last saved config, which
may differ from the actual runtime state at trade time. This is
a known limitation, and it's exactly the kind of edge case the
operator should know about before committing to (A).

## 10. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Concurrent writer corrupts the file | Low (only 2 writers, both single-process within their bot) | High (log unreadable) | File locking + atomic append. Test §5.6 pins the contract. |
| Slider tunes don't appear in log under (A) | Medium | Medium (sim trade fires on unsaved tune → harness uses stale config) | Document the limitation; consider per-tick logging if it bites in practice |
| Log diverges between `sim_bot` and `prod_bot` workstations | High if both run independently | Medium (operator confusion) | One log per workstation. Workstation identity is implicit — sim and prod bots share `config/strategies.py` on the same machine. Cross-machine sync is out-of-scope. |
| Hash computation changes between Python versions / JSON libraries | Low | High (stable-ordering invariant breaks) | Pin to `json.dumps(..., sort_keys=True, separators=(",", ":"))` — stdlib only, contract-stable across versions |
| Rotation logic missing when log hits 100MB | Low (years out) | Low (only affects read perf) | Address when it happens; v1 is fine without it |

## 11. Out-of-scope

- Cross-workstation log sync (sim machine vs prod machine).
- Per-strategy log (one big log is simpler; consumers filter
  client-side).
- A UI for browsing config history. Use `jq` against the JSONL
  file; that's the v1 UX.
- Auto-revert to a historical config ("roll back to last
  Thursday"). The log is read-only from the bot's perspective;
  edits flow through `config/strategies.py`.
