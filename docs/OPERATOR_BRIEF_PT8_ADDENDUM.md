# Operator Brief — pt8 Addendum (3-agent deep dive close)

**Date:** 2026-05-22 00:30 CT
**Branch:** `weekly-evolution/2026-05-17`
**Commits this round:** `c4e3c29` pt8 (pushed)
**Operator actions before tomorrow's open:** see §5 (TWO items, both ~30 sec)

---

## TL;DR

You asked four things. Direct answers:

1. **"Dig deeper into the microstructure filter."** — Done. The `msu_score` formula
   was measuring real microstructure with the **wrong sign**. Each of 4 of the 5
   deduction blocks rewarded adverse-selection conditions (perfect-tape entries
   = exactly when informed money is offloading into retail chase). IC of -0.152
   across 12k trades (z ≈ 16+) is too consistent to be random. **Fix shipped pt8:**
   `INVERT_PER_B031 = True` flag in `core/microstructure_filter.py` inverts blocks
   2-5 (spread block kept — wide spread is real slippage). Still advisory-only;
   watch IC over the next 1,000 trades and confirm it flips to ~+0.15.

2. **"Fix bugs/gaps in volumetric recorder and MBP-1."** — Done. Two-sided fix:
   - **Python side** (already shipped pt7): scheduled task fixed.
   - **C# side** (shipped pt8 — needs operator NT8 redeploy): `TickStreamer.cs` had
     a one-shot `volumetricWarned` flag that silently no-op'd forever after the
     first warning. That's the smoking gun for the 2026-05-19 23:03 CT cliff that
     went unnoticed for 4 days. Replaced with a 5-min rate-limited timestamp.
   - **MBP-1 audit**: `bid`/`ask` ARE true MBP-1 (per-tick top-of-book via
     `Calculate.OnEachTick` + `GetCurrentBid/GetCurrentAsk`). DOM fields are L2
     throttled to 500ms — lossy for sweep detection but adequate for "is the
     book lopsided." True MBP-10 capture deferred (separate sprint).
   - **Bridge hardening**: every volumetric write now stamps a `writer` field
     (`bridge_server@host/pid`) + `received_at` epoch into the persisted JSON,
     plus a `_LAST_VOLUMETRIC_EMIT_TS` heartbeat the dashboard can alarm on.

3. **"Go through each strategy one more time… replace old confluences with the
   best ones."** — Done. Agent B walked all 12 live strategies. Most are already
   correctly gated (after pt5/pt6/pt7). Concrete code changes shipped pt8:
   - `g_inside_bar_breakout` — added `tf5m_es_gate` (mirrors `e_multi_day_breakout`
     which lifted 77.8% → 96% WR with the same gate).
   - `vwap_band_reversion` — added `regime_veto(OPEN_MOMENTUM)` + fixed a B3-class
     wallclock backtest-unsafety bug.
   - `bias_momentum` — demoted two noise voters (`VWAP_relation` IC -0.04 and
     session `cvd_sign` IC 0.003) from score contributors to log-only labels.
   See §2 for the full per-strategy answer the agent produced.

4. **"Make sure old strategies are fully replaced by new ones. Bot must not be
   confused. Verify trading times. Verify TickStreamer + ES reading."** —
   - **Bot confusion check: NO confusion.** 12 strategies live, 14 inert
     (enabled=False, dispatch loop skips before any evaluate() call). All 11
     §1.1 Phase 13 winners correctly enabled+validated; all 4 §A kills correctly
     disabled. Verified by Agent C and pinned by a new CI guardrail
     (`test_allowed_legacies_stay_disabled`).
   - **Trading windows: clean.** 0 hard mismatches across the 12 live strategies.
     2 minor cosmetic gaps (vwap_band_pullback has no explicit window by design;
     raschke_baseline hardcodes its RTH window instead of reading config — neither
     is broken). Table in §3.
   - **TickStreamer: running and connected.** NT8 connections to bridge on :8765
     active, instrument `MNQM6` confirmed. Volumetric portion broken on NT8 side
     (B-032; operator must reload chart — see §5).
   - **ES reading: split-brain.** Bot reads ES *price* every 2 min via external
     MarketIntel polls (Alpaca + yfinance) → `es_nq_rs` field populated reliably
     (this is what every `tf60m_es_gate` consumes). Bot does NOT receive MES
     *bars* through the bridge — the `es_nq_confluence` strategy that needs
     them is dormant. See §4 for the full picture.

---

## 1. msu_score — what changed and why

**File:** `core/microstructure_filter.py`
**Flag:** `INVERT_PER_B031 = True` (line 38, single constant — reversible)

Old behavior (anti-edge):

