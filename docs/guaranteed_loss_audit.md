# Guaranteed-Loss Pattern Audit (WS-A)

**Date**: 2026-04-21
**Branch**: `feature/exit-audit-and-safety`
**Trigger**: Jennifer's `noise_area` finding — `target_rr=0` silently synthesized
`target=entry` in `bots/base_bot.py`, bleeding commission on every trade.
This audit sweeps every other enabled strategy for the same class of bug.

## Patterns audited

1. `target_rr == 0` or `target_rr < 1.0` on any enabled strategy
2. `managed_exit=True + target_rr` combination producing target at/behind entry
3. Missing `target_ticks`/`target_rr` causing `base_bot` to synthesize a degenerate target
4. `stop_ticks` missing or zero
5. `target_rr >= 10` without a trailing-stop mechanism (unreachable target)
6. `exit_trigger` without protective OCO fallback

## Noise-area fix already in place

`bots/base_bot.py` (HEAD 8cdda40) now recognizes the managed-exit case:

```python
_managed_exit_target = (
    getattr(signal, "target_price", None) is None
    and getattr(signal, "target_rr", 0) == 0
)
```

When true, base_bot places a far-OCO safety target (not `entry`) and relies on
`strategy.check_exit()` / `exit_trigger`. No other strategy currently ships
`target_rr=0`, so the synthesis bug is closed — but the CI test below locks
that door.

## Strategy-by-strategy table

| Strategy | enabled | target_rr | stop config | managed exit? | trailing / `exit_trigger` | Verdict |
|---|---|---|---|---|---|---|
| bias_momentum | yes | 5.0 | ATR-anchored, 40–120t clamp | no | no | OK |
| spring_setup | yes | 1.5 | ATR-anchored, 40–120t clamp | no | no | OK |
| vwap_pullback | yes | **20.0** | ATR-anchored, 40–120t clamp | no | **no** | **SUSPECT** |
| high_precision_only | no | 5.0 | 14t fixed | no | no | disabled — skipped |
| dom_pullback | yes | 2.5 | ATR-anchored, 40–120t clamp | no | no | OK |
| ib_breakout | yes | computed from IB width | dynamic, `max_stop_ticks=120` | no | no (structural target) | OK |
| orb | yes | 2.0 | `max_stop_points=25` | no | yes (`chandelier_trail_3atr`) | OK |
| noise_area | yes | 0.0 (intentional) | max(4, …) | **yes** (`uses_managed_exit=True`) | yes (`price_returns_inside_noise_area`) | OK (post-fix) |
| compression_breakout | yes | 5.0 | ATR-anchored, 40–120t clamp | no | no | OK |
| opening_session | yes | computed per sub-strategy | structural + universal clamp 40–100t | no | concrete `target_price` from `_build_signal` | OK |
| vwap_band_pullback | yes | 2.0 | 40–120t clamp | no | concrete `target_price` | OK |

### Category counts

- **CRITICAL** (guarantees loss): 0
- **BROKEN** (invalid bracket math): 0
- **SUSPECT** (wide target, no trailing): 1 — `vwap_pullback`

## SUSPECT detail — `vwap_pullback`

`config/strategies.py` declares `target_rr: 20.0`. With a 40-tick min stop
that is an 800-tick / 200-point target. The strategy does NOT set
`uses_managed_exit=True`, does NOT emit `exit_trigger`, and does NOT wire a
trailing stop. The only non-bracket exit is `max_hold_min=60`.

In practice the OCO target will never fire. Outcomes collapse to:

- hit 40-120t stop, or
- time out at 60 min (small flat / small green).

This is not a commission-bleed bug, but it IS a "research-style" target that
pretends to capture the kind of 50–100 point moves the comment claims
("Reversal+stall exit drives this"). Nothing in code drives it.

### Action taken

- Added `_wide_target_requires_trailing: True` marker in config (WS-A judgment
  call — documents intent without silently truncating an in-flight research
  target).
- Flagged for WS-C to pick up: either implement a reversal/stall managed exit
  (matching the comment), or reduce `target_rr` to something reachable (<10)
  with a real OCO bracket.

## CI tests added

`tests/test_strategy_config_sanity.py`:

- For every enabled strategy either `target_rr >= 1.0` OR `uses_managed_exit`
  on the strategy class is True.
- Every enabled strategy has `stop_ticks > 0` OR computes its own stop
  (ATR-anchored / structural).
- Every key in `STRATEGIES` maps to an importable module in `strategies/`.
- If `target_rr >= 10`, either `uses_managed_exit` is True, an `exit_trigger`
  is produced, OR config contains `_wide_target_requires_trailing=True`.

## Judgment calls

Recorded in `docs/final-sprint-assumptions.md`.
