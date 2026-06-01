"""Tests for analytics/prepared_queries.py.

Phoenix Strategy Oracle — Task 1.

These tests build a small synthetic in-memory DuckDB using the real schema
(via tools.warehouse.db.apply_schema) and exercise every prepared-query
function. They MUST run without touching the real warehouse at
data/warehouse/phoenix.duckdb.

Friction-enforcement is the single most important invariant: every P&L
query must exclude trades from runs where friction_applied = FALSE.
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from tools.warehouse.db import apply_schema

# Module under test — imports must succeed before tests can run.
from analytics import prepared_queries as pq


# ---------------------------------------------------------------------------
# Synthetic-DB fixture
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _ins_run(con, run_id, strategy, friction, csv_kind="trades"):
    con.execute(
        """
        INSERT INTO runs (run_id, source_filename, csv_kind, strategy, friction_applied)
        VALUES (?, ?, ?, ?, ?)
        """,
        [run_id, f"{run_id}.csv", csv_kind, strategy, friction],
    )


def _ins_trade(
    con,
    run_id,
    strategy,
    direction,
    entry_ts,
    pnl_dollars,
    pnl_ticks,
    mae_ticks=None,
    mfe_ticks=None,
    regime=None,
    tod_bucket=None,
    hold_minutes=15.0,
    entry_context=None,
):
    con.execute(
        """
        INSERT INTO trades (
            run_id, strategy, direction, entry_ts, entry_price,
            exit_ts, exit_price, pnl_dollars, pnl_ticks, hold_minutes, year,
            mae_ticks, mfe_ticks, regime, tod_bucket, entry_context
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            strategy,
            direction,
            entry_ts,
            21000.0,
            entry_ts + timedelta(minutes=hold_minutes),
            21010.0,
            pnl_dollars,
            pnl_ticks,
            hold_minutes,
            entry_ts.year,
            mae_ticks,
            mfe_ticks,
            regime,
            tod_bucket,
            entry_context,
        ],
    )


