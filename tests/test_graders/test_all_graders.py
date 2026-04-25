"""Tests for the six P1-P6 graders + harness — added 2026-04-25.

Each grader gets pass / fail / edge-case coverage. Synthetic LogEvent
fixtures avoid filesystem dependencies and let us simulate any session.

Layout:
  TestParser              — sim_bot_log parsing primitives (3 tests)
  TestOrbOrTooWideGrader  — P1 (3 tests)
  TestBiasVwapGateGrader  — P2 (3 tests)
  TestNoiseCadenceSpamGrader — P3 (3 tests)
  TestIbWarmupGrader      — P4 (3 tests)
  TestCompressionSqueezeGrader — P5 (3 tests)
  TestSpringSilenceGrader — P6 (3 tests)
  TestHarness             — orchestrator end-to-end (2 tests)
"""

from __future__ import annotations

from datetime import datetime, time

import pytest

from tools.log_parsers.sim_bot_log import LogEvent, parse_sim_bot_log
from tools.graders.orb_or_too_wide import OrbOrTooWideGrader
from tools.graders.bias_vwap_gate import BiasVwapGateGrader
from tools.graders.noise_cadence_spam import NoiseCadenceSpamGrader
from tools.graders.ib_warmup import IbWarmupGrader
from tools.graders.compression_squeeze import CompressionSqueezeGrader
from tools.graders.spring_silence import SpringSilenceGrader


def E(ts: str, module: str, level: str, message: str,
      *, kind: str = None, strategy: str = None, gate: str = None) -> LogEvent:
    """Tiny LogEvent factory for synthetic fixtures."""
    if not kind:
        # Auto-classify based on message content (mirrors parser)
        if "PRICE_SANITY" in message: kind = "PRICE_SANITY"
        elif "BLOCKED gate:" in message: kind = "BLOCKED"
        elif "REJECTED:" in message or " REJECTED:" in message: kind = "REJECTED"
        elif "NO_SIGNAL " in message: kind = "NO_SIGNAL"
        elif "warmup_incomplete" in message: kind = "SKIP"
        elif "SIGNAL " in message and "NO_SIGNAL" not in message: kind = "SIGNAL"
        elif "[TRADE]" in message: kind = "TRADE"
        elif "[EVAL]" in message: kind = "EVAL"
        else: kind = "OTHER"
    if not gate and "BLOCKED gate:" in message:
        import re as _re
        m = _re.search(r"BLOCKED gate:(\w+)", message)
        gate = m.group(1) if m else None
    return LogEvent(
        ts=datetime.fromisoformat(ts), level=level, module=module,
        message=message, kind=kind, strategy=strategy, gate=gate,
        raw_line=f"{ts} [{module}] {level} {message}",
    )


# ─────────── Parser tests ───────────

class TestParser:
    def test_parses_typical_line(self, tmp_path):
        log = tmp_path / "sim_bot.log"
        log.write_text(
            "2026-04-24 09:30:45,123 [strategies.bias_momentum] DEBUG [EVAL] bias_momentum: BLOCKED gate:vwap_long\n"
            "2026-04-24 09:31:00,000 [SimBot] INFO   [SIM:bias_momentum] REJECTED: VWAP_GATE: price 27400 below VWAP 27450\n",
            encoding="utf-8",
        )
        events = list(parse_sim_bot_log(log))
        assert len(events) == 2
        assert events[0].strategy == "bias_momentum"
        assert events[0].gate == "vwap_long"
        assert events[0].kind == "BLOCKED"
        assert events[1].kind == "REJECTED"

    def test_skips_malformed(self, tmp_path):
        log = tmp_path / "sim_bot.log"
        log.write_text("garbage line\n2026-04-24 10:00:00,000 [Bot] INFO valid\n", encoding="utf-8")
        events = list(parse_sim_bot_log(log))
        assert len(events) == 1

    def test_filters_by_time(self, tmp_path):
        log = tmp_path / "sim_bot.log"
        log.write_text(
            "2026-04-23 09:00:00,000 [Bot] INFO yesterday\n"
            "2026-04-24 09:00:00,000 [Bot] INFO today\n",
            encoding="utf-8",
        )
        events = list(parse_sim_bot_log(
            log, since=datetime(2026, 4, 24), until=datetime(2026, 4, 25)
        ))
        assert len(events) == 1
        assert "today" in events[0].message


