# Phoenix Strategy Oracle — Design Spec

**Status:** v1 design, awaiting build approval.
**Author:** Synthesis of 4 research files (Cowork / Claude / Gemini Deep / Gemini) + Cowork v2 + Claude critique, 2026-05-31.
**Slot:** Replaces `agents/historical_learner.py` (the 537-line 14-day JSONL weekly learner) and its supporting CLI / tests.
**Cadence:** Three modes — daily light pass, weekly batch, 5-year research (manual deep dive). Never on the trade path.

---

## 0. Structural invariants (non-negotiable)

These are not preferences. They are properties the design enforces and CI must verify.

1. **No AI gates a live trade. Ever.** The Council (4A), Pre-Trade Filter (4B), and Session Debriefer (4C) flags in `config/settings.py:213-215` stay OFF. This spec does not propose changing them.
2. **The Oracle has zero imports from the trade path.** No `bots/`, no `core/`, no `bridge/`, no `data_feeds/`. CI grep gate fails the build if it ever does.
3. **The Oracle writes only to `logs/oracle/`.** No write access to `config/strategies.py`, no write access to the warehouse, no write to anything under `bots/` or `core/`.
4. **The LLM is the narrator, never the calculator.** Every number in the final debrief is traceable to a row in `facts.json`, produced by audited Python.
5. **Every proposal is human-gated.** The Oracle stages proposals in `pending_changes.json`; Phase 4E (`agents/adaptive_params.py`) presents each one to the operator for explicit approval before any config change.

---

## 1. Hard pre-implementation blockers

These must clear before this agent is coded against production data. They are not pre-flight checks — they are pre-implementation gates.

1. **B13 commission fix deployed.** `position_manager.py:85` overstates historical P&L by ~$2.02/trade until corrected. Building parameter recommendations on uncorrected P&L means optimizing against a false foundation. EITHER fix B13 first, OR tag every warehouse row with `b13_corrected` boolean and run the Oracle only against fixed rows.
2. **`runs.friction_applied = TRUE` populated** for the run_ids being analyzed. The DuckDB warehouse schema puts this flag on `runs`, not `trades` — every P&L query must JOIN.
3. **`ANTHROPIC_API_KEY` in `.env`** at project root.
4. **`data/warehouse/phoenix.duckdb` exists** with at least 30 trades per active strategy.
5. **Pre-import sanity:** `python -c "from bots.base_bot import BaseBot; print('OK')"` passes (the `council_gate Any` import bug guard).
6. **Verify `trades.regime` is populated.** Schema has the column; if it's empty in v1 data, the §6f Time-of-Day × Regime heatmap drops from v1 scope.

---

## 2. Why this agent (and why it doesn't sit on the trade path)

The Council (4A), Pre-Trade Filter (4B), and Session Debriefer (4C) are intentionally disabled. Millisecond-critical execution is the wrong place for LLM judgment — the signal-to-noise ratio of an LLM verdict against a 3-second budget cannot beat deterministic risk gates.

The Strategy Oracle is the opposite design:

- **Slow** — batch operation; minutes-to-hours of token budget if needed.
- **Read-only** — never writes to `config/strategies.py` directly. Stages proposals only.
- **Off-line** — reads the DuckDB warehouse, not the live tick stream.
- **Stat-rigorous** — every claim cited to a number from the deterministic compute layer.

It owns one job: **mine the warehouse, propose parameter changes, hand the operator a debrief plus a queue of pending changes to approve.**

---

## 3. Three execution modes

CLI entry: `python -m agents.strategy_oracle <mode>`. Each mode binds a different window, token budget, hypothesis space, and output path.

| Mode | Trigger | Window | Token budget | Output |
|---|---|---|---|---|
| `research` | Manual only | Full warehouse (5 yr) | 200,000 | `logs/oracle/research/<date>_*` |
| `weekly` | Manual or cron (Sun 18:00 CT) | Trailing 7 days + 5yr context | 80,000 | `logs/oracle/weekly/<date>_*` |
| `daily` | Manual or cron (post-close ~16:30 CT) | Trailing 1 day | 15,000 | `logs/oracle/daily/<date>_*` |

