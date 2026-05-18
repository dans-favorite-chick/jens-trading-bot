"""
Databento TBBO Footprint Downloader
=====================================

Downloads historical TBBO (Trades + Best Bid/Offer) data for MNQ from
Databento, enabling true footprint reconstruction (per-price-level bid
volume vs ask volume per bar).

THREE MODES:
  estimate  — Compute Databento cost for a date range. FREE (no download).
              Use this FIRST to decide what range to buy.

  download  — Actually fetch data. CHARGES YOUR DATABENTO ACCOUNT.
              Confirms cost before proceeding (interactive prompt).

  convert   — Process downloaded TBBO into per-5m-bar footprint.
              FREE (local processing only).

USAGE:
  # 1. First-time setup: install SDK + set API key
  pip install databento
  setx DATABENTO_API_KEY "your-key-here"
  # (open new terminal so env var loads)

  # 2. Estimate cost for what you want to download
  python tools/databento_footprint_download.py estimate --start 2024-05-17 --end 2026-05-17

  # 3. If cost is acceptable, download
  python tools/databento_footprint_download.py download --start 2024-05-17 --end 2026-05-17

  # 4. Convert downloaded data into Phoenix-usable footprint
  python tools/databento_footprint_download.py convert

DATASET DETAILS:
  - Dataset:  GLBX.MDP3 (CME Globex MDP 3.0 — same as your OHLCV)
  - Schema:   tbbo (Trades + BBO)
  - Symbology: parent (matches MNQ front-month rolling)
  - Symbols:  MNQ.FUT (all MNQ futures contracts; we filter to front-month)

WHAT YOU CAN DO ONCE DOWNLOADED:
  - Backtest strategies/footprint_cvd_reversal.py (currently un-backtestable)
  - Validate the footprint-as-confluence hypothesis (Phase 13 Section R.5)
  - Build per-5m-bar footprint reconstruction:
      buy_volume / sell_volume / delta per bar
      per-price-level bid_vol / ask_vol (the actual footprint)
      stacked imbalance detection
      POC migration tracking intraday

COST ESTIMATE (as of 2026 — verify with actual `estimate` call):
  TBBO for MNQ front-month, ~1 month:   ~$10-30
  TBBO for MNQ front-month, ~1 year:    ~$100-300
  TBBO for MNQ front-month, ~5 years:   ~$500-1500
  Recommendation: start with 1 year (most recent), validate the
  hypothesis with that data, then backfill if results justify cost.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "historical" / "databento_tbbo"

DATASET = "GLBX.MDP3"
SCHEMA = "tbbo"
SYMBOLS = ["MNQ.FUT"]  # parent symbology covers all MNQ contracts (front-month rolling)
STYPE_IN = "parent"


def _check_setup() -> None:
    """Verify SDK installed + API key set. Exit with friendly error if not."""
    try:
        import databento  # noqa: F401
    except ImportError:
        print("[ERROR] databento package not installed.\n")
        print("Run: pip install databento")
        sys.exit(1)
    if not os.environ.get("DATABENTO_API_KEY"):
        print("[ERROR] DATABENTO_API_KEY environment variable not set.\n")
        print("Find your key at: https://databento.com/portal/keys")
        print("Then run:")
        print('  setx DATABENTO_API_KEY "db-..."   (Windows, persistent)')
        print("  Then open a NEW terminal so the env var loads.")
        sys.exit(1)


def _make_client():
    import databento as db
    return db.Historical(os.environ["DATABENTO_API_KEY"])


def cmd_estimate(start: str, end: str) -> None:
    """FREE — compute cost without downloading."""
    _check_setup()
    client = _make_client()
    print("=" * 70)
    print(f"DATABENTO COST ESTIMATE — {SCHEMA} for {SYMBOLS}")
    print("=" * 70)
    print(f"  Dataset:  {DATASET}")
    print(f"  Schema:   {SCHEMA}")
    print(f"  Symbols:  {SYMBOLS} (stype_in={STYPE_IN})")
    print(f"  Start:    {start}")
    print(f"  End:      {end}")
    print()
    try:
        cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=SYMBOLS,
            schema=SCHEMA,
            start=start,
            end=end,
            stype_in=STYPE_IN,
        )
        size = client.metadata.get_billable_size(
            dataset=DATASET,
            symbols=SYMBOLS,
            schema=SCHEMA,
            start=start,
            end=end,
            stype_in=STYPE_IN,
        )
        print(f"  ESTIMATED COST:  ${cost:.2f}")
        print(f"  Billable size:   {size:,} bytes ({size / 1e9:.2f} GB)")
        print()
        print("This estimate is FREE and not charged to your account.")
        print()
        print("If acceptable, download with:")
        print(f"  python tools/databento_footprint_download.py download --start {start} --end {end}")
    except Exception as e:
        print(f"[ERROR] Cost estimate failed: {e}")
        print()
        print("Common causes:")
        print("  - DATABENTO_API_KEY invalid or expired")
        print("  - Date range outside dataset coverage")
        print("  - Network/firewall blocking api.databento.com")
        sys.exit(1)


def cmd_download(start: str, end: str, skip_confirm: bool = False) -> None:
    """COSTS MONEY — actually downloads the data."""
    _check_setup()
    client = _make_client()
    print("=" * 70)
    print(f"DATABENTO DOWNLOAD — {SCHEMA} for {SYMBOLS}")
    print("=" * 70)
    print(f"  Start:    {start}")
    print(f"  End:      {end}")
    print()
    # Cost confirmation
    try:
        cost = client.metadata.get_cost(
            dataset=DATASET, symbols=SYMBOLS, schema=SCHEMA,
            start=start, end=end, stype_in=STYPE_IN,
        )
        size = client.metadata.get_billable_size(
            dataset=DATASET, symbols=SYMBOLS, schema=SCHEMA,
            start=start, end=end, stype_in=STYPE_IN,
        )
        print(f"  ESTIMATED COST:  ${cost:.2f}")
        print(f"  Download size:   {size / 1e9:.2f} GB")
    except Exception as e:
        print(f"[ERROR] Cost lookup failed: {e}")
        sys.exit(1)

    if not skip_confirm:
        print()
        resp = input("Type 'yes' to proceed with download (charges your account): ").strip().lower()
        if resp != "yes":
            print("Aborted.")
            sys.exit(0)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / f"mnq_tbbo_{start}_{end}.dbn.zst"

    print()
    print(f"Downloading to: {out_file}")
    print("This may take 10-30 min depending on size...")
    try:
        data = client.timeseries.get_range(
            dataset=DATASET, symbols=SYMBOLS, schema=SCHEMA,
            start=start, end=end, stype_in=STYPE_IN,
        )
        data.to_file(out_file)
        actual_mb = out_file.stat().st_size / 1e6
        print(f"[OK] Download complete. File size: {actual_mb:.1f} MB")
        print()
        print("Next: convert into per-bar footprint with:")
        print(f"  python tools/databento_footprint_download.py convert")
    except Exception as e:
        print(f"[ERROR] Download failed: {e}")
        sys.exit(1)


def cmd_convert() -> None:
    """FREE — process downloaded TBBO files into per-5m-bar footprint."""
    _check_setup()
    print("=" * 70)
    print("CONVERT TBBO -> PER-5M-BAR FOOTPRINT")
    print("=" * 70)
    files = sorted(OUTPUT_DIR.glob("*.dbn.zst"))
    if not files:
        print(f"[ERROR] No TBBO files found in {OUTPUT_DIR}")
        print("Run `download` first.")
        sys.exit(1)

    print(f"Found {len(files)} TBBO file(s):")
    for f in files:
        print(f"  {f.name} ({f.stat().st_size / 1e6:.1f} MB)")
    print()

    import databento as db
    import pandas as pd

    all_bars = []
    for tbbo_file in files:
        print(f"Processing {tbbo_file.name}...")
        store = db.DBNStore.from_file(tbbo_file)
        df = store.to_df()
        # TBBO columns: ts_event, action, side, price, size, bid_px_00, ask_px_00, ...
        # 'side' here is the aggressor side (B = buyer-aggressor, A = seller-aggressor)
        df["ts_event"] = pd.to_datetime(df["ts_event"], utc=True)
        df["bar_5m"] = df["ts_event"].dt.floor("5min")

        # Classify aggressor:
        #   side == 'B' -> buy aggressor (lifted ask)
        #   side == 'A' -> sell aggressor (hit bid)
        df["buy_vol"] = df.apply(lambda r: r["size"] if r["side"] == "B" else 0, axis=1)
        df["sell_vol"] = df.apply(lambda r: r["size"] if r["side"] == "A" else 0, axis=1)

        # Per-bar aggregation
        bar_agg = df.groupby("bar_5m").agg(
            total_volume=("size", "sum"),
            buy_volume=("buy_vol", "sum"),
            sell_volume=("sell_vol", "sum"),
            open=("price", "first"),
            close=("price", "last"),
            high=("price", "max"),
            low=("price", "min"),
            n_trades=("size", "count"),
        ).reset_index()
        bar_agg["delta"] = bar_agg["buy_volume"] - bar_agg["sell_volume"]

        # Per-bar per-price-level footprint (sparse format)
        footprint = df.groupby(["bar_5m", "price"]).agg(
            level_buy=("buy_vol", "sum"),
            level_sell=("sell_vol", "sum"),
        ).reset_index()
        footprint["level_total"] = footprint["level_buy"] + footprint["level_sell"]
        footprint["level_imbalance"] = (footprint["level_buy"] /
                                         footprint["level_sell"].replace(0, 0.001))

        # POC per bar = price with max volume
        poc_per_bar = footprint.loc[
            footprint.groupby("bar_5m")["level_total"].idxmax()
        ][["bar_5m", "price"]].rename(columns={"price": "poc"})
        bar_agg = bar_agg.merge(poc_per_bar, on="bar_5m", how="left")

        all_bars.append(bar_agg)

        # Save sparse footprint per file
        fp_out = OUTPUT_DIR / (tbbo_file.stem.replace(".dbn", "") + "_footprint_sparse.parquet")
        footprint.to_parquet(fp_out, index=False)
        print(f"  -> wrote {fp_out.name} ({len(footprint):,} price-level rows)")

    # Combine bars across all files
    combined = pd.concat(all_bars, ignore_index=True).sort_values("bar_5m")
    combined_out = OUTPUT_DIR / "mnq_footprint_5m.csv"
    combined.to_csv(combined_out, index=False)
    print()
    print(f"[OK] Combined per-5m-bar footprint: {combined_out.name}")
    print(f"   {len(combined):,} bars, "
          f"{combined['bar_5m'].min()} -> {combined['bar_5m'].max()}")
    print()
    print("Ready for backtest. Phoenix can now consume:")
    print(f"  mnq_footprint_5m.csv          — per-bar delta/POC summary")
    print(f"  *_footprint_sparse.parquet     — per-price-level detail")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Databento TBBO downloader for footprint reconstruction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_est = sub.add_parser("estimate", help="Cost estimate (FREE)")
    p_est.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_est.add_argument("--end", required=True, help="YYYY-MM-DD")

    p_dl = sub.add_parser("download", help="Download data (COSTS MONEY)")
    p_dl.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_dl.add_argument("--end", required=True, help="YYYY-MM-DD")
    p_dl.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    sub.add_parser("convert", help="Process downloaded TBBO -> footprint (FREE)")

    args = parser.parse_args()
    if args.cmd == "estimate":
        cmd_estimate(args.start, args.end)
    elif args.cmd == "download":
        cmd_download(args.start, args.end, skip_confirm=args.yes)
    elif args.cmd == "convert":
        cmd_convert()
    return 0


if __name__ == "__main__":
    sys.exit(main())