# ─────────── P1 ORB ───────────

class TestOrbOrTooWideGrader:
    def test_pass_when_under_40pct_and_signal_present(self):
        events = [E(f"2026-04-25T09:35:{i:02d}", "strategies.orb", "DEBUG",
                    "[EVAL] orb: BLOCKED gate:or_too_wide", strategy="orb", gate="or_too_wide")
                  for i in range(3)]
        events += [E(f"2026-04-25T09:36:{i:02d}", "strategies.orb", "DEBUG",
                     "[EVAL] orb: SIGNAL LONG entry=27425", strategy="orb")
                   for i in range(7)]
        # 3 of 10 = 30% < 40%, qualitative passes (signal seen)
        r = OrbOrTooWideGrader().grade(events, {})
        assert r.overall_pass

    def test_fail_when_over_40pct(self):
        events = [E(f"2026-04-25T09:35:{i:02d}", "strategies.orb", "DEBUG",
                    "[EVAL] orb: BLOCKED gate:or_too_wide", strategy="orb", gate="or_too_wide")
                  for i in range(8)]
        events += [E(f"2026-04-25T09:36:{i:02d}", "strategies.orb", "DEBUG",
                     "[EVAL] orb: SIGNAL LONG", strategy="orb")
                   for i in range(2)]
        r = OrbOrTooWideGrader().grade(events, {})
        assert not r.quant_pass

    def test_no_events_safe(self):
        r = OrbOrTooWideGrader().grade([], {})
        assert r.quant_value == 0.0
        # No candidates → qual fails
        assert r.qual_pass is False


# ─────────── P2 bias_momentum VWAP ───────────

class TestBiasVwapGateGrader:
    def test_pass_when_under_85pct(self):
        events = [E(f"2026-04-25T09:35:{i:02d}", "SimBot", "INFO",
                    f"[SIM:bias_momentum] REJECTED: VWAP_GATE: price 27400 (VCR=1.3)",
                    strategy="bias_momentum")
                  for i in range(7)]
        events += [E(f"2026-04-25T09:36:{i:02d}", "Bot", "INFO",
                     "EXPLOSIVE BAR: VCR=1.6x ... bypass active", strategy="bias_momentum")
                   for i in range(3)]
        r = BiasVwapGateGrader().grade(events, {})
        assert r.quant_value < 0.85
        assert r.qual_pass     # bypass_active line seen

    def test_fail_when_over_85pct(self):
        events = [E(f"2026-04-25T09:{35+i//60:02d}:{i%60:02d}", "SimBot", "INFO",
                    "[SIM:bias_momentum] REJECTED: VWAP_GATE",
                    strategy="bias_momentum")
                  for i in range(99)]
        events += [E("2026-04-25T09:36:00", "Bot", "INFO",
                     "EXPLOSIVE BAR: VCR=1.4 ... bypass active", strategy="bias_momentum")]
        r = BiasVwapGateGrader().grade(events, {})
        assert not r.quant_pass

    def test_qual_fails_without_bypass(self):
        events = [E(f"2026-04-25T09:35:{i:02d}", "SimBot", "INFO",
                    "[SIM:bias_momentum] REJECTED: VWAP_GATE",
                    strategy="bias_momentum")
                  for i in range(3)]
        events += [E(f"2026-04-25T09:36:{i:02d}", "Bot", "INFO",
                     "[EVAL] bias_momentum noting VCR=1.0", strategy="bias_momentum")
                   for i in range(7)]
        r = BiasVwapGateGrader().grade(events, {})
        # Quant might pass but qual fails (no bypass active)
        assert r.qual_pass is False


# ─────────── P3 noise_area cadence ───────────

