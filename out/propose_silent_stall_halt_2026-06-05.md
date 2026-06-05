# Proposed: SILENT_STALL signal-fire HALT — Phoenix Round 3 follow-up

_2026-06-05. Owner: Cowork-Claude PHANTOM-NT8 Round 3 sprint._

## TL;DR

NT8 SILENT_STALL has been continuously active since **2026-06-04 21:43:42 CDT** (≈11h27m at first 09:00 detection). The bridge correctly logs `[ERROR] NT8 SILENT_STALL — heartbeats fresh (Xs) but ticks stale (Ys). TCP alive, feed frozen.` (`bridge/bridge_server.py:828-835`) and the watcher_agent's PhantomGuard correctly raises `RED_ALERT/silent_stall` incidents (125 incidents today; see `logs/incidents/incident_2026-06-05_09-*.txt`). **But no signal-fire HALT executes** — if any strategy generated a signal on stale tick data, the bot would route an OIF to NT8.

The minimum-viable HALT requires either:
- a **bridge-side broadcast filter** (`bridge/bridge_server.py` — PROTECTED, NOT pre-authorized in Round 3) so silent_stall ticks never reach the bot, OR
- a **bot-side check** (`bots/_strategy_dispatch.py` or `bots/base_bot.py` — both touched by Forensics chat or PROTECTED-adjacent) that reads the bridge `/health` endpoint and skips signal evaluation when `nt8_status == "silent_stall"`.

Neither protected-edit path is OA'd for this sprint. This document proposes both options for a follow-up sprint, plus an unprotected sidecar fallback that could ship now.

## Why this is below CRITICAL right now

- Operator confirmed the bots are paused / under manual supervision while SILENT_STALL is active.
- Strategy evaluators that read tick-derived features (`ATR`, `VWAP`, bar volume, etc.) gate on `freshness_s` checks at the strategy level (e.g. `strategies/bias_momentum.py` rejects on stale bar age). Stale data signals are mostly self-rejecting via existing freshness guards.
- LIVE_TRADING is False; the worst case is a sim_bot signal landing as a stuck OIF, which Round 2's Guard A + Guard B already contain.

That said, with LIVE_TRADING about to flip per operator's canary plan, this gap needs a permanent close.

## Failure mode reconstructed

Phase 0 evidence:
- 125 incident files today, mix of `RED_ALERT/silent_stall` (raised by PhantomGuard at 1/min) and `MAJOR/ai_unresponsive` (Gemini 429 quota exhausted → no auto-action analysis).
- Earliest 09:00 incident `tick_age_s = 40662.7` (≈11.3h since last NT8 tick).
- Detection path is functional (`bridge/bridge_server.py:822-835`).
- Auto-action path: `tools/nt8_silent_stall_recovery.py` exists but requires `PHOENIX_NT8_AUTO_RECOVERY=1` env flag (operator opt-in), and is not currently running per `logs/nt8_silent_stall_recovery.log` mtime.
- **No bot-side consumer of `silent_stall` status**: `grep -rn "silent_stall\|nt8_status" bots/ core/` returns 0 hits in operational code.

## Three fix paths

### Option 1 — Bridge-side broadcast filter (PROTECTED — needs OA)

In `bridge/bridge_server.py` (PROTECTED), wrap the existing tick fan-out to bot clients with a `silent_stall_active` guard. When `was_silent_stall` is True, the bridge drops or marks tick broadcasts so bots see no fresh data.

Pros: single point of enforcement; covers every bot client automatically.
Cons: protected edit; risks accidental signal-fire suppression if the silent_stall detector false-positives during a slow-tick window.

### Option 2 — Bot-side health check (`bots/_strategy_dispatch.py` — Forensics-adjacent)

At the entry of `_eval_strategies`, poll `http://127.0.0.1:8767/health` (HTTP, in-process — already mocked in tests via `tools/nt8_silent_stall_recovery.py`'s pattern). If `nt8_status == "silent_stall"`, log warning and skip evaluation this tick. Mirror the Round 2 Guard A fail-open posture.

Pros: in-process; no IPC; surgical.
Cons: 8767 HTTP poll on every tick adds latency; needs caching with a short TTL.

### Option 3 — Sidecar HALT file (UNPROTECTED — ships now)

The `tools/nt8_silent_stall_recovery.py` daemon (already opt-in via `PHOENIX_NT8_AUTO_RECOVERY=1`) writes a `data/_silent_stall_halt.flag` file the moment it detects SILENT_STALL persisting > `STALL_THRESHOLD_S`. Both `prod_bot` and `sim_bot` already read various flag files via the existing config/settings.py pattern. A new flag check in `bots/_strategy_dispatch.py` is **non-protected** and could ship today.

Pros: smallest surface; no protected edit; opt-in via existing env flag.
Cons: requires the recovery daemon to be running. Operator confirmed it's NOT currently running.

## Recommendation

Ship **Option 3** in a follow-up sprint, gated behind the existing `PHOENIX_NT8_AUTO_RECOVERY=1` env flag. It surfaces the SILENT_STALL signal at the bot-eval boundary without touching protected files. Operator can opt in immediately by exporting the env var + launching the daemon.

For the live LIVE_TRADING canary, Options 1 + 2 should be on the roadmap as defense-in-depth.

## Acceptance criteria for the follow-up sprint

1. With `PHOENIX_NT8_AUTO_RECOVERY=1`, daemon up, simulated 60s tick-stale window → 0 new OIFs written by either bot.
2. Without the env flag, behavior is unchanged from today (no false-positive HALT).
3. Bridge health endpoint at `:8767/health` exposes a documented `nt8_status` field.
4. Operator can read `data/_silent_stall_halt.flag` in dashboard or tail it.

## Cross-references

- `bridge/bridge_server.py:780-838` — SILENT_STALL detection (PROTECTED).
- `tools/nt8_silent_stall_recovery.py` — existing auto-recovery daemon (opt-in).
- Round 2 commit `0d7c9d4` — Guard A + Guard B (similar pre-cycle gate pattern).
- This sprint's H3 fix in `core/position_manager.py:472-525` — same fail-open posture this proposal recommends.

---

**Status: PROPOSED — awaiting operator decision on which Option to ship.**

This sprint files `FINDING-2026-06-05-SILENT-STALL-NT8-12H` against this proposal.
