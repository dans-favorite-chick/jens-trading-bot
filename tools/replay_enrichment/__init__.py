"""Replay-enrichment reconstruction modules.

De-stub helpers that rebuild the live bot's strategy-branching market fields
(`cvd_health`, `es_nq_rs`, `day_type`, `cr_verdict`) from recorded data so the
backtester can be reconciled real-vs-real instead of real-vs-stub. Used only by
research/backtest tooling (tools/phoenix_real_backtest.py, reconcile) — never on
the live trade path.
"""
