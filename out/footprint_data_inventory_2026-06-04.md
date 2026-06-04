# Phoenix Footprint / CVD / DOM Data Inventory — 2026-06-04

_Phase 1 deliverable of the Footprint / CVD / DOM Feasibility Research sprint._
_Source of truth for what Phoenix ALREADY has before deciding whether to build/buy more._

---

## TL;DR

Phoenix already computes, persists, and replays a genuinely rich order-flow stack — far more than "we have nothing." The persisted volumetric stream (`logs/volumetric_history.jsonl`) carries per-bar level-by-level imbalance lists, stacked-imbalance flags, max-imbalance ratios up to ~75x, POC per bar, and signed delta — i.e. the primitives a Sierra Chart / NinjaTrader Order Flow+ user would call "footprint." What we LACK is per-tick tape and DOM-depth snapshots persisted over time. That's the real gap, not "footprint data."

---

## (a) What Phoenix computes LIVE

Source: `core/tick_aggregator.py` (`bot_name`-scoped, one per bot process).

| Field | Cadence | How it's built | Persisted in snapshot? |
|---|---|---|---|
| `cvd` (running session CVD) | per tick | Tick aggressor classification: `price>=ask → buy_vol`, `price<=bid → sell_vol`, else 50/50 split. Daily-reset on calendar day change. Fallback to price-direction when bid/ask absent. | yes |
| `bar_buy_vol`, `bar_sell_vol`, `bar_delta` | per 5m bar | Aggressor classifications accumulated over the 5-minute window. Released on bar close. | yes |
| `delta_history_5m` (deque) | per 5m bar | Last 10 `bar_delta` values, used for price/CVD-divergence detection | yes |
| `vsa_signal_5m` | per 5m bar | Volume-Spread-Analysis label: ABSORPTION / EFFORT_UP / EFFORT_DOWN / TEST_UP / TEST_DOWN / NEUTRAL | yes |
| `vol_climax_ratio` | per 5m bar | `last_bar_vol / avg_vol_5m` rolling-20 | yes |
| `dom_imbalance` | per DOM update | `bid_stack / (bid_stack + ask_stack)` from `process_dom()` | yes |
| `dom_bid_heavy`, `dom_ask_heavy` | per DOM update | imbalance > 0.60 or < 0.40 booleans | yes |
| `dom_signal` | per DOM update | `DOMAnalyzer.get_dom_signal()` — iceberg + absorption detection | yes |
| `dom_bid_stack`, `dom_ask_stack`, `dom_depth` | per DOM update | Raw bid/ask totals from the DOM message | yes |

**Derived on top of these (`core/cvd_trend_health.py`):**

`CVDTrendHealth.assess(direction)` returns a dict per minute with `slope`, `agreement`, `veto`, `veto_threshold`, etc. The live bot calls `cvd_health.update_bar(close, cumulative_cvd)` once per completed 1m bar and queries `.assess()` at entry. This is the gate that several strategies (notably `bias_momentum`) read as `market["cvd_health"]` / `market["cvd_health_short"]`.

**Strategies that consume CVD/footprint/DOM today** (count = grep hits for `cvd|bar_delta|delta_history|dom_*`):

| Strategy | Hits | Role of CVD/footprint/DOM | Status (validated/enabled) |
|---|---:|---|---|
| `bias_momentum` | 50 | `cvd_health` veto, `bar_delta` in explosive-bar VWAP-bypass, `cvd` directional gate in chop regimes | validated, enabled (live canary) |
| `footprint_cvd_reversal` | 56 | Primary signal — IQS scoring (HTF level + CVD divergence + footprint confirmation + CVD compression) | **enabled=False, validated=False** — see Phase 2 |
| `nq_lsr` | 15 | uses `cvd` directional context | TBD in Phase 2 |
| `dom_pullback` | 13 | `dom_imbalance` / `dom_signal` as primary signal | TBD in Phase 2 |
| `orb_v2` | 12 | CVD veto-style filter | TBD in Phase 2 |
| `ib_breakout` | 10 | CVD confirmation | TBD in Phase 2 |
| `orb_fade` | 9 | CVD veto | TBD in Phase 2 |
| `vwap_pullback_v2` | 7 | bar_delta + CVD | TBD in Phase 2 |
| `opening_session` | 7 | CVD confluence | TBD in Phase 2 |
| `spring_setup` | 5 | CVD divergence | TBD in Phase 2 |
| `vwap_pullback` | 3 | CVD informational | TBD in Phase 2 |
| `high_precision` | 3 | CVD | TBD in Phase 2 |
| `big_move_signal` | 1 | CVD touch | TBD in Phase 2 |
| `es_nq_confluence` | 1 | CVD touch | TBD in Phase 2 |
| `base_strategy` | (interface) | — | — |

