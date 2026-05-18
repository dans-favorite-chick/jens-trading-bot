"""Show the 10 most extreme z-score bars from the backtest."""
import csv
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "historical" / "backtest_results.csv"

with CSV_PATH.open() as f:
    rows = list(csv.DictReader(f))

rows.sort(key=lambda r: abs(float(r["spread_z"])), reverse=True)

print(f"{'TIMESTAMP':<21} {'NQ_CLOSE':>10} {'ES_CLOSE':>10} {'Z':>7} {'CORR':>6} {'BULL':>5} {'BEAR':>5} {'BOOST_L':>8} {'BOOST_S':>8}")
print("-" * 95)
for r in rows[:10]:
    print(
        f"{r['ts']:<21} "
        f"{r['nq_close']:>10} "
        f"{r['es_close']:>10} "
        f"{float(r['spread_z']):>+7.2f} "
        f"{float(r['correlation']):>6.2f} "
        f"{r['smt_bullish'][:5]:>5} "
        f"{r['smt_bearish'][:5]:>5} "
        f"{r['boost_long']:>8} "
        f"{r['boost_short']:>8}"
    )
