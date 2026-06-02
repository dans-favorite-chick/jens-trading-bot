# NT8-Dead Sink Auto-Pause — Design Spec

**Date:** 2026-06-02
**Branch target:** `weekly-evolution/2026-05-24`
**Status:** Approved for implementation. Implementation session
should read [`logs/oracle/research/2026-06-02_bug_sweep_report.md`](../../../logs/oracle/research/2026-06-02_bug_sweep_report.md)
first.

---

## Problem

On 2026-06-02 between 09:02 and 09:39 CT, prod_bot's NT8 ATI sink
became unresponsive. Phoenix detected the failure (14 OIF_STUCK +
14 PROTECT FAILED + 14 emergency_flatten cycles, each loudly logged
and Telegram-alerted) but kept attempting new entries. When NT8 ATI
recovered ~11:00 CT, 7+ stale BUY LIMIT MNQ M6 orders from the
morning's failed entries became working orders on the Sim1 chart —
risking unintended position exposure.

The full root cause is in
[`logs/oracle/research/2026-06-02_chart_orders_root_cause.md`](../../../logs/oracle/research/2026-06-02_chart_orders_root_cause.md).
Short version: prod_bot's account=Sim101 bypasses an existing
`PHANTOM_GUARD` defensive layer (deliberate exemption at
`bots/_trade_entry.py:802`), so it falls through to "assume filled
(paper mode)" and proceeds to write protect-leg + cancel + flatten
OIFs — all of which sit in NT8's `incoming/` folder waiting to be
consumed.

This spec adds a **global, operator-cleared pause** that trips after
the first `PROTECT ALL 3 RETRIES FAILED` event and prevents
subsequent entries from being attempted while NT8 is dead.

This spec is **complementary**, not a replacement, for the
PHANTOM_GUARD Sim101-exemption fix. See "Connection to today's
incident" below for how both interact.

---

## Architecture

One new module owns the state, the trip rule, and the persistence.
Two thin call-sites in existing files wire it in. The dashboard
command + UI banner sit on existing infrastructure.

```
core/nt8_sink_health.py   ← NEW: state owner + persist/load
   ↑                          ↑
   │ record_protect_failed()  │ is_paused() / clear()
   │                          │
bots/_trade_entry.py:980      bots/_trade_entry.py:~84
(trip on PROTECT FAILED)      (gate before OIF write)
                              bots/_dashboard_commands.py
                              (/nt8_clear handler)
                              dashboard/server.py
                              (banner + /api/nt8_health endpoint)
```

**Boundaries:**

- `core/nt8_sink_health.py` — purely state + I/O. No knowledge of
  strategies, signals, OIF wire format. Testable in isolation.
- `bots/_trade_entry.py` — two ~5-line additions: one gate check at
  the top of `enter_trade`, one `record_protect_failed()` call right
  after the existing CRITICAL log at line 980.
- `bots/_dashboard_commands.py` — one new command handler.
- `dashboard/server.py` + `dashboard/templates/dashboard.html` —
  `/api/nt8_health` endpoint + banner element.

**Files NOT touched:**

- No protected files (`oif_writer.py`, `risk_manager.py`,
  `portfolio_risk_gate.py`, `pending_entry_tracker.py`,
  `nt8_order_id_capture.py`, `live_canary_gate.py`, `config/settings.py`
  protected symbols, `config/strategies.py` validated/enabled flips,
  `prod_bot.py:only_validated`).
- No strategies (`strategies/*.py`).
- No tuning values.

---

## State model

Single dataclass persisted as JSON, one file per bot:

```python
# core/nt8_sink_health.py

@dataclass
class NT8SinkState:
    paused: bool = False
    paused_at: str | None = None      # ISO local timestamp
    paused_reason: str | None = None  # "PROTECT FAILED on 2a0c8180 (bias_momentum LONG)"
    paused_trade_id: str | None = None
    paused_strategy: str | None = None
    paused_account: str | None = None
    cleared_at: str | None = None
    cleared_by: str | None = None     # "dashboard" | "slash:/nt8_clear" | "boot:fresh"
```

**Persistence path:** `runtime/nt8_sink_state_{bot_name}.json` — e.g.
`runtime/nt8_sink_state_prod.json`, `runtime/nt8_sink_state_sim.json`.
Atomic write via temp file + os.replace().

**Lifecycle:**

- Bot boot: `get_sink_health(bot_name)` loads state from disk if file
  exists. If the on-disk state has `paused=True`, the bot starts
  paused and the boot banner emits one CRITICAL log:
  `[NT8_SINK] booting paused from {paused_at} ({paused_reason}). /nt8_clear required to resume.`
