# Phoenix Bot — Project Prompt (operator-facing architecture notes)

This document captures operator-facing architecture decisions that
don't fit in CLAUDE.md (developer reference) or BUILD_MAP.md (roadmap).
Edit inline when behavior changes.

---

## Daily Flatten Architecture (B84, 2026-04-22)

Phoenix uses a defense-in-depth schedule for end-of-day position closure
— the bot does the primary work; NT8 catches stragglers; CME enforces
the hard floor. All times are America/Chicago.

| Time     | Layer                             | Implemented in                                  |
|----------|-----------------------------------|-------------------------------------------------|
| 15:53 CT | Phoenix stops accepting NEW entries | `BaseBot._is_no_new_entries_window` guard fires at the top of `_enter_trade` |
| 15:54 CT | Phoenix `DailyFlattener` (PRIMARY) | `bots/daily_flatten.py` + `BaseBot._daily_flatten_loop` |
| 15:54:45 | Phoenix logs WARN if anything still open | `BaseBot._emit_grace_end_warn_if_open` |
| 15:55 CT | NT8 Auto Close Position (SAFETY NET) | **NT8 GUI — not Python**: Tools → Settings → Trading → Auto Close Position = 03:55:00 PM, All Instruments, platform timezone confirmed Central Time |
| 16:00 CT | CME globex 1-hour maintenance break (HARD FLOOR) | Exchange-side; no bot code |
| 17:00 CT | Globex reopens — new-entries gate lifts | `BaseBot._is_no_new_entries_window` returns False again |

### Source of truth

The Phoenix-side timings are configured by constants in `config/settings.py`:

```python
DAILY_FLATTEN_HOUR_CT        = 15
DAILY_FLATTEN_MINUTE_CT      = 54
NO_NEW_ENTRIES_HOUR_CT       = 15
NO_NEW_ENTRIES_MINUTE_CT     = 53
FILL_CONFIRMATION_GRACE_SECONDS = 45
```

`DailyFlattener` reads these as defaults via `_default_flatten_hour()` /
`_default_flatten_minute()`. Changing one constant moves the whole
system — do not hard-code times elsewhere.

### Strategy-level managed exits (interact with but don't replace the flatten)

Some strategies run their own managed-exit logic tied to cash-equity
session boundaries. These are expressed in **Eastern Time** and must
resolve to a CT time ≤ 15:54 CT so the bot flatten stays the primary:

| Strategy   | Mode     | `eod_flat_time_et` | CT equivalent |
|------------|----------|---------------------|----------------|
| noise_area | lab/sim  | `"16:54"` (B84 aligned)  | 15:54 CT       |
| noise_area | prod     | `"10:55"` (90-min window) | 09:55 CT       |
| ORB        | lab/sim  | `"16:54"` (B84 aligned)  | 15:54 CT       |
| ORB        | prod     | `"10:55"` (90-min window) | 09:55 CT       |

The prod 90-min-window values are deliberately earlier than the bot
flatten — prod strategies self-exit by 09:55 CT, and the 15:54 CT bot
flatten catches anything unexpectedly still open.

### NT8 GUI configuration reference

Operator must verify once per NT8 install / profile migration:

- **NT8 → Tools → Options → Trading → Auto Close Position**
  - Enabled: **Yes**
  - Time: **03:55:00 PM**
  - Instruments: **All instruments**
  - NT8 platform timezone: **Central Time** (verify via Tools → Options → General — offset should match local CDT/CST)

### Restart required for code changes to take effect

