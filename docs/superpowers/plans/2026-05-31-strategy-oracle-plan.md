# Phoenix Strategy Oracle — Implementation Plan

**Companion to:** `docs/superpowers/specs/2026-05-31-strategy-oracle-design.md`
**Branch:** `weekly-evolution/2026-05-24` (or feature branch off it)
**Execution model:** Sequential tasks, one subagent per task, two-stage review per task (spec compliance, then code quality).

---

## Architectural recap (for subagents — full detail in the spec)

Five new files implement the Phoenix Strategy Oracle:

```
analytics/
├── __init__.py            (empty marker)
├── prepared_queries.py    (~12 named SELECT statements; friction_applied enforced)
├── compute_engine.py      (PSR / DSR / MinTRL / HLZ / effective N / strategy panel / delta)
├── regime_gate.py         (z-score halt gate; mode-aware)
└── verifier.py            (Phase 3 pure-Python claim checker + look-ahead/causal scanners)
agents/
└── strategy_oracle.py     (orchestrator + 5 LLM tools + 3 mode dispatch)
tools/
└── run_oracle.py          (CLI entry: `python -m tools.run_oracle <mode>`)
tests/
└── test_oracle.py         (Tier 1-4 tests)
```

**Replaces:** `agents/historical_learner.py` (deleted in Task 8 migration).
**Working directory:** `C:\Trading Project\phoenix_bot`.
**Python version:** Whatever the existing project uses (check `Python/` or `.python-version`).

**Invariants every task must honor:**

1. No imports from `bots/`, `core/`, `bridge/`, or `data_feeds/` in any new file.
2. `analytics/prepared_queries.py` is the ONLY module that opens DuckDB; everything else calls into it.
3. The verifier is pure Python — no LLM calls.
4. Every P&L query JOINs `runs` and filters `runs.friction_applied = TRUE`.
5. `agents/strategy_oracle.py` writes only to `logs/oracle/`; never to `config/strategies.py` or the warehouse.
6. No `print()` of non-ASCII to stdout (Windows cp1252 trap — operator memory says so). Logging is fine.

---

## Task 1 — `analytics/prepared_queries.py` (foundation)

**Goal:** A library of ~12 named, parameterized SELECT functions against the DuckDB warehouse. No LLM-authored SQL exists in this codebase after this task.

**Files:**
- CREATE `analytics/__init__.py` (empty)
- CREATE `analytics/prepared_queries.py`
- CREATE `tests/test_prepared_queries.py`

**TDD:** Write `tests/test_prepared_queries.py` first using a synthetic in-memory DuckDB with the schema from `tools/warehouse/schema.sql`. Tests must fail. Then implement queries until tests pass.

**Required queries (function names + return shape):**

