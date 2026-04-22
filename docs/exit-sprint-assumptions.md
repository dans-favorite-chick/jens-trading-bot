# Exit Sprint Assumptions

## S5 trade_memory hygiene (B69/B70) — 2026-04-22

- Phase-C cutoff = `2026-04-21` (ISO date, lexicographic compare on `recorded_at`).
- Heuristic for null bot_id backfill:
  - `account == "Sim101"` AND `recorded_at >= 2026-04-21` -> `prod`
  - `account` starts with `"Sim"` (dedicated SimXxx) -> `sim`
  - `recorded_at < 2026-04-21` -> `legacy`
  - else -> `unknown`
- 38 rows dated 2026-04-21 before the Phase-C merge landed (20:39) have no
  `account` field and were classified `unknown` — they predate per-bot
  account routing and cannot be attributed post-hoc.
- Write-time guard in `core/trade_memory.record()` defaults missing bot_id
  to `"unknown"` + WARNING log rather than silently writing null.

## S1 stop/target math audit — 2026-04-21

- **Jennifer's "bias_momentum 600-pt stop" is the TARGET, not the stop.**
  Log line interpreted as `stop=27434` was actually `target=27434.5`
  on LONG @ 26834.5. Stop was 30.25 pts below entry (correct side,
  121t). The 600-pt target comes from `_RIDER_STRATEGIES` forcing
  `target_rr=20.0` in base_bot (lines 1913-1919). Flagging to Jennifer
  but not treating as a math bug.
- **noise_area `target_rr=0` is intentional.** Signal has
  `target_price=None` + `exit_trigger` set. Real bug was base_bot
  materializing `target_price=entry` when `signal.target_price is None`
  and `target_rr==0`. Fix landed in base_bot, NOT in noise_area config.
- **Universal sanity gate** placed in `base_bot._sanity_check_entry`,
  invoked in `_execute_trade` right before OCO submission. Ticks range
  [5, 200] per sprint spec. Logs `[STOP_SANITY_FAIL]` CRITICAL and
  aborts the signal.
- No strategy files under `strategies/` needed edits — all produce
  correct geometry; the defect was in base_bot's target-resolution
  path for managed-exit signals.

## S3 — opening strategies silence (2026-04-22)

- Jennifer's pre-briefing had two factual errors: (a) sim_bot first-start
  today was 09:07:52 CDT, not 10:23 (10:23 was a later restart after a
  MenthorQ reload cycle); (b) `data/menthorq_daily.json` was ~2 h old at
  bot start, not 108 h. The silence root cause is still upstream of these.
- Primary root cause: producer/consumer field-name mismatch between
  `strategies/opening_session.py` (reads `rth_5min_close_last`,
  `rth_1min_*`, `avg_1min_volume`) and
  `core/session_levels_aggregator.get_levels_dict()` (emits
  `rth_5min_close` only; no 1m bar fields). Every ORB eval at 09:07+
  hit `SKIP orb missing_fields`; premarket_breakout (08:30-08:45) was
  missed entirely because the bot wasn't running that window.
- Shipped B66 (observability only) rather than B65/B67/B68 from the
  brief. Evidence showed none of those hypotheses were causal today,
  so shipping them would be speculative gold-plating. Structural fix
  for field coverage (R1 in the investigation doc) is deferred with
  a spec — it needs a new 1m rolling bar aggregator + avg-1m-volume
  EMA, non-trivial and deserves its own slot.
- `is_in_window()` remains tz-naive. Acceptable today because upstream
  `tick_aggregator._bar_now = datetime.fromtimestamp(bar.end_time)`
  is naive-local on the CT Windows host. If the host TZ ever moves,
  this breaks silently; a future hardening ticket (B68-equivalent) is
  still worth filing, just not under "explains today's silence".

## S2 — target fire verification (B63/B64) — 2026-04-22

- **"Consumed" = file disappeared from `incoming/`.** Existing
  `_verify_consumed` treats NT8 deletion of the OIF file as proof of
  acceptance. Weaker than "Working in NT8" (which would require
  reading `outgoing/` orders file), but it's the contract the rest of
  the codebase assumes. B63 preserves that contract.
- **Half-success recovery re-places ONLY the missing leg once, not
  the full OCO pair.** Re-submitting both risks double-attach — NT8
  OCO semantics for duplicate registrations aren't documented. We
  re-stage the single missing leg with the same OCO id. If that also
  fails, we emit a cleanup `CANCEL_ALL` on the account before
  giving up so the caller's outer 3-retry loop starts from a clean
  NT8 state.
- **`[TARGET_MISS_SUSPECT]` threshold = MFE ≥ 20 ticks**, not the
  literal 80-tick (20-point) Jennifer cited. 20 t (5 pts) is a low
  enough bar to surface managed-exit leakage, and today's sample had
  no MFE ≥ 80 trades at all — 20 t gives us signal.
- **Forensic logging is non-blocking.** The `[EXIT_FORENSIC]` /
  `[TARGET_MISS_SUSPECT]` emit is wrapped in a bare `try/except`
  with DEBUG swallow. A logging defect must never crash the exit
  path.
- **Root cause for today's apparent target misses is strategy-side,
  not OCO-side.** 7/7 MFE≥20 non-target exits were managed exits
  (`ema_dom_exit`, `trend_stall`, `time_stop`) firing before price
  reached the LIMIT. Targets for the two big winners (105 t and 83 t
  MFE) were physically unreachable inside the trade's hold. That's
  S1/strategy territory. B63 closes the observability gap so if the
  real OCO-half-attach bug does occur it can't hide any more.

## S6 — Directional conflict observability (B70-B72, 2026-04-21)

- **Non-blocking**: conflicts are DETECTED and LOGGED only. No block,
  arbitrate, or auto-flatten. Decision deferred 2-4 weeks pending data.
- **P&L attribution**: per-event P&L in the 17:00 CT recap and in
  `analyze_conflicts.py` is matched by `trade_id` out of trade_memory.
  Both halves of a conflict pair contribute independently; "conflict
  cost" is the sum of P&L across every trade that was part of ANY pair
  that day. Overlap time reported is the MAX overlap any pair reached
  (simpler than per-pair integration and good enough for the Yes/No
  "are we seeing conflicts" question).
- **Dedup key format**: `conflict_opened:<strategyA>-<strategyB>` with
  the two names alphabetically sorted, so "A vs B" and "B vs A" share
  one 15-minute cooldown (per B54's send_sync).
- **Registry instance**: base_bot lazily constructs a local
  `StrategyRiskRegistry` for conflict detection only (attr
  `_conflict_reg`). This does NOT replace sim_bot's per-strategy halt
  registry — the detector is stateless w.r.t. halts, it only needs the
  two helper methods (`detect_directional_conflicts`, `exposure_snapshot`).
- **Log dir override**: tests set `PHOENIX_CONFLICT_LOG_DIR` to sandbox
  jsonl writes; production uses `logs/conflicts/YYYY-MM-DD.jsonl`.
- **B73 dashboard panel**: deferred. Scope-trade to keep the sprint
  tight; conflict data is already captured to jsonl and accessible via
  the CLI. Add panel in a follow-up if Jennifer wants live visibility.