**Mode-specific behavior:**

- **`research`** runs the full quant rigor pipeline against the entire warehouse. Generates a baseline `facts.json` and a long-form report. Used to (re-)establish ground truth and propose foundational parameter changes. Operator-triggered.
- **`weekly`** loads the last `research` baseline as memory, computes delta-vs-prior-run, surfaces new findings, runs the full gate stack against the trailing week. This is the primary cadence.
- **`daily`** is a light pass. Skips the regime stability gate (one day is too noisy for a z-test). Output is labeled explicitly "preliminary, not actionable." Cannot stage proposals — only writes findings with confidence ≤ LOW. Its job is anomaly surfacing, not decision-making.

The compute kernel is shared. Mode is a parameter on the public entry points; the compute layer reads it for window selection and the orchestrator reads it for prompt selection and tool gating.

---

## 4. Architecture — phases

```
                       Trigger (CLI: research | weekly | daily)
                                       │
                                       ▼
       ┌───────────────────────────────────────────────────────────┐
       │  PHASE 0 — PRE-FLIGHT GATES (pure Python, no LLM)          │
       │  • warehouse exists; b13/friction_applied verified          │
       │  • mode-appropriate regime stability check                  │
       │  • prior pending_changes.json reviewed (no stale pile-up)   │
       │  • CI invariant scan (no trade-path imports)                │
       │  • HALT on any failure with structured reason               │
       └───────────────────────────┬───────────────────────────────┘
                                   ▼
       ┌───────────────────────────────────────────────────────────┐
       │  PHASE 1 — DETERMINISTIC COMPUTE LAYER (pure Python)       │
       │  ← reads warehouse via prepared SELECT statements only      │
       │                                                             │
       │  Per-strategy facts (computed in code, NEVER by LLM):        │
       │  • PSR, DSR, MinTRL, profit factor, Sortino, Calmar         │
       │  • Harvey-Liu-Zhu t-stat, BHY-adjusted p (effective-N)      │
       │  • WR by regime / by hour CT / by IB regime                  │
       │  • MAE/MFE elbow distribution (long vs short separately)     │
       │  • Confluence count → WR/PF lift table                      │
       │  • Parameter proximity matrix (neighbor sensitivity)         │
       │  • IS/OOS Walk-Forward Efficiency from wfa_summary table     │
       │  • Delta vs prior facts.json (weekly/daily)                  │
       │                                                             │
       │  Output: immutable facts.json artifact + trace log           │
       └───────────────────────────┬───────────────────────────────┘
                                   ▼
       ┌───────────────────────────────────────────────────────────┐
       │  PHASE 2 — ORACLE (Claude Sonnet 4.6, single orchestrator) │
       │  ← receives facts.json + prior findings (30-day memory)     │
       │  ← cannot recompute anything                                │
       │  ← tools: think / fetch_strategy_stats / check_regime /     │
       │           write_finding / propose_change                    │
       │  ← token budget per mode                                    │
       │                                                             │
       │  Job: form hypotheses citing facts, draft narrative,        │
       │       stage parameter proposals. Nothing else.              │
       └───────────────────────────┬───────────────────────────────┘
                                   ▼
       ┌───────────────────────────────────────────────────────────┐
       │  PHASE 3 — VERIFIER (pure Python — NO LLM)                  │
       │  • every number in narrative ↔ key in facts.json            │
       │  • every macro tag ↔ row in macro_events table              │
       │  • every proposal has confidence ≥ MEDIUM and n ≥ 30        │
       │  • event-keyword-near-date scanner (look-ahead defense)     │
       │  • REJECT + log any unverifiable claim → exclude from MD    │
       └───────────────────────────┬───────────────────────────────┘
                                   ▼
       Outputs (per mode subfolder):
       • <date>_debrief.md          (operator reads)
       • <date>_facts.json          (machine record, immutable)
       • <date>_audit.jsonl         (every tool call)
       • pending_changes.json       (Phase 4E queue; shared, not per-date)
```