```python
WAREHOUSE_PATH = r"C:\Trading Project\phoenix_bot\data\warehouse\phoenix.duckdb"

# Connection helper — always read-only
def open_conn(path: str = WAREHOUSE_PATH) -> duckdb.DuckDBPyConnection: ...

# 1. Strategy roster (which strategies have ≥ N trades in window)
def strategies_with_trades(conn, window_days: int, min_n: int = 30) -> list[str]: ...

# 2. Per-strategy trades with friction enforced (used everywhere downstream)
def trades_for_strategy(conn, strategy: str, window_days: int) -> pd.DataFrame: ...
# Columns: entry_ts, exit_ts, direction, pnl_dollars, pnl_ticks, mae_ticks, mfe_ticks,
#          regime, tod_bucket, session_date, market_open_minutes, hold_minutes
# REQUIRED: WHERE clause includes the JOIN to runs with friction_applied = TRUE

# 3. Monthly Sharpe-proxy series for regime z-score
def monthly_sharpe_proxy(conn, months_back: int = 6) -> pd.DataFrame: ...
# Columns: month, trade_count, avg_pnl, pnl_stddev, sharpe_proxy, win_rate

# 4. Per-strategy WFA summary (already a first-class table)
def wfa_summary_for_strategy(conn, strategy: str) -> dict: ...
# Keys: n_windows, mean_is_pf, mean_oos_pf, median_oos_pf, pct_windows_degraded, robust

# 5. Per-strategy WFA windows detail (for OOS drift detail)
def wfa_windows_for_strategy(conn, strategy: str) -> pd.DataFrame: ...

# 6. WR / PF by hour-of-day CT bucket
def panel_by_hour_ct(conn, strategy: str, window_days: int) -> pd.DataFrame: ...
# Columns: hour_ct, n_trades, wins, win_rate, profit_factor, avg_pnl

# 7. WR / PF by regime label
def panel_by_regime(conn, strategy: str, window_days: int) -> pd.DataFrame: ...
# Columns: regime, n_trades, win_rate, profit_factor, avg_pnl

# 8. WR / PF by direction (long vs short)
def panel_by_direction(conn, strategy: str, window_days: int) -> pd.DataFrame: ...
# Columns: direction, n_trades, win_rate, profit_factor, avg_pnl

# 9. MAE/MFE histogram for elbow analysis
def mae_mfe_distribution(conn, strategy: str, direction: str, window_days: int) -> pd.DataFrame: ...
# Columns: bucket_ticks, n_trades, win_rate (binned by mae_ticks)

# 10. IB-regime classifier helper: daily IB width vs 20d ATR median
def daily_ib_regime(conn, window_days: int) -> pd.DataFrame: ...
# Columns: session_date, ib_width_ticks, atr_20d, ib_regime (Narrow/Normal/Wide)
# NOTE: may need bar_events data — if not in v1 warehouse, return empty + flag

# 11. Confluence lift table (parse confluences JSON from entry_context)
def confluence_lift(conn, strategy: str, window_days: int) -> pd.DataFrame: ...
# Columns: confluence_count, n_trades, win_rate, profit_factor

# 12. Existing strategy parameter snapshot (AST-parsed, read-only)
def current_param_value(strategy: str, parameter_name: str,
                        config_path: str = r"C:\Trading Project\phoenix_bot\config\strategies.py") -> Any: ...
# Reads config/strategies.py via ast.parse; never imports the module.
# Returns the current literal value or raises KeyError if not found.
```

**SQL rules (enforced by code review):**
- Every query touching `pnl_dollars` MUST use the pattern:
  ```sql
  FROM trades t JOIN runs r USING(run_id) WHERE r.friction_applied = TRUE
  ```
- Use `trades_ct` view (not `trades`) when needing `session_date` or `market_open_minutes`.
- All queries are parameter-bound via DuckDB's `?` placeholder; no f-string interpolation into SQL.
- All queries are SELECT-only; the module-level `EXECUTE_GUARD` constant lists banned keywords and is checked at query-build time.

**Tests required (Tier 1 unit tests):**
- A synthetic DuckDB has known rows; each query's output is verified against hand-computed expected values.
- `EXECUTE_GUARD` rejects strings containing INSERT/UPDATE/DELETE/DROP/ALTER/CREATE.
- `trades_for_strategy` returns zero rows when `friction_applied = FALSE` on the source run.
- `current_param_value` correctly parses a simple `STRATEGIES = {...}` literal and rejects an attempt to import the module.

**Definition of done:**
- All 12 functions implemented with type hints.
- `tests/test_prepared_queries.py` passes (≥ 20 test cases).
- Module exports a `__all__` list of the public function names.
- No raw f-string SQL anywhere in the module.
- Commit on the current branch.

---

## Task 2 — `analytics/compute_engine.py` (the math)

**Goal:** Pure-Python deterministic compute layer. All risk metrics computed here so the LLM never has to.

**Files:**
- CREATE `analytics/compute_engine.py`
- CREATE `tests/test_compute_engine.py`

**Depends on:** Task 1 complete.

**Required functions:**