- First-time boot (no file): creates a fresh `NT8SinkState(paused=False)`,
  persists, continues.
- Trip: `record_protect_failed(trade_id, strategy, direction, account)`
  sets paused=True, persists atomically, fires Telegram once
  (idempotent — no-op if already paused).
- Clear: `clear(by="dashboard" | "slash" | …)` sets
  paused=False + cleared_at/cleared_by, persists, fires Telegram.

**Per-bot isolation:** prod_bot and sim_bot have independent state
files. Pausing prod does not pause sim. This is intentional —
sim_bot's PHANTOM_GUARD already handles its own per-strategy
sub-account scenario, and the two bots may legitimately diverge.

---

## Trip rule

Single entry point on the state owner:

```python
def record_protect_failed(self, trade_id, strategy, direction, account, reason=None):
    if self.state.paused:
        return  # idempotent
    self.state.paused = True
    self.state.paused_at = datetime.now().isoformat()
    self.state.paused_reason = (
        reason or f"PROTECT FAILED on {trade_id} ({strategy} {direction} on {account})"
    )
    self.state.paused_trade_id = trade_id
    self.state.paused_strategy = strategy
    self.state.paused_account = account
    self._persist()
    self._fire_telegram_pause()
```

**Call site:** [`bots/_trade_entry.py:980`](../../../bots/_trade_entry.py:980),
immediately after the existing `logger.critical("[PROTECT:{tid}] ALL 3
RETRIES FAILED …")` line:

```python
# NEW — right after the critical log at line 980
from core.nt8_sink_health import get_sink_health
get_sink_health(self.bot.name).record_protect_failed(
    trade_id=tid, strategy=signal.strategy,
    direction=signal.direction, account=_account,
)
```

That's it — no additional logic in `_trade_entry.py` for the trip.
The existing emergency_flatten + CANCEL logic continues exactly as
today (the auto-pause stops the *next* entry, not the in-flight one).

---

## Gate

In `bots/_trade_entry.py::enter_trade`, immediately after the existing
`is_no_new_entries_window()` check (around line 84-91), add:

```python
# NEW — NT8 sink-health gate
from core.nt8_sink_health import get_sink_health
_sink = get_sink_health(self.bot.name)
if _sink.is_paused():
    logger.info(
        f"[SKIP_NT8_DEAD:{signal.trade_id}] {signal.strategy} {signal.direction} — "
        f"NT8 sink paused since {_sink.state.paused_at} ({_sink.state.paused_reason})"
    )
    self.bot.last_rejection = f"NT8 sink paused: {_sink.state.paused_reason}"
    try:
        sig_dict = {"direction": signal.direction, "strategy": signal.strategy,
                    "confidence": signal.confidence, "entry_score": signal.entry_score,
                    "reason": signal.reason}
        self.bot.history.log_near_miss(sig_dict, market, "nt8_sink_paused")
    except Exception:
        pass
    return
```

Evals continue normally — strategies still evaluate, `best_signal`
still selects, the `_pending_signal` is still set by the dispatcher.
The gate sits at the very top of `enter_trade`, so the SKIP is logged
as a near_miss with reason="nt8_sink_paused" in the existing history
JSONL. Operator sees in `logs/history/<date>_<bot>.jsonl` exactly
which trades would have fired during the pause.

---

## Resume — dashboard banner + slash command

**Slash command** in `bots/_dashboard_commands.py` — add a new
handler matching the existing pattern:

```python
elif cmd == "/nt8_clear":
    from core.nt8_sink_health import get_sink_health
    _sink = get_sink_health(self.bot.name)
    if not _sink.is_paused():
        return "NT8 sink is not paused. No-op."
    _sink.clear(by="slash:/nt8_clear")
    return f"NT8 sink cleared. Bot will resume new entries on next eval."
```

**Dashboard endpoint** in `dashboard/server.py`:

```python
@app.route("/api/nt8_health")
def api_nt8_health():
    from core.nt8_sink_health import get_sink_health
    snap = {}
    for bot_name in ("prod", "sim"):
        s = get_sink_health(bot_name).state
        snap[bot_name] = {
            "paused": s.paused,
            "paused_at": s.paused_at,
            "paused_reason": s.paused_reason,
        }
    return safe_jsonify(snap)

@app.route("/api/nt8_clear/<bot_name>", methods=["POST"])
def api_nt8_clear(bot_name):
    from core.nt8_sink_health import get_sink_health
    if bot_name not in ("prod", "sim"):
        return safe_jsonify({"error": "unknown bot"}), 400
    get_sink_health(bot_name).clear(by="dashboard")
    return safe_jsonify({"ok": True})
```