---

## 5. Module map

Five new files. No edits to the protected-file list. Existing `agents/historical_learner.py` is deleted as part of the change (see §17 migration).

| Module | Responsibility | Owns |
|---|---|---|
| `analytics/compute_engine.py` | All deterministic math + DuckDB I/O via prepared queries | PSR, DSR, MinTRL, HLZ t-stat, WR/PF panels, MAE/MFE elbow, IB classification, long/short splits, proximity matrix, delta-vs-prior |
| `analytics/regime_gate.py` | Pre-flight regime z-score check | Halt verdict; mode-aware (daily skips) |
| `analytics/verifier.py` | Phase 3 verifier — pure Python, no LLM | Claim-checker (number-in-facts), look-ahead event-keyword scanner, causal-language detector, proposal gate enforcement |
| `analytics/prepared_queries.py` | ~12 named SELECT statements | Read-only DuckDB access; every P&L query includes `JOIN runs USING(run_id) WHERE runs.friction_applied = TRUE` |
| `agents/strategy_oracle.py` | Orchestrator only — tools, prompt, loop | LLM session, output assembly, mode dispatch |

Supporting:

| Module | Responsibility |
|---|---|
| `tools/run_oracle.py` | Thin CLI: parses mode, invokes `agents.strategy_oracle.run(mode)`, handles exit codes |
| `tests/test_oracle.py` | Tier-1 through Tier-4 tests (§16) |

Each `analytics/*.py` file is independently importable and unit-testable without the LLM client.

---

## 6. The tool belt (LLM-facing, exactly 5 tools)

The LLM does not pick which SQL to run. The compute layer pre-computes the full per-strategy panel into `facts.json`. A compact summary table (n_trades, DSR, PSR, all-gates-pass flag, top failed gate) is **injected inline in the user message at run start**; full per-strategy detail is **fetched on demand** via the dict-lookup tool below. This minimizes inline token usage while keeping detail accessible.

| Tool | Purpose | Side effects |
|---|---|---|
| `think(reasoning: str)` | Required before any state-changing call. No-op pass-through that forces structured reasoning into the audit log. | None |
| `fetch_strategy_stats(strategy: str)` | Returns the full pre-computed panel for one strategy from `facts.json`. Pure dict lookup. Returns error if strategy not in panel. | None |
| `check_regime()` | Returns the pre-computed regime stability verdict. Mirrors the pre-flight gate; exposed so the LLM can defensively re-check before staging proposals. | None |
| `write_finding(...)` | Append a structured finding to `facts.json` `findings[]`. Blocks `n < 30` and `verdict = FAILED`. Returns finding_id. | Append to JSON |
| `propose_change(...)` | Stage a parameter proposal in `pending_changes.json`. Requires `confidence ∈ {MEDIUM, HIGH}` AND a `finding_id` linking back to evidence. **Rejected in `daily` mode** (daily produces only findings, never proposals). Never touches `config/strategies.py`. | Append to pending_changes |

**No raw `duckdb_query` tool.** Per the Spider 2.0 benchmark (GPT-4o ≈ 10% accuracy on enterprise schemas), letting the LLM author SQL against real schemas is the dominant failure mode. All SQL lives in `analytics/prepared_queries.py`. If a hypothesis genuinely requires a query not in the library, the agent reports it as an open question for the operator rather than fabricating SQL.

**Two deterministic checks run automatically in pre-flight, not as LLM tools:**

- `check_lookahead(window)` — runs in Python. Result is injected into the prompt as context. If the analysis window overlaps the LLM's training cutoff, interpretive findings get confidence downgraded one tier; fact-transcription statements are not penalized.
- The Phase 3 verifier scans the LLM's debrief for **event-keywords near dates** (e.g., "crash," "rally," "Fed pivot," "FOMC" within ±10 tokens of `YYYY-MM`). Bare date references in fact transcription (e.g., transcribing `session_date` values) are not penalized. Hits are rejected from the published debrief.

