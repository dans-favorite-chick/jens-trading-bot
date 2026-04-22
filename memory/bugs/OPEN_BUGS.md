# Phoenix Bot — Bug Tracker

_Newest-first ledger. OPEN = needs work. Everything else = historical audit trail._

_Last comprehensive audit: 2026-04-21 late evening. End-to-end validated with
`tools/test_all_accounts.py` → **17/17 NT8 sim accounts PASS** (entry fill,
flatten, FLAT verification)._

---

## 🔴 OPEN

_No open bugs as of 2026-04-21 20:30 CDT._

## ✅ RESOLVED — 2026-04-21 (today's fixes)

### B47 — NT8 fill verification via outgoing/ position file
**Resolution**: `bridge/oif_writer.verify_nt8_position()` reads
`outgoing/{inst}_{account}_position.txt` and asserts direction+qty match
expected. Wired into `bots/base_bot.py` entry path for sim_bot only.
On mismatch: abort entry + Telegram alert.
**Commit**: `2bb9ed6`. **Status**: RESOLVED.

### B46 — Post-submit incoming/ clearance check
**Resolution**: `bridge/oif_writer._verify_consumed()` waits 1s after
bracket commit for NT8 to consume the files. Still present → logs
`[OIF_STUCK]` + Telegram alert. Catches silent NT8 rejections (wrong
TIF, bad account, ATI off).
**Commit**: `2bb9ed6`. **Status**: RESOLVED.

### B45 — OIF staging extension issues
**Resolution**: Rev 3 reverts to direct write in `incoming/*.txt`.
Rev 1 (`.tmp`) spewed cosmetic "Could not find file" errors; rev 2
(`.stage` in sibling dir + `os.replace`) completely broke NT8 ATI
consumption because cross-directory replace doesn't fire NT8's
FileSystemWatcher. Direct write's sub-millisecond partial-write
window is not a real race.
**Commits**: `2bb9ed6` (rev 2), `079b3ba` (rev 3 final).
**Status**: RESOLVED.

### B44 — CANCELALLORDERS param count (14→13 fields)
**Resolution**: `cancel_all_orders_line` trailing semicolons reduced
from 12 to 11. NT8 ATI rejected `CANCELALLORDERS;Sim101;;;;;;;;;;;;`
("should be 13 but is 14"). Spec: 13 fields = 12 semicolons total.
Tests in `tools/verification_2026_04_18/test_p5b_oif_correctness.py`
updated to assert 12-semi form.
**Commit**: `2bb9ed6`. **Status**: RESOLVED.

### B43 — Claude timeout 10s insufficient for real prompts
**Resolution**: Session Debriefer `timeout_s=90`, Historical Learner
`timeout_s=120`. Default 10s × 3 retries was hitting 33s ceiling and
timing out every large-context Claude call (2500–3500 input tokens).
Real Claude debrief first successful run: 60246ms latency.
**Commit**: `2d8d6b0`. **Status**: RESOLVED.

