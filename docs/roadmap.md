# Phoenix Bot — Roadmap

What's done. What's in flight. What's next. **Sequenced by dependency, not by
calendar.**

The current plan is the unified plan from
[`audits/SYNTHESIS_2026-05-24.md`](audits/SYNTHESIS_2026-05-24.md). This file
mirrors that ordering with operator-facing context.

For the strategy-level Phase 13 source plan, see
[`PHOENIX_BEST_PLAN.md`](PHOENIX_BEST_PLAN.md) and
[`PHASE_13_IMPLEMENTATION_PLAN.md`](PHASE_13_IMPLEMENTATION_PLAN.md).

For live-state and current decisions, read
[`memory/context/OPEN_QUESTIONS.md`](../memory/context/OPEN_QUESTIONS.md).

---

## Done (recent — for context)

- **Phase 13 research complete** (2026-05-18) — 3 new winning strategies tested
  (`inside_bar_breakout`, `multi_day_breakout`, `asian_continuation`), 4
  strategies on the kill list, bug B2 and B3 root causes identified, infra D1–D4
  scoped. Plan: [`PHASE_13_IMPLEMENTATION_PLAN.md`](PHASE_13_IMPLEMENTATION_PLAN.md).
- **B3 fix shipped** (`strategies/orb_fade.py:159-166`, 2026-05-18) — wallclock
  freshness check now uses `now_ct.timestamp()`. Audit A listed this as
  still-open; it is fixed.
- **B2 partial fix shipped** (`strategies/opening_session.py:361-368`,
  2026-05-18) — `pivot_pp` require-check removed. Target design decision still
  pending (see P2-1 below).
- **Phase 0 sim overrides restored** (2026-05-20) — `DAILY_LOSS_LIMIT`,
  `PER_STRATEGY_DAILY_LOSS_CAP`, the $50 trade-cap (3 days late — see
  [incidents.md](incidents.md) 2026-05-20).
- **Trade-memory canonical reader audit** (commit `c9099d7`, 2026-05-13) — 12
  files routed through `core.trade_memory.load_all_trades()`. See
  [incidents.md](incidents.md).
- **PhoenixWatcher 5-min Repetition pattern** (2026-05-13) — alerting
  self-heals across Ctrl+C and logon. Auto-memory entry:
  `scheduled_task_repetition_pattern.md`.
- **Anti-mutation invariant on R-distance** (commit `4d4e15d`, 2026-05-13) — 
  `_initial_stop_frozen` captured at entry, prevents drift on stop modifications.