---

## 7. Statistical rigor gates

### 7a. Confidence tiers (sample size + WFA agreement)

| Sample size | Tier | Decisions allowed |
|---|---|---|
| n < 30 | **INSUFFICIENT** | Log gap only; no finding written |
| 30 ≤ n < 100 | **LOW** | Finding written, no proposal staged |
| 100 ≤ n < 200 | **MEDIUM** *if* WFA agrees | Proposal eligible for staging |
| n ≥ 200 | **HIGH** eligible | Proposal eligible for staging at HIGH |

WFA agreement = `wfa_summary.mean_oos_pf >= 0.6 * mean_is_pf` for that strategy. If WFA disagrees, confidence is bumped down one tier regardless of n.

### 7b. Per-proposal gates (ALL must pass before staging)

| Gate | Threshold | Source |
|---|---|---|
| **Deflated Sharpe Ratio** | DSR ≥ 0.90 for LUCK floor; DSR ≥ 0.95 to stage proposal | Bailey & López de Prado 2014 |
| **Probabilistic Sharpe** | PSR ≥ 0.90 (skew/kurtosis adjusted for MNQ returns) | Bailey & López de Prado 2012 |
| **Multiple-testing** | BHY-adjusted p ≤ 0.05 with **effective N** (cluster-corrected, not raw strategy count) | Harvey-Liu-Zhu 2016 |
| **MinTRL** | `n_trades ≥ compute_min_trl(returns, target_sr=1.0)` | Bailey & López de Prado 2012 |
| **Walk-Forward Efficiency** | `mean_oos_pf ≥ 0.6 * mean_is_pf` from `wfa_summary` | Pardo |
| **Parameter proximity** | Proposed value sits on a flat plateau (±1 step within 10% of metric variation) | Gemini |
| **Regime stability** | `|z_score|` of latest month sharpe vs trailing 6-mo baseline ≤ 1.5 | Cowork |
| **Look-ahead** | Interpretive claim → confidence -1 tier; fact transcription → no penalty | Sarkar & Vafa 2024 |
| **Causal restraint** | No causal claims about macro events — only conditional associations with n and CI | Goldsmith-Pinkham & Lyu 2025 |

A proposal that fails ANY gate gets logged as a finding but does NOT get staged. The debrief explains which gate failed.

### 7c. Effective N for BHY adjustment

Trial count is **not** the count of strategies. It is the count of distinct hypotheses tested across the run, with correlated trials clustered. For each strategy, count distinct parameter variations explored × distinct splits (regime / IB / hour-bucket). Apply Optimal Number of Clusters (per Bailey/LdP) to collapse highly-correlated trials (e.g. overlapping MA windows count as ~1 effective trial, not N). `effective_N` is computed by `analytics/compute_engine.py` and reported in `facts.json`.

---

## 8. Analysis targets (the compute layer pre-computes; the Oracle reads)

### 8a. Long vs short asymmetry

Separate stats for every metric. The Oracle **MAY NOT propose a long parameter for a short setup or vice versa.** Two independent recommendation channels per strategy. Enforced by `propose_change` requiring `direction ∈ {LONG, SHORT}` and the verifier checking that proposed parameter scopes match.

### 8b. MAE/MFE elbow analysis

For each strategy × direction, plot the distribution of `trades.mae_ticks` and `trades.mfe_ticks` (columns already exist). Find the elbow where widening the stop no longer increases WR. Propose `stop_atr_mult` adjustments only when the elbow is statistically distinct from current setting (proximity gate must clear).

### 8c. Initial Balance regime classification

Classify each session by IB width vs 20-day ATR median: **Narrow / Normal / Wide**. Recompute every metric conditional on IB regime. Flag strategies whose edge collapses outside their IB-favorable bucket.

### 8d. NQ/ES intermarket spread — **Phase 2, deferred**

Requires ES data in the warehouse. Currently MNQ-only. Spec flags this section but does not implement it in v1.

### 8e. Confluence lift table