### B42 — load_dotenv ignored .env keys when OS env had them empty
**Resolution**: `load_dotenv(override=True)` across 15 loaders. Host OS
(Claude Code's OAuth shim) was setting `ANTHROPIC_API_KEY=""` at
process level; dotenv default behavior skips any key already in
`os.environ`. Direct probe after fix: 108 chars loaded, `DEGRADED=False`.
Also unified GOOGLE_API_KEY vs GEMINI_API_KEY precedence across all
call sites.
**Commits**: `eac5ae4` (primary), `7f11aaa` (extended audit).
**Status**: RESOLVED.

### B41 — Entry orders TIF=DAY rejected by 24/7-sim connections
**Resolution**: All OIF entry + exit + CLOSEPOSITION + PARTIAL_EXIT
paths now use TIF=GTC. "My Coinbase" sim connection rejects DAY
because crypto has no session concept. GTC is universally accepted
by NT8 (Kinetick, Live broker, Coinbase). DailyFlattener at 16:00 CT
prevents unintended overnight holds.
**Commits**: `65cc9d6` (entry), `66a8e7a` (exit paths).
**Status**: RESOLVED.

### B40 — NT8 ATI "not configured for multi-account routing"
**Resolution**: FALSE ROOT CAUSE. Real issue was B41 (TIF=DAY).
Once entries used GTC, all 16 dedicated Sim accounts filled.
Hotfix kill-switch `MULTI_ACCOUNT_ROUTING_ENABLED` was reverted;
flag stays True by default.
**Validated**: 17/17 accounts PASS via `tools/test_all_accounts.py`.
**Status**: RESOLVED (misdiagnosis, no actual routing issue).

### B39 — Silent phantom-fill divergence in sim_bot (HIGH)
**Resolution**: `bots/base_bot.py` entry path now:
1. For sim_bot specifically, fill-timeout ABORTS entry (was "assume
   filled" → phantom Python position while NT8 had nothing).
2. Post-fill, reads NT8 `outgoing/position.txt` to verify direction
   + qty match (B47). Rejects + Telegram alerts on mismatch.
3. Paper-mode prod_bot (Sim101-only) keeps legacy "assume filled"
   for mock tracking.
**Commit**: `2bb9ed6`. **Status**: RESOLVED.

### B38 — gamma_regime missing from log_eval
**Resolution**: First-class field in `core/history_logger.log_eval`.
Enum `.value` flattened for JSON serialization.
**Commit**: `341f15a` (Phases E–H sprint, S1). **Status**: RESOLVED.

### B37 — 4C integration test gap
**Resolution**: New `tests/test_4c_integration.py` — 12 tests covering
_require_account guard, routing→OIF→disk round-trip, byte-exact account
string survival, Sim101 fallback.
**Commit**: `4158e35` (Phases E–H sprint, S2). **Status**: RESOLVED.

### B33 — Parallel MenthorQ data sources, stale Path A
**Resolution**: `score_menthorq_gamma()` rewired Path A → Path B —
reads `market_snapshot["gamma_regime"]` enum directly (fresh B27 data)
instead of the stale `menthorq_daily.json`. Overclaiming warning
tightened to list only real consumers.
**Commit**: `e11dafe` (Phases E–H sprint, S3). **Status**: RESOLVED.

### B32 — Alpaca VIX API 401 Unauthorized
**Resolution**: yfinance promoted to **primary** VIX source (was
silently succeeding as fallback all along). Alpaca demoted to
optional-try-first with a module-level `_alpaca_latch` that skips
after first 401 until bot restart — eliminates repeated 401 log spam.
**Commit**: `7cc171b`. **Status**: RESOLVED.

### B26 — MenthorQ parser empty-value robustness
**Resolution**: `_coerce_float()` helper in `core/menthorq_feed.py`
handles empty strings, NaN, None, negative zero. Round-trip tests
added in `tests/test_menthorq_feed.py`.
**Commit**: `8d09b23` (Phases E–H sprint, S1). **Status**: RESOLVED.

### B21 — noise_area stop_ticks inflates position sizing
**Resolution**: `BaseStrategy.uses_managed_exit` attribute added; set
True on `noise_area`. In `core/risk_manager.py`, position-sizing
pathway substitutes a risk-reference stop (from
`PER_STRATEGY_DAILY_LOSS_CAP` / contracts) when the strategy uses a
managed exit, preventing the 150-600 tick structural stop from
artificially shrinking position sizes.
**Commit**: `337f60c`. **Status**: RESOLVED.

### B19 — simple_sizing.py stale default
**Resolution**: `core/simple_sizing.py` no longer hardcodes
`max_daily_loss_usd=15.0`. Now requires from settings.
**Commit**: `955e366`. **Status**: RESOLVED.

### B16 — Trade memory bot_id attribution missing
**Resolution**: `trade_memory.record()` stamps `bot_id=self.bot_name`
at every trade close site in `bots/base_bot.py`. New trade_memory
entries now carry `prod`/`sim`/`lab` attribution for dashboard P&L
split and post-hoc analysis.
**Commit**: `905b31b`. **Status**: RESOLVED.

---

## 🟡 CLOSED / PARKED / SUPERSEDED

### B36 — Lab bot silent crash PID 40908
**Status**: PARKED — lab decommissioned 2026-04-21. Preserving
defensive shutdown telemetry in base_bot for possible recurrence
on prod/sim.

### B35 — base_bot.py:2421 missing account= parameter
**Status**: CLOSED (not a bug) — line was a `return` statement,
not a write_oif call. PowerShell grep artifact.

### B27 — GammaLevels Net GEX + Total GEX magnitudes
**Status**: FIXED on `feat/b27-net-gex-regime`, merged prior to
Phases E–H.

### B23 — Third-party DEBUG log noise
**Status**: FIXED commit `62b2085` (websockets/yfinance/peewee silenced).

### B22 — EVAL debug logs invisible at lab INFO level
**Status**: RESOLVED commit `e22a4a1` (lab log level DEBUG); lab
subsequently retired.

### B20 — ib_breakout structural stop exceeds NQ ceiling
**Status**: RESOLVED commit `7e0dab1` (max_stop_ticks=120 skip guard).

### B18 — Stop placement audit: remaining fixed-tick strategies
**Status**: RESOLVED via Fix 7 + Fix 8 (commit `645b097` / `7e0dab1`).

### B17 — Dashboard state push stale after full reboot
**Status**: RESOLVED via merge `f4647b1` (datetime ISO-serialize fix).

### B15 — Six pre-existing test failures
**Status**: CLEARED in Phases E–H sprint commits 267ced4, f850fde,
814a269 — all 6 were test-stale (B13 commission math, cooloff
threshold, regime override contract drift). No production regressions.

### B12 — fix/b12-vwap-pullback-base-strategy
**Status**: SUPERSEDED — reshaped into `strategies/vwap_band_pullback.py`
via commit `adf6b4e`.

---

## End-to-End Validation (2026-04-21 20:28 CDT)

`python tools/test_all_accounts.py` — **17/17 PASS**.

For each of Sim101 + 16 dedicated sub-accounts:
- Submitted MARKET BUY 1-contract with TIF=GTC
- Verified NT8 consumed OIF file (disappeared from `incoming/` within 3s)
- Verified NT8 `outgoing/position.txt` reports LONG 1 at a valid price
- Submitted MARKET SELL 1-contract flatten
- Verified NT8 position file flips to FLAT 0 0

Report: `logs/ati_smoke_test_2026-04-21_2028.md`.
