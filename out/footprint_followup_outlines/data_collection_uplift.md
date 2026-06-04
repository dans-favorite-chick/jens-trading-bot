# Outline — Data Collection Uplift (DOM persistence)

_Follow-on outline from 2026-06-04 Footprint/CVD/DOM feasibility sprint._
_NOT a master prompt. Operator + Cowork will turn this into a proper sprint prompt._

## Hypothesis

Phase 1 of the feasibility sprint flagged the largest fillable gap as **DOM-depth snapshots over time**. Phoenix's live `tick_aggregator` computes `dom_bid_stack`, `dom_ask_stack`, `dom_imbalance`, `dom_bid_heavy`, `dom_ask_heavy`, and `dom_signal` (iceberg/absorption detection) on every DOM update, but NONE of this is persisted. The result:
- Backtests can't replay any DOM-dependent decision; they assume the gate is neutral.
- The 4.3% WR of `dom_pullback` over 30 days (Phase 3) suggests its primary signal lacks edge, BUT we can't reproduce the live DOM context that the strategy saw at entry, so we can't even tell if the failure was DOM-data-driven or signal-design-driven.

**Data-collection uplift hypothesis:** mirror the existing volumetric-recorder pattern to persist DOM snapshots to a `logs/dom_history.jsonl` (or `data/historical/dom/<YYYY-MM-DD>.jsonl`) file. Cost is ~1 day of engineering and storage is similar to volumetric_history.jsonl (~1MB/day). After 30 days of collection, run the Phase-3-style WIN/LOSS pattern analysis with DOM features added — which Phase 3 today only had via the FROZEN snapshot at entry (one point), not a time series.

## Files touched

- `bridge/bridge_server.py` — wherever `_handle_dom` lives (analog to `_handle_volumetric_bar`). Add a `_dom_history_path` + append-on-DOM-update writer.
- `tools/volumetric_snapshot_recorder.py` — pattern reference for the new `tools/dom_snapshot_recorder.py` (or absorb into same recorder).
- New: `logs/dom_history.jsonl` (new daily-rolled or single-file, TBD).
- New: `tools/replay_enrichment/recorded_dom.py` — analog to `recorded_cvd.py`, exposes `DOMReplayProvider.dom_state_at(ts)`.

## Protection status

`bridge/bridge_server.py` is in the PROTECTED ZONE. Adding a writer for a new log file requires operator sign-off per the CLAUDE.md protocol. The change is read-side-only (no order routing affected) so the risk is bounded to "extra disk I/O on the hot path" — needs perf review but should be cheap.

## Operator approval needed

YES — protected file (`bridge/bridge_server.py`). Standard protocol: propose diff in chat, wait for explicit go-ahead, ship + run full pytest suite, commit message with `OPERATOR-APPROVED: 2026-MM-DD`.

## Effort estimate

S (~1–2 days):
- 0.5 day: add DOM writer to bridge_server.py + recorder script
- 0.5 day: tests + integrate with existing recorder pattern
- 0.25 day: operator sign-off + commit

Plus 30 days of waiting before the dataset is useful for analysis — that's why this outline is LOWER priority than `filter_integration.md` (which can use live `dom_imbalance` from snapshots today) and `new_strategy_buildout.md` (which doesn't need DOM at all).

## Dependencies

- No conflict with `filter_integration.md` or `new_strategy_buildout.md` — orthogonal data domain.
- ONLY blocks the future "DOM-aware version of `filter_integration.md`" sprint, ~30 days out from when the DOM recorder ships.

## Out of scope for this outline

- Backfilling pre-2026-06 DOM data — not retroactively reconstructable from Databento (TBBO captures trades + BBO but not full DOM ladder).
- Resurrecting `dom_pullback` strategy — the Phase 3 result (WR 4.3%) is robust enough that re-enabling pre-data-collection is not justified.

## When to skip this outline entirely

If `filter_integration.md` runs to completion and finds the inverted-direction signal is genuine and tradeable WITHOUT DOM features (i.e. with volumetric-derived deltas alone), this outline becomes lower priority — DOM is an extra dimension we don't strictly need to act on the feasibility sprint's findings.