class TestNoiseCadenceSpamGrader:
    def test_pass_with_silent_cadence(self):
        # Build 10 ON CADENCE lines across 10 unique 30-min windows
        events = []
        # 9:30, 10:00, 10:30, 11:00, 11:30, 12:00, 12:30, 13:00, 13:30, 14:00
        for hour in range(9, 14):
            for minute in (30, 0) if hour == 9 else (0, 30):
                events.append(E(
                    f"2026-04-25T{hour:02d}:{minute:02d}:01",
                    "strategies.noise_area", "INFO",
                    "[EVAL] noise_area: ON CADENCE — evaluating at "
                    f"{hour:02d}:{minute:02d} ET (3 off-cadence skips since last)",
                    strategy="noise_area"))
        r = NoiseCadenceSpamGrader().grade(events, {})
        assert r.overall_pass

    def test_fail_with_block_spam(self):
        # 100 BLOCKED lines + 0 ON CADENCE
        events = [E(f"2026-04-25T09:{30+i//60:02d}:{i%60:02d}",
                    "strategies.noise_area", "DEBUG",
                    "[EVAL] noise_area: BLOCKED gate:not_on_30min_cadence",
                    strategy="noise_area", gate="not_on_30min_cadence")
                  for i in range(100)]
        r = NoiseCadenceSpamGrader().grade(events, {})
        assert not r.quant_pass

    def test_no_events_safe(self):
        r = NoiseCadenceSpamGrader().grade([], {})
        # No blocks = quant pass; no ON CADENCE = qual fail
        assert r.quant_pass is True
        assert r.qual_pass is False


# ─────────── P4 ib_breakout warmup ───────────

class TestIbWarmupGrader:
    def test_clear_within_10min(self):
        events = [
            E("2026-04-25T08:31:00", "strategies.ib_breakout", "DEBUG",
              "[EVAL] ib_breakout: SKIP warmup_incomplete", strategy="ib_breakout"),
            E("2026-04-25T08:35:00", "strategies.ib_breakout", "DEBUG",
              "[EVAL] ib_breakout: SKIP warmup_incomplete", strategy="ib_breakout"),
            E("2026-04-25T08:39:30", "strategies.ib_breakout", "INFO",
              "[EVAL] ib_breakout: NO_SIGNAL no_ib_breakout_or_already_traded",
              strategy="ib_breakout"),
        ]
        r = IbWarmupGrader().grade(events, {})
        assert r.quant_pass        # 9.5min < 10
        assert r.qual_pass

    def test_fail_when_warmup_persists(self):
        events = [E(f"2026-04-25T08:{30+i:02d}:00", "strategies.ib_breakout", "DEBUG",
                    "[EVAL] ib_breakout: SKIP warmup_incomplete", strategy="ib_breakout")
                  for i in range(20)]
        r = IbWarmupGrader().grade(events, {})
        assert not r.quant_pass

    def test_warmup_log_after_clear_fails_qual(self):
        events = [
            E("2026-04-25T08:35:00", "strategies.ib_breakout", "INFO",
              "[EVAL] ib_breakout: NO_SIGNAL no_ib_breakout_or_already_traded",
              strategy="ib_breakout"),
            E("2026-04-25T09:00:00", "strategies.ib_breakout", "DEBUG",
              "[EVAL] ib_breakout: SKIP warmup_incomplete", strategy="ib_breakout"),
        ]
        r = IbWarmupGrader().grade(events, {})
        assert r.quant_pass        # cleared at +5min
        assert not r.qual_pass     # but warmup logged AFTER clear → fail


# ─────────── P5 compression squeeze ───────────

