# Phase B+ Roadmap

**Last updated:** 2026-04-25
**Owner:** Jennifer
**Test floor:** 1081 passing as of EOD 2026-04-25 (was 986 at session start)

Phase B+ is the bridge between today's working sim/prod loop and the meta-labeler / live-trading scale of Phase C. Each item below has a status, the files that own it, acceptance criteria, and known blockers.

## Summary table

| # | Item                                  | Status         | Primary files                                                                 |
|---|---------------------------------------|----------------|-------------------------------------------------------------------------------|
| 1 | ~~Chicago VPS migration~~             | **CANCELLED 2026-04-25** | `docs/chicago_vps_migration_plan.md` (deprecated, kept for reference)        |
| 2 | Risk gate skeleton                    | Skeleton shipped | `core/risk/*`, `tools/risk_gate_runner.py`, `tools/watchdog_runner.py`     |
| 3 | FinBERT module                        | **Real model on disk** | `core/sentiment_finbert.py`, `tools/bench_finbert.py`, `agents/sentiment_flow_agent.py`, `models/finbert_onnx_int8/` |
| 4 | Finnhub WS client                     | Key set, client impl pending | `core/news/finnhub_ws.py`, `.env:13` (`FINNHUB_API_KEY` set)         |
| 5 | TradingView webhook HMAC hardening    | Not started    | _pending_                                                                     |
| 6 | OIF kill-switch template              | Not started    | _pending_ (see note in section)                                              |
| 7 | FRED macros integration               | Not started    | `core/data_feeds/` (partial scaffolding)                                      |

---

## 1. ~~Chicago VPS migration~~ — CANCELLED

**Status:** **CANCELLED 2026-04-25 (Jennifer)** — Phoenix stays on the dev PC.
**Files preserved:**
- `docs/chicago_vps_migration_plan.md` — left on disk for reference only; deprecated header at top.
- `tools/verify_jsonl_continuity.py` — kept; useful for local-disk backup verification regardless of VPS plans.

**Why cancelled:** Operator decision; sub-1ms latency to CME Aurora not worth the
licensing / NT8 re-binding / Tailscale operational overhead at current scale.
The dev PC continues to host prod + sim. Re-evaluate if/when account size and
strategy count grow enough that latency is the binding constraint.

---

## 2. Risk gate skeleton

**Status:** Skeleton shipped today.
**Files touched:**
- `core/risk/risk_gate.py`
- `core/risk/risk_config.py`
- `core/risk/oif_writer.py`
- `core/risk/pipe_server.py`
- `tools/risk_gate_runner.py`
- `tools/watchdog_runner.py`
- `phoenix_bot/orchestrator/oif_writer.py` (OIFSink shim; DirectFileSink default)

**Acceptance criteria:**
- `pytest tests/test_risk_gate/` green.
- `PHOENIX_RISK_GATE=0` is the default; `base_bot.py` is unchanged.
- Manual round-trip via PowerShell pipe client verified ACCEPT/REFUSE shapes.

**Blockers / notes:**
- `base_bot` migration to actually call the gate is the next session's work.
- Roll out per-strategy (start with `bias_momentum`) once operator validation passes.

---

## 3. FinBERT module

**Status:** **Real model on disk** (Sunday 2026-04-25 — operator installed in `.venv-ml`).
**Files touched:**
- `core/sentiment_finbert.py`
- `tools/bench_finbert.py`
- `agents/sentiment_flow_agent.py`
- `models/finbert_onnx_int8/model_quantized.onnx` + tokenizer files

**Acceptance criteria:**
- `pytest tests/test_sentiment/` green.
- Council voter wired at `DEFAULT_WEIGHT = 0.0` -- observation only, does not move the council vote.
- Bench against real model: re-run `tools/bench_finbert.py` to verify p50 <= 10 ms, p99 <= 50 ms gate.

**Blockers / notes:**
- Real ONNX INT8 quantized model now resides at `models/finbert_onnx_int8/`.
- Weight tuning waits on 14 days of observation data persisted to ChromaDB.

---

## 4. Finnhub WS client

**Status:** Stub only — **API key already set** at `.env:13`.
**Files touched:**
- `core/news/finnhub_ws.py`

**Acceptance criteria:**
- Stub raises `NotImplementedError` clearly when called without an API key.
- Documented caps: 60 calls/min REST, 50-symbol WebSocket.
- Real client implements heartbeat + reconnect + back-off.

**Blockers / notes:**
- `FINNHUB_API_KEY` is set; the activation work is purely client-implementation
  (real `wss://ws.finnhub.io` socket + reconnect/backoff/dedup + wiring into
  `SentimentFlowAgent`).
- 60-call/min rate limits drive the client design.

---

## 5. TradingView webhook HMAC hardening

**Status:** Not started.
**Files touched:** _pending_ (will live in `bridge/tradingview_webhook.py`).

**Acceptance criteria:**
- HMAC-SHA256 signature verification on every inbound webhook.
- Rate limit 10 req/min per source IP.
- 24-hour replay-protection cache (nonce + ts in payload).
- Strategy-allowlist filter in the handler.

**Blockers / notes:**
- Decide whether webhook input is a Phase C signal source or a manual-override channel only.
- Next session.

---

## 6. OIF kill-switch template

**Status:** Not started (template artifact).
**Files touched:** _pending_.

**Acceptance criteria:**
- Standalone OIF template file that, when dropped into `incoming/`, cancels all working orders via NT8 ATI and closes any open position on the configured account.
- Operator runbook for "press the button" scenarios.

**Blockers / notes:**
- `KillSwitch.bat` already exists for the Phoenix bot stack (stops the local Python processes), but the OIF-level template for canceling all working orders via NT8 ATI is a separate artifact and is what's tracked here.
- `core/risk/oif_writer.py::write_killswitch` provides the writer primitive; the standalone operator template is what's still missing.
- Next session.

---

## 7. FRED macros integration

**Status:** Not started (cleanly).
**Files touched:** `core/data_feeds/` (partially scaffolded -- `api.stlouisfed.org` calls are visible in logs already).

**Acceptance criteria:**
- Daily snapshot of FED_FUNDS, CPI_YOY, UNEMPLOYMENT, T10Y2Y persisted to ChromaDB.
- Structured caching (TTL + on-disk JSON) so the council voters can read macro features as a confluence signal.
- Council wiring as the second sentiment dimension after FinBERT.

**Blockers / notes:**
- FRED API key (operator action; free).
- Ordering: lands after the Finnhub live key arrives so the two sentiment dimensions can be evaluated together.

---

## Sequencing note

TradingView HMAC hardening (item 5) and the OIF kill-switch template (item 6)
are scheduled for the next session. FRED macros (item 7) and the Finnhub WS
client implementation (item 4) come in the session after that. **Item 1
(Chicago VPS) is cancelled** -- Phoenix runs on the dev PC permanently.
