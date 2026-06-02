# Chart Orders Root Cause — 2026-06-02

**Operator observation:** 7+ BUY LIMIT MNQ M6 orders visible on Sim1
chart around mid-day 2026-06-02.

**Verdict: PROVEN.** The morning's 14 prod_bot PROTECT FAILED cycles
left 14 LIMIT entry OIFs in NT8's `incoming/` folder; when ATI came
back online ~11:00 CT, NT8 processed them as working orders. The
defensive layer that should have stopped this (`PHANTOM_GUARD`)
is **explicitly disabled for the Sim101 account** at
[bots/_trade_entry.py:802](bots/_trade_entry.py:802).

---

## Evidence chain

### 1. Morning entries that left naked LIMITs

Today's INTENT-to-OIF activity, both bots:

| Bot | INTENTs today | All in window | OIF_STUCK count | PROTECT FAILED count | After failure |
|---|---:|---|---:|---:|---|
| prod_bot (account = Sim101) | 14 | 09:02:54 → 09:38:55 | 145* | 14 | Falls to "assume filled (paper mode)" → PROTECT → cancel → all stuck |
| sim_bot (per-strategy sub-accounts, e.g. "SimBias Momentum") | 6 | 09:10:54 → 09:37:55 | 0 | 0 | Falls to `PHANTOM_GUARD` → aborts entry + removes stuck OIF cleanly |

\* The 145 OIF_STUCK lines in `prod_bot_stdout.log` include the morning
14 entries × ~5 follow-on OIFs (3 OCO retries + CANCEL +
emergency_flatten) plus earlier historical OIF_STUCK from 2026-05-27.
The 14 PROTECT FAILED count is the more authoritative figure for
"entries that left a naked LIMIT today."

### 2. The Sim101 exemption (the real bug)

`bots/_trade_entry.py:790-874` is the fill-confirmation branch.
Inside the `TIMEOUT` case (no fill ack within 5s), the code splits
three ways:

- **LIVE_TRADING=True:** abort immediately, log error. (Lines 790-796.)
- **LIVE_TRADING=False AND `_account != "Sim101"`:** run PHANTOM_GUARD.
  Globs `incoming/*_{tid}*.txt` — if any OIF is still sitting there,
  NT8 rejected it; abort, delete the stuck OIF, fire Telegram. (Lines 797-870.)
- **LIVE_TRADING=False AND `_account == "Sim101"`:** keep legacy
  "assume filled" behavior. (Lines 872-874.)

The `_account != "Sim101"` exemption was added (per the inline comment
B39/B48) to distinguish per-strategy sub-account paper trading
(sim_bot) from the single-account legacy mock (prod_bot Sim101). The
exemption made sense at the time — prod_bot on Sim101 was treated as
a fully-mocked path where every fill is synthetic.

But on 2026-06-02, prod_bot WAS using Sim101 AND submitting real OIFs
to NT8 AND those OIFs were being rejected by ATI. The Sim101
exemption sent every one of these into "assume filled," which then
flowed into the PROTECT cycle, which failed three times, which fired
CANCEL + emergency_flatten, all of which also got stuck. The entry
OIF was never consumed at time of write but was also never deleted —
it sat in `incoming/` until NT8 ATI eventually returned (the OUTGOING
folder shows the first NT8-written `Sim101_*.txt` ack at 11:09 CT).

When NT8 resumed, it processed every stale OIF in `incoming/`. The
14 entry-LIMIT OIFs each became a working BUY LIMIT order; the
CANCEL/flatten OIFs (also stale, tagged with the original trade_id)
were processed but did not necessarily match the working orders'
NT8-assigned order IDs, so they did not cancel them. Net result: a
chart full of working BUY LIMIT orders for prices set at 09:02-09:38.

### 3. Timestamp correlation

- Operator's "around mid-day" observation aligns with NT8 ATI
  recovering between 09:39 (last OIF_STUCK in the log) and 11:09
  (first NT8-written ack in `outgoing/`).
- Operator saw 7+ working LIMITs. Of the 14 morning entries, some
  may have been filled, rejected by NT8 as too-stale, or auto-flushed.
  7+ surviving is consistent with the 14 issued.