class TestCompressionSqueezeGrader:
    def test_30pct_drop_passes(self):
        baseline = {"compression_breakout_squeeze_not_held_per_session": 1000}
        # Today: 600 → 40% drop, exceeds 30%
        events = [E(f"2026-04-25T09:30:{i%60:02d}",
                    "strategies.compression_breakout", "DEBUG",
                    "[EVAL] compression_breakout: NO_SIGNAL squeeze_not_held_min_bars",
                    strategy="compression_breakout")
                  for i in range(600)]
        events += [E("2026-04-25T10:00:00", "strategies.compression_breakout", "INFO",
                     "[EVAL] compression_breakout: squeeze_held — explosion",
                     strategy="compression_breakout")]
        r = CompressionSqueezeGrader().grade(events, baseline)
        assert r.overall_pass

    def test_no_drop_fails(self):
        baseline = {"compression_breakout_squeeze_not_held_per_session": 1000}
        events = [E(f"2026-04-25T09:30:{i%60:02d}",
                    "strategies.compression_breakout", "DEBUG",
                    "[EVAL] compression_breakout: NO_SIGNAL squeeze_not_held_min_bars",
                    strategy="compression_breakout")
                  for i in range(950)]
        r = CompressionSqueezeGrader().grade(events, baseline)
        assert not r.quant_pass

    def test_no_baseline_skips_quant(self):
        events = []
        r = CompressionSqueezeGrader().grade(events, {})
        # No baseline → quant_pass True (skipped); no breakout → qual fail
        assert r.quant_pass is True
        assert r.qual_pass is False


# ─────────── P6 spring_setup silence ───────────

class TestSpringSilenceGrader:
    def test_pass_with_no_spring_logs(self):
        events = [E("2026-04-25T09:30:00", "Bot", "INFO",
                    "[SIM] Strategies: ['bias_momentum','vwap_pullback']")]
        r = SpringSilenceGrader().grade(events, {})
        assert r.overall_pass

    def test_fail_when_strategy_in_init(self):
        events = [E("2026-04-25T09:30:00", "Bot", "INFO",
                    "Strategies: ['bias_momentum','spring_setup']", strategy="spring_setup")]
        r = SpringSilenceGrader().grade(events, {})
        # The synthetic event has "spring_setup" in message → counted as fresh
        # AND init line includes it → qual fails
        assert not r.qual_pass

    def test_ignores_anthropic_debrief_payloads(self):
        events = [E("2026-04-25T17:30:00", "anthropic._base_client", "DEBUG",
                    'Request options: {... "strategy": "spring_setup" ...}',
                    strategy="spring_setup")]
        r = SpringSilenceGrader().grade(events, {})
        # The grader filters debrief payloads → quant passes
        assert r.quant_pass


# ─────────── Harness end-to-end ───────────

class TestHarness:
    def test_run_against_synthetic_log(self, tmp_path):
        from tools.grade_open_predictions import run_grading
        from datetime import date as date_cls

        log = tmp_path / "sim_bot.log"
        log.write_text(
            "2026-04-25 09:30:00,000 [Bot] INFO Strategies: ['bias_momentum']\n",
            encoding="utf-8",
        )
        # Patch GRADES_DIR + SUMMARY_LOG to tmp so we don't pollute
        import tools.grade_open_predictions as h
        h.GRADES_DIR = tmp_path / "grades"
        h.SUMMARY_LOG = tmp_path / "summary.log"
        h.BASELINE_DIR = tmp_path / "baselines"
        exit_code, results = run_grading(
            log_path=log, session_date=date_cls(2026, 4, 25),
            emit_json=True, emit_md=True, emit_html=True, notify=False,
        )
        # 6 grader results, ≥1 should fail (synthetic log lacks most events)
        assert len(results) == 6
        # Check JSON / MD / HTML emitted
        assert (h.GRADES_DIR / "2026-04-25.json").exists()
        assert (h.GRADES_DIR / "2026-04-25.md").exists()
        assert (h.GRADES_DIR / "2026-04-25.html").exists()
        # Summary log has one line
        assert h.SUMMARY_LOG.exists()
        assert "2026-04-25" in h.SUMMARY_LOG.read_text(encoding="utf-8")

    def test_missing_log_returns_2(self, tmp_path):
        from tools.grade_open_predictions import run_grading
        from datetime import date as date_cls
        exit_code, results = run_grading(
            log_path=tmp_path / "does_not_exist.log",
            session_date=date_cls(2026, 4, 25),
            emit_json=False, emit_md=False, emit_html=False, notify=False,
        )
        assert exit_code == 2
        assert results == []
