---
description: Phoenix morning pre-flight check — runs the deterministic-verdict morning ritual (process/port/NT8/FMP/MQ/watcher checks) plus AI overnight commentary, writes artifacts, and only Telegram-pings on RED. Safe to invoke any time pre-session.
---

# Phoenix morning ritual

Run the morning pre-flight check. The verdict (GREEN / YELLOW / RED) is computed
purely from deterministic checks; AI commentary appears in a labeled appendix
that does NOT affect the verdict.

Steps the user wants you to take:

1. Run the routine via shell:
   ```
   python tools/routines/morning_ritual.py
   ```
   (omit `--skip-ai` if Anthropic is configured; pass `--skip-ai` for a fast
    deterministic-only run)

2. Read the printed verdict + per-check results.

3. Surface to the user:
   - The overall verdict (GREEN / YELLOW / RED) prominently
   - Any check that returned non-GREEN, with its detail
   - The AI overnight commentary if it ran (clearly labeled "advisory only")
   - Path to the Markdown + HTML + PDF artifact

4. If verdict is RED, suggest immediate action items:
   - process_down → check `KillSwitch` marker, run `PhoenixStart.bat`
   - nt8_single_stream RED with multi-stream → run docs/nt8_multi_stream_recovery.md
   - fmp_drift RED → check NT8 instrument selector
   - markers RED → KillSwitch may still be engaged from yesterday

5. If verdict is YELLOW, list the soft warnings without being alarmist —
   most YELLOW states are recoverable in <2 minutes.

Do NOT run `morning_ritual.py` while a Claude Code session is mid-edit; the
script writes to `out/morning_ritual/<date>.{md,html,pdf}` and any concurrent
Edit on those paths would conflict.
