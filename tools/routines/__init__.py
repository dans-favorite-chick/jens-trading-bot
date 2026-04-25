"""Phoenix Routines (post-2026-04-25 §3.6).

Three autonomous routines + shared scaffolding:
  - morning_ritual.py        06:30 CT Mon-Fri — deterministic pre-flight verdict
  - post_session_debrief.py  16:05 CT Mon-Fri — chains PhoenixGrading + risk metrics + AI debrief
  - weekly_evolution.py      Sunday 18:00 — adaptive-params proposals with validation checkboxes

Common scaffolding lives in `_shared.py`:
  - RoutineReport            Markdown + HTML + verdict tracker
  - DigestQueue              File-backed pending-digest queue (avoids alert fatigue)
  - call_claude / call_gemini AI wrappers, fail-soft if no key
  - send_telegram_now        Bypass queue for RED / system-down alerts
  - write_artifacts          out/<routine>/YYYY-MM-DD.{md,html,pdf}
"""