10 of 14 application strategies materially consume order-flow inputs. This is NOT a system with "no CVD usage" — quite the opposite.

---

## (b) What Phoenix PERSISTS to disk

### Canonical file: `logs/volumetric_history.jsonl`

- **Size as of 2026-06-04 16:00 CT:** 14.47 MB
- **Records:** 10,787
- **Coverage:** 2026-05-04 21:24:44 → 2026-06-04 15:58:02 (31 calendar days)
- **Tick-bar cadence:** every 1,500 NQ ticks (NOT a time bar)
- **Bar timestamp:** naive local wall-clock (no tz), assumed CT per machine convention

### Per-record schema

```
{
  "type": "volumetric_bar",
  "ts": "2026-05-04T21:24:44.0330000",   # naive, 7 frac digits, machine-local
  "instrument": "MNQM6",
  "bar_size_ticks": 1500,
  "open": 27818.25,
  "high": ...,
  "low": ...,
  "close": ...,
  "delta": 176,                          # buy_volume - sell_volume (signed)
  "total_volume": 1710,
  "buy_volume": 943,
  "sell_volume": 767,
  "poc": 27815.75,                       # Point Of Control price (max-vol level)
  "imbalances": [
    {"price": 27808.5,  "bid_vol": 12, "ask_vol": 2,  "ratio": 6.0,  "side": "sell"},
    {"price": 27808.75, "bid_vol": 20, "ask_vol": 2,  "ratio": 10.0, "side": "sell"},
    ...
  ],
  "stacked_buy":  false,                 # ≥3 adjacent same-side imbalances (long bias)
  "stacked_sell": true,                  # ≥3 adjacent same-side imbalances (short bias)
  "max_imbalance_ratio": 14.0,           # max ratio observed in this bar
  "cvd_session": 1750.0                  # running session CVD value at bar close
}
```

### Distributions across the 31-day window (first-200-bar sample)

| Field | Min | p25 | p50 | p75 | Max |
|---|---:|---:|---:|---:|---:|
| `delta` (per 1500-tick bar) | −1374 | −240 | +36 | +239 | +1430 |
| `max_imbalance_ratio` (nonzero) | 5.67 | — | — | — | 75.00 |
| `stacked_buy=True` | — | — | — | — | ~40% of bars (sample) |
| `stacked_sell=True` | — | — | — | — | ~41% of bars (sample) |

`max_imbalance_ratio` mean of nonzero samples = ~20x. That's a HEAVY ask/bid skew at the price-level scale.

### Daily-rolled volumetric file copies

`data/historical/volumetric/<YYYY-MM-DD>.jsonl` — separate daily slices (10 days visible, 2026-05-18 → 2026-06-04). Likely written by `tools/volumetric_snapshot_recorder.py`. Useful for per-day analysis but the canonical full-history file (`logs/volumetric_history.jsonl`) is what `RecordedCVDProvider` and the strategy load.

---

## (c) What's REPLAYABLE via `tools/replay_enrichment/recorded_cvd.py`

### API surface

```python
from tools.replay_enrichment.recorded_cvd import RecordedCVDProvider

provider = RecordedCVDProvider(
    volumetric_path="logs/volumetric_history.jsonl",
    lookback_bars=6,
    veto_threshold=-0.3,
    instrument=None,
)
health = provider.health_at("2026-05-15T09:23:11", direction="LONG")
# returns the same dict CVDTrendHealth.assess("LONG") would have produced
# at that timestamp, given the recorded order-flow history.
```

### How it works

1. **Loads** `volumetric_history.jsonl`, filters by `instrument` if requested, sorts by `ts`.
2. **Aggregates** tick-bars into per-minute buckets (`floor(ts, minute=1)`). Within a minute, deltas sum and the LAST tick-bar's close wins.
3. **Replays** by walking minute buckets chronologically: maintains a cumulative session CVD (reset on calendar-date change in machine-local tz), calls `cvd_health.update_bar(close, cumulative_cvd)` once per minute, caches LONG and SHORT `assess()` dicts.
4. **Queries** `health_at(ts, dir)`: returns the exact-minute hit if cached, otherwise the most-recent prior minute within the **same calendar date** (no cross-session bleed).

This mirrors the live bot's CVD pipeline byte-for-byte EXCEPT for one approximation: per-bar `delta` is summed across tick-bars within a minute, where the live bot accumulates from raw aggressor classifications. The difference is negligible at minute resolution because each tick-bar's `delta` IS the real tick-classification sum for that 1500-tick window.

### What it ENABLES

