# OBI Feature — Order Book Imbalance

Passive-order indicator complementing the existing CVD (active-order) signal. Goal: provide phoenix_bot's strategies and AI council with information about *resting limit orders* — where defended levels sit, where walls exist, where institutions are absorbing flow.

## Status

**Not yet started.** This folder contains the design and validation plan. No code has been added to the production system yet.

Predecessor work (CVD, absorption score, confluence) is already in `MarketDataBroadcasterV3.cs`. OBI will be the next addition to that indicator, plus broadcast fields and bot-side filter logic.

## What OBI gives us

- **Static OBI**: snapshot of resting limit orders. Big walls = defended levels.
- **Dynamic OBI (refill ratio)**: rate at which liquidity gets replenished after being hit. High refill = institutional absorption — the strongest signal in the family.
- **Wall detection**: spoof-resistant identification of significant resting levels.
- **Regime flag**: detects when the book has collapsed (news / halt-near) → bots SIT_OUT.

## Hard rule: OBI is a FILTER, not a TRIGGER

OBI signal half-life is seconds. Our bot architecture has 50–200ms latency. We cannot *race* OBI; we can only *use* it as additional context on signals already produced by bar-close strategies.

This is non-negotiable. Every code review on OBI features should check: "is this using OBI as a trigger?" If yes, reject.

## Documents in this folder

| File | Purpose |
|---|---|
| `README.md` | This file — overview + nav |
| `DESIGN.md` | Full architectural design, phase-by-phase. Read this before writing any code. |
| `DECISIONS.md` | Pre-committed hypothesis and kill criterion. Sacred — write once, do not edit. |
| `PHASE_0A.md` | Step 1: verify Level 2 data subscription. Start here. |

## Quick reference — the 8 holes and their status

| # | Hole | Status |
|---|---|---|
| 1 | No historical L2 for backtests | ✅ Fixed by L2 recorder (Phase 0b), runs in parallel |
| 2 | Out-of-order events | ✅ Fixed by event-timestamp + tolerance + drop logic |
| 3 | 200ms data refresh limit | ⚠️ Routed around — design uses ≥1s windows only |
| 4 | Latency budget pile-up | ✅ Fixed by filter-not-trigger discipline + freshness gate |
| 5 | Token cost for AI council | ✅ Non-issue (~$0.01/day extra) |
| 6 | Chart-must-be-open / watchdog | ✅ Fixed by heartbeat + degraded-mode fallback |
| 7 | News / event regime | ✅ Fixed by calendar blackouts + market-based detector |
| 8 | Iceberg orders | ⚠️ Mitigated by behavioral detection — full fix requires MBO ($) |
| 9 | Data feed quality | ✅ Verified in Phase 0a |
| 10 | Spending months and getting no edge | ✅ Fixed by pre-committed kill criterion in DECISIONS.md |

Full details in `DESIGN.md`.

## Phased rollout

| Phase | Duration | Goal |
|---|---|---|
| **0a** | 1 day | Verify L2 feed quality on MNQ |
| **0b** | 1 day build, then continuous | Deploy recorder to bank historical data |
| **1** | 1 week | Indicator additions: book state, out-of-order detection, heartbeat |
| **2** | 1 week | Feature engineering: OBI, EMA, walls, regime, iceberg |
| **3** | 4 weeks | Log-mode validation — bots receive OBI but don't act on it |
| **4** | If 3 passes | Enable as PreTradeFilter context only |
| **5** | If 4 passes | Wall-aware stop placement |

Each gate is a kill switch.

## Where to start

**Phase 0a** in `PHASE_0A.md`. It's a 1-day, zero-risk check that produces a Go/No-Go on the whole feature before any real work begins.
