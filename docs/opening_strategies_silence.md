# Opening Strategies Silence — Investigation (S3, 2026-04-22)

Jennifer observed zero signals from any `opening_session` sub-strategy during 08:30-10:00 AM CT on 2026-04-22 (open_drive, open_test_drive, open_auction_in, open_auction_out, premarket_breakout, opening-ORB).

## Timeline (today, CT)

| Time CT | Event | Source |
|---|---|---|
| 08:30 | RTH open. No bot running (sim_bot not started). | — |
| 09:07:52 | sim_bot first start. Aggregator `[RESTORE] Loaded 461 time bars`. Strategy `opening_session` loaded with `ZERO GATE override`. | `logs/sim_bot_stderr.log:1` |
| 09:08:52 → 10:22:56 | 88 `opening_session` evals. **ALL returned `SKIP orb missing_fields`.** No other sub-evaluator ever fired. | `logs/sim_bot_stderr.log` |
| 10:22 | MenthorQ bridge reload, bot restarted. | `stderr` tail |
| 10:23:13 | sim_bot current PID start. Same symptom continues: `SKIP orb missing_fields` on every eval through 11:28. | `logs/sim_bot_stdout.log` (78 occurrences) |

NOTE: Jennifer's brief claimed sim_bot started at 10:23 and menthorq was 108h stale. Evidence contradicts both: sim_bot had its first start at 09:07:52 today, and `data/menthorq_daily.json` mtime is 2026-04-22 08:58 CDT (< 2 h old at 10:23).

## Hypothesis results

| # | Hypothesis | Verdict |
|---|---|---|
| 1 | Bots in pre-restart state during 08:30-10:00 | **TRUE (partially).** No bot ran 08:30-09:07. This alone explains silence for premarket_breakout (08:30-08:45) and all of 08:30-09:07 for every sub. |
| 2 | Timezone bug in `is_in_window()` | FALSE. `is_in_window` is tz-naive but `bar_now = datetime.fromtimestamp(bar.end_time)` in `tick_aggregator.py:597` produces a naive local-time datetime on a CT host; window checks are correct. (Minor hygiene: no explicit `ZoneInfo` — fragile if host TZ ever changes.) |
| 3 | `opening_type` classification failing | Indirect. After 09:07 start, RTH 5-min window (08:30-08:35) had already closed; `SessionLevelsAggregator._update_opening_type_at_835` only fires when `now_ct.time() >= 08:35` AND only on fresh bar flow — ruled in briefly at 09:08 evals, but the aggregator's 5-min bar for the 08:30-08:35 slot was **never captured live** (it was reconstructed from replay but not flagged as captured). End result: `opening_type=None`, which gates out 4 of 6 subs. |
| 4 | Gamma regime stale block | FALSE. Log shows `GAMMA: Loaded date=2026-04-21 complete=True`; gamma_regime in history = `positive_normal` throughout 08-11 window. |
| 5 | `PREENTRY_SKIP` from B50 | FALSE. No `PREENTRY_SKIP.*opening_session` entries. Skip is upstream at strategy evaluate(). |
| 6 | Warmup incomplete | FALSE. `[WARMUP] Aggregator state restored` at 09:07:52, and indicators were usable (other strategies produced non-SKIP results). |

## Root cause

**Field-name / field-coverage mismatch between producer and consumer.** `_evaluate_orb` (the only sub whose window is open for ~all of 08:45-14:30) reads `market["rth_5min_close_last"]` (line 736) — **this key is NEVER produced** by `SessionLevelsAggregator.get_levels_dict()` (only `rth_5min_close` exists). So `_evaluate_orb` always hits `SKIP orb missing_fields`, and because the dispatcher evaluates it last-in-the-always-open-window, that's the only message logged.

Additionally, the following consumer fields are missing from the producer and would have silently blocked the corresponding subs once the bot was up:

| Consumer field | Needed by | Producer status |
|---|---|---|
| `rth_5min_close_last` | ORB | **NOT produced** |
| `rth_1min_open/high/low/close/volume` | open_auction_in | **NOT produced** |
| `avg_1min_volume` | open_drive, open_test_drive, premarket_breakout, open_auction_in, open_auction_out | **NOT produced** |
| `rth_15min_high/low` | (not read by evaluators but computed) | produced (unused at consumer) |

With those fields missing and the bot starting at 09:07 (after the 08:30-08:45 premarket_breakout window), **no sub-evaluator could ever clear its `missing_fields` guard**, regardless of market conditions.

## One-line fixes

- **R1 (primary).** Populate the missing consumer fields in `SessionLevelsAggregator.get_levels_dict()` (or refactor evaluators to use `rth_5min_close`): track the last-closed 5m bar, and add a 1m rolling window aggregator for open/high/low/close/volume + moving-avg-of-1m-volume.
- **R2.** Restart discipline: start sim_bot by 08:00 CT via scheduled task so premarket_breakout and opening-type 08:30-08:35 bar are captured live.
- **R3 (observability).** The dispatcher's silent "NO_SIGNAL + empty reason" path in `history.jsonl` eval events makes silences invisible. Surface the most-recent `_log_eval` reason into the Signal-less result (B66).

## Actions taken this sprint slot

Given the fix for R1 requires adding a 1m bar aggregator (structural; out-of-scope for the silence-investigation slot without risking 720-test baseline), this slot delivers:

- **Investigation report** (this file) with concrete field-mismatch evidence, so S4/S5 can take R1 directly.
- **B66 (observability fix)** — ensure `opening_session.evaluate()` returns a _reason_-bearing sentinel in the history eval event when every sub skipped, so "silence" tomorrow shows up as `NO_SIGNAL all_subs_skipped_missing_fields` rather than empty string.

R1 deferred to a follow-up with design spec: extend `SessionLevelsAggregator` with `_rth_1min_rolling` (last completed 1m bar) and `_avg_1min_volume` (30-bar EMA over RTH), + rename `rth_5min_close` snapshot key emitted as `rth_5min_close_last` (alias both for compat).

## Test baseline

`python -m pytest tests/ --collect-only -q` → **720 tests** (brief said 714; project has grown 6).