```python
def compute_psr(returns: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio (Bailey & López de Prado 2012).
    Returns probability that true SR > sr_benchmark, accounting for skew + kurtosis."""

def compute_dsr(returns: np.ndarray, n_trials_effective: int) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).
    Penalizes maximum-of-many-trials inflation."""

def compute_min_trl(returns: np.ndarray, target_sr: float = 1.0, alpha: float = 0.05) -> int:
    """Minimum Track Record Length — # trades needed for SR > target at confidence 1-alpha."""

def compute_hlz_tstat(returns: np.ndarray, lag: int = None) -> float:
    """Newey-West adjusted t-statistic per Harvey-Liu-Zhu 2016. Lag defaults to floor(n^(1/4))."""

def compute_effective_n(trial_returns: list[np.ndarray]) -> int:
    """Cluster correlated trials via the Optimal Number of Clusters algorithm (Bailey-LdP).
    Returns the effective number of independent trials for DSR / BHY."""

def compute_strategy_metrics(trades_df: pd.DataFrame,
                             wfa_summary: dict,
                             n_trials_effective: int) -> dict:
    """Full per-strategy panel. Returns the strategies[name] sub-dict shape from spec §12c."""

def compute_proximity(neighbor_pnls: dict[str, np.ndarray],
                      center_metric: float,
                      tolerance_pct: float = 0.10) -> dict:
    """Parameter proximity stress: returns {plateau: bool, neighbor_drift: dict}."""

def compute_delta_vs_prior(current_facts: dict, prior_facts: dict | None) -> dict:
    """Returns delta dict per spec §12b for the 'Delta vs Last Run' section."""

def classify_confidence_tier(n_trades: int, wfa_passes: bool) -> str:
    """Returns one of INSUFFICIENT / LOW / MEDIUM / HIGH per spec §7a."""
```

**Tests required (Tier 1 golden):**
- Each `compute_*` function tested against a published Bailey-LdP / HLZ golden number to ≤ 1e-4 tolerance.
- `compute_effective_n` collapses 5 synthetic overlapping MA-window return streams to N_eff in [1, 2].
- `classify_confidence_tier` matches §7a truth table exactly.
- `compute_proximity` flags a fragile peak as `plateau=False` and a flat plateau as `True`.

**Definition of done:**
- All formulas reference Bailey-LdP or HLZ papers in docstring.
- Tests pass.
- Commit.

---

## Task 3 — `analytics/regime_gate.py`

**Goal:** Pre-flight regime stability check. Halt verdict.

**Files:**
- CREATE `analytics/regime_gate.py`
- CREATE `tests/test_regime_gate.py`

**Depends on:** Task 1 (uses `monthly_sharpe_proxy`).

**Required functions:**

```python
def check_regime_stability(conn, mode: str, z_threshold: float = 1.5) -> dict:
    """Returns {stable: bool, z_score: float, warning: str | None, mode_skipped: bool}.
    Daily mode short-circuits with mode_skipped=True (one day is too noisy)."""
```

**Tests:**
- Synthetic monthly series with a stable baseline → `stable=True`.
- Synthetic series with last-month outlier → `stable=False`, `z > 1.5`.
- `mode='daily'` returns `mode_skipped=True` immediately.

**Definition of done:** function + tests + commit.

---

## Task 4 — `analytics/verifier.py` (Phase 3, PURE PYTHON ONLY)

**Goal:** Reconcile the LLM's narrative against `facts.json`. No LLM in this module.

**Files:**
- CREATE `analytics/verifier.py`
- CREATE `tests/test_verifier.py`

**Required functions:**

```python
def extract_numbers(text: str) -> list[tuple[str, float]]:
    """Return [(matched_token, parsed_value)] for every number in text.
    Handle: 52.1%, 0.95, $1,840.50, n=87, DSR=0.71, +0.05."""

def verify_numbers_in_facts(narrative: str, facts: dict, tolerance: float = 0.005) -> dict:
    """Returns {ok: bool, unmatched: [(token, value), ...]}.
    Every number in narrative must appear (within tolerance) somewhere in facts dict.
    Walk facts recursively; allow exact match or within ±tolerance (relative)."""

def check_lookahead_keywords(narrative: str,
                             event_keywords: list[str] = DEFAULT_EVENT_KEYWORDS,
                             date_pattern: str = r"\b\d{4}-\d{2}\b",
                             window_tokens: int = 10) -> dict:
    """Returns {ok: bool, violations: [(keyword, date, span)]}.
    Flags any event keyword within ±window_tokens of a YYYY-MM date.
    DEFAULT_EVENT_KEYWORDS = ['crash', 'rally', 'pivot', 'FOMC', 'CPI', 'NFP',
                              'collapse', 'spike', 'meltdown', 'surge']."""

def check_causal_language(narrative: str,
                          causal_phrases: list[str] = DEFAULT_CAUSAL) -> dict:
    """Returns {ok: bool, violations: [(phrase, span)]}.
    DEFAULT_CAUSAL = ['because', 'due to', 'caused by', 'resulted in', 'led to',
                      'driven by'] — context-sensitive: 'because n<30' is acceptable
    if the surrounding span has no macro/regime token nearby."""

def classify_finding_type(finding: dict, facts: dict, tolerance: float = 0.005) -> str:
    """Returns 'TRANSCRIPTION' if every numeric claim in finding['rationale']
    appears in facts; else 'INTERPRETATION'."""

def verify_report(facts: dict, narrative_md: str, findings: list[dict],
                  lookahead_active: bool) -> dict:
    """Top-level entry. Runs all four checks, applies tier downgrade to
    INTERPRETATION findings if lookahead_active. Returns:
    {pass: bool, downgrades: [(finding_id, old, new)], rejections: [...], cleaned_md: str}."""
```