**Dashboard banner** in `dashboard/templates/dashboard.html`:
poll `/api/nt8_health` at the existing health-poll cadence (60s).
When any bot has `paused=True`, render a red banner at the top of
the dashboard:

```
⚠ NT8 SINK PAUSED — prod bot. Cause: PROTECT FAILED on 2a0c8180
  (bias_momentum LONG) at 09:03:07. [Clear] [View incident]
```

The `[Clear]` button POSTs to `/api/nt8_clear/{bot}`. The
`[View incident]` link routes to `/logs?filter=nt8_sink_paused`
(existing log viewer).

**Telegram on clear:**

```
✅ NT8 SINK CLEARED — prod bot resuming. Cleared by {by} after {duration_min:.1f} min.
```

---

## Testing strategy

### `tests/test_nt8_sink_health.py` — unit tests for the state owner

| Test | Expectation |
|---|---|
| `test_initial_state_unpaused` | Fresh load returns `paused=False`. |
| `test_record_protect_failed_trips` | After call, `is_paused()` True, fields populated. |
| `test_record_protect_failed_idempotent` | Second call while paused does NOT update timestamps and does NOT fire Telegram twice (mock telegram). |
| `test_clear_resets_state` | After `clear(by="x")`, `is_paused()` False, `cleared_at` populated, `cleared_by="x"`. |
| `test_state_persists_to_disk` | Write → instantiate new instance → load → fields match. |
| `test_state_survives_round_trip_with_paused_state` | Paused state at boot triggers boot-paused log. |
| `test_per_bot_isolation` | `get_sink_health("prod")` and `get_sink_health("sim")` use separate files; mutating one doesn't affect the other. |
| `test_atomic_write_does_not_corrupt_on_crash` | Write fails mid-stream; load still returns prior valid state. (Use unittest.mock to inject failure.) |

### `tests/test_trade_entry_nt8_pause.py` — integration tests for the gate

| Test | Expectation |
|---|---|
| `test_enter_trade_skips_when_paused` | With `is_paused()=True` (monkeypatched), `enter_trade` returns before any OIF write. Verify OIF writer's `submit_place` is NOT called. |
| `test_enter_trade_logs_near_miss_when_paused` | Verify `history.log_near_miss` called with reason="nt8_sink_paused". |
| `test_enter_trade_runs_normally_when_not_paused` | With `is_paused()=False`, the existing test fixture (signal flows through full path) still passes — regression guard. |
| `test_protect_failed_call_in_trade_entry` | Mock the existing PROTECT-retry path to fail all 3; verify `record_protect_failed` is called with correct trade_id / strategy / direction / account. |

No integration test for the dashboard button (matches existing dashboard test pattern — Flask routes are smoke-tested only).

---

## Connection to today's incident

This design would have prevented today's incident chain as follows:

