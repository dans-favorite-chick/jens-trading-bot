# Trailing Stop & BE-Move Audit (WS-C)

Branch: `feature/exit-audit-and-safety`
Baseline HEAD: `8cdda40`
Date: 2026-04-21
Question from Jennifer: *"Trades went +100 into profit then reversed to stop-out. Bias_momentum and spring_setup had stops/targets nearly 900 points away. Were they chandelier-trailed? Are stops being moved up?"*

## TL;DR — DIRECT ANSWERS

1. **Were bias_momentum / spring_setup stops chandelier-trailed today?** **NO.** Neither attaches a chandelier. Only `orb` attaches `chandelier_trail_3atr`. Today (2026-04-21 prod log): **0** `[CHANDELIER]` log lines. 3 `[TRAIL]` log lines (rider-mode `_trail_stop` midpoint tighten on bias_momentum).
2. **Are stops being moved up during the trade?** **Partially — but only in Python memory, NOT in NT8.** The `[TRAIL]`, `[BE_STOP]`, and `[RIDER ... BE STOP]` events all mutate `pos.stop_price` in-process; they do NOT write any OIF to move the NT8 bracket stop. The only in-trade OIF stop-modification path is `_scale_out_trade` → `write_be_stop` (line 3004), which is NOT invoked for single-contract rider trades (scale-out needs ≥2 contracts).
3. **Root cause of "+100 → stop-out":** rider trades run at 1-contract with BE-move happening only in Python. When price reverses, NT8's original stop (40-120t below entry) fills, not the Python BE level. The `+100 pt` MFE was never locked in at the NT8 layer.

## Per-strategy table

| Strategy | target_rr | chandelier attached? | ATR mult / TF | BE-move rule | Python→NT8 OIF? |
|---|---|---|---|---|---|
| bias_momentum | **5.0 → overridden to 20.0 (RIDER)** | NO | — | rider BE @ 0.5R or 1R (day-type) | **NO** (Python-only) |
| spring_setup | 1.5 | NO | — | none | n/a |
| vwap_pullback | 20.0 | NO | — | none | **NO — misconfig** |
| high_precision_only | 5.0 | NO | — | none | n/a |
| dom_pullback | 2.5 → **20.0 (RIDER)** | NO | — | rider BE @ 0.5R or 1R | **NO** (Python-only) |
| ib_breakout | 1.5 (target_extension) | NO | — | none | n/a |
| orb | 2.0 (scale) + chandelier runner | **YES** | 3.0 × ATR(14) 5m | scale_out @ 1R → write_be_stop (OIF ✓) | **YES for scale path** |
| noise_area | managed exit (price back in band) | NO | — | none | n/a |
| compression_breakout | 5.0 | NO | — | none | n/a |
| opening_session | varied | NO | — | none | n/a |
| vwap_band_pullback | 2.0 | NO | — | none | n/a |

## Evidence from today's log (`logs/prod_bot_stdout.log` 2026-04-21)

```
2026-04-21 08:34:08,918 [Bot] INFO [TRAIL:ca491c62] Stop trailed to 26751.62 (mid)
2026-04-21 09:03:37,952 [Bot] INFO [TRAIL:55df5162] Stop trailed to 26830.12 (mid)
2026-04-21 10:19:04,727 [Bot] INFO [TRAIL:fd3a3346] Stop trailed to 26759.62 (mid)
```
No `[CHANDELIER]`, no `[OIF:BE_STOP]`, no `[OIF:MODIFY_STOP]` for any of the three. The Python stop moved; the NT8 bracket stop did not.

## Key findings

### F1. Python-only stop moves — the primary bug
`_trail_stop` (base_bot.py:164), the rider BE trigger (base_bot.py:938), and `move_stop_to_be` (position_manager.py:387) all mutate `pos.stop_price` without writing any OIF. NT8 still holds the original bracket stop. Python's `check_exits` will fire an EXIT market order when price crosses the Python stop — BUT only if a tick arrives AT the moved level. If price jumps through, the NT8 original bracket catches it first at a worse price. Either way, there is no actual trailing stop in NT8.

### F2. Chandelier `should_exit` sends a market exit, not a stop modification
`core/chandelier_exit.py.should_exit()` returns True; base_bot.py:1038 calls `_exit_trade(..., "chandelier_trail_hit")`. That is a synthetic market exit, not a stop-order amendment. Works functionally for ORB because ORB is the only caller and it fires only when violated.

### F3. target_rr=20.0 without trailing = guaranteed giveback
`bias_momentum` and `dom_pullback` are force-set to `target_rr=20.0` by the rider override (base_bot.py:1948-1951). `vwap_pullback` has `target_rr=20.0` hardcoded. With no chandelier and no NT8-side stop move, any +100t MFE that reverses hits the original 40-120t stop for a full loss.

### F4. Misconfigurations
- `vwap_pullback` target_rr=20.0 has NO rider, NO chandelier, NO managed exit. Pure holdover. Target is mathematically unreachable for a reversion strategy → acts as "runner with no trail" = giveback.
- `compression_breakout` target_rr=5.0 with no trail; better fit with ATR trail.

## Recommended fixes

1. **Wire stop-modification OIFs into all Python stop moves** (bots/base_bot.py + new `bridge/oif_writer.write_modify_stop`). Every `[TRAIL]` / `[BE_STOP]` / `[CHANDELIER trail update]` must CANCEL the existing bracket stop and submit a new `PLACE_STOP_*` at the new price on the position's account. Without this, Python stops are decorative.
2. **Drop `vwap_pullback` target_rr 20.0 → 2.0** or attach a chandelier. A mean-reversion strategy should not hold for 20R.
3. **Attach chandelier (3.0×ATR 5m) to bias_momentum and dom_pullback rider positions** as a third exit leg alongside stall + reversal. Currently the BE-stop is the ONLY downside floor past entry.
4. **Raise rider BE-trigger from 0.5R to 1.0R on non-trend days IF** chandelier is attached (the chandelier captures giveback; BE becomes safety net, not primary).

## Status of deliverables

- Audit document: **this file** — done.
- Code fix wiring `[TRAIL]`/`[BE]`/`[CHANDELIER]` → OIF stop modify: **NOT committed** (scope > single session; requires bridge-side + NT8 side validation). Written up as ticket-ready spec below.
- Tests asserting OIF on trail/BE: **scaffolded** in `tests/test_chandelier_trailing.py`, `tests/test_be_move.py` — both currently expected to FAIL against HEAD (they assert the bug is fixed). Marked with `pytest.xfail` so baseline stays green.

## OIF stop-modify spec (for follow-up sprint)

```python
# bridge/oif_writer.py
def write_modify_stop(direction, new_stop_price, n_contracts, trade_id, account,
                      old_stop_order_id: str) -> list[str]:
    """Cancel old bracket stop and place new STOPMARKET at new_stop_price.
    Two-line OIF: cancel_single_order_line(old_stop_order_id) + _build_stop_line(...)"""
```
Callers to update:
- `_trail_stop(pos, price)` → after mutating `pos.stop_price`, await `write_modify_stop(...)`.
- Rider BE trigger block (base_bot.py:925-943) → same.
- Chandelier trail update block (base_bot.py:1020-1041) → emit modify on each ratchet (debounced by ≥N ticks to avoid OIF spam).

The main obstacle is that `pos.old_stop_order_id` is not currently tracked. That field must be populated at bracket-place time from the fill confirmation.
