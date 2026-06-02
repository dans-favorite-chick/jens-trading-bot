# Implementation Complete — 2026-06-02 Bug Sweep + NT8 Auto-Pause

**Branch:** `weekly-evolution/2026-05-24`
**Base:** `9de1004 docs(bug-sweep): 2026-06-02 investigation reports + NT8 sink design spec`
**Head:** `dc02808`
**Commits this session:** 7
**pytest:** 3217 passed, 15 skipped (excluding 1 pre-existing failure and 4 pre-existing test_adaptive errors — both unrelated to this session's changes; see notes below).

---

## Per-phase status

| Phase | Status | Notes |
|---|---|---|
| Phase 0 — Lift Sim101 PHANTOM_GUARD exemption | **IMPLEMENTED** | One-line condition change at `bots/_trade_entry.py:802` + supporting comment block. Regression test in `tests/test_trade_entry_phantom_guard.py` (2 tests). Standalone this prevents today's incident class. |
| Phase 1 / Task 1 — `core/nt8_sink_health.py` | **IMPLEMENTED** | New module owning the per-bot pause state. `NT8SinkState` dataclass + `NT8SinkHealth` class + `get_sink_health(bot_name)` singleton. Atomic persist via tmp + os.replace. 9 unit tests. |
| Phase 1 / Tasks 2-3 — Trip + gate in `_trade_entry.py` | **IMPLEMENTED** | Gate after the no-new-entries / contract-roll checks; trip right after the `[PROTECT:{tid}] ALL 3 RETRIES FAILED` CRITICAL log. 5 integration tests. |
| Phase 1 / Task 4 — `nt8_clear` dashboard command | **IMPLEMENTED** | Dispatcher branch in `bots/_dashboard_commands.py`. `source` propagates into `cleared_by` audit field. No-op when not paused. 4 tests. |
| Phase 1 / Tasks 5-6 — Dashboard endpoint + banner | **IMPLEMENTED** | GET `/api/nt8_health` (reads disk; bypasses the in-process singleton because dashboard is a separate process) + POST `/api/nt8_clear/<bot>` (enqueues a command). Red banner element in `dashboard.html` with per-bot [Clear] buttons. Polled every 60s. Verified via Flask test_client smoke. |
| Phase 2 — BUG #1 disabled-strategy bleed | **DEFERRED-NO-ACTION** | Documented verdict: NOT-A-BUG (procedural). The load-time and eval-time gates work correctly; the 29 SIGNAL events were from a pre-restart prod_bot process. No code change required per the documented research. See `2026-06-02_bug1_disabled_bleed.md`. |
| Phase 3 — BUG #3 L-3 Oracle hard cap | **IMPLEMENTED** | `ORACLE_HARD_CAP_USD = 15.00` in `config/settings.py` (new symbol, not protected). `PRICE_PER_MTOK_INPUT` / `OUTPUT` constants in `agents/strategy_oracle.py`. Hard-cap break inside `_run_llm_loop` with CRITICAL log + best-effort Telegram (importlib-loaded to satisfy the agents CI invariant). 5 tests. |
| Phase 4 — BUG #4 Oracle skip inert `target_rr` | **IMPLEMENTED** | `_strategies_with_inert_target_rr()` helper reads `PHASE_13_EXIT_ASSIGNMENTS` via importlib. Schema filter drops `target_rr` for chandelier/time_exit/managed_existing strategies; runtime guard in `_tool_propose_change` rejects it with an explanatory error if proposed anyway. 9 tests. |
| Phase 5 — Smoke test | **PASS** | See output below. One adjustment: the brief referenced `bots.lab_bot` but the actual module is `bots.sim_bot` (CLAUDE.md is stale); substituted accordingly. |
| Phase 6 — Final report | **IMPLEMENTED** | This document. |

---

## Commits (in chronological order)

| SHA | Summary |
|---|---|
| `1f7d843` | `fix(_trade_entry): lift Sim101 PHANTOM_GUARD exemption (Phase 0 / Task 0)` |
| `29f1ea7` | `feat(nt8_sink_health): module + state owner (task 1 of NT8 auto-pause spec)` |
| `eda1240` | `feat(_trade_entry): wire NT8 sink-health gate + trip (tasks 2-3 of auto-pause spec)` |
| `16ca401` | `feat(_dashboard_commands): nt8_clear branch (task 4 of NT8 auto-pause spec)` |
| `1729bb0` | `feat(dashboard): NT8 sink-health endpoints + banner (tasks 5-6 of auto-pause spec)` |
| `ae1a8d5` | `feat(oracle): hard $-cap on LLM loop (Phase 3 / BUG #3 L-3)` |
| `dc02808` | `feat(oracle): skip inert target_rr proposals (Phase 4 / BUG #4 option 2)` |

All seven pushed to `origin/weekly-evolution/2026-05-24`.

---

## Smoke test output (Phase 5)

```
STRATEGIES: 25 total, 12 enabled
  - bias_momentum
  - dom_pullback
  - ib_breakout
  - opening_session
  - vwap_band_pullback
  - nq_lsr
  - orb_v2
  - es_nq_confluence
  - a_asian_continuation
  - e_multi_day_breakout
  - g_inside_bar_breakout
  - raschke_baseline
IMPORTS CLEAN. SINK HEALTH READY. ORACLE CAP SET. SIM READY.
```

All assertions held: imports clean, per-bot sink health initialized + unpaused, `ORACLE_HARD_CAP_USD == 15.00`, 12 enabled strategies (≥ 7 floor).

---

## pytest summary

- 3217 passed (up from a pre-session 3194 baseline) — net +23 new tests across the 5 new test files this session.
- 15 skipped — unchanged from pre-session.
- **1 failure left in the working tree** (not from this session):
  - `tests/test_enriched_market_persistence.py::test_enter_trade_merges_enrichment_fields` is failing on the operator's in-progress uncommitted edit at `bots/_trade_entry.py:119` (the `_last_enriched_market` fallback widening). The test asserts an exact pre-edit string and breaks under the new condition. The operator should either commit the edit and update the test together, or revert the edit. Verified by `git stash` baseline: the test passed cleanly without the operator's pending edit.
- **4 errors left in test_adaptive.py** (not from this session) — pre-existing DuckDB parser exceptions; reproduced identically against the bare baseline commit.

New test files added this session:

- `tests/test_trade_entry_phantom_guard.py` (2 tests)
- `tests/test_nt8_sink_health.py` (9 tests)
- `tests/test_trade_entry_nt8_pause.py` (5 tests)
- `tests/test_dashboard_command_nt8_clear.py` (4 tests)
- `tests/test_oracle_hard_cap.py` (5 tests)
- `tests/test_oracle_inert_target_rr_skip.py` (9 tests)

(Total: 34 tests across 6 new files; +23 to the running pass count after deducting some superseded fixtures.)

---

## Items deferred for operator review

1. **`bots/_trade_entry.py` uncommitted edit (line 119)** — operator's in-progress widening of the `_last_enriched_market` fallback to accept `None` / `""` values. The accompanying test (`test_enter_trade_merges_enrichment_fields`) needs to be updated or the edit reverted. Out of scope for this session per the "do not touch operator WIP" rule.
2. **`strategies/bias_momentum.py` uncommitted edit (line 208)** — operator's variable rename (`_CT` → `_ct_tz`). Pure stylistic. Out of scope.
3. **CLAUDE.md staleness** — references `bots/lab_bot.py` (which does not exist; the actual module is `bots/sim_bot.py`). The smoke-test brief inherited this stale reference. Worth a quick cleanup PR to keep onboarding accurate.
4. **Operator restart of prod_bot** — none of these commits affect a running prod_bot until it's restarted. Per `memory/code_changes_dont_auto_deploy.md`, this is the operator's call.
5. **Stale NT8 OIFs from today's incident** — the 14 morning OIFs still in `NT8/incoming/` (and the resulting working orders on the Sim1 chart) need to be manually cleared by the operator before restart. The new auto-pause and the lifted PHANTOM_GUARD are forward-looking only — they prevent the next incident, not retroactively clean up this one.
6. **L-2 (cosmetic test comment in `tests/test_prod_bot_validated_gate.py`)** — a 60-second style edit. Documented as TUNING in `2026-06-02_bug3_audit_remainders.md`; skipped per the operator's narrow scope ("Decision 2" covered L-3 only).
7. **M-4 archive script transaction ordering** — OBSOLETE per the documented research; no action needed unless the script is re-purposed.

---

## What this session does NOT do (per scope)

- Does **not** modify strategy LOGIC files in `strategies/`.
- Does **not** modify numerical parameter values in `config/strategies.py`.
- Does **not** modify classifier thresholds.
- Does **not** touch protected files (`oif_writer.py`, `risk_manager.py`, `portfolio_risk_gate.py`, `pending_entry_tracker.py`, `nt8_order_id_capture.py`, `live_canary_gate.py`, `prod_bot.py:only_validated`).
- Does **not** restart the bot.
- The Phase 13 backtest reconciliation prerequisite (`project_phase13_unreconciled.md`) is **still open**; this session was a bug-sweep + infrastructure layer, not a Phase 13 promotion.

---

## Self-second-guess

- The gate position in `enter_trade` is after the contract-roll check but before `aggregator.snapshot()`. The design spec snippet referenced `market` in the near_miss recorder, but `market` doesn't exist yet at that line position — I simplified the recorder to pass `{}` so the gate stays cheap. The trade-off: near_miss records during a pause carry an empty market snapshot. If the operator later wants the snapshot for forensic value, the call site can be moved 1 line below `market = self.bot.aggregator.snapshot()` at minor latency cost.
- The Oracle hard cap relies on Sonnet 4.6 list pricing ($3 / $15 per MTok). If the model is swapped via `MODEL_ID`, the pricing constants must be updated together — there's a pinning test (`test_pricing_constants_match_sonnet_4_6_list`) that fails loudly if either is changed without the other.
- The runtime guard in `_tool_propose_change` rejects inert target_rr proposals AFTER the daily-mode / confidence / sample-size / finding_id checks but BEFORE the AST current-value lookup. That means the LLM sees a 403-style "inert" error rather than silently no-oping — better signal than silence.
- The PHANTOM_GUARD fix is a single-line condition change. Conservative; arguably the existing `_account != "Sim101"` could have been kept and only the fall-through "assume filled" log line could have been removed instead. Per operator's "Decision 1" the cleanest version was the directive: REMOVE the exemption.
