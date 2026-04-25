# Phase B+ Log

A simple chronological log of Phase B+ work as it happens. Newest entries go on top.

## 2026-04-25 -- Skeleton sprint day

Six skeleton items shipped today; defaults remain SAFE.

- **Multi-stream detector** -- `core/bridge/stream_validator.py` + `tools/nt8_stream_quarantine.py` deliver a price-band / peer-MAD / tick-grid validator. Default OFF behind `PHOENIX_STREAM_VALIDATOR=1`.
- **Fail-closed risk gate** -- `core/risk/risk_gate.py` + `tools/risk_gate_runner.py` + `tools/watchdog_runner.py` ship the named-pipe gate, OIFSink shim, atomic OIF writer, and heartbeat watchdog. Default `PHOENIX_RISK_GATE=0`; `base_bot.py` is unchanged.
- **FinBERT sentiment skeleton** -- `core/sentiment_finbert.py` + `agents/sentiment_flow_agent.py`. Council voter wired at `DEFAULT_WEIGHT = 0.0` (observation only, persists to ChromaDB).
- **Chicago VPS migration plan** -- `docs/chicago_vps_migration_plan.md` + `tools/verify_jsonl_continuity.py`. Plan-only; no infra changes performed.
- **SKILLS auto-digest** -- `tools/skills_digest.py` generates `SKILLS.md` and is now wired into the SessionStart hook so each new Claude session starts with a fresh inventory.
- **Dashboard Grades + Logs tabs** -- new tabs surface the output of yesterday's `tools/grade_open_predictions.py` grading harness directly in the Flask dashboard.

Supporting fixes:

- `.gitignore` updated so the new `models/finbert_onnx_int8/` directory and `logs/risk_gate/*.jsonl` heartbeats are not tracked.

Locked-in tactical strategy fixes (A-F) from 2026-04-24 also got their dedicated regression tests committed under `tests/test_lock_in_epic_v1/`. Test count went from 986 -> 1081 passed.
