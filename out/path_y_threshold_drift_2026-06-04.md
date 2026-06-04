# Path Y — Threshold-Drift Diagnostic

_Drafted: 2026-06-04 — confluence-firing sprint, Phase 2 deliverable._
_Branch: `weekly-evolution/2026-05-24` · HEAD: `08fa958` · FREEZE_ACTIVE = True._

---

## TL;DR — Verdict

**Path Y hypothesis ("live thresholds drifted above 5y backtest values, causing
firing-rate gap") is NOT supported by evidence. Recommendation: DEFER
threshold decision. Do NOT ship a Phase 3b config change.**

Two pieces of evidence overturn the hypothesis:

1. **Source-of-truth alignment.** The 5y backtest at `tools/phoenix_real_backtest.py:1422`
   reads thresholds directly from `config/strategies.py` via `cfg = dict(STRATEGIES[name])`.
   There is no separate "old-values config" the backtest could have used.
   The 2026-06-02 backtest ran against the same `STRATEGY_DEFAULTS = {min_confluence: 5.0, min_momentum_confidence: 80, ...}` and the same per-strategy
   `STRATEGIES["bias_momentum"] = {min_confluence: 5.5, min_momentum: 80, ...}` that live uses today.

2. **Rejection-log evidence.** Across 7,928 eval events from 2026-05-28 →
   2026-06-04, the dominant bias_momentum reject reasons are NOT
   `low_confluence`:

   | Rank | Count | Reject reason                                                |
   |-----:|------:|---|
   |    1 |   144 | `BIAS_MOM: session block window 04:00-04:59 CT` (intentional, Oracle 2026-06-01 finding) |
   |    2 |   201 | `TF_VOTES: only 0-1/4 in {LONG,SHORT} direction, need >=2` |
   |    3 |    80 | `ES_GATE: ES/NQ relative-strength sign disagrees with {LONG,SHORT}` |
   |    4 |   ~150 | `BIAS_MOM: CVD net-selling in AFTERNOON_CHOP` (institutional flow opposition) |
   |    — |     0 | `min_confluence` / `low_confluence` rejects |

   The min_confluence threshold is **not the binding gate**. Loosening it
   from 5.0 to 3.5 would NOT unblock more signals — the rejections are
   coming from TF-vote alignment, ES/NQ correlation, CVD opposition, and
   the deliberate session-block window. None of these would change at
   min_confluence=3.5.

---

## Phase 2.1 — Live `STRATEGY_DEFAULTS`

From `config/strategies.py:54-95` (the runtime override block):

```python
STRATEGY_DEFAULTS = {
    "min_confluence": 5.0,           # raised from 3.5 (comment)
    "min_momentum_confidence": 80,   # raised from 60 (comment)
    "min_precision": 48,
    "risk_per_trade": 15.0,
    "max_daily_loss": 45.0,
    "base_rr_ratio": 5.0,            # raised from 1.5 (comment)
    "be_on_bar_close": True,
}
```

The "raised from X" comments document the 2026-04-18 commit `73921e4`
("strategies: param rework"), which moved the global defaults to support
"larger-move capture" — fewer/better entries aimed at 20-80pt moves
rather than 5-10pt scalps. Co-authored with Claude Opus 4.7. **This
commit landed 6 weeks before the 5y backtest ran on 2026-06-02**, so the
backtest used the post-bump values.

---

## Phase 2.2 — How the 5y backtest sources thresholds

`tools/phoenix_real_backtest.py:1422`:

```python
cfg = dict(STRATEGIES[name])
cfg["is_prod_bot"] = False
out[name] = class_map[name](cfg)
```

The backtest reads `STRATEGIES[name]` from `config/strategies.py` at run
time, then constructs the strategy with that config. **There is no
threshold-override mechanism on the backtest side.** Whatever `STRATEGIES`
holds when the backtest starts is what the backtest sees.

Backtest ran 2026-06-02; `config/strategies.py` had `min_confluence=5.0`,
`min_momentum_confidence=80`, `STRATEGIES["bias_momentum"].min_confluence=5.5`
since 2026-04-18 (commit `73921e4`). Result: backtest used the same
values live uses today.

---

## Phase 2.3 — 5y summary snapshot

From `backtest_results/phoenix_real_5year_2026-06-02_summary.csv`:

| Strategy             | enabled at run | n_trades 5y | per_year | WR     | PF    | Expectancy | Total 5y P&L | Tier             |
|----------------------|:-------------:|-----------:|---------:|-------:|------:|-----------:|-------------:|------------------|
| bias_momentum        | ✓             |    28,501  |    5,700 | 41.9%  | 1.56  |    $11.08  |   +$315,815  | HIGH_CONFIDENCE  |
| opening_session      | ✓             |     3,719  |      744 | 44.1%  | 1.79  |    $13.67  |    +$50,855  | HIGH_CONFIDENCE  |
| e_multi_day_breakout | ✓             |       622  |      124 | 70.7%  | 5.30  |    $13.78  |     +$8,573  | VALIDATED        |
| g_inside_bar_breakout| ✓             |       973  |      195 | 59.2%  | 4.33  |    $13.47  |    +$13,109  | HIGH_CONFIDENCE  |
| raschke_baseline     | ✓             |       801  |      160 | 55.2%  | 3.87  |    $18.57  |    +$14,875  | HIGH_CONFIDENCE  |
| a_asian_continuation | ✓             |       340  |       68 | 85.6%  | 11.88 |    $10.97  |     +$3,731  | TENTATIVE        |
| es_nq_confluence     | ✓             |       131  |       26 | 45.8%  | 3.38  |    $15.48  |     +$2,028  | TENTATIVE        |
| ib_breakout          | ✓             |       185  |       37 | 48.1%  | 1.15  |     $6.29  |     +$1,165  | TENTATIVE        |
| vwap_band_pullback   | ✓             |       295  |       59 | 43.7%  | 1.15  |     $5.22  |     +$1,540  | TENTATIVE        |
| nq_lsr               | ✓             |       967  |      193 | 21.8%  | 0.83  |    -$1.56  |    -$1,512   | HIGH_CONFIDENCE — LOSER |
| dom_pullback         | ✓             |         0  |        0 |      — |    —  |        —   |         $0   | (zero-signal)    |
| orb_v2               | ✓             |         1  |      0.2 |      0%|    0  |   -$30.00  |       -$30   | INSUFFICIENT_SAMPLE |

**bias_momentum expected fires/day from backtest: 22.6**
**bias_momentum live fires today (2026-06-04): 1 (PHANTOM-NT8 rejected)**
**Sim bias_momentum signals 2026-05-28 → 2026-06-04 (7 days): 295 across all strategies; per-day average across strategies suggests bias_momentum was firing low-double-digits — order-of-magnitude consistent with the 22/day expectation when wires-of-the-day are healthy.**

---

## Phase 2.4 — Threshold-change git history (Apr 1 → Jun 4)

Searched `git log -G 'min_confluence'` and `'raised from 3\.5'`:

| Commit   | Date       | Author                | What changed                          |
|----------|------------|-----------------------|---------------------------------------|
| `73921e4`| 2026-04-18 | Jennifer Brennan + Claude Opus 4.7 | STRATEGY_DEFAULTS: min_confluence 3.5→5.0, min_momentum_confidence 60→80, base_rr_ratio 1.5→5.0; STRATEGIES["bias_momentum"].target_rr 2.0→5.0, max_hold 25→60 min, added max_ema_dist_ticks=60 |

This is the ONLY commit in the window that changed the global thresholds.
The backtest on 2026-06-02 ran post-bump. Same values as live today.

---

## Phase 2.5 — Per-strategy threshold table (the 7 enabled+validated winners)

Live runtime overrides STRATEGY_DEFAULTS onto per-strategy config via
`bots/_strategy_dispatch.py:147-152`:

```python
profile_keys = ("min_confluence", "min_momentum", "min_momentum_confidence",
                "min_precision", "risk_per_trade", "max_daily_loss")
for strat in self.bot.strategies:
    for key in profile_keys:
        if key in self.bot._runtime_params:
            strat.config[key] = self.bot._runtime_params[key]
```

`_runtime_params` initialized at `bots/base_bot.py:1067` as
`dict(STRATEGY_DEFAULTS)`. The backtest does NOT apply this override —
strategies receive `cfg = dict(STRATEGIES[name])` as-is.

| Strategy            | enabled | validated | walk_fwd | bt min_conf | live min_conf | bt min_mom | live min_mom | drift verdict |
|---------------------|:-------:|:---------:|:--------:|:-----------:|:-------------:|:----------:|:------------:|---|
| bias_momentum       | ✓       | ✓         | hard_block | **5.5** | **5.0** off-window; 5.5 on-window via `_REGIME_OVERRIDES` | 80 | 80 | LOOSENED off-window (live 0.5 looser); ALIGNED on-window |
| opening_session     | ✓       | ✓         |    —     | (no per-strategy min_conf — strategy reads internal gates) | (live runtime sets 5.0 on strat.config, but strategy doesn't read min_confluence) | (n/a) | (n/a) | ALIGNED (threshold not a gate for this strategy) |
| e_multi_day_breakout| ✓       | ✓         |    —     | (n/a — pattern-based gate, no min_confluence) | (live sets 5.0 on strat.config, ignored by strategy) | (n/a) | (n/a) | ALIGNED |
| g_inside_bar_breakout|✓       | ✓         |    —     | (n/a) | (live override ignored) | (n/a) | (n/a) | ALIGNED |
| raschke_baseline    | ✓       | ✓         |    —     | (n/a) | (live override ignored) | (n/a) | (n/a) | ALIGNED |
| a_asian_continuation| ✓       | ✓         |    —     | (n/a) | (live override ignored) | (n/a) | (n/a) | ALIGNED |
| es_nq_confluence    | ✓       | ✗ (validated=False) | — | (n/a) | (live override ignored) | (n/a) | (n/a) | ALIGNED |

**Six of seven winners use pattern-based gates (multi-day high break,
inside-bar formation, Asian-session range break, etc.) — `min_confluence`
isn't a binding gate for them.** Only bias_momentum reads
`min_confluence` from strat.config; for it, the regime override
(`bias_momentum.py:_REGIME_OVERRIDES` at lines 36-58) takes precedence
during golden windows (OPEN_MOMENTUM, MID_MORNING set 5.5 directly).
Off-window, live uses 5.0 from STRATEGY_DEFAULTS override (looser than
backtest's 5.5).

**Net: there is NO direction "live tighter than backtest" drift. For
bias_momentum, off-window live is slightly LOOSER than backtest. For the
other 6 winners, the threshold doesn't gate signals at all.**

---

## Phase 2.6 — Categorization

| Strategy             | Category | Note |
|----------------------|---|---|
| bias_momentum        | **LOOSENED** off-window; **ALIGNED** on-window | Drift is in the wrong direction to explain firing-rate gap |
| opening_session      | **ALIGNED** | min_confluence not a gate |
| e_multi_day_breakout | **ALIGNED** | pattern gate, no threshold |
| g_inside_bar_breakout| **ALIGNED** | pattern gate, no threshold |
| raschke_baseline     | **ALIGNED** | pattern gate, no threshold |
| a_asian_continuation | **ALIGNED** | range-break gate, no threshold |
| es_nq_confluence     | **ALIGNED** | composite gate, no min_confluence |

---

## Phase 2.7 — Proposed CONFIG DIFF section

**No diff proposed.** The hypothesis that motivated this section is not
supported by the data.

The sprint spec asked me to propose restoring backtest values where
live is stricter. For the only strategy where there's any drift
(bias_momentum off-window), live is LOOSER. Restoring to backtest's
5.5 would TIGHTEN live and produce FEWER signals — which is contrary
to the operator's intent of unblocking firing.

If the operator wants to ship a threshold change anyway (e.g., to push
live even looser than today by setting STRATEGY_DEFAULTS.min_confluence=3.5
on the gamble that more signals = more good trades), that's a freeze-gated
re-tune decision, not a "correctness restore." It would need explicit
sign-off and a fresh backtest at the new threshold before shipping —
neither is in this sprint's scope.

---

## Phase 2.8 — Expected impact table

**No impact table to produce.** With zero `min_confluence` rejects in
the rejection log (`0 rejects in 7 days across 7,928 evals`), restoring
to 3.5 would unblock zero additional signals from the confluence gate.
The "5x–15x firing rate" estimate in the sprint outline was the
hypothetical IF the gate were binding — but the rejection log shows
it isn't.

Per the operator's stated rule ("Use ACTUAL rejection-log evidence,
NOT hand-waved estimates"), I will not fabricate an impact estimate to
justify a change the evidence doesn't support.

---

## What's ACTUALLY behind the firing-rate gap (best current theory)

1. **PHANTOM-NT8 today.** The 1-signal fire on 2026-06-04 was rejected
   by NT8 ATI silently — operator confirmed. `FINDING-2026-06-04-PHANTOM-NT8`
   OPEN, deferred to its own sprint.
2. **TF_VOTES alignment.** 201 bias_momentum rejects in 7 days at
   `"only 0-1/4 in {direction} direction, need >=2"`. The multi-TF
   EMA-stack vote is the dominant binding gate. In choppy / regime-
   ambiguous conditions, fewer TFs align directionally. The backtest's
   5-year average masks this — over 5 years the TF_VOTES gate fires
   plenty of signals; on a specific week of choppy regime, it doesn't.
3. **ES_GATE strictness.** 80 bias_momentum rejects in 7 days for
   "ES/NQ relative-strength sign disagrees". This gate (in
   `core/confluence_gates.py:tf60m_es_gate`) is research-validated at
   `+$4.05/trade avg edge` (a16cf0ef research). It correctly rejects
   when NQ-vs-ES correlation breaks down — and 2026-05 has seen
   notable NQ/ES decoupling.
4. **CVD opposition in AFTERNOON_CHOP.** ~150 bias_momentum rejects
   for net-selling CVD blocking longs. Working as designed.
5. **04:00-04:59 CT session block.** 144 intentional blocks per Oracle's
   2026-06-01 finding (hour 4 = breakeven hour, PF=0.995 across 28k trades).

None of these are threshold-drift bugs. They are structural gates
behaving as designed in current market microstructure.

---

## Recommendation

| Option                                | Recommended?              |
|---------------------------------------|---|
| "Ship the proposed diff as-is"        | N/A — no diff is proposed (data doesn't support it) |
| "Ship with modifications"             | N/A |
| "**Defer threshold decision**"        | **YES — this is the verdict the evidence supports**. Tracker row stays OPEN as `FINDING-2026-06-04-THRESHOLD-DIAG` with status `RESOLVED-NO-CHANGE` (or `DEFERRED`) and the note: "no drift found; rejection log shows threshold not binding; PHANTOM-NT8 + TF_VOTES + ES_GATE are the real bottlenecks." |
| "Kick to freeze-lift sprint"          | NO — there's nothing to lift the freeze for on this front. The real follow-ups (PHANTOM-NT8 investigation; potential gate-loosening retune) are separately scoped. |

**Operator decision required:** confirm "Defer threshold decision" (option C),
or override with explicit go-ahead to ship a specific threshold change you
want (option B).
