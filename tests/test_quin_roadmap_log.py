"""Tests for tools/quin_roadmap_log.py — daily roadmap capture +
post-session calibration.

Covers:
  - schema validation (required fields, regime enum, fit enum, prob range)
  - capture writes the JSON to logs/quin_roadmap/<date>.json
  - parse_daily_summary extracts per-strategy P&L from Sprint C report
  - reconcile produces a calibration JSON + appends CSV row
  - is_consistent classifier (ideal/favorable expects pnl>=0; hostile <0;
    mixed/conditional/watch_open/disabled return None)
  - summary aggregates trailing N rows
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TOOL = ROOT / "tools" / "quin_roadmap_log.py"


def _today_capture(date="2026-05-04"):
    """A valid morning roadmap capture mimicking the May 4 doc."""
    return {
        "date": date,
        "fetched_ts_ct": "2026-05-04T07:30:00",
        "regime": "POSITIVE_STRONG",
        "net_gex_M": 4.21,
        "total_gex_M": 12.62,
        "iv_30d_pct": 19.63,
        "q_score": {"momentum": 5, "seasonality": -1,
                    "volatility": 1, "options": 5},
        "conviction": "high_bullish",
        "levels": {
            "call_resistance": 28000,
            "put_support": 25000,
            "hvl": 27340,
            "odte_call_resistance": 28000,
            "odte_put_support": 27750,
            "blind_spots": [27433.38, 27501.88, 27789.92,
                            28069.90, 26882.02],
        },
        "predicted_range": {"low": 27750, "high": 28000,
                            "probability": 0.75},
        "predicted_strategy_fit": {
            "vwap_band_reversion": "ideal",
            "vwap_pullback": "favorable",
            "dom_pullback": "favorable",
            "bias_momentum": "hostile",
            "noise_area_momentum": "mixed",
            "opening_session": "watch_open",
            "high_precision_only": "hostile",
            "spring_setup": "conditional",
        },
        "manual_notes": "0DTE expiration today",
    }


# ─── schema validation ───────────────────────────────────────────────

def test_validate_clean():
    from tools.quin_roadmap_log import validate_capture
    errs = validate_capture(_today_capture())
    assert errs == [], f"clean capture failed validation: {errs}"


def test_validate_missing_top_level():
    from tools.quin_roadmap_log import validate_capture
    d = _today_capture()
    del d["regime"]
    errs = validate_capture(d)
    assert any("regime" in e for e in errs)


def test_validate_bad_regime():
    from tools.quin_roadmap_log import validate_capture
    d = _today_capture()
    d["regime"] = "FROBNICATED"
    errs = validate_capture(d)
    assert any("regime" in e for e in errs)


def test_validate_probability_out_of_range():
    from tools.quin_roadmap_log import validate_capture
    d = _today_capture()
    d["predicted_range"]["probability"] = 1.5
    errs = validate_capture(d)
    assert any("probability" in e for e in errs)


def test_validate_bad_fit_value():
    from tools.quin_roadmap_log import validate_capture
    d = _today_capture()
    d["predicted_strategy_fit"]["bias_momentum"] = "totally_doomed"
    errs = validate_capture(d)
    assert any("totally_doomed" in e for e in errs)


def test_validate_missing_qscore_key():
    from tools.quin_roadmap_log import validate_capture
    d = _today_capture()
    del d["q_score"]["momentum"]
    errs = validate_capture(d)
    assert any("q_score" in e for e in errs)


# ─── capture phase ───────────────────────────────────────────────────

def test_capture_writes_file(tmp_path):
    from tools.quin_roadmap_log import capture
    in_path = tmp_path / "today.json"
    in_path.write_text(json.dumps(_today_capture()))
    out_path = capture(in_path, tmp_path)
    assert out_path.exists()
    assert out_path.name == "2026-05-04.json"
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["regime"] == "POSITIVE_STRONG"
    assert data["levels"]["hvl"] == 27340
    # fetched_ts_ct preserved if supplied
    assert data["fetched_ts_ct"] == "2026-05-04T07:30:00"


def test_capture_adds_fetched_ts_if_missing(tmp_path):
    from tools.quin_roadmap_log import capture
    d = _today_capture()
    del d["fetched_ts_ct"]
    in_path = tmp_path / "today.json"
    in_path.write_text(json.dumps(d))
    out_path = capture(in_path, tmp_path)
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert "fetched_ts_ct" in data


def test_capture_rejects_invalid_schema(tmp_path):
    from tools.quin_roadmap_log import capture
    d = _today_capture()
    d["regime"] = "INVALID"
    in_path = tmp_path / "bad.json"
    in_path.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="schema validation failed"):
        capture(in_path, tmp_path)


# ─── parse_daily_summary ────────────────────────────────────────────

def test_parse_daily_summary(tmp_path):
    from tools.quin_roadmap_log import parse_daily_summary
    md = tmp_path / "out" / "daily_summary_2026-05-04.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "# Phoenix Daily Session Summary - 2026-05-04\n\n"
        "## Bot: `sim`\n\n"
        "- Total signals:  10\n\n"
        "### Per strategy\n\n"
        "| strategy | signals | fills | wins | losses | net P&L |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        "| `bias_momentum` | 4 | 3 | 1 | 2 | $-42.50 |\n"
        "| `vwap_band_reversion` | 6 | 6 | 5 | 1 | $+87.25 |\n\n"
        "### Top 10 rejection reasons\n\n"
        "| strategy | reason | count |\n"
        "|---|---|---:|\n"
        "| `bias_momentum` | foo | 5 |\n",
        encoding="utf-8",
    )
    parsed = parse_daily_summary(md)
    assert "sim" in parsed
    assert parsed["sim"]["bias_momentum"]["fills"] == 3
    assert parsed["sim"]["bias_momentum"]["net_pnl"] == -42.50
    assert parsed["sim"]["vwap_band_reversion"]["wins"] == 5
    assert parsed["sim"]["vwap_band_reversion"]["net_pnl"] == 87.25


def test_parse_daily_summary_missing_file_returns_empty(tmp_path):
    from tools.quin_roadmap_log import parse_daily_summary
    out = parse_daily_summary(tmp_path / "doesnt_exist.md")
    assert out == {}


# ─── consistency classifier ──────────────────────────────────────────

def test_is_consistent_ideal_with_profit():
    from tools.quin_roadmap_log import is_consistent
    actual = {"signals": 6, "fills": 6, "wins": 5, "losses": 1, "net_pnl": 87.25}
    assert is_consistent("ideal", actual) is True


def test_is_consistent_ideal_with_loss():
    from tools.quin_roadmap_log import is_consistent
    actual = {"signals": 6, "fills": 6, "wins": 1, "losses": 5, "net_pnl": -50.0}
    assert is_consistent("ideal", actual) is False


def test_is_consistent_hostile_with_loss():
    """Predicted hostile + actual loss = prediction was right."""
    from tools.quin_roadmap_log import is_consistent
    actual = {"signals": 4, "fills": 3, "wins": 1, "losses": 2, "net_pnl": -42.50}
    assert is_consistent("hostile", actual) is True


def test_is_consistent_hostile_with_profit():
    """Predicted hostile but it actually made money = prediction wrong."""
    from tools.quin_roadmap_log import is_consistent
    actual = {"signals": 4, "fills": 3, "wins": 3, "losses": 0, "net_pnl": 25.0}
    assert is_consistent("hostile", actual) is False


def test_is_consistent_mixed_returns_none():
    from tools.quin_roadmap_log import is_consistent
    actual = {"signals": 1, "fills": 1, "wins": 1, "losses": 0, "net_pnl": 5.0}
    assert is_consistent("mixed", actual) is None


def test_is_consistent_zero_fills_returns_none():
    """If a strategy didn't fire, we can't evaluate the prediction."""
    from tools.quin_roadmap_log import is_consistent
    actual = {"signals": 0, "fills": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
    assert is_consistent("ideal", actual) is None


# ─── reconcile end-to-end ────────────────────────────────────────────

def _seed_capture_and_summary(tmp_path):
    """Helper: write a capture + a fake daily_summary so reconcile has
    something to chew on."""
    cap_dir = tmp_path / "logs" / "quin_roadmap"
    cap_dir.mkdir(parents=True)
    (cap_dir / "2026-05-04.json").write_text(
        json.dumps(_today_capture()), encoding="utf-8"
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "daily_summary_2026-05-04.md").write_text(
        "## Bot: `sim`\n\n"
        "### Per strategy\n\n"
        "| strategy | signals | fills | wins | losses | net P&L |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        "| `bias_momentum` | 4 | 3 | 1 | 2 | $-42.50 |\n"
        "| `vwap_band_reversion` | 6 | 6 | 5 | 1 | $+87.25 |\n"
        "| `vwap_pullback` | 3 | 3 | 2 | 1 | $+10.00 |\n"
        "| `dom_pullback` | 2 | 2 | 1 | 1 | $-5.00 |\n",
        encoding="utf-8",
    )


def test_reconcile_writes_calibration_json(tmp_path):
    from tools.quin_roadmap_log import reconcile
    _seed_capture_and_summary(tmp_path)
    result = reconcile("2026-05-04", tmp_path)
    cal_path = tmp_path / "out" / "quin_calibration_2026-05-04.json"
    assert cal_path.exists()
    data = json.loads(cal_path.read_text(encoding="utf-8"))
    assert data["date"] == "2026-05-04"
    # bias_momentum hostile + actual loss => consistent True
    bm = data["per_strategy_actual"]["bias_momentum"]
    assert bm["consistent"] is True
    assert bm["net_pnl"] == -42.50
    # vwap_band_reversion ideal + actual profit => consistent True
    vbr = data["per_strategy_actual"]["vwap_band_reversion"]
    assert vbr["consistent"] is True
    # dom_pullback favorable but lost $5 => consistent False
    dp = data["per_strategy_actual"]["dom_pullback"]
    assert dp["consistent"] is False
    # calibration score: 3 of 4 evaluable were consistent
    assert data["evaluable_count"] >= 3
    assert data["calibration_score"] is not None


def test_reconcile_appends_csv_row(tmp_path):
    from tools.quin_roadmap_log import reconcile
    _seed_capture_and_summary(tmp_path)
    reconcile("2026-05-04", tmp_path)
    csv_path = tmp_path / "out" / "quin_phoenix_calibration.csv"
    assert csv_path.exists()
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-05-04"
    assert r["regime"] == "POSITIVE_STRONG"
    assert float(r["net_gex_M"]) == 4.21
    assert int(r["evaluable_strategies"]) >= 3


def test_reconcile_two_days_two_rows(tmp_path):
    """Running reconcile on two different dates appends — never overwrites."""
    from tools.quin_roadmap_log import capture, reconcile
    cap_dir = tmp_path / "logs" / "quin_roadmap"
    cap_dir.mkdir(parents=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    for d in ("2026-05-04", "2026-05-05"):
        c = _today_capture(date=d)
        (cap_dir / f"{d}.json").write_text(json.dumps(c), encoding="utf-8")
        (out_dir / f"daily_summary_{d}.md").write_text(
            f"## Bot: `sim`\n\n### Per strategy\n\n"
            f"| strategy | signals | fills | wins | losses | net P&L |\n"
            f"|---|---:|---:|---:|---:|---:|\n"
            f"| `vwap_band_reversion` | 1 | 1 | 1 | 0 | $+10.00 |\n",
            encoding="utf-8",
        )
        reconcile(d, tmp_path)
    rows = list(csv.DictReader(
        (tmp_path / "out" / "quin_phoenix_calibration.csv").open(encoding="utf-8")
    ))
    assert len(rows) == 2
    assert {r["date"] for r in rows} == {"2026-05-04", "2026-05-05"}


def test_reconcile_missing_capture_raises(tmp_path):
    from tools.quin_roadmap_log import reconcile
    with pytest.raises(FileNotFoundError):
        reconcile("2026-12-31", tmp_path)


def test_reconcile_missing_summary_still_succeeds(tmp_path):
    """If daily_summary is missing (bot was down all day), reconcile
    still runs — every strategy just shows zeros + None consistency."""
    from tools.quin_roadmap_log import reconcile
    cap_dir = tmp_path / "logs" / "quin_roadmap"
    cap_dir.mkdir(parents=True)
    (cap_dir / "2026-05-04.json").write_text(
        json.dumps(_today_capture()), encoding="utf-8"
    )
    (tmp_path / "out").mkdir(parents=True)
    result = reconcile("2026-05-04", tmp_path)
    assert result["evaluable_count"] == 0
    assert result["calibration_score"] is None


# ─── CLI smoke ──────────────────────────────────────────────────────

def test_cli_capture(tmp_path):
    """Run the tool as a subprocess: --capture --input <path>.
    Pre-create logs/ so _data_root() prefers cwd over the real ROOT."""
    (tmp_path / "logs").mkdir()
    in_path = tmp_path / "today.json"
    in_path.write_text(json.dumps(_today_capture()))
    result = subprocess.run(
        [sys.executable, str(TOOL), "--capture", "--input", str(in_path)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "CAPTURED" in result.stdout
    assert (tmp_path / "logs" / "quin_roadmap" / "2026-05-04.json").exists()


def test_cli_capture_then_reconcile(tmp_path):
    in_path = tmp_path / "today.json"
    in_path.write_text(json.dumps(_today_capture()))
    (tmp_path / "out").mkdir(parents=True)
    (tmp_path / "out" / "daily_summary_2026-05-04.md").write_text(
        "## Bot: `sim`\n\n### Per strategy\n\n"
        "| strategy | signals | fills | wins | losses | net P&L |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        "| `bias_momentum` | 4 | 3 | 1 | 2 | $-42.50 |\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(TOOL),
         "--capture", "--input", str(in_path),
         "--reconcile", "--date", "2026-05-04"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "CAPTURED" in result.stdout
    assert "RECONCILED" in result.stdout


def test_cli_summary_empty(tmp_path):
    """--summary on a fresh dir prints 'no calibration history'."""
    result = subprocess.run(
        [sys.executable, str(TOOL), "--summary"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "No calibration history" in result.stdout


def test_cli_no_args_prints_help_and_exits_nonzero(tmp_path):
    """No --capture, --reconcile, or --summary → print help, exit 1."""
    result = subprocess.run(
        [sys.executable, str(TOOL)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 1