@pytest.fixture
def db():
    """Fresh in-memory DuckDB with phoenix schema + synthetic fixture data.

    Layout:
      - run R1 ("alpha", friction=True)   -> 6 alpha trades, mix LONG/SHORT
      - run R2 ("alpha", friction=FALSE)  -> 2 alpha trades (must be excluded!)
      - run R3 ("beta",  friction=True)   -> 5 beta trades
      - run R4 ("gamma", friction=True)   -> 8 gamma trades (for sharpe months)
      - wfa_summary + wfa_windows for "alpha" only
    """
    con = duckdb.connect(":memory:")
    apply_schema(con)

    # ----- runs -----
    _ins_run(con, "R1", "alpha", True)
    _ins_run(con, "R2", "alpha", False)
    _ins_run(con, "R3", "beta", True)
    _ins_run(con, "R4", "gamma", True)

    # ----- alpha (R1, friction TRUE) — 6 trades, mix of LONG/SHORT, two regimes -----
    # Use a fixed reference "now" so window_days slicing is deterministic.
    # We pin to a recent UTC winter date (CST = UTC-6) so 14:30 UTC == 08:30 CT.
    # (April would be CDT and shift hours by 1; testing with a fixed offset
    #  keeps the hour-bucket arithmetic deterministic across systems.)
    base = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)  # 08:30 CST
    alpha_rows = [
        # (offset_days, direction, pnl_dollars, pnl_ticks, mae, mfe, regime, hour_offset_min)
        (-1, "LONG",  100.0,  4.0,  2, 6,  "TREND", 0),     # 08:30 CT hour=8
        (-2, "LONG", -50.0,  -2.0,  3, 1,  "TREND", 30),    # 09:00 CT hour=9
        (-3, "SHORT",  75.0,  3.0,  1, 4,  "CHOP",  60),    # 09:30 CT hour=9
        (-4, "SHORT", -25.0, -1.0,  2, 2,  "CHOP",  90),    # 10:00 CT hour=10
        (-5, "LONG",  200.0,  8.0,  1, 8,  "TREND", 120),   # 10:30 CT hour=10
        (-6, "SHORT", 150.0,  6.0,  0, 6,  "TREND", 0),     # 08:30 CT hour=8
    ]
    for off_d, direction, pnl_d, pnl_t, mae, mfe, regime, hour_off in alpha_rows:
        ts = base + timedelta(days=off_d, minutes=hour_off)
        _ins_trade(
            con, "R1", "alpha", direction, ts, pnl_d, pnl_t,
            mae_ticks=mae, mfe_ticks=mfe, regime=regime,
            tod_bucket="RTH_OPEN",
        )

    # ----- alpha (R2, friction FALSE) — must be EXCLUDED everywhere -----
    bad = datetime(2026, 1, 10, 14, 30, tzinfo=UTC)
    _ins_trade(con, "R2", "alpha", "LONG",  bad, 9999.0, 400.0,
               mae_ticks=0, mfe_ticks=400, regime="TREND")
    _ins_trade(con, "R2", "alpha", "SHORT", bad + timedelta(hours=1),
               -9999.0, -400.0, mae_ticks=400, mfe_ticks=0, regime="CHOP")

    # ----- beta (R3, friction TRUE) — 5 trades for strategies_with_trades min_n check -----
    for i in range(5):
        ts = base + timedelta(days=-(i + 1))
        _ins_trade(
            con, "R3", "beta", "LONG", ts, 10.0 * (i + 1), 1.0 * (i + 1),
            mae_ticks=1, mfe_ticks=2, regime="TREND",
        )

    # ----- gamma (R4, friction TRUE) — many trades across distinct months for sharpe -----
    # Place 3 trades in each of 3 distinct months so monthly_sharpe_proxy has data.
    months = [
        datetime(2026, 1, 15, 15, 0, tzinfo=UTC),
        datetime(2026, 2, 15, 15, 0, tzinfo=UTC),
        datetime(2026, 3, 15, 15, 0, tzinfo=UTC),
    ]
    pnls_per_month = [
        [100.0, -50.0, 75.0],
        [200.0, -100.0, 150.0],
        [10.0, 20.0, -5.0],
    ]
    for m_base, pnls in zip(months, pnls_per_month):
        for i, p in enumerate(pnls):
            _ins_trade(
                con, "R4", "gamma", "LONG",
                m_base + timedelta(hours=i),
                p, p / 25.0,
                mae_ticks=1, mfe_ticks=3, regime="TREND",
            )

    # ----- wfa_summary + wfa_windows for "alpha" -----
    con.execute(
        """
        INSERT INTO wfa_summary
          (run_id, strategy, n_windows, mean_is_pf, mean_oos_pf,
           median_oos_pf, pct_windows_degraded, robust)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ["R1", "alpha", 5, 1.6, 1.25, 1.30, 0.20, True],
    )
    for i in range(5):
        con.execute(
            """
            INSERT INTO wfa_windows
              (run_id, strategy, window_idx, is_start, is_end, oos_start, oos_end,
               is_pf, is_trades, oos_pf, oos_trades, oos_net, wfe, degraded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "R1", "alpha", i,
                datetime(2025, 1, 1).date() + timedelta(days=i * 30),
                datetime(2025, 1, 30).date() + timedelta(days=i * 30),
                datetime(2025, 1, 31).date() + timedelta(days=i * 30),
                datetime(2025, 2, 28).date() + timedelta(days=i * 30),
                1.5 + 0.1 * i, 100,
                1.2 + 0.1 * i, 40,
                500.0,
                0.8,
                False if i != 1 else True,
            ],
        )

    yield con
    con.close()


# ---------------------------------------------------------------------------
# assert_select_only
# ---------------------------------------------------------------------------

def test_assert_select_only_accepts_plain_select():
    pq.assert_select_only("SELECT 1")


def test_assert_select_only_accepts_column_named_created_at():
    # Word-boundary match — should NOT trip on "created_at" containing "CREATE".
    pq.assert_select_only("SELECT created_at FROM trades")