- **Wilson-CI promotion guardrail** (commit `477e31d`, #22, 2026-05-13) —
  refuses promotion to `validated=True` at n < 100. Operator memory:
  `promotion_on_vibes_failure_mode.md`. Still being overridden by inline
  comments (see P0-2 below).

---

## In flight / blocked

### P0 — Foundation (blocks everything else)

| ID | Task | Status |
|----|------|--------|
| P0-1 | Restore stack health — `prod_bot` down at audit time, 47 missed heartbeats, watchdog exhausted retries, Gemini quota exhausted | **🔴 BLOCKING** — operator action required. See [incidents.md](incidents.md) 2026-05-24. |
| P0-2 | Convert "operator override" config pattern from inline comments to `config/sim_overrides.py` opt-in (`PHOENIX_SIM_OVERRIDES=1`) | Pending. Closes F-08, F-09. |
| P0-3 | Fix `WEEKLY_LOSS_LIMIT < DAILY_LOSS_LIMIT` (5-minute fix; raise weekly to ≥ $600) | Pending. Closes F-02. **Singleton finding from Audit B — Audits A and C missed it.** |
| P0-4 | Disable AGENT_COUNCIL / AGENT_PRETRADE_FILTER / AGENT_DEBRIEF — no measured uplift, active quota cost, adds latency | Pending. Closes F-03. |

**Phase 0 sign-off gate:** all four done + Phase 0 checks in synthesis §3
pass + test suite still 2,110+ pass / 0 fail. Only then start P1.

### P1 — Capital integrity (touches money, data, or order correctness)

| ID | Task | Sign-off | Status |
|----|------|----------|--------|
| P1-1 | Live-vs-backtest reconciliation harness for `bias_momentum` (one strategy first) | — | Pending. **The most consequential single task in this sprint.** Closes F-13, F-16. |
| P1-2 | `RiskGateSink` fail-CLOSED when `PHOENIX_RISK_GATE=1` explicitly set | 🛑 STOP | Pending sign-off. Closes F-05. |
| P1-3 | Portfolio-level directional/contract cap (correlation Jaccard + dollar sum) | 🛑 STOP | Pending sign-off. Closes F-07, F-20. |
| P1-4 | NT8 silent-stall auto-recovery (kill + relaunch NT8 at > 180s of 0 ticks) | 🛑 STOP | Pending sign-off. Closes F-10. **Worst-case open issue — already cost one full primary window.** |
| P1-5 | External dead-man's switch (heartbeat probe from off-PC) | — | Pending. Closes F-22. |
| P1-6 | WS-watchdog: distinguish "no ticks" from "WS dead"; add `wsping` proof of life | — | Pending. Closes F-11. |
| P1-7 | Pending-order lifecycle truth (every entry ends filled/canceled/adopted/flattened) | 🛑 STOP | Pending sign-off. |
| P1-8 | Stop-order ID capture OR disable dynamic-stop strategies | 🛑 STOP | Pending sign-off. |

### P2 — Correctness (real bugs, not capital-critical)

| ID | Task | Status |
|----|------|--------|
| P2-1 | Bug B2 design decision + ship: `open_drive` continuation (R1/S1) vs reversion (PP) | **Awaiting operator** |
| P2-2 | Audit `time.time() - bar_ts` style gates across all strategies (generalize B3 pattern) | Pending |
| P2-3 | Roll-event handling: auto-flatten T-15 before contract roll, refuse new entries, swap | Pending. Closes F-14. |
| P2-4 | Grader-config divergence: align `tools/grade_open_predictions.py` with current strategy set | Pending. Closes F-19. |
| P2-5 | `dom_pullback` decision: keep deleted OR re-add `enabled=False` with explicit data-gap doc | **Awaiting operator**. See [incidents.md](incidents.md) 2026-05-21. |

### P3 — Roadmap completion

| ID | Task | Status |
|----|------|--------|
| P3-1 | Dashboard 3C tuning sliders end-to-end (slider → `RiskManager.set_*` → live behavior) | Open roadmap |
| P3-2 | `feed.html` (the `/feed` window) | Open roadmap |
| P3-3 | Cull active strategy roster to 3-5 with clearest TENTATIVE-tier live evidence | Pending. Replaces ad-hoc strategy add cadence. |
| P3-4 | Cull broken cross-links after `docs/` restructure | Pending (this synthesis's restructure introduced new paths). |

### P4 — Hardening (observability, resilience, tech debt)

| ID | Task | Sign-off |
|----|------|----------|
| P4-1 | Decompose [`bots/base_bot.py`](../bots/base_bot.py) (5,951 → < 1,500 LOC) | 🛑 STOP |
| P4-2 | Per-signal correlation/trace ID across the lifecycle | — |
| P4-3 | Latency SLO: p99 tick-in → OIF-out | — |
| P4-4 | Migrate `trade_memory` + halts + equity-state JSON → SQLite | — |
| P4-5 | Walk-forward / CPCV / DSR / PBO harness wired in `weekly_evolution.py` | — |
| P4-6 | A/B uplift harness for AI agents (pretrade filter first) | — |
| P4-7 | `tier_3000` compounding rollout — **gated** on P1-1 passing for every strategy + 60-day live observation cap of 3 contracts | — |

---

## Never list (do not pursue, even if asked)

1. **Auto-promote `validated=True` based on a backtest alone.** Live n ≥ 100,
   PF ≥ 1.3, Wilson-CI lower bound > 0.5 are the floor. Anything else is
   conjecture.
2. **Swap NT8 OIF for a third-party broker router** before stack health (P0-1)
   and reconciliation (P1-1) hold for 60 days.
3. **Add a second instrument** (ES / NQ / MES live trading) until MNQ is
   boring for 90 days.
4. **Add another data vendor** before P1-1 reconciliation confirms existing
   data path is faithful to live.
5. **Premature feed switches** (TradingView, alternative L2 sources).
   TradingView Premium was already stricken.
6. **Promote any AI agent to "blocking" mode** before A/B uplift CI > 0.
7. **A second strategy zoo.** If Phase 14 wants 7 strategies, test 1.
8. **Custom NT8 indicators beyond `TickStreamer` and `PhoenixTradeOverlay`.**
9. **Auto-modify live strategy params with AI.**
10. **Compounding sizing (`tier_3000`)** before P4-7 preconditions met.

---

## The one question

Repeat from synthesis Step 7, because it is the fact that most changes the
plan:

> Have you, or has anyone, ever sat down and compared `sim_bot`'s per-strategy
> 30-day live-paper output against the corresponding 30-day slice of the Phase
> 13 5-year backtest — same strategies, same date range, same input bars —
> and produced a per-strategy divergence number (trade count, win rate, net
> P&L) that you'd defend in writing?

If yes (with numbers): P1-1 becomes a confirmation pass, not a prerequisite.
If no / "sort of": every recommendation below P0 is conditional on building
that comparison first.