**Tests (covers Tier 1 + Tier 4 adversarial):**
- `extract_numbers` parses 52.1%, $1,840.50, n=87 correctly.
- `verify_numbers_in_facts` passes on a transcription, fails on a fabricated number.
- `check_lookahead_keywords` flags "the market crashed in 2022-10".
- Bare `2022-10-15` mentioned in a context without event keywords does NOT flag.
- `check_causal_language` flags "because the Fed pivoted".
- Tier 4 adversarial: inject synthetic finding with DSR=0.99 & n=10; verifier rejects it from the final MD.

**Definition of done:** all functions + tests + commit.

---

## Task 5 — `agents/strategy_oracle.py` (orchestrator)

**Goal:** The LLM-facing orchestrator. Dispatches mode, sets up Anthropic client, runs the tool-use loop, assembles output.

**Files:**
- CREATE `agents/strategy_oracle.py`
- CREATE `tests/test_strategy_oracle.py`

**Depends on:** Tasks 1-4 complete.

**Module structure:**

```python
# Top of file: structural invariant comment + CI-grep marker
# ORACLE_INVARIANT: no_trade_path_imports = True

import json, os, logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Literal

# Allowed imports:
from analytics import compute_engine, regime_gate, verifier, prepared_queries
from anthropic import Anthropic

# FORBIDDEN (CI grep will fail if present):
# from bots... / from core... / from bridge... / from data_feeds...

Mode = Literal["research", "weekly", "daily"]

MODE_CONFIG = {
    "research": {"window_days": 1825, "token_budget": 200_000, "can_propose": True},
    "weekly":   {"window_days": 7,    "token_budget": 80_000,  "can_propose": True},
    "daily":    {"window_days": 1,    "token_budget": 15_000,  "can_propose": False},
}

SYSTEM_PROMPT_TEMPLATES = { ... }  # one per mode; built from spec §0-§9

TOOLS = [ ... ]  # exactly 5 tools per spec §6

def run(mode: Mode, save_baseline: bool = True) -> dict:
    """Main entry. Returns summary dict with output file paths."""
```

**Required behaviors:**
- Pre-flight calls `regime_gate.check_regime_stability(conn, mode)` — halt on `stable=False` (research/weekly modes only).
- Compute layer builds `facts.json` before any LLM call.
- Inject compact summary table inline; full panel via `fetch_strategy_stats` tool.
- Load prior findings (last 30 days, expired filtered) from `logs/oracle/*/`.
- Mode-aware tool gating: `propose_change` returns error if `mode == "daily"`.
- Every tool call writes one line to `audit.jsonl`.
- After LLM loop, Phase 3 verifier runs over the narrative; rejected lines are removed from final debrief.md.
- Output paths follow spec §12a structure.

**Tests:**
- Mode dispatch routes to correct config.
- `propose_change` rejected in daily mode.
- Mock LLM responds with a tool_use → dispatcher routes to right function.
- `audit.jsonl` line count == number of tool calls.
- CI invariant: `ast.parse` the module and assert no `bots.` / `core.` / `bridge.` / `data_feeds.` imports.