- 11:27 CT log line:
  `[OPEN:RECONCILED_Sim101_1d23f071] LONG 3x @ 30658.75 strat=big_move_signal`
  — `StartupReconciliation` adopted an orphan position from NT8.
  This is corroborating evidence that NT8 had positions/orders the
  bot was not tracking (i.e., they came from elsewhere — likely the
  morning's stale OIFs).

### 4. What sim_bot did right

Same `_trade_entry.py` code, different `_account` value, different
branch taken. Sample (sim_bot 09:10:54):

```
[OIF] WARNING [OIF:3b73a1c6] No fill confirmation after 5.0s
[Bot] ERROR [PHANTOM_GUARD:3b73a1c6] NT8 REJECTED order — 1 OIF(s) stuck in incoming/. Aborting entry + removing stuck legs. Account=SimBias Momentum strategy=bias_momentum
```

This is exactly the behavior prod_bot needs: detect stuck OIF, abort
entry, remove the stuck file, no naked LIMIT left behind.

---

## Connection to the NT8-Dead Auto-Pause design

The NT8-Dead Auto-Pause spec (`docs/superpowers/specs/2026-06-02-nt8-sink-auto-pause-design.md`)
prevents this from recurring by tripping a global pause on the first
`PROTECT ALL 3 RETRIES FAILED` event — so signals 2 through 14 would
have been gated at the entry path.

**However, today's actual root cause is more specific** than "no
auto-pause." Two complementary fixes are on the table:

| Fix | Stops at signal # | Side effect |
|---|---:|---|
| **(A) Lift Sim101 exemption from PHANTOM_GUARD** (`_trade_entry.py:802`) | **Signal #1** | Aborts cleanly per-attempt, no global pause state. Pure infrastructure fix. |
| **(B) NT8-Dead Auto-Pause (the spec)** | Signal #2 onward | Adds operator-visible state, requires manual /nt8_clear, persists across restart. Signal #1's entry still issues. |
| **Both together** | **Signal #1** AND prevents the 13 subsequent attempts from incurring even the abort path | Recommended. |

The implementation session should treat (A) as the primary fix for
this incident and (B) as the operational guard for any future
NT8-dead window. (A) alone would have left zero working LIMITs on the
chart today; (B) alone would have left exactly one.

(A) is a single-line edit in a non-protected file. The exemption was
added for "Sim101-only mock tracking" but the path is no longer
fully-mocked — prod_bot does write OIFs against Sim101. The exemption
should be removed (or narrowed to a `LIVE_TRADING is False AND
NT8_BRIDGE_DISABLED is True`-style flag) — but that's an
implementation decision for the next session.

---

## What would NOT have prevented this

- **The existing 3-retry PROTECT logic** — already in place; doesn't
  help because the entry OIF was already in `incoming/` before PROTECT
  even ran. The OCO retries are about protecting an assumed-filled
  position; they do not retract the entry order.
- **The existing PendingEntrySweeper CANCEL logic** — fires correctly,
  but the CANCEL OIF was also rejected by ATI, so the entry stayed
  intact.
- **Operator restart of prod_bot** — restarting the bot does nothing
  to the OIFs already sitting in NT8's `incoming/`. They survive bot
  restarts; only NT8 ATI consuming or the operator manually deleting
  them removes them.

---

## Files and line numbers cited

- `bots/_trade_entry.py:790-874` — the entire TIMEOUT branch
- `bots/_trade_entry.py:802` — the Sim101 exemption (the bug)
- `bots/_trade_entry.py:813-841` — the PHANTOM_GUARD body (the fix that prod_bot skips)
- `bots/_trade_entry.py:872-874` — the "assume filled (paper mode)" fall-through
- `logs/prod_bot_stdout.log` — search `PROTECT.*ALL 3 RETRIES FAILED` (14 matches), `OIF_STUCK` (~140 matches, including historical from 05-27)
- `logs/sim_bot_stdout.log` — search `PHANTOM_GUARD` (6 matches today)
- NT8 incoming/ folder — `oif784XXX_phoenix_48828_*.txt` files are prod_bot writes; `oif759XXX_phoenix_193064_*.txt` files are sim_bot writes
- NT8 outgoing/ folder — first today's NT8-written `Sim101_*.txt` ack at 11:09 CT confirms ATI return

## Self-second-guess

- "PROVEN" assumes the 7+ chart LIMITs the operator saw are the
  morning's stale OIFs. If the operator can confirm the prices of
  the working orders matched 09:02-09:38 entries (e.g., LIMIT @
  30587.25, 30604.75, 30616.75, 30612.75 from the prod_bot log), the
  match is airtight. If the chart prices don't match, there's a
  different source and I'd downgrade to LIKELY.
- I have not seen NT8's order log directly. Everything above is
  reconstructed from Phoenix-side logs + OIF folder timestamps.
- The Sim101 exemption is unambiguous in the code; the only question
  is whether the operator wants it lifted (and there's a B39/B48
  reason it was added — implementation session should re-read those
  comments before removing).