- Backtest replay of `cvd_health` veto decisions with real (not approximated) data — already used by `tools/reconcile_sim_vs_backtest.py --real-enrichment`.
- Per-trade reconstruction of the `cvd_health` state at trade entry for any trade with an `entry_time` falling in the recorded window — i.e. the exact analysis Phase 3 needs.
- Direction-aware: separate LONG and SHORT caches, so a same-minute lookup can compare both.

### What it does NOT replay

- DOM depth (`dom_bid_stack`, `dom_ask_stack`, `dom_imbalance`, `dom_signal`) — these come from DOM messages, not from `volumetric_history`. Not persisted historically.
- Raw per-tick stream (would need `data/historical/raw_ticks/` or a DataBento subscription).
- `vsa_signal_5m`, `bar_delta` time-bar state, `vol_climax_ratio` — these are 5m-time-bar values, not 1500-tick-bar values. Reconstructable from raw ticks but not from `volumetric_history` (which is a tick-bar stream).

---

## (d) What's MISSING — the actual gaps

| Gap | Why it matters | Cost to close |
|---|---|---|
| **DOM-depth snapshots over time** | Live bot uses `dom_imbalance`, `dom_bid_heavy`, `dom_ask_heavy`, `dom_signal` (iceberg/absorption) — none persisted. Can't replay any DOM-dependent decision. Forces backtests to assume the DOM gate was neutral. | Engineering only: write `dom_history.jsonl` from `bridge_server._handle_dom`. Operationally trivial (~1 file). Doesn't solve the "we have no history" problem for past months. |
| **Per-tick tape over time** | Couldn't reconstruct `bar_delta` for arbitrary timeframes, can't do tick-level imbalance analysis, can't measure trade-by-trade order-flow at sub-second resolution. | Either Phoenix-side (`tools/tick_recorder.py` already exists in the tools tree? — TBD) or a paid provider (Databento MBO MNQ ~$200–$500/mo, multi-year history available). |
| **VSA signal / 5m bar delta over time** | The `vsa_signal_5m` and 5m `bar_delta` aren't persisted — they're live-only fields in `tick_aggregator.snapshot()`. | Could reconstruct from raw ticks if persisted (gap #2), or persist them at snapshot emit time (engineering, ~1 day). |
| **Pre-2026-05-04 volumetric history** | First record is 2026-05-04 — coincident with TickStreamer.cs volumetric emitter ship date. We CANNOT analyze CVD/footprint patterns for the 5y backtest window. | Cannot retro-build. Either accept the 31-day window for live analysis OR pay for a Databento backfill. |
| **MES / cross-market correlation tape** | `es_nq_confluence` strategy depends on it; today it sits enabled=True, validated=False because the MES feed hasn't landed (per config comment). | NT8 indicator on MES chart + bridge plumbing. ~1–2 weeks engineering. |
| **Sweep tape (lit cross-exchange aggregator)** | Real institutional sweep detection (e.g. CME futures + spot ETF cross-print bursts) is beyond Phoenix's current capability. | Genuinely third-party — needs a market-data partnership (e.g. Polygon, Quodd). Out of scope for an SMB-style operation. |

---

## Inventory verdict

**Phoenix HAS:**
- 31 days of per-1500-tick volumetric bars with full footprint primitives (per-level bid/ask vol, stacked-imbalance flags, POC, signed delta, max ratio).
- A live CVD pipeline (tick aggressor → session CVD → `CVDTrendHealth` veto) consumed by ≥10 strategies.
- A replay shim (`RecordedCVDProvider`) that gives historical `cvd_health.assess()` dicts byte-identical to live.
- A DOM analyzer (iceberg/absorption detector) running live but not persisted.

**Phoenix LACKS (gaps that matter):**
- Historical DOM-depth snapshots (live-only).
- Per-tick tape persistence (couldn't reconstruct fine-grained features over time).
- Volumetric history older than 2026-05-04 (~30 days as of the report date).

**Phoenix DOES NOT NEED** (often-cited "footprint" feature that's actually a non-issue):
- A new "footprint engine" written from scratch — Phoenix already has the primitives.
- A new MenthorQ-style HTF level overlay — `core/price_action_levels.py` ships this.
- A new CVD divergence detector — `delta_history_5m` + bar highs/lows are already in the snapshot.

The real question for Phase 2 / 3 / 4 is not "should we build footprint?" but "**given that Phoenix already has these primitives, does the disabled `footprint_cvd_reversal` strategy + the recorded 30-day volumetric window contain enough signal to justify either (a) re-enabling that strategy, (b) using its primitives as a filter on the existing 10 strategies, or (c) declining both?**"

That's what Phase 2 and Phase 3 answer.