| Block | Old reward | Adverse-selection meaning |
|---|---|---|
| 1. Spread | tight spread → no penalty | (this one was correct) |
| 2. DOM persistence | dom-supports-direction → no penalty | bid wall in your LONG = where informed sellers sweep through |
| 3. Delta confirms | CVD positive on LONG → no penalty | retail buying has already moved price = entering on top |
| 4. Recent price | price rising into LONG → no penalty | classic chase entry |
| 5. DOM signal align | dom_analyzer aligned → +5 bonus | informed footprint absorbing your side |

After pt8 (block 1 unchanged, 2-5 inverted):

| Block | New behavior |
|---|---|
| 1. Spread | tight spread → still no penalty (kept — real slippage cost) |
| 2. DOM persistence | dom-supports-direction → -25 (adverse-selection trap) |
| 3. Delta confirms | CVD confirming → -20 (retail chasing) |
| 4. Recent price | price chasing into signal → -15 (chase entry) |
| 5. DOM signal align | dom_analyzer aligned → -15 (informed absorbing); opposing → +5 |

**Important:** msu_score remains **advisory-only** — never blocks a trade
(`base_bot.py:4281-4289` logs it). To revert for A/B comparison, change the
single constant to `INVERT_PER_B031 = False`. Watch `[MICRO] score=...` log
lines over the next 1,000 trades; expect IC to flip to ~+0.15.

---

## 2. Per-strategy answer (Agent B summary)

For each strategy I list **what changed pt8** vs. **what's already correct**.
Full deep-dive analysis in `docs/OPERATOR_BRIEF_PT5_PT7.md` §2 + agent transcript.

| Strategy | Status | Change pt8 |
|---|---|---|
| `bias_momentum` | ✅ Already has `tf60m_es_gate`, `regime_veto(OVERNIGHT_RANGE)` (pt5/pt6) | **Demoted 2 noise voters** (VWAP_relation +20→0, cvd_sign ±10/−5→0) |
| `spring_setup` | ✅ Has `tf60m_es_gate` (pt6) | None (B-035 DOM-sign instrumentation deferred) |
| `ib_breakout` | ⚠️ No `tf60m_es_gate` | None — deferred per B-036 (small-n discipline) |
| `opening_session.orb` | ✅ Has all 4 gates (pt6) | None |
| `opening_session.open_drive` | ✅ Has `tf60m_es_gate` (pt6) | None (B-037 sample-buildup watch) |
| `vwap_band_pullback` | ✅ Has `regime_veto(OPEN_MOMENTUM)` (pt6) | None |
| `vwap_band_reversion` | ⚠️ Was missing veto + had B3 wallclock bug | **Added `regime_veto(OPEN_MOMENTUM)` + fixed `market.get("now_ct")`** |
| `vwap_pullback_v2` | ✅ Has `tf60m_es_gate` (pt6) | None |
| `es_nq_confluence` | ⚠️ Dormant (no MES bridge feed) | None (D4 infra prerequisite) |
| `a_asian_continuation` | ⚠️ No `tf60m_es_gate` | None — needs overnight-specific voter backtest first |
| `e_multi_day_breakout` | ✅ Has `tf5m_es_gate` (pt6, the +18pp WR gate) | None |
| `g_inside_bar_breakout` | ⚠️ Was missing `tf5m_es_gate` despite same structure | **Added `tf5m_es_gate`** (mirrors e_multi_day_breakout) |

---

## 3. Trading-window audit (Agent C summary)

Full table in agent transcript; here's the punch line per strategy:

| Strategy | Code window | Mismatch? |
|---|---|---|
| `bias_momentum` | any-hour + regime-gated | ✅ |
| `spring_setup` | any-hour (wick pattern) | ✅ |
| `ib_breakout` | IB 08:30-08:40 CT → breakout after | ✅ |
| `opening_session.{orb,open_drive,...}` | per-sub: open_drive 08:35-09:00, orb 08:45-14:30, etc. | ✅ (matches docstring word-for-word) |
| `vwap_band_pullback` | any-hour (relies on `regime_veto(OPEN_MOMENTUM)`) | ⚠️ no explicit window by design |
| `vwap_band_reversion` | RTH minus 08:30-09:30 + `regime_veto(OPEN_MOMENTUM)` (pt8) | ✅ |
| `vwap_pullback_v2` | overnight only (17:00-04:59 CT per J.2) | ✅ |
| `es_nq_confluence` | any-hour (dormant) | ✅ |
| `a_asian_continuation` | 03:00-08:00 CT | ✅ |
| `e_multi_day_breakout` | 08:45-13:00 CT | ✅ |
| `g_inside_bar_breakout` | 08:45-14:00 CT, 5m boundaries only | ✅ |
| `raschke_baseline` | 08:30-15:00 CT (hardcoded in `_in_rth()`) | ⚠️ cosmetic — should read config |