def test_assert_select_only_accepts_column_named_updated_at():
    pq.assert_select_only("SELECT updated_at, deleted FROM trades")


def test_assert_select_only_rejects_delete():
    with pytest.raises(ValueError):
        pq.assert_select_only("DELETE FROM trades")


def test_assert_select_only_rejects_drop():
    with pytest.raises(ValueError):
        pq.assert_select_only("DROP TABLE trades")


def test_assert_select_only_rejects_insert_lowercase():
    with pytest.raises(ValueError):
        pq.assert_select_only("insert into trades values (1)")


def test_assert_select_only_rejects_update_in_middle():
    with pytest.raises(ValueError):
        pq.assert_select_only("SELECT 1; UPDATE trades SET x=1")


def test_assert_select_only_rejects_truncate():
    with pytest.raises(ValueError):
        pq.assert_select_only("TRUNCATE TABLE trades")


# ---------------------------------------------------------------------------
# strategies_with_trades — verifies friction filter + min_n
# ---------------------------------------------------------------------------

def test_strategies_with_trades_filters_by_min_n(db):
    # alpha has 6 friction-true trades, beta has 5, gamma has 9.
    # min_n=6 → alpha and gamma only.
    out = pq.strategies_with_trades(db, window_days=400, min_n=6)
    assert set(out) == {"alpha", "gamma"}


def test_strategies_with_trades_excludes_friction_false(db):
    # If R2 were counted, alpha would have 8 trades. Use min_n=7 to prove
    # R2's 2 trades are excluded.
    out = pq.strategies_with_trades(db, window_days=400, min_n=7)
    assert "alpha" not in out
    assert "gamma" in out  # gamma has 9


def test_strategies_with_trades_returns_list(db):
    out = pq.strategies_with_trades(db, window_days=400, min_n=1)
    assert isinstance(out, list)
    assert all(isinstance(s, str) for s in out)


# ---------------------------------------------------------------------------
# trades_for_strategy
# ---------------------------------------------------------------------------

REQUIRED_TRADE_COLS = {
    "entry_ts", "exit_ts", "direction", "pnl_dollars", "pnl_ticks",
    "mae_ticks", "mfe_ticks", "regime", "tod_bucket",
    "session_date", "market_open_minutes", "hold_minutes",
}


def test_trades_for_strategy_columns(db):
    df = pq.trades_for_strategy(db, "alpha", window_days=400)
    assert REQUIRED_TRADE_COLS.issubset(df.columns)


def test_trades_for_strategy_excludes_friction_false_run(db):
    df = pq.trades_for_strategy(db, "alpha", window_days=400)
    # 6 friction-true trades for alpha; R2's 2 trades MUST NOT appear.
    assert len(df) == 6
    # Sentinel check: the friction-false trades had pnl 9999 / -9999.
    assert (df["pnl_dollars"].abs() < 9000).all()


def test_trades_for_strategy_window_truncation(db):
    # Alpha trades are spaced 1-6 days back from 2026-04-15. With
    # window_days=3 (relative to "now") we'd get zero since now > base.
    # Use a very small window relative to today's clock — should yield 0.
    df = pq.trades_for_strategy(db, "alpha", window_days=1)
    # All synthetic trades are far in the past relative to test runtime;
    # window_days=1 must exclude them.
    assert len(df) == 0


# ---------------------------------------------------------------------------
# monthly_sharpe_proxy
# ---------------------------------------------------------------------------

def test_monthly_sharpe_proxy_columns(db):
    df = pq.monthly_sharpe_proxy(db, months_back=240)
    assert {"month", "trade_count", "avg_pnl", "pnl_stddev",
            "sharpe_proxy", "win_rate"}.issubset(df.columns)


