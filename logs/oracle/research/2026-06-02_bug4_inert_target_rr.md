# BUG #4 — Inert `target_rr` on Chandelier / TimeExit Strategies

**Verdict: DOCUMENTED-TUNING (no infrastructure fix needed today).**
The `target_rr` keys on `raschke_baseline` (3.5), `e_multi_day_breakout`
(2.5), and `g_inside_bar_breakout` (3.0) are vestigial in live —
Phase 13 exit policies own the live target. The divergence is already
documented inline in `config/strategies.py` (BACKTEST-ONLY warning
comments added in commit `de777f3`).

---

## The mechanism (live path)

**Step 1 — strategy emits Signal with target_rr.** Each strategy's
`evaluate()` builds a `Signal` with `target_rr=3.5` (or 2.5, 3.0). At
this point `signal.target_price` is None — the strategy lets the
downstream pipeline compute the actual price from `target_rr`.

**Step 2 — Phase 13 override pre-computes target.** In
`bots/base_bot.py:393-414` `_apply_phase13_overrides` calls
`policy.compute_initial_target(direction, entry_price, stop_price)`
for the strategy's assigned exit policy from
`PHASE_13_EXIT_ASSIGNMENTS` in `core/exit_policies.py`. The return
value is assigned **unconditionally** to `signal.target_price`.
`signal.target_rr` is not read.

**Step 3 — what the policies return:**
- `ChandelierPolicy.compute_initial_target` ([core/exit_policies.py:200-207](core/exit_policies.py:200))
  returns `entry ± 10R` — a hard-coded wide placeholder. The chandelier
  trail (not a hard target) governs the actual exit.
- `TimeExitPolicy.compute_initial_target` ([core/exit_policies.py:306-313](core/exit_policies.py:306))
  returns `entry ± 5R` — same pattern. The 30-min time exit governs.

**Step 4 — base_bot recomputes after stop is finalized.**
`recompute_phase13_target` in `bots/base_bot.py:535-541` is called
from `bots/_trade_entry.py:362-387, 529-549` once the real stop price
exists. Same `compute_initial_target` is called; same 10R / 5R is
written. Still ignores `signal.target_rr`.

**Step 5 — OIF bracket carries the policy-derived target**, not the
config `target_rr`-derived target.

---

## The mechanism (backtest path)

`tools/phoenix_real_backtest.py::_resolve_stop_and_target`
([phoenix_real_backtest.py:1103-1148](tools/phoenix_real_backtest.py:1103))
reads `sig.target_rr` directly:
```python
target = entry + (stop_dist * sig.target_rr * direction_sign)
```
No import of `core.exit_policies` in this module. No call to
`recompute_phase13_target` anywhere (grep returns zero matches). The
backtest does not run `ChandelierPolicy.should_exit` or
`TimeExitPolicy.should_exit` at all.

Result: future backtests for these three strategies simulate a hard
`target_rr` target exit while the live bot runs a chandelier trail
(or 30-min time exit). **The two paths produce non-comparable PnL.**

---

## Existing in-code documentation

Commit `de777f3` (2026-06-02 08:13 CT) added BACKTEST-ONLY warning
blocks above each affected `target_rr` key:

- `config/strategies.py:1119-1127` — `e_multi_day_breakout`
- `config/strategies.py:1153-1158` — `g_inside_bar_breakout`
- `config/strategies.py:1201-1214` — `raschke_baseline`

The warning text states explicitly: *"target_rr below ONLY affects
tools/phoenix_real_backtest.py … Live impact is ZERO."*

The same commit's message: *"Not in this commit (operator decisions /
out of overnight scope) … M-4 archive script transaction ordering …
L-1/L-2/L-3 LOW findings (style, no functional impact)."* — The audit
itself acknowledged the live/backtest divergence; the comments were
intentional documentation, not a fix attempt.

---

## Why this is (c) "vestigial config" — not (a) or (b)

**Not (a) — make compute_initial_target consult `sig.target_rr`.**
Re-enabling a hard target on chandelier strategies re-instates the
"hard target wins vs. trail" race that Phase 13 §U.3 deliberately
retired. Chandelier was tick-validated as superior to fixed RR on
these three breakouts; lowering the hard target to 3R (or 2.5R)
would clip winners that the trail intends to ride.

**Not (b) — make base_bot honor whichever target is smaller.**
"Smaller wins" inverts Phase 13's design intent: the 10R chandelier
placeholder is deliberately wide so the bracket never fires the
target — the trail is the exit mechanism. A 3R cap would re-instate
the bracket-fire path the design rejected.

**(c) is correct — `target_rr` on these three strategies is a
backtest-simulator knob.** The fix is one of:
1. Strip `target_rr` from the three configs entirely. Backtest then
   falls through to `_LONG_INF / _SHORT_INF` (no hard target,
   simulator exits on stop / time / max-hold / EoD only). This makes
   backtest-live divergence smaller.
2. Keep `target_rr` and patch the Oracle pipeline to skip `target_rr`
   proposals on strategies whose `PHASE_13_EXIT_ASSIGNMENTS` entry is
   `chandelier`, `time_exit`, or `managed_existing` (so the Oracle
   stops tuning inert knobs).
3. Make the backtest actually run `ChandelierPolicy.should_exit` and
   `TimeExitPolicy.should_exit` so the simulator matches live
   behavior. This is the LARGEST fix; ties into the
   `project_phase13_unreconciled.md` watchlist item.

All three are operator decisions, not infrastructure bugs.

---

## Files cited

- `bots/base_bot.py:393-414` — `_apply_phase13_overrides`, first call site
- `bots/base_bot.py:535-541` — `recompute_phase13_target`, second call site
- `bots/_trade_entry.py:362-387` — deferred-recompute trigger from market-price branch
- `bots/_trade_entry.py:529-549` — deferred-recompute trigger from LIMIT branch
- `core/exit_policies.py:200-207` — `ChandelierPolicy.compute_initial_target` (10R)
- `core/exit_policies.py:306-313` — `TimeExitPolicy.compute_initial_target` (5R)
- `core/exit_policies.py:395-408` — `PHASE_13_EXIT_ASSIGNMENTS` mapping
- `tools/phoenix_real_backtest.py:1103-1148` — backtest target resolution
- `config/strategies.py:1094-1220` — the three strategy blocks with BACKTEST-ONLY warnings

## Self-second-guess

- I'm reporting this as "tuning, no fix" because all three options
  (1/2/3 above) are intent decisions the operator owns. If the
  operator's actual goal is "live behavior should change to match
  backtest projections," then option (a) is the right fix — but
  that would change strategy exit behavior on three production-
  validated strategies, which is well outside the scope of a
  bug-fix sweep.
- The Oracle pipeline tuning `target_rr` on inert knobs (option 2)
  is wasted compute. Not enough to block today's sweep, but worth
  surfacing.
- Option 3 (make backtest simulate the actual exit policies) is
  the proper fix for backtest fidelity. Tied to the larger
  reconciliation-harness project on the watchlist.