Carried forward from the existing `historical_learner.py` analysis. WR by confluence count, surfacing the optimal threshold per strategy.

### 8f. Time-of-Day × Regime heatmap — **gated on data**

24×8 heatmap (CT hour × regime bucket). Propose session-window changes only where the cell has n ≥ 30 AND BHY-adjusted significance.

**Implementation gate:** Requires `trades.regime` column to be populated. The pre-flight checks this. If the column is empty, §8f drops from v1 scope and the spec footnotes it as Phase 2.

---

## 9. Look-ahead defense (refined)

**Problem:** Claude Sonnet 4.6's training cutoff is in 2025. The LLM has *memorized* how MNQ behaved during much of the 5-year analysis window. Naïve judgments about that period are contaminated.

**Defense (three layers):**

1. **`check_lookahead(window)` runs in pre-flight.** Output goes into the prompt as context.
2. **Confidence downgrade is selective, not blanket.** The verifier classifies each finding as TRANSCRIPTION or INTERPRETATION: a finding is **TRANSCRIPTION** if every numeric claim in its rationale appears in `facts.json` (key+value match within rounding). Otherwise it is **INTERPRETATION**. Apply -1 tier only to INTERPRETATION findings when the analysis window overlaps the training cutoff. Pure transcription ("DSR = 0.97, n = 250, BHY-p = 0.001") gets no penalty. Without this distinction, every finding gets downgraded forever and the rigor framework becomes noise.
3. **Phase 3 verifier scans for event-keywords near dates.** Reject narratives that say "the market crashed in October 2022" or "rallied after the Fed pivot." Bare date mentions tied to `session_date` columns are fine.

This single guardrail is the difference between an analyst and an expensive autocomplete.

---

## 10. Macro layer — Phase 2, deferred

Valuable but not in v1 scope. Stage when ready:

- **FRED API** → daily VIX regime classification, CPI/NFP/FOMC date stamps
- **FedWatch** → implied Fed Funds path for backward-tagging trade dates
- **USMPD** (SF Fed) → 30-min/70-min event windows for monetary surprises

When added: every trade gets joined to a deterministic `macro_events` table. The Oracle reports "this strategy's losses cluster on CPI-release days" as a **conditional association with n and CI**, not as causation. The LLM is explicitly prohibited from causation; the verifier's causal-language detector enforces it.

---

## 11. Cost routing

- **Compute layer** — pure Python, $0 in tokens.
- **Verifier** — pure Python, $0 in tokens. **No Haiku.** Adding an LLM to the verifier reintroduces non-determinism into the layer whose entire purpose is determinism.
- **Oracle (synthesis)** — Sonnet 4.6 with adaptive thinking.

Estimated cost per run by mode:

| Mode | Token budget | Approx cost |
|---|---|---|
| `research` | 200,000 | $3–$6 |
| `weekly` | 80,000 | $0.50–$2 |
| `daily` | 15,000 | $0.10–$0.30 |

Annual estimate at default cadence (weekly + daily Mon–Fri + research quarterly): ~$200/yr.

---

## 12. Output schema

### 12a. Directory layout

```
logs/oracle/
├── research/
│   ├── 2026-05-31_debrief.md
│   ├── 2026-05-31_facts.json
│   └── 2026-05-31_audit.jsonl
├── weekly/
│   ├── 2026-05-31_debrief.md
│   ├── 2026-05-31_facts.json
│   └── 2026-05-31_audit.jsonl
├── daily/
│   ├── 2026-05-31_debrief.md
│   ├── 2026-05-31_facts.json
│   └── 2026-05-31_audit.jsonl
└── pending_changes.json    (shared, append-only queue for Phase 4E)
```

### 12b. Debrief format (operator-readable)