def test_monthly_sharpe_proxy_handcomputed_row(db):
    df = pq.monthly_sharpe_proxy(db, months_back=240)
    # Gamma month 2026-02 had pnls [200, -100, 150] => mean=83.33, count=3,
    # wins=2 (200 and 150 are >0), win_rate=2/3 ≈ 0.6667.
    # Filter by trade_count==3 AND avg_pnl > 80 for the Feb row.
    feb = df[(df["trade_count"] == 3) & (df["avg_pnl"] > 80)]
    assert not feb.empty
    row = feb.iloc[0]
    assert abs(row["avg_pnl"] - (200 - 100 + 150) / 3.0) < 1e-6
    assert abs(row["win_rate"] - 2 / 3) < 1e-6


# ---------------------------------------------------------------------------
# WFA summary / windows
# ---------------------------------------------------------------------------

def test_wfa_summary_for_strategy_returns_dict(db):
    d = pq.wfa_summary_for_strategy(db, "alpha")
    assert isinstance(d, dict)
    for k in ("n_windows", "mean_is_pf", "mean_oos_pf", "median_oos_pf",
              "pct_windows_degraded", "robust"):
        assert k in d
    assert d["n_windows"] == 5
    assert d["robust"] is True
    assert abs(d["pct_windows_degraded"] - 0.20) < 1e-9


def test_wfa_summary_for_strategy_missing(db):
    d = pq.wfa_summary_for_strategy(db, "does_not_exist")
    assert d == {} or d.get("n_windows") in (None, 0)


def test_wfa_windows_for_strategy_returns_frame(db):
    df = pq.wfa_windows_for_strategy(db, "alpha")
    assert len(df) == 5
    assert {"window_idx", "is_pf", "oos_pf", "degraded"}.issubset(df.columns)


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def test_panel_by_hour_ct_columns(db):
    df = pq.panel_by_hour_ct(db, "alpha", window_days=400)
    assert {"hour_ct", "n_trades", "wins", "win_rate",
            "profit_factor", "avg_pnl"}.issubset(df.columns)


def test_panel_by_hour_ct_handcomputed(db):
    df = pq.panel_by_hour_ct(db, "alpha", window_days=400)
    # Alpha trades placed at CT hours 8, 9, 9, 10, 10, 8 (08:30,09:00,09:30,10:00,10:30,08:30).
    # So hour buckets should be {8: 2, 9: 2, 10: 2}.
    counts = dict(zip(df["hour_ct"].astype(int), df["n_trades"].astype(int)))
    assert counts.get(8, 0) == 2
    assert counts.get(9, 0) == 2
    assert counts.get(10, 0) == 2


def test_panel_by_regime_handcomputed(db):
    df = pq.panel_by_regime(db, "alpha", window_days=400)
    # Alpha regimes: TREND x 4 (rows 0,1,4,5), CHOP x 2 (rows 2,3).
    counts = dict(zip(df["regime"], df["n_trades"].astype(int)))
    assert counts.get("TREND") == 4
    assert counts.get("CHOP") == 2


def test_panel_by_direction_handcomputed(db):
    df = pq.panel_by_direction(db, "alpha", window_days=400)
    # Alpha: LONG x 3 (rows 0,1,4), SHORT x 3 (rows 2,3,5).
    counts = dict(zip(df["direction"], df["n_trades"].astype(int)))
    assert counts.get("LONG") == 3
    assert counts.get("SHORT") == 3


# ---------------------------------------------------------------------------
# MAE/MFE
# ---------------------------------------------------------------------------

def test_mae_mfe_distribution_columns(db):
    df = pq.mae_mfe_distribution(db, "alpha", "LONG", window_days=400)
    assert {"bucket_ticks", "n_trades", "win_rate"}.issubset(df.columns)


def test_mae_mfe_distribution_buckets_long(db):
    df = pq.mae_mfe_distribution(db, "alpha", "LONG", window_days=400)
    # Alpha LONG mae_ticks: 2, 3, 1 -> buckets 1,2,3 each n=1.
    assert int(df["n_trades"].sum()) == 3


# ---------------------------------------------------------------------------
# Graceful-degradation queries
# ---------------------------------------------------------------------------

