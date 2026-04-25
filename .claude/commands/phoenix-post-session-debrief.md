---
description: Phoenix post-session debrief — chains today's PhoenixGrading output, computes risk metrics from trade_memory.json, scans logs for new error signatures, runs the AI debriefer, drains the consolidated digest queue, sends ONE Telegram, writes artifacts. Run after 16:00 CT.
---

# Phoenix post-session debrief

Run the post-session debrief. This routine intentionally chains the existing
`PhoenixGrading` scheduled task (which fires at 16:00 CT) — it expects today's
grade JSON to already be on disk at `out/grades/YYYY-MM-DD.json`.

Steps:

1. Run the routine:
   ```
   python tools/routines/post_session_debrief.py
   ```
   (will wait up to 120s for the grade file if it's not already there)

2. Surface to the user:
   - The grade summary (passed/total)
   - Risk metrics: total P&L, win rate, profit factor, daily Sharpe, max DD
   - Any NEW error signatures vs the rolling 7-day baseline
   - The AI debrief markdown
   - Path to the PDF (if reportlab is available)

3. The routine drains the DigestQueue — so today's morning_ritual report and
   any system-down events that landed in the queue are folded into the SAME
   consolidated Telegram. This is intentional; don't fire a second Telegram.

4. If trades=0, lead with that: was this a no-signal day or a halted day?
   - Cross-reference with morning_ritual GREEN/YELLOW/RED to disambiguate.

5. If new error signatures appeared, treat as a yellow flag for tomorrow's
   pre-market check.

This routine is normally fired by Windows Task Scheduler at 16:05 CT Mon-Fri
(post-session-debrief task chained 5min after PhoenixGrading). Manual
invocation is rare — used mostly for re-running on a non-trading day to
test the assembly pipeline.
