# PHANTOM-NT8 — 30-day Pattern Audit

_Phase 4 of the diagnostic sprint. Authoritative source for NT8-side throughput: `C:\Users\Trading PC\Documents\NinjaTrader 8\log\log.YYYYMMDD.00000.txt`._

## TL;DR

- **NT8 OIF processing has been on-and-off for the whole window** (29 days observed).
- **11 / 29 days have zero NT8-processed OIFs.** Most align with operator-idle days (weekends, multi-day breaks). Several do not.
- **2026-06-04 is the worst working day in the window.** Phoenix wrote 125 OIFs; NT8 processed 3. **97.6 % silent-rejection rate on a live trading day.**
- The all-day outage on 06-04 started cleanly at the boundary: the last `OIF, '...' processing` entry in NT8’s log on 06-03 was **2026-06-03 22:20:53 CDT**. The next one was **2026-06-04 17:47:51 CDT** — a **19 h 27 m gap** during which Phoenix wrote 4 live entries (05:05, 05:11×2, 05:14) and 121 reconciliation flatten OIFs.
- The PhantomGuard at the bot layer correctly aborted all 4 live entries — **zero phantom positions resulted** from the outage.

## Source-of-truth definitions

| Counter | Source | Meaning |
|---|---|---|
| `NT8 OIF processed` | `log.YYYYMMDD.00000.txt`: `OIF, '(PLACE\|CANCEL\|CLOSEPOSITION\|MODIFY)..' processing` | NT8 ATI saw the file, parsed it, and entered the order-router. |
| `NT8 rejected: no position / no order` | same log, error-level row | NT8 parsed but bounced — e.g. `CANCEL ... order does not exist`, `CLOSEPOSITION ... no position for instrument`. These are *processed* outcomes, not silent rejects. |
| `phoenix_markers.jsonl errors` | same log: `Unknown OIF file type ... phoenix_markers.jsonl` | NT8’s file watcher fires on every file in `incoming/`; sees the jsonl marker file and logs an error because the extension is not `.txt`. Incidental noise; proves file-watcher is alive. |
| `Phoenix committed` | `restart_bridge_stderr.log`: `[OIF:...] committed oif...txt` | Bridge wrote the file to incoming/. |
| `Phoenix STUCK` | same: `[OIF_STUCK:...] NT8 did NOT consume 1 OIF` | Bridge’s 2 s polling loop never saw the file deleted. Strong evidence NT8 didn’t consume. |

## 30-day NT8-side throughput

| Date | NT8 OIF processed | no_pos | no_ord | jsonl errs |
|---|---:|---:|---:|---:|
| 2026-05-07 | 211 | 43 | 0 | 0 |
| 2026-05-08 | 109 | 6 | 0 | 2 |
| 2026-05-09 | **0** | 0 | 0 | 0 |
| 2026-05-10 | 18 | 1 | 0 | 0 |
| 2026-05-11 | 91 | 12 | 0 | 0 |
| 2026-05-12 | 4 | 0 | 0 | 0 |
| 2026-05-13 | **0** | 0 | 0 | 0 |
| 2026-05-14 | 105 | 17 | 0 | 1 |
| 2026-05-15 | 17 | 0 | 0 | 0 |
| 2026-05-16 | **0** | 0 | 0 | 0 |
| 2026-05-17 | 91 | 5 | 0 | 0 |
| 2026-05-18 | 99 | 3 | 0 | 0 |
| 2026-05-19 | 37 | 6 | 0 | 1 |
| 2026-05-20 | 72 | 3 | 0 | 1 |
| 2026-05-21 | 215 | 4 | 0 | 0 |
| 2026-05-22 | 33 | 10 | 0 | 0 |
| 2026-05-23 | **0** | 0 | 0 | 0 |
| 2026-05-24 | **0** | 0 | 0 | 0 |
| 2026-05-25 | 152 | 16 | 0 | 4 |
| 2026-05-26 | 199 | 12 | 0 | 0 |
| 2026-05-27 | **0** | 0 | 0 | 0 |
| 2026-05-28 | **0** | 0 | 0 | 0 |
| 2026-05-29 | **0** | 0 | 0 | 0 |
| 2026-05-30 | **0** | 0 | 0 | 0 |
| 2026-05-31 | **0** | 0 | 0 | 0 |
| 2026-06-01 | **0** | 0 | 0 | 0 |
| 2026-06-02 | 85 | 0 | 28 | 60 |
| 2026-06-03 | 263 | 110 | 13 | 42 |
| **2026-06-04** | **3** | 1 | 0 | **113** |

Totals: NT8 processed 1804 OIFs across 29 days; 224 phoenix_markers errors.

## Concentration analysis

### Time-of-day
The 05:05:12 incident sits in NT8's Globex overnight window (00:00–08:30 CDT). The prior 06-02 incident peaked in the RTH transition window (09:02–11:09 CDT). No tight time-of-day correlation — these are stuck windows that span hours, not minute-scale glitches.

### Strategy / account
06-04 today: 1 Sim101 entry (bias_momentum, the 05:05 incident); 16 SimBias Momentum sim entries. 06-03 yesterday: 10 distinct sim sub-accounts active (SimBias Momentum 78, Sim101 53, SimSpring Setup 28, SimDom Pull Back 12, SimVwap Reversion 17, etc.). No strategy-level concentration; the stuck-OIF condition is account-agnostic. Sim101 has slightly worse exposure simply because it is the prod-bot’s account.

### Regime
The 05:05 signal regime was `OVERNIGHT_RANGE`. The 06-02 prior incident was `MOMENTUM` / `MIXED`. Regime-agnostic. The stuck-OIF condition is in NT8 itself, not in Phoenix’s signal generation.

## Key inference

The data lets us separate two failure classes:

1. **Day-level NT8 outage** (the dominant pattern). When NT8 is restarted, paused, disconnected, or has its ATI/incoming-folder reader in a stuck state, *all* OIFs that day go un-consumed. This is the 06-04 failure mode. It can run for many hours (19 h 27 m in this incident; on 06-02 it ran ~2 h before recovering).

2. **Per-OIF transient stuck during otherwise-healthy day** (06-03: 408 committed, 103 stuck = 25 % stuck while NT8 processed 263). Likely race conditions, parser hiccups, or NT8 being busy with chart-strategy activity that delays the OIF-reader thread past Phoenix’s 2 s window. These are partially false-positive PhantomGuard triggers — NT8 *did* eventually consume some of them, just past the 2 s threshold.

The 05:05 incident is firmly in class 1.

## Implication for the fix proposal

- A bridge-side fix (longer `_verify_consumed` timeout, retry the OIF write, etc.) addresses class 2 but **not** class 1. During a 19 h outage no amount of retry helps.
- A real fix needs NT8-side observability + operator alerting: detect within minutes that NT8’s OIF reader is dead, page the operator, *and* prevent Phoenix from continuing to write OIFs that will pile up.
- The 232 RECONCILED OIFs currently piled in `incoming/` are evidence that the *cleanup* arm of PhantomGuard does not fire on the reconciliation code path. Fixing that is also in scope.
