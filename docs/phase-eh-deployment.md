# Phase E–H Deployment Guide

_Sprint complete: 2026-04-21 evening. Branch `feature/phases-e-h`._

## What shipped

Five AI agents + infrastructure, plus Phase E gamma rewire, Phase F test-backlog
cleanup, Phase G bug fixes.

### Agents (all in `agents/`)

| Agent | Module | Trigger | Model | Output |
|---|---|---|---|---|
| Council Gate (4A) | `council_gate.py` | Session open 8:30 CT + regime shift | Gemini Flash ×7 voters + Gemini Pro synth | `logs/council/YYYY-MM-DD.json` |
| Pre-Trade Filter (4B) | `pretrade_filter.py` | Per-entry, inline in `base_bot` | Gemini Flash | `logs/agents/*.jsonl` |
| Session Debriefer (4C) | `session_debriefer.py` | 16:00 CT post-flatten (sim_bot loop) | Claude Sonnet | `logs/ai_debrief/YYYY-MM-DD.md` |
| Historical Learner (4D) | `historical_learner.py` | Weekly (CLI/cron) | Claude Sonnet | `logs/ai_learner/weekly_*.md` + `pending_recommendations.json` |
| Adaptive Params (4E) | `adaptive_params.py` | After 4D; CLI-gated | Deterministic | `logs/ai_learner/proposals/*.md` |

### Infra (`agents/base_agent.py`, `agents/config.py`)

- `AIClient` wraps Gemini + Claude with 10s default timeout, 3-retry exp backoff, JSON-mode parser, JSONL cost/latency log (`logs/agents/YYYY-MM-DD_agent_calls.jsonl`).
- `BaseAgent.safe_call(fn, default_value=...)` — every AI call returns the default on timeout/error, never raises.
- Missing API key → `DEGRADED=True` flag, agents short-circuit to safe defaults (CLEAR, NEUTRAL, empty recs). Bot never crashes.

## Env setup

```
GOOGLE_API_KEY=...       # Gemini Flash + Pro (council, pretrade, legacy)
ANTHROPIC_API_KEY=...    # Claude Sonnet (debrief, learner)
TELEGRAM_BOT_TOKEN=...   # optional debrief dispatch
```

Put these in `.env` at repo root. If either missing: agents log CRITICAL once, operate in degraded mode, do not crash.

## Runbook

### First-time flip
1. Pull `feature/phases-e-h`, run `python -m pytest --tb=no -q` → expect 695 passing.
2. Set API keys in `.env`.
3. Restart sim_bot — Pre-Trade Filter and Session Debriefer wire up automatically via hooks in `bots/base_bot.py` (`# [AI-PRETRADE-HOOK]` ~L1148) and `bots/sim_bot.py::_maybe_run_debrief`.
4. Verify: `tail -f logs/agents/*.jsonl` — expect advisory-mode pretrade calls on each entry attempt.

### Weekly learner run (manual or cron)
```
python tools/run_weekly_learner.py --days 14
```
Writes `logs/ai_learner/weekly_YYYY-MM-DD.md` + `pending_recommendations.json`.

### Proposal review flow
```
python tools/list_proposals.py
python tools/approve_proposal.py <proposal_id>   # builds branch ai-proposal/<id>, runs tests, STOPS
# review diff, merge manually if good
```

**Never auto-merges.** Jennifer always in the loop.

## Safety

- Pre-trade default mode is `"advisory"` — logs only, never blocks a trade. Switch a strategy to `"blocking"` via `config/strategies.py` only after reviewing its pretrade jsonl logs.
- Adaptive Params enforces hardcoded safety bounds: no `risk_per_trade > $100`, no stops outside 4–200 ticks, no disabling risk gates, no size_mult > 3x, no edits to `account_routing.py` or live-trading flags. Rejections → `logs/ai_learner/rejected.jsonl`.
- Council is optional-filter only; `get_current_bias()` is advisory, not blocking.

## Known gaps / follow-ups

- Council trigger is currently function-call-only; no automatic wiring into session_manager's regime-shift event yet. Call from bot manually or add event hook.
- Dashboard has no Phase E–H observability panel yet (deferred to a future Phase D).
- `data/menthorq/menthorq_daily.json` still needs the daily paste — see `docs/daily_ritual.md`.
