"""Scale-out (partial exit) — extracted from base_bot.py 2026-05-24
(P4-1 Stage 4).

⚠ CRITICAL EXECUTION PATH. Writes OIF via _sink_submit_partial_exit.
Handles CONTINUATION-day partial-out, BE stop arming, runner contract
management. Trend-rider behavior gated by TREND_RIDER_ENABLED + day
classifier score.

Live blast radius bounded by core/live_canary_gate.py.

Original location: bots/base_bot.py async def _scale_out_trade.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("ScaleOut")


class ScaleOut:
    def __init__(self, bot):
        self.bot = bot

    async def scale_out_trade(self, ws, price: float) -> None:
        """Body of BaseBot._scale_out_trade, behaviorally verbatim.

        Trend rider scale-out: exit 1 contract at SCALE_OUT_RR, keep runner.

        1. Cancel NT8 OCO brackets
        2. Write partial exit OIF (sell/buy 1 contract at market)
        3. Record partial P&L
        4. Move stop to break-even
        5. Place new BE stop order in NT8
        6. Activate rider mode — stall detector now owns the exit
        """
        # Lazy imports — module-level helpers/constants live in base_bot.py
        from config.settings import TREND_RIDER_MIN_SCORE
        from bots.base_bot import (
            _sink_submit_partial_exit,
            _move_nt8_stop,
            _signal_viz,
        )
        from core import telegram_notifier as tg

        pos = self.bot.positions.position
        if not pos or pos.scaled_out or pos.contracts < 2:
            return

        tid = pos.trade_id
        n_exit = 1   # Always exit 1 contract, keep remainder running

        # Check momentum score — only ride when score >= threshold
        mom_score = 0
        try:
            if self.bot._last_cr:
                mom_score = self.bot._last_cr.momentum_score
        except Exception:
            pass

        rider_eligible = (mom_score >= TREND_RIDER_MIN_SCORE or
                          (self.bot._last_cr and self.bot._last_cr.verdict == "CONTINUATION"))

        if not rider_eligible:
            logger.info(f"[SCALE_OUT:{tid}] Score {mom_score} < {TREND_RIDER_MIN_SCORE} "
                        f"— using full exit instead of scale-out")
            # Fall through to normal target_hit exit; don't scale
            return

        logger.info(f"[SCALE_OUT:{tid}] Initiating: price={price:.2f} "
                    f"dir={pos.direction} contracts={pos.contracts} "
                    f"mom_score={mom_score}")

        # STEP 1: SKIP pre-exit CANCEL_ALL (B75).
        # Pre-B75 we sent account-scoped CANCELALLORDERS here to clear
        # the OCO bracket before submitting new orders. Problem: NT8 ATI
        # IGNORES the account field on CANCELALLORDERS and nukes every
        # pending order on every connected account, wiping sim_bot's
        # OCO protection on unrelated positions (root cause of the
        # 2026-04-22 orphan-long incidents on SimSpring Setup + SimNoise
        # Area). Solution: don't explicitly cancel. When we send the
        # partial-exit MARKET order below, position contracts decrease
        # from N to N-n_exit. The existing OCO stop/target remain but
        # will now target a smaller position; NT8 automatically adjusts
        # the OCO qty to match the remaining position size. If qty
        # reaches zero, NT8 auto-cancels the OCO.

        # STEP 2: Write partial exit OIF (exit n_exit contracts at market)
        # Sink-mediated PARTIAL_EXIT — DirectFileSink delegates straight to
        # bridge.oif_writer.write_partial_exit when the gate flag is off.
        try:
            _se_resp = _sink_submit_partial_exit(
                direction=pos.direction,
                n_contracts=n_exit,
                trade_id=f"{tid}_scale1",
                account=pos.account,
            )
            if _se_resp.get("decision") != "ACCEPT":
                logger.error(
                    f"[SCALE_OUT:{tid}] Partial exit refused by sink "
                    f"{_se_resp.get('sink','?')}: {_se_resp.get('reason','?')}"
                )
                return
        except Exception as e:
            logger.error(f"[SCALE_OUT:{tid}] Partial exit OIF failed: {e}")
            return

        # STEP 3: Record partial P&L in Python position manager
        partial = self.bot.positions.scale_out_partial(price, n_exit, "scale_out_target")
        if partial:
            self.bot.risk.record_trade(partial["pnl_dollars"])
            self.bot.trade_memory.record(partial, bot_id=self.bot.bot_name)
            logger.info(f"[SCALE_OUT:{tid}] Partial P&L: ${partial['pnl_dollars']:.2f} "
                        f"({partial['pnl_ticks']:.1f}t)")

        # STEP 4: Move stop to break-even in Python
        be_price = pos.entry_price
        self.bot.positions.move_stop_to_be(be_price)

        # STEP 5: Move the EXISTING OCO stop to break-even via the B76
        # cancel+replace flow. Do NOT use write_be_stop (which only PLACES
        # a new stop without cancelling the old one) — that leaves TWO
        # stops on the account:
        #   - the original OCO stop (auto-reduced by NT8 from qty=2 to
        #     qty=1 after the partial-exit market order filled), still at
        #     the original entry-stop_price
        #   - the new BE stop at entry_price
        # If the market moves adversely, the BE stop fires for qty=1,
        # CLOSING the position. But the OCO stop is still working — if
        # price bounces and hits IT, NT8 places a REVERSAL fill. That's
        # the orphan-phantom-trade signature from the 2026-04-22 incident.
        # P0.5 (D4) fix: use _move_nt8_stop → write_modify_stop, which
        # stages PLACE-new-stop + CANCEL-old-stop atomically (PLACE first,
        # CANCEL second — the safe ordering; see write_modify_stop docstring
        # for the full hierarchy).
        try:
            _move_nt8_stop(pos, pos.entry_price, be_price)
        except Exception as e:
            logger.warning(f"[SCALE_OUT:{tid}] BE stop-modify failed (non-blocking): {e}")

        # Chart overlay hook 3/4: stop moved to break-even after 1R scale-out.
        _signal_viz.emit_stop_moved(
            trade_id=tid, new_stop=float(be_price), reason="scale_out_1r_BE"
        )

        # STEP 6: Activate rider mode — stall detector now owns the exit
        pos.rider_mode = True
        self.bot._rider_active = True
        self.bot._stall_detector.reset()  # Fresh stall tracking for the runner

        self.bot.status = "IN_TRADE"
        logger.info(f"[SCALE_OUT:{tid}] Complete — {pos.contracts}x running "
                    f"BE@{be_price:.2f}, stall detector active")

        asyncio.ensure_future(tg.notify_alert(
            "SCALE OUT - RIDER ACTIVE",
            f"{pos.direction} partial exit {n_exit}x @ {price:.2f} "
            f"(+${partial['pnl_dollars']:.2f})\n"
            f"Runner: {pos.contracts}x | BE stop @ {be_price:.2f} | "
            f"Momentum score: {mom_score}"
        ))