Changes to `DAILY_FLATTEN_HOUR_CT` / `_MINUTE_CT` / `NO_NEW_ENTRIES_*`
live in `config/settings.py`. **Running bot processes cache these at
import time** — a change requires restarting `sim_bot.py` (and
`prod_bot.py` if it's running positions) to take effect. The
`DailyFlattener` instance is created once per bot process.

---

## History log — `session_close` event (B84)

Emitted once at the 15:54 CT flatten by `HistoryLogger.log_session_close_event()`.
One line per day, in `logs/history/YYYY-MM-DD_<bot>.jsonl`. Fields:

- `event: "session_close"`
- `ts`: tz-aware CT ISO timestamp of the flatten moment
- `flattened_trade_ids`: list of trade_ids the bot closed itself
- `still_open_trade_ids`: list of trade_ids handed off to NT8 safety net
- `flattened_count`, `still_open_count`: integer counts
- `session_pnl`: today's P&L in dollars (best-effort until B13 ships)
- `b13_commission_applied`: bool — flags whether commission math has been
  corrected per B13 or is still best-effort gross
- `note`: string — "B13 commission math pending …" when b13 is False,
  else null

Consumers: AI debrief, daily recap, forensic review of days where NT8
Auto Close fires (`still_open_count > 0` means the bot missed some).

---

## Phase B+ in progress (2026-04-25)

Six tactical fixes from 2026-04-24 (Epic v1) are locked in via dedicated regression tests under `tests/test_lock_in_epic_v1/`:

- **Fix A** — ORB ATR-adaptive max_or_size: `min(max(80pt, ATR×4), 150pt)` ([config/strategies.py:184-188](config/strategies.py:184), [strategies/orb.py:80-95](strategies/orb.py:80))
- **Fix B** — bias_momentum VCR threshold 1.5→1.2, close-pos 0.65/0.35 ([config/strategies.py:83-89](config/strategies.py:83))
- **Fix C** — noise_area silent cadence + band_mult 1.0→0.7 ([config/strategies.py:200-205](config/strategies.py:200), [strategies/noise_area.py:171-195](strategies/noise_area.py:171))
- **Fix D** — ib_breakout ib_minutes 30→10 ([config/strategies.py:163-168](config/strategies.py:163))
- **Fix E** — compression_breakout min_squeeze_bars 5→12 ([config/strategies.py:230-234](config/strategies.py:230))
- **Fix F** — spring_setup retired (`enabled: False`) ([config/strategies.py:84-92](config/strategies.py:84))

### New Phase B+ infrastructure (Section deliverables 2026-04-25)

| Component | File(s) | Status | Notes |
|---|---|---|---|
| **Multi-stream detector** | [core/bridge/stream_validator.py](core/bridge/stream_validator.py), [config/instrument_price_bands.yaml](config/instrument_price_bands.yaml) | LIVE — wired into bridge fanout | Quarantines NT8 clients that violate static band, peer MAD, or tick grid |
| **Live monitor tool** | [tools/nt8_stream_quarantine.py](tools/nt8_stream_quarantine.py) | Live | `--watch` for 1-Hz table; `--once` for client count |
| **Recovery doc** | [docs/nt8_multi_stream_recovery.md](docs/nt8_multi_stream_recovery.md) | New | Operator runbook for the 2026-04-24 multi-stream class of failure |
| **Grading harness** | [tools/grade_open_predictions.py](tools/grade_open_predictions.py) + [tools/graders/](tools/graders/) + [tools/log_parsers/sim_bot_log.py](tools/log_parsers/sim_bot_log.py) | Live | 6 graders P1-P6, JSON+MD+HTML output |
| **Grading scheduled task** | [scripts/register_phoenix_grading_task.ps1](scripts/register_phoenix_grading_task.ps1) | Ready (run as admin) | 16:00 CT Mon-Fri |
| **Risk gate skeleton** | [core/risk/risk_gate.py](core/risk/risk_gate.py), [core/risk/risk_config.py](core/risk/risk_config.py), [core/risk/oif_writer.py](core/risk/oif_writer.py), [core/risk/pipe_server.py](core/risk/pipe_server.py) | Skeleton — NOT in default path | Default `PHOENIX_RISK_GATE=0`. base_bot.py unchanged. |
| **OIFSink shim** | [phoenix_bot/orchestrator/oif_writer.py](phoenix_bot/orchestrator/oif_writer.py) | Skeleton | DirectFileSink default; RiskGateSink behind env flag |
| **Risk gate runners** | [tools/risk_gate_runner.py](tools/risk_gate_runner.py), [tools/watchdog_runner.py](tools/watchdog_runner.py) | Ready | Manual-launch only today |
| **FinBERT sentiment** | [core/sentiment_finbert.py](core/sentiment_finbert.py), [models/finbert_onnx_int8/](models/finbert_onnx_int8/) | Live (observation mode) | INT8 ONNX, p50=4.5ms p99=6.85ms |
| **Sentiment council agent** | [phoenix_bot/council/sentiment_flow_agent.py](phoenix_bot/council/sentiment_flow_agent.py) | Wired with `weight=0.0` | Persists to ChromaDB; doesn't change council vote |
| **Finnhub WS stub** | [core/news/finnhub_ws.py](core/news/finnhub_ws.py) | Stub only | No live API key; `FINNHUB_API_KEY` env documented |
| **Chicago VPS plan** | [docs/chicago_vps_migration_plan.md](docs/chicago_vps_migration_plan.md), [tools/verify_jsonl_continuity.py](tools/verify_jsonl_continuity.py) | Plan only — no migration | QuantVPS Chicago Pro recommended; Aurora not NJ |

See [docs/phase_b_plus_roadmap.md](docs/phase_b_plus_roadmap.md) for status of every Phase B+ item.

### Test counts (2026-04-25 EOD)

- Baseline (pre-Section work): **986 passed**
- After Sections 1-6 land: **1081 passed, 4 skipped, 0 failed** (+95 new tests)

---

## Phase B+ in progress (started 2026-04-25)

**Date:** 2026-04-25
**What shipped today:** Six locked-in tactical strategy fixes (A-F) plus six new infrastructure skeletons (stream validator, risk gate, FinBERT sentiment, VPS migration plan, skills digest, grading dashboard tabs). Defaults remain SAFE -- no flag flips today.

### Six locked-in fixes (A-F)

- **A** -- ORB ATR-adaptive `max_or_size`: `min(max(80pt, ATR x 4), 150pt)` replaces the static cap.
- **B** -- bias_momentum: VCR threshold 1.5 -> 1.2, close-pos thresholds 0.65/0.35, SHORT mirror added.
- **C** -- noise_area: silent cadence on suppressed entries; `band_mult` 1.0 -> 0.7.
- **D** -- ib_breakout: `ib_minutes` 30 -> 10.
- **E** -- compression_breakout: `min_squeeze_bars` 5 -> 12.
- **F** -- spring_setup retired (`enabled: False`).

### New modules and tools delivered today

- `core/bridge/stream_validator.py` + `tools/nt8_stream_quarantine.py` -- multi-stream detector (default OFF behind `PHOENIX_STREAM_VALIDATOR=1`).
- `core/risk/risk_gate.py` + `tools/risk_gate_runner.py` + `tools/watchdog_runner.py` -- fail-closed risk gate skeleton (`PHOENIX_RISK_GATE=0` default; not yet migrated).
- `core/sentiment_finbert.py` + `agents/sentiment_flow_agent.py` -- FinBERT skeleton + Council voter at weight=0 (observation only).
- `tools/verify_jsonl_continuity.py` + `docs/chicago_vps_migration_plan.md` -- VPS migration plan (NOT executed).
- `tools/skills_digest.py` + `SKILLS.md` -- auto-generated skills digest now wired into SessionStart hook.
- `tools/grade_open_predictions.py` (yesterday) + the dashboard's new Grades + Logs tabs.

### Defaults kept SAFE

- `PHOENIX_RISK_GATE=0` -- DirectFileSink remains the default OIF writer; base_bot.py is unchanged.
- `PHOENIX_STREAM_VALIDATOR=0` -- bridge fanout behavior unchanged in production.
- Migration to enable the gate is a future session (operator validation pass required first).

