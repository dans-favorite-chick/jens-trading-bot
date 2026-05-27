"""Market enricher — extracted from base_bot.py 2026-05-24 (P4-1 Stage 2).

Feeds the 15+ detectors that observe each bar close and stashes their
outputs on the bot for strategy consumption. Read-only with respect to
positions/risk/OIF — pure pre-trade observability.

Original location: bots/base_bot.py:_on_bar lines 2816-2995 (the
detector-update block, NOT the _evaluate_strategies dispatch or the
post-bar regime/council/equity logic).

Detectors fed (each is owned by BaseBot — we just call them):
- smc (1m, 5m)                        — pattern signals
- chart_patterns (1m, 5m)             — Phase 8 chart patterns
- footprint_1m / footprint_5m         — close-bar footprint aggregation
- volume_profile (1m close)           — POC/VAH/VAL update
- swing_state_5m                      — ATR-ZigZag pivot detection
- reversal_detector (5m)              — climax / reversal confirmation
- sweep_watcher (5m)                  — failed-BOS sweep detection
- rsi_divergence (1m)                 — divergence on close price
- _stall_detector (1m)                — trend stall bar history
- cvd_health / cvd_flip / cvd_div (1m) — CVD detectors
- hmm_regime (5m)                     — HMM regime change point
- intermarket (5m)                    — NQ price feed
- pandas_ta (5m)                      — pandas-ta indicators
- htf_scanner (5m/15m/60m)            — HTF pattern scanner

State writes back to BaseBot (preserved field names):
- bot._last_footprint_signals
- bot._last_climax_warning
- bot._last_sweep_event
- bot._last_rsi_divergence
- bot._price_bar_highs / _price_bar_lows (trimmed to last 10)

Plus a save call to `bot.aggregator.save_state(bot._aggregator_state_path)`.

No OIF writes. No _enter_trade calls. No risk gate mutation.
"""
from __future__ import annotations

import logging

from core.footprint_patterns import scan_bar as scan_footprint_bar

logger = logging.getLogger("MarketEnricher")


