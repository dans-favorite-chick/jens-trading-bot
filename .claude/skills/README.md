# Phoenix Claude Code Skills

Skills are markdown contracts that Claude Code reads BEFORE editing the
matching code. They encode entry gates, known-broken patterns, and
anti-regression rules so that fixes don't drift back into bugs we
already paid for.

This tree follows Gemini's "Multi-Tier Agent Skills" pattern (per the
2026-04-25 plugin audit, `docs/audits/2026-04-25_plugin_skill_audit.md`):

- **Per-strategy skills** under `strategies/` — one skill per strategy file in `strategies/`.
- **Layer skills** at the root — one per architectural layer (backtest, signal generation, risk + compliance).

## Layer skills (read these first for cross-cutting work)

| Skill | Triggers | What it covers |
|---|---|---|
| `backtest_expert.md` | backtest, lab, replay, phoenix_real_backtest | Canonical backtester (`tools/phoenix_real_backtest.py`), F-06 bar-level CVD proxy gotcha, P1-1 reconciliation harness plan, F-16 suspect compounding curve. |
| `signal_gen.md` | strategy, signal, entry, exit, stop, target | Mandatory pre-delivery verification (no look-ahead, `now_ct.timestamp()` not `time.time()`, new strategies ship disabled). Phase 13 exit-policy override mechanism. Confluence-gate helpers. |
| `risk_compliance.md` | OIF, risk, stop order, position cap, execution, kill switch | Immutable OIF rules (atomic `.tmp`→`.txt`, `phoenix_<pid>_` prefix, PhoenixOIFGuard regex). Active Phase 0 freeze per SYNTHESIS_2026-05-24.md. 🛑 STOP gates on `bridge/oif_writer.py`, `phoenix_bot/orchestrator/oif_writer.py`, `core/risk_manager.py`. |

## Per-strategy skills

Each skill matches one strategy in `strategies/*.py` that is currently
listed in `config/strategies.py` (whether enabled or temporarily
disabled). Retired/deleted strategies (`high_precision_only`,
`noise_area`, `compression_breakout` family except where shipped,
`dom_pullback` which is deleted, `vwap_pullback` V1 which is superseded,
`big_move_signal` which is disabled, `nq_lsr` which is disabled,
`vwap_band_reversion` which is demoted, `orb` V1 which is superseded)
intentionally have NO skill file — flipping them back on requires more
than a skill refresh.

### Active strategies (Phase 13 cohort + V2 deployment)

| Strategy | Skill | Status | One-line |
|---|---|---|---|
| `bias_momentum` | [strategies/bias_momentum.md](strategies/bias_momentum.md) | enabled, validated | Multi-TF momentum follow; largest claimed P&L line; P1-1 reconciliation target |
| `opening_session` | [strategies/opening_session.md](strategies/opening_session.md) | enabled, validated | Family of 6 opening-window sub-strategies (open_drive, open_test_drive, open_auction_in, open_auction_out, premarket_breakout, orb) + orb_fade sub |
| `spring_setup` | [strategies/spring_setup.md](strategies/spring_setup.md) | enabled, validated | Wyckoff Rule-of-Three liquidity-grab reversal; pattern rare on MNQ |
| `ib_breakout` | [strategies/ib_breakout.md](strategies/ib_breakout.md) | enabled, validated (operator override) | Initial Balance breakout; regime-aware morning-only |
| `vwap_band_pullback` | [strategies/vwap_band_pullback.md](strategies/vwap_band_pullback.md) | enabled, validated | 1σ/2σ band + RSI(2) on trending days |
| `vwap_pullback_v2` | [strategies/vwap_pullback_v2.md](strategies/vwap_pullback_v2.md) | enabled, validated | V2 NQ-tuned drop-in for V1 vwap_pullback |
| `es_nq_confluence` | [strategies/es_nq_confluence.md](strategies/es_nq_confluence.md) | enabled, NOT validated | Phase 12C; **dormant pending MES feed** |
| `a_asian_continuation` | [strategies/a_asian_continuation.md](strategies/a_asian_continuation.md) | enabled, validated | Phase 13; PF 8.29 / 6/6y |
| `e_multi_day_breakout` | [strategies/e_multi_day_breakout.md](strategies/e_multi_day_breakout.md) | enabled, validated | Phase 13; 3-day RTH range breakout |
| `g_inside_bar_breakout` | [strategies/g_inside_bar_breakout.md](strategies/g_inside_bar_breakout.md) | enabled, validated | Phase 13; 5m inside-bar breakout |
| `raschke_baseline` | [strategies/raschke_baseline.md](strategies/raschke_baseline.md) | enabled, validated | Phase 13; Linda Raschke 20-EMA pullback; largest Phase 13 P&L |
| `footprint_cvd_reversal` | [strategies/footprint_cvd_reversal.md](strategies/footprint_cvd_reversal.md) | DISABLED | Dormant pending volumetric feed |

### Disabled-but-documented (skill exists so re-enable work has a contract)

| Strategy | Skill | Status | One-line |
|---|---|---|---|
| `orb_fade` | [strategies/orb_fade.md](strategies/orb_fade.md) | DISABLED 2026-05-20 (F-004) | Counter-strategy to ORB; canonical B3 bug fix at lines 159-166 |
| `orb_v2` | [strategies/orb_v2.md](strategies/orb_v2.md) | DISABLED 2026-05-20 (F-004) | Superseded by `opening_session.orb`; kept as V2-pattern reference |

## Skill format

Each per-strategy skill has frontmatter (`name`, `description`) followed by:

- `What it does` — 2-3 sentence summary
- `Trigger condition` — when in market regime / what setup
- `Entry gates` — list of confluence checks, filters
- `Stop / target` — stop policy, target policy, Phase 13 override if applicable
- `Known issues` — pulled from `memory/context/KNOWN_ISSUES.md` and in-file comments
- `Reference files` — file paths + line numbers for entry logic, config block, helpers
- `DO NOT` — anti-regression rules specific to this strategy

Layer skills follow the same shape but cover cross-cutting concerns.

## How to add a new skill

1. Read the actual strategy file — do not hallucinate gates.
2. Cross-reference `config/strategies.py` for the config block and any `# DELETED` / `# RETIRED` markers.
3. Check `memory/context/KNOWN_ISSUES.md` and the strategy's in-file commit-trail comments for the regression history.
4. Add the skill file, then update the table in this README.
5. The strategy file itself stays untouched — skills document, they don't modify.