**Definition of done:** module + tests + commit. **Does NOT make a real Anthropic API call in tests** (use a stub client).

---

## Task 6 — `tools/run_oracle.py` (CLI)

**Goal:** Thin CLI wrapper.

**Files:**
- CREATE `tools/run_oracle.py`

**Depends on:** Task 5.

**Required behavior:**

```python
# Usage: python -m tools.run_oracle <research|weekly|daily> [--save-baseline]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["research", "weekly", "daily"])
    parser.add_argument("--save-baseline", action="store_true", default=True)
    args = parser.parse_args()
    result = strategy_oracle.run(args.mode, save_baseline=args.save_baseline)
    sys.exit(0 if result.get("status") == "complete" else 1)
```

**Tests:** Integration smoke test that the CLI parses args and invokes `strategy_oracle.run` (stub the real call).

**Definition of done:** CLI + smoke test + commit.

---

## Task 7 — `tests/test_oracle.py` (Tier 1-4 consolidated)

**Goal:** Consolidate the test discipline from spec §16 in one file even if some pieces exist as sub-tests elsewhere. This is what the operator runs as the pre-flight gate.

**Files:**
- CREATE `tests/test_oracle.py` (re-uses fixtures from prior test files)

**Depends on:** Tasks 1-6.

**Required tests (mirroring spec §16):**

- Tier 1 unit: rejects unknown query name, rejects LOW-confidence proposal, rejects n<30 proposal, verifier rejects unmatched number, verifier flags causal language, golden numbers for compute_dsr/psr/min_trl/hlz/effective_n.
- Tier 2 golden: total trades by strategy reproduces a known answer from the warehouse.
- Tier 3 consistency: same week → same top finding across 3 stub runs (uses a deterministic-seeded mock LLM).
- Tier 4 adversarial: inject `{dsr:0.99, n:10}` finding → propose_change blocks, verifier rejects narrative mention.

**Definition of done:** All four tiers pass. Commit.

---

## Task 8 — Migration

**Goal:** Delete the old learner, update callers, migrate `adaptive_params.py` path.

**Files:**
- DELETE `agents/historical_learner.py`
- UPDATE `agents/adaptive_params.py` — read from `logs/oracle/pending_changes.json` (path constant change + schema-key rename `pending_recommendations` → `pending`)
- DELETE `tools/run_weekly_learner.py` (replaced by `tools/run_oracle.py`)
- DELETE `tests/test_learner.py` (replaced by `tests/test_oracle.py`)
- UPDATE imports in any other file referencing `historical_learner`

**Pre-task search:**
```
Grep "historical_learner" in the project — capture full callsite list before editing.
```

**Tests:** After migration, full repo pytest run must pass.

**Definition of done:**
- No file under `phoenix_bot/` imports `agents.historical_learner` anymore.
- `agents/adaptive_params.py` reads the new path and schema key.
- Existing `logs/ai_learner/` folder preserved untouched.
- Commit.

---

## Final pass — Critique & polish

After Task 8 completes:

1. **Top-to-bottom critique** of the implementation:
   - Architectural coherence (does it match the spec?)
   - Unit decomposition (are interfaces clean?)
   - Test depth (would these tests catch a real regression?)
   - Failure modes (what happens on partial DuckDB, missing API key, malformed prior findings?)
   - Operator UX (are debrief outputs actually readable? does the error message on regime halt tell the operator what to do?)
2. **Implement any changes** the critique surfaces.
3. **Final code review** of the entire implementation diff.
4. **Use `superpowers:finishing-a-development-branch`** to decide next step.

---

## Acceptance criteria for the full project

The Oracle is considered DONE when:

- All Tier 1-4 tests pass (`pytest tests/test_oracle.py -v`).
- A stub-mode `weekly` run produces a non-empty `debrief.md`, `facts.json`, `pending_changes.json`, `audit.jsonl` in `logs/oracle/weekly/`.
- The pre-flight invariant CI scan finds no trade-path imports in `agents/strategy_oracle.py`.
- `agents/historical_learner.py` no longer exists.
- `agents/adaptive_params.py` reads `logs/oracle/pending_changes.json` with no errors.
- A real-data dry run is queued for the operator to trigger.