```
# Phoenix Strategy Oracle — Weekly Debrief
## Week of 2026-05-25 → 2026-05-31

## TL;DR
3 proposals staged, 2 strategies flagged for watch, 1 regime warning, 4 findings recorded.

## Delta vs Last Run
- bias_momentum DSR moved 0.71 → 0.76 (+0.05)
- footprint_cvd_reversal dropped from MEDIUM watch to LOW (PF decay continued)
- 1 prior proposal (orb_fade.entry_delay) was operator-rejected; not re-staged
- 0 new proposals in scope this week (vs 2 last week)

## Proposals (review pending_changes.json before approving)
1. bias_momentum.session_end_time: 09:45 → 09:15
   Why: WR drops from 52.1% (n=87) to 38.9% (n=35) after 09:15 CT.
   Confidence: MEDIUM (n=35 post-cutoff is borderline).
   DSR=0.71, BHY-adjusted p=0.018, WFE pass.

## Watch List
- footprint_cvd_reversal: profit factor decay (3.1 → 1.6 over 4 weeks). Not actioning.

## Report Card (always produced, even when no proposals stage)
- 15 strategies analyzed
- 3 cleared all gates → proposals
- 7 failed sample-size floor (collect 4-8 more weeks)
- 3 failed DSR gate (luck-floor 0.90)
- 2 failed WFA agreement (OOS PF < 60% of IS PF)

## Regime
Stable (z=0.42 vs 1.5 threshold). Analysis proceeded normally.

## Look-Ahead Note
Analysis window 2021-06-01 → 2026-05-31 overlaps Claude's training cutoff.
Interpretive findings downgraded one tier. Fact transcription unaffected.
```

### 12c. facts.json (machine record)

Every number in the debrief MUST appear here. Verifier enforces. Structure:

```json
{
  "run_mode": "weekly",
  "run_date": "2026-05-31",
  "window_start": "2026-05-24",
  "window_end": "2026-05-30",
  "regime": {"stable": true, "z_score": 0.41, "warning": null},
  "n_trials_effective": 28,
  "strategies": {
    "bias_momentum": {
      "metrics": {
        "n_trades": 122, "psr": 0.93, "dsr": 0.76, "min_trl": 87,
        "hlz_t_stat": 3.4, "bhy_p_adjusted": 0.018,
        "profit_factor": 1.82, "sortino": 1.41, "calmar": 0.93,
        "max_drawdown_dollars": -1840.5,
        "oos_pf": 1.62, "is_pf": 2.21, "wfe_ratio": 0.73, "wfa_pass": true
      },
      "gates": {"all_pass": false, "failed": ["dsr_0_95"]},
      "splits": {
        "by_hour_ct": {...},
        "by_regime": {...},
        "by_ib_regime": {...},
        "long_vs_short": {...},
        "mae_mfe_elbow": {...}
      }
    },
    ...
  },
  "findings": [...],
  "prior_findings_loaded": [...],
  "delta_vs_prior": {...}
}
```

### 12d. pending_changes.json (Phase 4E queue)

The `current_value` field is populated by the compute layer reading `config/strategies.py` via a read-only AST parse at run start. The Oracle never writes to `config/strategies.py` and never imports it as a Python module (avoids any side effects from imports).


```json
{
  "pending": [
    {
      "proposed_at": "2026-05-31",
      "run_mode": "weekly",
      "strategy": "bias_momentum",
      "direction": "BOTH",
      "parameter_name": "session_end_time",
      "current_value": "09:45",
      "proposed_value": "09:15",
      "rationale": "...",
      "confidence": "MEDIUM",
      "sample_size": 87,
      "finding_id": "bias_momentum_post_915_decay_2026-05-31",
      "metrics": {"dsr": 0.71, "bhy_p": 0.018, "wfe": 0.73},
      "status": "PENDING_HUMAN_REVIEW",
      "approved": false,
      "applied": false
    }
  ]
}
```

### 12e. audit.jsonl

One line per tool call: `{ts, tool, input, result_summary, mode, run_id}`. R-Level reproducibility per Gemini Deep.

---

## 13. Prior-findings memory load

At start of `weekly` and `daily` runs, the compute layer loads all findings from the last 30 days (across modes), filtering expired ones (`expires_after_days` honored). These are injected into the LLM prompt as context with the rule: **do not re-investigate findings marked `still_valid=true` in the last 14 days unless a key metric materially changed**.