def test_daily_ib_regime_missing_table(db):
    # bar_events does not exist in the schema. Must return an empty DataFrame
    # with the right columns and NOT raise.
    df = pq.daily_ib_regime(db, window_days=400)
    assert isinstance(df, pd.DataFrame)
    assert {"session_date", "ib_width_ticks", "atr_20d", "ib_regime"}.issubset(df.columns)
    assert len(df) == 0


def test_confluence_lift_no_confluences_subobject(db):
    # None of our synthetic entry_contexts have a 'confluences' sub-object.
    df = pq.confluence_lift(db, "alpha", window_days=400)
    assert isinstance(df, pd.DataFrame)
    # Either empty, or the only bucket is 0.
    if not df.empty:
        assert set(df["confluence_count"].unique()).issubset({0})


# ---------------------------------------------------------------------------
# current_param_value (AST-based, never imports)
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_strategies_file(tmp_path):
    p = tmp_path / "strategies.py"
    p.write_text(textwrap.dedent("""
        # Synthetic test fixture.
        SOMETHING_ELSE = 99

        STRATEGIES = {
            "alpha": {
                "enabled": True,
                "min_stop_ticks": 24,
                "target_rr": 2.5,
                "session_block": ["10:00-13:29"],
            },
            "beta": {
                "enabled": False,
                "min_stop_ticks": 40,
            },
        }
    """).strip(), encoding="utf-8")
    return p


def test_current_param_value_reads_int(tiny_strategies_file):
    v = pq.current_param_value("alpha", "min_stop_ticks", str(tiny_strategies_file))
    assert v == 24


def test_current_param_value_reads_float(tiny_strategies_file):
    v = pq.current_param_value("alpha", "target_rr", str(tiny_strategies_file))
    assert v == 2.5


def test_current_param_value_reads_bool(tiny_strategies_file):
    v = pq.current_param_value("beta", "enabled", str(tiny_strategies_file))
    assert v is False


def test_current_param_value_reads_list(tiny_strategies_file):
    v = pq.current_param_value("alpha", "session_block", str(tiny_strategies_file))
    assert v == ["10:00-13:29"]


def test_current_param_value_missing_strategy_raises(tiny_strategies_file):
    with pytest.raises(KeyError):
        pq.current_param_value("ghost", "enabled", str(tiny_strategies_file))


def test_current_param_value_missing_param_raises(tiny_strategies_file):
    with pytest.raises(KeyError):
        pq.current_param_value("alpha", "nonexistent_param", str(tiny_strategies_file))


def test_current_param_value_bad_file_raises(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("this is not valid (((( python", encoding="utf-8")
    with pytest.raises(ValueError):
        pq.current_param_value("alpha", "x", str(bad))


def test_current_param_value_does_not_import_module(tmp_path):
    # If the module is imported, the side-effect (writing a sentinel file)
    # would trigger. AST parse must NOT execute it.
    sentinel = tmp_path / "side_effect.flag"
    p = tmp_path / "evil_strategies.py"
    p.write_text(textwrap.dedent(f"""
        # If this is imported the file below would be created.
        with open(r"{sentinel}", "w") as fh:
            fh.write("imported")

        STRATEGIES = {{
            "alpha": {{"x": 1}},
        }}
    """).strip(), encoding="utf-8")
    v = pq.current_param_value("alpha", "x", str(p))
    assert v == 1
    assert not sentinel.exists(), "current_param_value must not import the module"


# ---------------------------------------------------------------------------
# open_conn
# ---------------------------------------------------------------------------

def test_open_conn_is_readonly(tmp_path):
    # Build a real on-disk DB so we can verify read-only enforcement.
    db_path = tmp_path / "ro.duckdb"
    setup = duckdb.connect(str(db_path))
    apply_schema(setup)
    setup.close()

    con = pq.open_conn(str(db_path))
    with pytest.raises(Exception):
        con.execute("INSERT INTO runs (run_id, source_filename, csv_kind) VALUES ('x', 'x.csv', 'trades')")
    con.close()
