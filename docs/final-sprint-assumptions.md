# Final Sprint — Assumptions & Judgment Calls

Log of non-obvious decisions made by parallel workstreams during the exit-audit
and safety sprint. Append-only.

## WS-A — Guaranteed-loss pattern audit (2026-04-21)

- **vwap_pullback kept at target_rr=20.0 despite no trailing exit.**
  The header comment claims "Reversal+stall exit drives this — target is not
  the OCO bracket." No such exit is wired in `strategies/vwap_pullback.py` at
  HEAD 8cdda40. Rather than silently lowering the target (which destroys
  whatever research intent produced `target_rr=20`) or silently adding a
  trailing exit (out of WS-A scope), I added
  `_wide_target_requires_trailing=True` to the config and flagged it for
  WS-C. This preserves the signal in the dashboard/logs while the CI sanity
  test refuses to let a future edit remove the marker without also either
  bringing target_rr back under 10 or wiring a real managed exit.

- **CI test accepts `_wide_target_requires_trailing` as an escape hatch.**
  Alternative would have been a hard `target_rr < 10` gate. Rejected because
  opening_session sub-strategies could legitimately compute wide `target_rr`
  at runtime (they emit concrete `target_price` so the `target_rr` field is
  just a ratio-for-display), and because noise_area's `target_rr=0.0` is
  also wide-adjacent — the contract that actually matters is "either a
  managed exit or a realistic OCO," and the marker documents that.

- **high_precision_only (disabled) left untouched.**
  Config has `stop_ticks=14` and `target_rr=5.0` but `enabled=False`. CI
  sanity tests skip disabled strategies on purpose — if the user re-enables
  it, the same tests will re-evaluate its config at that moment.

- **No change to `bots/base_bot.py`.**
  The managed-exit synthesis path that caused the noise_area bug is already
  patched at HEAD. The new CI tests prevent a regression reaching it with
  `target_rr=0` and no `uses_managed_exit` flag.

## WS-B: opening_session aggregator fix (2026-04-21)

- `avg_1min_volume` emits `None` until 20 RTH 1m bars accumulated (warmup); strategies treat this as `missing_fields` and SKIP — acceptable.
- `rth_1min_*` track the *latest completed* 1m bar during RTH (8:30-15:00 CT). Reset daily via existing `_init_live_state`.
- `rth_5min_close_last` tracks latest 5m close during RTH (distinct from `rth_5min_close` which is the first-5m-bar close used by classifier).
- RTH close boundary = 15:00 CT. 1m bars after this do not update fields (preserves day-end state for exit logic).
- Rolling volume window = 20 bars (hardcoded). Tuning deferred.