"Materially changed" = a relevant metric in `facts.json` moved by more than the change threshold:
- DSR / PSR: ±0.05
- WR: ±3 percentage points
- Profit factor: ±0.20
- Sample size: +30 trades (n grew enough for a new tier check)
- Regime z-score: crossed the 1.5 halt threshold

The compute layer flags which prior findings are "materially changed" in the prompt context; the LLM may re-investigate those without violating the rule.

`research` mode skips the memory load — it rebuilds ground truth.

---

## 14. Report card mode (always produce output)

If no proposals clear gates AND regime is stable, the debrief still includes a **Report Card** section explaining what was analyzed, what failed which gates, and what additional data is needed to re-evaluate. Silent halts hide useful information.

If regime is unstable, the debrief includes ONLY the Report Card + regime warning — no proposals, no interpretive findings.

---

## 15. Pre-flight checklist (every run)

- [ ] `data/warehouse/phoenix.duckdb` exists and has data
- [ ] B13 commission fix confirmed deployed (or window explicitly post-fix)
- [ ] `runs.friction_applied = TRUE` exists for the run_ids being analyzed
- [ ] `trades.regime` populated (if §8f shipping in this run)
- [ ] `logs/oracle/<mode>/` directory exists
- [ ] `.env` has `ANTHROPIC_API_KEY`
- [ ] Prior `pending_changes.json` reviewed (no unapproved pile-up >14 days old)
- [ ] All Tier-1 unit tests pass (`pytest tests/test_oracle.py -v`)
- [ ] `python -c "from bots.base_bot import BaseBot; print('OK')"` passes
- [ ] CI invariant scan: `strategy_oracle.py` has no trade-path imports

---

## 16. Test tiers

### Tier 1 — Unit tests (must all pass before any real run)

- Rejects unknown prepared-statement name
- Rejects proposal with `confidence ∈ {LOW, INSUFFICIENT}`
- Rejects proposal with `n < 30`
- Verifier rejects narrative claim that has no matching `facts.json` key
- Verifier rejects event-keyword-near-date (e.g., "crash" within ±10 tokens of `2022-10`)
- Verifier rejects causal language ("because the Fed", "due to FOMC")
- `compute_dsr` matches published Bailey-LdP golden number
- `compute_psr` matches published Bailey-LdP golden number
- `compute_min_trl` matches published Bailey-LdP golden number
- `check_lookahead` correctly flags an in-window date range
- `effective_N` clusters correlated trials (synthetic test with 5 overlapping MA windows → N_eff ≈ 1)

### Tier 2 — Golden dataset queries

A short list of facts the operator has already verified manually from the warehouse (total trades by strategy, overall WR, best/worst month). The Oracle must reproduce these to the cent. If it doesn't, the warehouse plumbing has drifted.

### Tier 3 — Consistency check

Run the same week 3 times. ≥2 of 3 must surface the same top finding. If proposals flip direction across runs, hypotheses are under-constrained — tighten the prompt before going live.

### Tier 4 — Adversarial

Inject a synthetic pre-computed fact `{strategy: "X", dsr: 0.99, n: 10}`. Verify:
- The Oracle does NOT stage a proposal (n<30 hard gate).
- The verifier REJECTS any narrative mentioning the synthetic finding.
- The audit log records the rejection with reason.

---

## 17. Migration from existing `agents/historical_learner.py`

The existing 537-line file is deleted as part of this change. Two callers must be updated:

| Caller | Current | New |
|---|---|---|
| `tests/test_learner.py` | `from agents import historical_learner as hl` | Replaced by `tests/test_oracle.py` (Tier 1-4) |
| `tools/run_weekly_learner.py` | `from agents.historical_learner import run_weekly_learner` | Replaced by `tools/run_oracle.py` with `--mode weekly` |

