QUARANTINED 2026-05-27 — corrupt stale volumetric snapshots.

These 5 daily JSONL files each contain ~144 identical rows, all carrying the
SAME stale snapshot (ts=2026-05-19T23:03:00). Root cause: NT8 TickStreamer's
chart lost its volumetric data-feed connection on 2026-05-19; the bridge kept
serving the last-good volumetric_latest.json, and the recorder's dedup was
broken (2048-byte tail-read couldn't parse the ~5KB JSONL lines), so it
appended the same stale snapshot every 10 min for 8 days.

Fixes applied 2026-05-27:
  - tools/volumetric_snapshot_recorder.py _last_recorded_ts() rewritten to
    walk backwards for the full last line (dedup now works).
  - NT8 data-feed reconnected + TickStreamer placed on the live 1500-tick
    Volumetric chart. Stream restored ~22:56 CT 2026-05-27.

DO NOT feed these files into any footprint/order-flow backtest — they are
8 days of a single duplicated bar. Retained for forensic reference only.
Note: 2026-05-27.jsonl was left in the active dir (it has a leading stale
block + fresh rows appended after the stream recovered; filter ts=2026-05-19).