**No hard mismatches.** 2 cosmetic gaps documented.

---

## 4. TickStreamer / ES feed — full status

**TickStreamer (C# indicator in NT8):**
- ✅ Running. NT8 connects to bridge :8765 from PIDs you can see in
  `logs/connection.log`. Instrument `MNQM6` confirmed.
- ✅ Tick stream live (bid/ask MBP-1 + 1m/5m bars + DOM @ 500ms throttle).
- ❌ **Volumetric snapshot writes stopped 2026-05-19 23:03 CT** (B-032). The
  C# indicator's `EmitVolumetricBar()` is silently failing the `Bars.BarsType
  as VolumetricBarsType` check. **Operator action: §5 step 1.**

**ES reading (split-brain):**

| ES data type | Source | Status | Used by |
|---|---|---|---|
| `es_nq_rs` (NQ vs ES % relative-strength) | external poll (Alpaca + yfinance, every 2 min via `core/market_intel.py`) | ✅ Live | every `tf60m_es_gate` and `tf5m_es_gate` call → used by **all 7** pt6 strategies + bias_momentum |
| `market["mes_bars_5m"]` (real MES futures 5-min bars) | bridge → NT8 TickStreamer | ❌ Not flowing | `es_nq_confluence` strategy — **dormant** until this feed lands |

**Bottom line:** ES *gating* works fine across the whole bot. ES *bar correlation*
strategy is dormant until D4 infra lands (add MES as a 2nd instrument in
TickStreamer.cs, or accept the dormant state — plan §1.1 lists it conditional on
MES feed).

---

## 5. Operator actions required before tomorrow's RTH open

### Action 1 — Reload TickStreamer indicator (B-032 NT8 side; from pt7 brief)
30 sec, fixes data/volumetric_latest.json so it starts updating again.

1. Right-click MNQM6 chart in NT8 → Indicators…
2. Remove Phoenix TickStreamer → Apply
3. Add it back → Apply → OK
4. Verify within 1 min: `data/volumetric_latest.json` mtime advances.

### Action 2 — NEW: redeploy edited TickStreamer.cs (pt8 hardening)
Per memory note `nt8_tickstreamer_dual_path.md`, repo and NT8 indicator files
are separate. Required for the silent-failure fix to be active:

1. Copy `C:\Trading Project\phoenix_bot\ninjatrader\TickStreamer.cs`
   → `C:\Users\Trading PC\Documents\NinjaTrader 8\bin\Custom\Indicators\TickStreamer.cs`
2. In NinjaTrader: Tools → NinjaScript Editor → F5 (Compile All)
3. Right-click chart → Indicators → Reload (or just re-add TickStreamer — covers
   both actions 1 and 2 in one step)

Verify in the NT8 Output window after reload: if the chart is still set to a
non-Volumetric bar series, you'll see the "TickStreamer: chart is NOT a
Volumetric bars series — …" message printed once, then re-printed every 5
minutes until you fix the chart configuration (instead of going silent forever
like it did 2026-05-19 → 2026-05-22).

---

## 6. What to watch tomorrow

Same windows as pt7 brief plus:

- **`[MICRO] score=...` log lines** — values should now look INVERTED vs. yesterday
  (a setup that scored 95 yesterday should score in the low single digits today,
  and vice versa). Confirms the sign-flip is wired and active. Until 1,000
  trades land, this is observation-only.
- **`tf5m_es_gate` rejects on `g_inside_bar_breakout`** — new gate; expect to
  see `NO_SIGNAL tf5m_disagree` lines on chop days.
- **`vwap_band_reversion` regime rejects** — new gate; expect `NO_SIGNAL
  regime_veto=OPEN_MOMENTUM` lines during the open.

---

## Sprint commit ledger

| pt | Commit | Headline |
|---|---|---|
| pt5 | `f0dbce1` | 3 operator-approved gates (overnight veto, inline tf60m+ES, ORB cap 80→110) |
| pt6 | `c7b495a` | 7-strategy confluence-gate sweep using shared `core/confluence_gates.py` |
| pt7 | `06c55b7` | B-032 Python-side fix (volumetric scheduled-task PATH) |
| pt7-brief | `14cad72` | Operator brief pt5 → pt7 |
| pt8 | `c4e3c29` | 3-agent deep dive (msu sign-flip, NT8 hardening, per-strategy gates) |

Validation across all 4 commits: 2 of those changed test count (pt6 +19, pt8 +1).
Current: **2207 passed, 14 skipped, 0 failed** in 97s.

Both bots (sim PID 27976, prod PID 124108) restarted post-pt8 with all 12
strategies loaded clean. NT8 chart reload + C# redeploy still required for the
volumetric portion to come back online.