| Time | Without auto-pause (today's reality) | With auto-pause |
|---|---|---|
| 09:02:54 | Entry #1 INTENT → entry OIF written → PROTECT × 3 fail → CANCEL stuck → emergency_flatten stuck → 1 naked LIMIT in NT8 incoming/ | Same — design tripps AFTER the first PROTECT FAILED |
| 09:03:07 | `[PROTECT] ALL 3 RETRIES FAILED` fires; bot logs and continues | `record_protect_failed()` invoked → state=paused, persisted → Telegram fired |
| 09:05:56 → 09:38:55 | 13 more entries each follow the same cycle; 14 naked LIMITs accumulate in incoming/ | All 13 gated at `[SKIP_NT8_DEAD:tid]`; near_miss recorded; no OIF writes |
| ~11:00 (NT8 ATI returns) | NT8 processes 14 stale LIMITs as working orders; chart shows 7+ BUY LIMITs | NT8 processes 1 stale LIMIT (the first); chart shows ~1 working order |
| 11:52 (operator notices) | 7+ orders on chart; reconciliation tries to flatten orphan position | 1 order on chart; recon flatten is cleaner |
| Resume | Operator restarts bot, hopes NT8 is OK, watches what fires | Operator clicks [Clear] in dashboard (or runs /nt8_clear); bot resumes with the explicit affirmation step |

**Net reduction:** 14 → 1 naked LIMITs left on the chart. Not zero —
the first entry still leaks through because the trip rule is "after
the first PROTECT FAILED." Combined with the PHANTOM_GUARD Sim101-
exemption fix recommended in
[`logs/oracle/research/2026-06-02_chart_orders_root_cause.md`](../../../logs/oracle/research/2026-06-02_chart_orders_root_cause.md)
(implementation session decision), the count drops to **0**.

This spec deliberately does not bundle the PHANTOM_GUARD fix — that
is a separate one-line edit to the same file (`_trade_entry.py:802`)
and the implementation session should treat it as task 0
(prerequisite) before this spec's tasks. Both together are <100 LOC.

---

## Explicit non-goals

- **No auto-resume / probe heartbeat.** Operator-explicit resume is
  the whole point of the design. If a future "tier 2" auto-resume is
  wanted, this state model supports it cleanly (add a probe loop in
  `core/nt8_sink_health.py`, gate behind an opt-in setting), but it
  is out of scope.
- **No per-strategy pause.** NT8 is a shared sink; pause is global
  per-bot. The state model would extend cleanly to per-strategy
  (add `paused_strategies: set[str]`), but no current need.
- **No backfill of in-flight signals.** When unpaused, the bot
  starts fresh from the next tick. Signals that fired during the
  pause exist in `near_miss` history but are not retried automatically.
- **No change to the existing 3-retry PROTECT logic.** That logic
  stays — the pause is a defensive layer ABOVE it, not a replacement.
- **No change to `oif_writer.py`** or any protected file.
- **No change to strategy code** or strategy config.

---

## Implementation tasks (numbered, sequential)

The implementation session should work these tasks in order. Each
task lists the file(s) edited and the test that covers it.

### TASK 0 — Prerequisite: lift PHANTOM_GUARD Sim101 exemption

**File:** `bots/_trade_entry.py:802`

**Change:** narrow the exemption condition. The current line is:
```python
if (_account and _account != "Sim101"):
```
This skipped PHANTOM_GUARD when `_account == "Sim101"` — exactly the
prod_bot case that caused today's incident. Replace with a condition
that runs PHANTOM_GUARD for **any** non-LIVE bot when an OIF is stuck,
or remove the Sim101 carve-out entirely.

**Caveat:** the exemption was added per inline comments B39/B48 for
"Sim101-only mock tracking." Re-read those comments before editing
and confirm the original intent is no longer load-bearing. If it is,
gate the exemption behind a new `config/settings.PHANTOM_GUARD_SKIP_ACCOUNTS`
opt-in list (default empty) rather than removing the conditional.

**Test:** add a new test in `tests/test_trade_entry_phantom_guard.py`
asserting PHANTOM_GUARD runs and aborts cleanly when account=Sim101
and an OIF is stuck. (Existing `test_phantom_guard_*` suite, if any,
should grow this case.)

**Why this is task 0:** without it, today's first-entry naked-LIMIT
still leaks through even with the new auto-pause.

---

### TASK 1 — Create `core/nt8_sink_health.py`

**File:** `core/nt8_sink_health.py` (NEW, ~120 LOC)

**Content:**
- `NT8SinkState` dataclass per the State Model section above
- `class NT8SinkHealth`: `__init__(bot_name)`, `is_paused()`,
  `record_protect_failed(...)`, `clear(by)`, `_persist()`, `_load()`,
  `_fire_telegram_pause()`, `_fire_telegram_cleared()`
- Module-level `get_sink_health(bot_name) -> NT8SinkHealth`
  singleton (one instance per bot_name; cached)
- Atomic write helper (temp file + `os.replace`)
- Telegram fire wrapped in try/except so a telegram failure never
  prevents the state from updating

**Test:** `tests/test_nt8_sink_health.py` per the testing section.

**Definition of done:**
- All 8 unit tests pass.
- File is < 150 LOC.
- No imports from `bots/` or `strategies/` (this module is downstream
  of both; circular imports would break startup).

---

### TASK 2 — Wire the trip call site in `_trade_entry.py`

**File:** `bots/_trade_entry.py`, around line 980 (right after the
existing CRITICAL log for `ALL 3 RETRIES FAILED`).

**Change:** 4-line insert:
```python
from core.nt8_sink_health import get_sink_health
get_sink_health(self.bot.name).record_protect_failed(
    trade_id=tid, strategy=signal.strategy,
    direction=signal.direction, account=_account,
)
```

**Test:** `tests/test_trade_entry_nt8_pause.py::test_protect_failed_call_in_trade_entry`.

**Definition of done:**
- Test passes.
- Manual trace: trigger the test fixture's PROTECT-failed path,
  verify the on-disk state file flips to paused.

---

### TASK 3 — Wire the gate in `_trade_entry.py`

**File:** `bots/_trade_entry.py`, immediately after the existing
`is_no_new_entries_window()` block (~line 84-91).

**Change:** ~12-line gate per the Gate section above.

**Test:** `tests/test_trade_entry_nt8_pause.py::test_enter_trade_skips_when_paused`,
`test_enter_trade_logs_near_miss_when_paused`,
`test_enter_trade_runs_normally_when_not_paused`.

**Definition of done:**
- All 3 tests pass.
- Existing test_trade_entry test suite still passes (regression guard).

---

### TASK 4 — Add `/nt8_clear` slash command

**File:** `bots/_dashboard_commands.py`

**Change:** add a new `elif cmd == "/nt8_clear":` branch per the
Resume section above.

**Test:** extend existing `tests/test_dashboard_commands.py` (if it
exists) with a `test_nt8_clear_command_when_paused` and
`test_nt8_clear_command_when_not_paused` (no-op) case.

---

### TASK 5 — Add `/api/nt8_health` and `/api/nt8_clear/<bot>` endpoints

**File:** `dashboard/server.py`

**Change:** add two routes per the Resume section above. Wrap
each in `try/except` returning `{"error": "unavailable"}` on
failure, matching the existing `api_market_state` defensive
pattern (per audit finding M-1).

**Test:** smoke test via curl in the verification step
(no automated test required, matches existing dashboard route pattern).

---

### TASK 6 — Dashboard banner

**File:** `dashboard/templates/dashboard.html`

**Change:** add a banner element polling `/api/nt8_health` at the
existing 60s health-poll cadence. Banner styling: red background,
white text, prominent at top of viewport. Includes [Clear] and
[View incident] buttons.

**Test:** manual visual check in browser. (Existing dashboard tests
are server-side only.)

---

### TASK 7 — Verification + commit

**Steps:**
1. Run `pytest` — full suite must pass.
2. Run prod_bot in a test environment; trigger a fake PROTECT FAILED
   (e.g., kill NT8 ATI mid-bracket). Verify:
   - Telegram alert fires once.
   - Dashboard banner appears within 60s.
   - `runtime/nt8_sink_state_prod.json` exists with `paused: true`.
   - Subsequent signals log `[SKIP_NT8_DEAD]` and do not write OIFs.
3. Click `[Clear]` in dashboard. Verify:
   - State file flips to `paused: false`.
   - Telegram clear alert fires.
   - Next signal flows through normal path.
4. Commit each task separately with messages like:
   `feat(nt8_sink_health): module + state owner (task 1 of NT8 auto-pause spec)`
5. Push to `weekly-evolution/2026-05-24`.

---

## Dependencies on this session's other documents

- [`logs/oracle/research/2026-06-02_chart_orders_root_cause.md`](../../../logs/oracle/research/2026-06-02_chart_orders_root_cause.md) —
  full root cause + the Sim101-exemption finding that drives TASK 0.
- [`logs/oracle/research/2026-06-02_bug_sweep_report.md`](../../../logs/oracle/research/2026-06-02_bug_sweep_report.md) —
  master handoff; read first.

## Self-second-guess

- **The trip threshold is "first PROTECT FAILED."** If transient NT8
  blips are more common than I assume, this could pause unnecessarily.
  The existing 3-retry PROTECT logic already absorbs 5s of NT8
  silence; if you're hitting `ALL 3 RETRIES FAILED`, NT8 has been
  silent for at least 5s on this particular OCO pair. That's a
  strong "NT8 is dead" signal, but if false positives prove painful,
  the trip rule could be "2 PROTECT FAILED within 10min" (option B
  from the brainstorming) — one-line change in the trip method.
- **TASK 0 (PHANTOM_GUARD) is technically optional** for shipping
  this spec — without it, the auto-pause still works, just leaves
  the first entry's LIMIT as a phantom. I'm flagging it as the
  prerequisite because today's incident hinged on it; the
  implementation session should not skip it without operator sign-off.
- **The per-bot state file design** prevents pausing prod from
  pausing sim. If you decide later that "NT8 dead" should affect
  every bot writing to that incoming/ folder, the state would need
  to be process-shared (e.g., file-based lock + watch). Out of scope.
- **No NT8-side probe.** This design trusts that the operator
  notices the pause and verifies NT8 manually. An auto-probe (write
  a no-op heartbeat OIF, see if it gets consumed) would be a clean
  add — but adds new OIF traffic and is a future tier-2 feature.