class MarketEnricher:
    def __init__(self, bot):
        self.bot = bot

    def enrich(self, timeframe: str, bar) -> None:
        """The detector-update block extracted from _on_bar verbatim.
        BaseBot field accesses go through `self.bot.X`.
        Behaviorally identical to the original inline block.
        """
        # Feed SMC pattern detector on 1m and 5m bars
        if timeframe in ("1m", "5m"):
            try:
                smc_signals = self.bot.smc.update(bar)
                for s in smc_signals:
                    logger.info(f"[SMC {timeframe}] {s.pattern} {s.direction} "
                                f"str={s.strength:.0f} — {s.description}")
            except Exception as e:
                logger.debug(f"[SMC] Update error (non-blocking): {e}")

            # Phase 8: Feed chart pattern detector on 1m and 5m bars
            try:
                chart_pats = self.bot.chart_patterns.update(timeframe, bar)
                for cp in chart_pats:
                    logger.info(f"[CHART {timeframe}] {cp.pattern} {cp.direction} "
                                f"str={cp.strength:.0f} tgt={cp.target_price:.2f}")
            except Exception as e:
                logger.debug(f"[CHART PATTERNS] Update error (non-blocking): {e}")

            # ─── NEW Apr 2026 modules: close footprint bars + feed reversal/sweep ───
            # All wrapped in try/except — SHADOW MODE must never break live trading.
            if timeframe == "1m":
                try:
                    self.bot.footprint_1m.close_bar()
                    self.bot.volume_profile.on_bar_close()
                except Exception as e:
                    logger.debug(f"[FOOTPRINT 1m] close error: {e}")

            if timeframe == "5m":
                try:
                    fp_bar = self.bot.footprint_5m.close_bar()
                    if fp_bar is not None:
                        history = self.bot.footprint_5m.completed_bars[:-1]
                        signals = scan_footprint_bar(fp_bar, history)
                        self.bot._last_footprint_signals = [s.to_dict() for s in signals]
                        for s in signals:
                            logger.info(f"[FOOTPRINT 5m] {s.pattern} {s.direction} "
                                        f"sev={s.severity:.2f} @ {s.price:.2f}")
                except Exception as e:
                    logger.debug(f"[FOOTPRINT 5m] close error: {e}")

                # Feed swing detector on 5m bars (ATR-ZigZag)
                try:
                    atr_5m = self.bot.aggregator.atr.get("5m", 5.0) or 5.0
                    bar_idx = len(self.bot.swing_state_5m.pivots) + 100  # Running index
                    new_pivot = self.bot.swing_state_5m.update(bar, bar_idx, atr_5m)
                    if new_pivot:
                        logger.info(f"[SWING 5m] {new_pivot.classification} "
                                    f"@ {new_pivot.price:.2f}")
                        # Feed sweep watcher with pivot breaks
                        try:
                            # On a new HIGH pivot, the prior UP move may have broken a prior LOW pivot
                            # (simplified: we track the pivot extremes for sweep watcher)
                            # The full mechanism requires pivot break event detection; simplified here.
                            pass
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"[SWING] update error: {e}")

                # Feed climax/reversal detector on 5m bars
                try:
                    atr_5m = self.bot.aggregator.atr.get("5m", 5.0) or 5.0
                    session_cvd = getattr(self.bot.aggregator, "cvd_session", 0)
                    bar_idx_rev = len(self.bot.aggregator.bars_5m.completed)
                    warning, signal = self.bot.reversal_detector.update(
                        bar, atr_5m, session_cvd, bar_idx_rev
                    )
                    if warning:
                        self.bot._last_climax_warning = {
                            "direction": warning.direction,
                            "climax_extreme": warning.climax_extreme,
                            "bars_ago": 0,
                        }
                    if signal:
                        logger.info(f"[REVERSAL CONFIRMED] {signal.direction} "
                                    f"@ {signal.entry_price:.2f} "
                                    f"(stop {signal.stop_price:.2f})")
                except Exception as e:
                    logger.debug(f"[REVERSAL] update error: {e}")

                # Feed sweep watcher — check for failed-BOS sweeps
                try:
                    bar_idx_sw = len(self.bot.aggregator.bars_5m.completed)
                    sweep = self.bot.sweep_watcher.check_sweep(bar, bar_idx_sw)
                    if sweep:
                        self.bot._last_sweep_event = {
                            "direction": sweep.reversal_direction,
                            "pivot_price": sweep.pivot_price,
                            "sweep_extreme": sweep.sweep_extreme,
                        }
                except Exception as e:
                    logger.debug(f"[SWEEP] check error: {e}")

                # 2026-05-06 Sprint J: gamma flip detector update removed
                # (depended on MenthorQ HVL — subscription retired). Detector
                # class kept for future reactivation with another HVL source.

        # Feed RSI divergence detector on every 1m bar close
        if timeframe == "1m":
            div = self.bot.rsi_divergence.update(bar.close)
            if div:
                self.bot._last_rsi_divergence = div
                logger.info(f"[RSI DIV] {div['type'].upper()} divergence "
                            f"strength={div['strength']:.0f} "
                            f"RSI={div['rsi_current']:.1f} "
                            f"bars_apart={div['bars_apart']}")

            # Feed trend stall detector bar history (keep last 10)
            try:
                bar_high  = getattr(bar, "high",  bar.close)
                bar_low   = getattr(bar, "low",   bar.close)
                self.bot._stall_detector.update_bar(bar_high, bar_low, bar.close)
                self.bot._price_bar_highs.append(bar_high)
                self.bot._price_bar_lows.append(bar_low)
                # Trim to keep only the last 10 bars
                if len(self.bot._price_bar_highs) > 10:
                    self.bot._price_bar_highs = self.bot._price_bar_highs[-10:]
                    self.bot._price_bar_lows  = self.bot._price_bar_lows[-10:]
            except Exception:
                pass

            # ── 2026-05-13: feed the CVD detectors on every 1m bar close ──
            # Inputs:
            #   - bar_close (price)
            #   - market["cvd"] (cumulative CVD — read fresh from aggregator
            #     snapshot to avoid relying on whatever's cached)
            #   - market["bar_delta"] (this-bar delta — buy_vol minus sell_vol)
            # Each detector is wrapped in try/except — if a feed fails, the
            # bar update is skipped but the bot keeps running. CVD updates
            # are advisory, not safety-critical.
            try:
                _snap = self.bot.aggregator.snapshot()
                _cum_cvd = float(_snap.get("cvd", 0) or 0)
                _bar_delta = float(_snap.get("bar_delta", 0) or 0)
                _bar_high = getattr(bar, "high", bar.close)
                _bar_low = getattr(bar, "low", bar.close)
                self.bot.cvd_health.update_bar(bar.close, _cum_cvd)
                self.bot.cvd_flip.update_bar(_bar_delta)
                self.bot.cvd_div.update_bar(_bar_high, _bar_low, _cum_cvd)
            except Exception as _cvd_err:
                logger.debug(f"[CVD] detector update failed: {_cvd_err!r}")

        # Phase 7: Feed HMM regime detector on 5m bar completions
        if timeframe == "5m":
            try:
                hmm_result = self.bot.hmm_regime.update(bar)
                if hmm_result.get("change_point"):
                    logger.info(f"[HMM] Change point detected! Regime={hmm_result['regime']} "
                                f"conf={hmm_result['confidence']:.2f}")
            except Exception as e:
                logger.debug(f"[HMM] Update error (non-blocking): {e}")

            # Phase 8: Feed intermarket engine with NQ price on 5m bars
            try:
                self.bot.intermarket.update_nq(bar.close)
            except Exception:
                pass

            # Phase 8: Feed pandas-ta detector on 5m bar completions
            try:
                self.bot.pandas_ta.update(bar)
            except Exception as e:
                logger.debug(f"[PandasTA] Feed error (non-blocking): {e}")

        # Feed HTF pattern scanner on 5m/15m/60m bar completions
        if timeframe in ("5m", "15m", "60m"):
            htf_patterns = self.bot.htf_scanner.on_bar(timeframe, bar)
            if htf_patterns:
                for sig in htf_patterns:
                    p = sig["pattern"]
                    logger.info(f"[HTF PATTERN] {timeframe} {p['pattern']} "
                                f"({p.get('direction','?')}) "
                                f"strength={p.get('strength',0)}")

        # Persist aggregator state on every bar (survive restarts)
        try:
            self.bot.aggregator.save_state(self.bot._aggregator_state_path)
        except Exception:
            pass