`agents/adaptive_params.py` currently reads `logs/ai_learner/pending_recommendations.json`. The Oracle writes to `logs/oracle/pending_changes.json`. Migration: update `adaptive_params.py` to read the new path. This file is not in the protected list. Schema changes: `pending_recommendations` becomes `pending` (key rename), and the per-item schema gains `metrics`, `run_mode`, `direction` fields (existing fields preserved).

Operator messaging: the existing `logs/ai_learner/` folder is preserved untouched as a historical record. New outputs go to `logs/oracle/`.

---

## 18. Risk register

| Risk | Mitigation |
|---|---|
| LLM hallucinates a column name | All queries prepared, no LLM-authored SQL |
| LLM invents a P&L number | Phase 3 verifier rejects any narrative number not in `facts.json` |
| LLM claims causation from correlation | System prompt forbids causal language; verifier flags it |
| Sample size too small surfaces as finding | `write_finding` blocks n<30 |
| Multi-testing inflation across many hypotheses | BHY adjustment with effective-N clustering |
| Look-ahead memorization contamination | `check_lookahead` + selective tier downgrade + event-keyword-near-date scanner |
| Proposal applied without review | Oracle never edits `config/strategies.py`; Phase 4E requires human approval |
| Token runaway | Hard cap per mode, agent wraps gracefully at limit |
| Pre-B13 P&L misleading | Pre-flight blocker; system prompt warned |
| Regime transition tuning trap | Regime z-score halt for `research` + `weekly` modes |
| Audit gap | Every tool call logged to `audit.jsonl` with full trace |
| Verifier as LLM reintroduces non-determinism | Verifier is pure Python only (explicit non-goal: Haiku) |
| Oracle drifts onto trade path | CI invariant scan: grep fails build on any trade-path import |
| Operator gets nothing when no proposals clear | Report Card mode always produces useful output |

---

## 19. Open questions for the operator

1. **B13 commission fix status.** Is it deployed? If not, this design cannot ship to production data; build against a `b13_corrected` filter or fix B13 first.
2. **`trades.regime` column populated?** If empty, §8f drops from v1. Spec assumes yes for now.
3. **Cron vs manual for daily/weekly modes.** Spec assumes manual for v1 (safer). Cron via Windows Task Scheduler can be wired later.
4. **adaptive_params.py path migration timing.** Update at the same time as Oracle ships, or one release later?
5. **Should `research` mode auto-save its baseline as the "current" facts.json for delta computation in weekly runs?** Recommendation: yes, with explicit `--save-baseline` flag (default ON).

---

## 20. Build order

1. **Spec approval** (this document)
2. `analytics/prepared_queries.py` — write the ~12 named SELECT statements; unit-test each against a synthetic DuckDB
3. `analytics/compute_engine.py` — DSR/PSR/MinTRL/HLZ + per-strategy panel + effective-N
4. `analytics/regime_gate.py` — z-score check, mode-aware
5. `analytics/verifier.py` — claim-checker + look-ahead scanner + causal-language detector
6. `agents/strategy_oracle.py` — orchestrator, tools, prompts, mode dispatch
7. `tools/run_oracle.py` — CLI entry
8. `tests/test_oracle.py` — Tier 1-4 tests
9. Migration: delete `agents/historical_learner.py`, update `tests/test_learner.py` → `tests/test_oracle.py`, update `tools/run_weekly_learner.py` → `tools/run_oracle.py`, update `agents/adaptive_params.py` to read new path
10. Dry run against warehouse in report-only mode (`propose_change` returns success but writes to `pending_changes_dryrun.json`)
11. Enable proposals; first live weekly run

Total new code estimate: ~1100-1400 lines across the five files + tests.

---

## 21. What's explicitly out of v1 scope

- NQ/ES intermarket spread analysis (§8d) — Phase 2
- Macro layer (§10) — Phase 2
- §8f Time-of-Day × Regime heatmap — gated on `trades.regime` populated
- Multi-instrument analysis (MNQ only)
- Council integration (Oracle stands alone; council remains OFF)
- Cron scheduling (manual triggers for v1)
- LLM-based verifier (Phase 3 verifier is pure Python; non-goal)

---

*End of spec.*
