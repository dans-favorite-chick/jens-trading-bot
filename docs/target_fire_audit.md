# Target-Fire Audit — S2, Exit Sprint

Date: 2026-04-22
Branch: `feature/exit-audit-and-safety`
Scope: Today's sim bot trades. Jennifer's observation — winners
going +20 points then reversing to LOSS without the LIMIT target
triggering.

---

## Today's target-miss statistics

Source: `logs/history/2026-04-22_sim.jsonl` (today) +
`logs/history/2026-04-21_sim.jsonl` (prior day for sample volume).
Total exits in window: **14** across both days.

| Metric | Count | Notes |
|---|---|---|
| Total exits | 14 | small sample — sim just ramping on this branch |
| `exit_reason = target_hit` | 1 | out of 14 exits, only one fired the LIMIT |
| Trades with MFE ≥ 15 ticks | 9 | favorable excursion ≥ 15t |
| Of those, exited without `target_hit` | 9 (**100%**) | bug threshold (>20%) massively exceeded |
| Trades with MFE ≥ 20 ticks | 7 | Jennifer's bar |
| Of those, exited without `target_hit` | 7 (**100%**) | |
| MFE ≥ 80 ticks (20 points) AND pnl < 0 | 0 | literal "+20 pts then red" case not seen yet |
| MFE ≥ 60 ticks AND capture_ratio < 30% | 0 | no gave-it-all-back case |

Conclusion: 100 % of trades with meaningful favorable excursion
exited via a reason other than `target_hit`. Jennifer's literal
"+20 points then red" pattern was not observed in today's sample,
but the rate at which managed exits pre-empt the LIMIT target is
suspicious and warranted the investigation and the new forensic
logging.

## Suspect trade IDs & timelines

All seven trades flagged at MFE ≥ 20 ticks, non-target exit:

| ts (CST-ish) | strategy | dir | entry | exit | mfe_t | mae_t | reason | pnl |
|---|---|---|---|---|---|---|---|---|
| 2026-04-22T08:45:57 | spring_setup | LONG | 26840.00 | 26861.50 | 105 | -42 | ema_dom_exit | +$41.28 |
| 2026-04-22T09:40:59 | spring_setup | LONG | 26986.25 | 27001.00 | 83 | -21 | ema_dom_exit | +$27.78 |
| 2026-04-21T18:38:37 | spring_setup | SHORT | 26743.75 | 26755.50 | 30 | -32 | stop_loss | -$25.22 |
| 2026-04-21T19:09:20 | spring_setup | SHORT | 26778.25 | 26790.75 | 28 | -49 | stop_loss | -$26.72 |
| 2026-04-21T19:32:33 | spring_setup | LONG | 26803.50 | 26814.50 | 58 | -2 | ema_dom_exit | +$20.28 |
| 2026-04-21T20:18:26 | spring_setup | LONG | 26806.25 | 26800.25 | 30 | -29 | time_stop | -$13.72 |
| 2026-04-21T20:39:58 | spring_setup | LONG | 26817.50 | 26827.75 | 58 | 0 | ema_dom_exit | +$18.78 |

## Root-cause analysis

### Target leg — was it Working in NT8?

Evidence reviewed:

1. `bridge/oif_writer.write_protection_oco` pre-B63 already wrote
   both stop + target and called `_verify_consumed()` on both. On
   ANY leg stuck it deleted both and returned `[]` so the caller
   (`bots/base_bot.py` B55 retry loop, lines 2640–2700) treated it
   as a full failure and retried — or, after 3 failures, flattened.
   That means if we have a live position today, **both** OCO files
   were consumed by NT8 at submit time.

2. `OIF_INCOMING` is empty right now (0 files). No stuck OIFs.

3. `logs/sim_bot_stdout.log` has zero `PROTECT:` lines — the sim
   bot hasn't even written any protection OCO during this stdout
   log window. (Either logging went elsewhere, or the stdout log
   was rotated, or sim hasn't taken a live-routed trade yet this
   session.) Cannot confirm from stdout directly.

4. Examined the 2 spring_setup winners that gave 105 t and 83 t of
   MFE. Targets were 26990 (150 pt away) and 27136.25 — physically
   unreachable inside the 5-minute holds those trades lasted. The
   managed exit `ema_dom_exit` firing was therefore by **strategy
   design**, not a failed target fire.

### Verdict on Jennifer's bug

1. For today's sample, the dominant cause of "no target_hit" is
   that target prices were placed well beyond the realistic MFE,
   and strategy-level managed exits (`ema_dom_exit`, `trend_stall`,
   `time_stop`) fired first. This is a **strategy-tuning concern**
   (targets possibly too far, or managed exits too eager) — S1's
   territory, not S2's.

2. We found **no direct evidence** in today's data of an OCO half-
   attach (target leg never made it to Working in NT8). But the
   pre-B63 code would have silently masked it — `_verify_consumed`
   returned all-stuck paths but the function cleaned both, leaving
   no trace in the logs beyond the `[OIF_STUCK]` error from
   `_verify_consumed`.

3. B63 closes this observability gap:
   - Distinguishes "both stuck" from "half-stuck".
   - Logs `[PROTECT_HALF]` warnings when exactly one leg is stuck
     and attempts a single in-flight retry of the missing leg
     before returning failure.
   - On retry failure, emits a cleanup `CANCEL_ALL` on the account
     so the lingering Working leg doesn't double-fill when the
     caller's outer retry re-submits the full OCO pair.

4. B64 adds `[EXIT_FORENSIC]` on every exit and `[TARGET_MISS_SUSPECT]`
   warnings on MFE ≥ 20 t + non-target-hit exits. Going forward,
   every trade that gave meaningful favorable excursion without
   firing the LIMIT will leave a searchable breadcrumb, tying the
   managed-exit reason to the MFE curve.

## Next steps (not in-scope for S2)

- S1: investigate whether `spring_setup` targets are set too far
  for their realistic hold duration.
- S1/S3: consider whether managed-exit triggers should respect a
  "let target work" zone once MFE ≥ ≈50 % of target distance.
- Live prod: monitor for any `[PROTECT_HALF]` or
  `[TARGET_MISS_SUSPECT]` warnings; they now have distinct tags.
