"""
tbbo_cache_builder.py - Canonical MNQ TBBO tick cache builder + loader.

PURPOSE
-------
The raw Databento TBBO file `mnq_tbbo_2026-03-17_2026-05-17.dbn.zst` was
downloaded with `MNQ.FUT` continuous symbology (stype_in='parent').  That
download pulls in EVERYTHING the parent symbol matched during the window:

  * Multiple expirations: MNQH6, MNQM6, MNQU6, MNQZ6, MNQH7, MNQM7 outrights
  * Calendar spread instruments: MNQH6-MNQM6, MNQM6-MNQZ6, MNQH6-MNQH7, ...
  * Time-overlap between an expiring contract and its replacement

This is a hygiene LANDMINE for any downstream tool that expects a single
instrument's tick stream:

  1. Spread instruments price at ~$200-$1000 (the spread, not the outright).
     If your slippage / fill-quality / trail-stop simulator treats those as
     fills for a MNQM6 trade at $24,800, you get nonsense like a SHORT at
     24818 'filling' at 215 for $49K of fake P&L.
  2. Without a `symbol` column on the cached parquet, future tooling can't
     even tell which rows are which.
  3. During the H6 -> M6 rollover (mid-March 2026), the same minute can
     contain ticks from BOTH outrights interleaved.  A naive consumer that
     just sorts by ts and computes price diffs will see synthetic ~$200
     gaps that are actually contract swaps, not market moves.

Two earlier downstream tools each rebuilt their own MNQM6-only cache:

  * `mnq_ticks.parquet`           (built by `phoenix_tick_trail_verification.py`)
  * `mnq_ticks_slim.parquet`      (built by `phoenix_tick_entry_quality.py`)

Both work, both are incompatible (different column names, neither carries
`symbol`).  This module replaces both with a CANONICAL cache that any
future tool can blindly trust:

  * `mnq_ticks_clean.parquet`     <-- THIS IS THE ONE TO USE
  * `mnq_ticks_clean.metadata.json`  (provenance, drop counts, sanity stats)

HYGIENE FILTERS APPLIED (in this exact order)
---------------------------------------------
  a. KEEP the `symbol` column on output.
  b. DROP any row whose symbol contains a hyphen (calendar spreads).
  c. KEEP only outright single-contract symbols matching ^MNQ[HMUZ]\\d+$.
  d. For each trading day (UTC date), pick the symbol with the highest
     volume that day.  Keep ONLY rows belonging to that symbol on that day.
     This handles rollovers smoothly: front month switches around expiry
     and the dominant-by-volume rule follows it.
  e. Sort by ts_event (event time).
  f. Verify max tick-to-tick price jump is <500 ticks (= 125 price points);
     transitions at session close can reach ~330 ticks which is normal.

OUTPUT SCHEMA (`mnq_ticks_clean.parquet`)
-----------------------------------------
  Index: ts_event (datetime64[ns, UTC]), sorted ascending
  Columns:
    symbol     : str        e.g. 'MNQM6'
    price      : float64    trade price
    size       : uint32     trade size in contracts
    side       : str        'A' (aggressor=buyer), 'B' (aggressor=seller), 'N'
    bid_px_00  : float64    best bid at the time
    ask_px_00  : float64    best ask at the time

USAGE
-----
From any future tool:
    from tools.tbbo_cache_builder import load_clean_ticks
    df = load_clean_ticks()  # full window
    df = load_clean_ticks(start='2026-04-01', end='2026-04-15')
    df = load_clean_ticks(symbol_filter='MNQM6')

From the CLI:
    python tools/tbbo_cache_builder.py            # build if missing
    python tools/tbbo_cache_builder.py --rebuild  # force full rebuild
    python tools/tbbo_cache_builder.py --inspect  # show metadata, do not rebuild

CONSTRAINTS
-----------
  * ASCII-only output (Windows cp1252 console).
  * pandas 3.x, pyarrow >=16, databento 0.78+.
  * Tick data uncompressed is ~3-4 GB in memory; we use float64 for
    correctness (price diffs of 0.25 ticks must round-trip cleanly).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------
# Paths (module-level so importers can introspect)
# ----------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
TBBO_DIR = ROOT / "data" / "historical" / "databento_tbbo"
SRC_DBN = TBBO_DIR / "mnq_tbbo_2026-03-17_2026-05-17.dbn.zst"
CLEAN_PARQUET = TBBO_DIR / "mnq_ticks_clean.parquet"
CLEAN_METADATA = TBBO_DIR / "mnq_ticks_clean.metadata.json"

# The two older incompatible caches we are deprecating.
LEGACY_PARQUET_A = TBBO_DIR / "mnq_ticks.parquet"        # phoenix_tick_trail_verification.py
LEGACY_PARQUET_B = TBBO_DIR / "mnq_ticks_slim.parquet"   # phoenix_tick_entry_quality.py

# MNQ outright contract codes: H=Mar, M=Jun, U=Sep, Z=Dec.  Year is 1-2 digits.
OUTRIGHT_RE = re.compile(r"^MNQ[HMUZ]\d+$")

# Sanity bands for MNQ outright prices during 2026-Q1/Q2.
PRICE_LO = 18000.0
PRICE_HI = 35000.0
TICK_SIZE = 0.25
MAX_TICK_JUMP_ALLOWED = 500  # in ticks; ~125 price points


# ----------------------------------------------------------------------
# Build pipeline
# ----------------------------------------------------------------------

def _load_raw_dbn(src: Path) -> pd.DataFrame:
    """Load the DBN file into a pandas DataFrame, with the `symbol` column
    mapped from the embedded symbology."""
    import databento as db  # heavy import; defer

    print(f"[build] reading DBN: {src.name}")
    t0 = time.time()
    store = db.DBNStore.from_file(str(src))
    print(f"[build] schema={store.schema}")
    df = store.to_df()
    print(f"[build] loaded {len(df):,} raw rows in {time.time()-t0:.1f}s "
          f"(mem ~{df.memory_usage(deep=True).sum()/1024**2:.0f} MB)")
    return df


def _apply_hygiene_filters(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Apply spread + outright + dominant-day filters.  Returns (clean_df, drop_stats)."""
    drop_stats: dict = {}
    n0 = len(df)

    if "symbol" not in df.columns:
        raise RuntimeError(
            "DBN load did not produce a 'symbol' column - cannot apply hygiene. "
            "This usually means the embedded symbology mapping is missing."
        )

    # (b) Drop calendar spreads (any symbol containing a hyphen).
    spread_mask = df["symbol"].str.contains("-", na=False)
    n_spreads = int(spread_mask.sum())
    drop_stats["calendar_spreads"] = n_spreads
    df = df[~spread_mask]
    print(f"[build] dropped {n_spreads:,} calendar-spread rows "
          f"({n_spreads/n0*100:.3f}%)")

    # (c) Keep only single outright contracts matching ^MNQ[HMUZ]\d+$.
    outright_mask = df["symbol"].str.match(OUTRIGHT_RE)
    n_non_outright = int((~outright_mask).sum())
    drop_stats["non_outright_after_spread_drop"] = n_non_outright
    df = df[outright_mask]
    if n_non_outright:
        print(f"[build] dropped {n_non_outright:,} non-outright rows "
              f"(symbols not matching {OUTRIGHT_RE.pattern})")

    # Use ts_event for the canonical timeline, not ts_recv (the index).
    # Some downstream consumers want the index reset.
    if df.index.name == "ts_recv":
        df = df.reset_index(drop=False)

    # We need a UTC date column for dominant-day picking.  Use ts_event.
    df = df.copy()
    df["_date"] = df["ts_event"].dt.tz_convert("UTC").dt.date

    # (d) Dominant-by-volume per day.
    vol_by_day_sym = (
        df.groupby(["_date", "symbol"], observed=True)["size"]
          .sum()
          .reset_index()
          .rename(columns={"size": "day_volume"})
    )
    # For each date, choose the symbol with max volume.
    idx = vol_by_day_sym.groupby("_date")["day_volume"].idxmax()
    dominant = vol_by_day_sym.loc[idx, ["_date", "symbol"]].rename(
        columns={"symbol": "dominant_symbol"}
    )
    print("[build] dominant-symbol-per-day picks:")
    for _, row in dominant.iterrows():
        d = row["_date"]
        s = row["dominant_symbol"]
        # report this symbol's count and the runners-up share
        day_rows = vol_by_day_sym[vol_by_day_sym["_date"] == d]
        total = day_rows["day_volume"].sum()
        share = day_rows[day_rows["symbol"] == s]["day_volume"].iloc[0] / max(total, 1)
        print(f"  {d}  dominant={s}  share={share*100:.2f}%")

    df = df.merge(dominant, on="_date", how="left")
    keep_mask = df["symbol"] == df["dominant_symbol"]
    n_non_dom = int((~keep_mask).sum())
    drop_stats["non_dominant_symbol_per_day"] = n_non_dom
    df = df[keep_mask].drop(columns=["dominant_symbol"])
    print(f"[build] dropped {n_non_dom:,} non-dominant-on-that-day rows")

    # Apply broad price-band sanity drop (defense in depth - if any spread
    # somehow slipped through, its price would be way off).
    band_bad = ~df["price"].between(PRICE_LO, PRICE_HI)
    n_band = int(band_bad.sum())
    if n_band:
        drop_stats["price_band_out_of_range"] = n_band
        print(f"[build] dropped {n_band:,} rows outside [{PRICE_LO}, {PRICE_HI}] price band")
        df = df[~band_bad]
    else:
        drop_stats["price_band_out_of_range"] = 0

    # (e) Sort by ts_event.
    df = df.sort_values("ts_event", kind="mergesort").reset_index(drop=True)
    print(f"[build] retained {len(df):,} rows after all filters "
          f"(kept {len(df)/n0*100:.3f}% of raw)")

    df = df.drop(columns=["_date"])
    return df, drop_stats


def _compute_sanity_stats(df: pd.DataFrame) -> dict:
    """Compute max tick jump, price min/max, etc.  Used to gate the build.

    We report TWO max-jump numbers:
      * max_price_jump_ticks_overall : max diff across all consecutive ticks
        within a symbol.  This INCLUDES the Fri-close / Sun-open weekend gap,
        which on MNQ can be $200-$400 (= 800-1600 ticks).  Real market data,
        not a hygiene problem.
      * max_price_jump_ticks_intrasession : max diff between consecutive
        ticks separated by LESS than 30 minutes.  This is the meaningful
        sanity metric - if the data is clean and same-instrument, this
        should be well under 500 ticks ($125 price points).
    """
    stats: dict = {}
    stats["row_count"] = int(len(df))
    stats["date_range_start"] = df["ts_event"].iloc[0].isoformat()
    stats["date_range_end"] = df["ts_event"].iloc[-1].isoformat()
    stats["price_min"] = float(df["price"].min())
    stats["price_max"] = float(df["price"].max())

    # Per-symbol counts (rounded to int so JSON-safe).
    counts = df.groupby("symbol", observed=True).size().to_dict()
    stats["symbols_included"] = {str(k): int(v) for k, v in counts.items()}

    intrasession_threshold_ns = 30 * 60 * 1_000_000_000  # 30 minutes in ns
    overall_max = 0
    overall_loc = None
    intra_max = 0
    intra_loc = None
    for sym, sub in df.groupby("symbol", observed=True, sort=False):
        sub = sub.sort_values("ts_event")
        px = sub["price"].to_numpy()
        # tz-aware datetime -> int64 ns since epoch (UTC).  Force ns
        # resolution first so pandas doesn't warn about us->ns conversion.
        ts = (
            sub["ts_event"]
            .astype("datetime64[ns, UTC]")
            .astype("int64")
            .to_numpy()
        )
        if len(px) < 2:
            continue
        gap_ns = np.diff(ts)
        diff = np.abs(np.diff(px))

        # overall
        i_over = int(np.argmax(diff))
        if diff[i_over] / TICK_SIZE > overall_max:
            overall_max = int(round(diff[i_over] / TICK_SIZE))
            overall_loc = {
                "symbol": str(sym),
                "ts_event": pd.Timestamp(ts[i_over + 1], tz="UTC").isoformat(),
                "price": float(px[i_over + 1]),
                "prev_price": float(px[i_over]),
                "gap_ms": float(gap_ns[i_over] / 1e6),
            }

        # intra-session
        intra_mask = gap_ns < intrasession_threshold_ns
        if intra_mask.any():
            masked = diff * intra_mask  # zero out across-gap diffs
            i_intra = int(np.argmax(masked))
            if masked[i_intra] / TICK_SIZE > intra_max:
                intra_max = int(round(masked[i_intra] / TICK_SIZE))
                intra_loc = {
                    "symbol": str(sym),
                    "ts_event": pd.Timestamp(
                        ts[i_intra + 1], tz="UTC"
                    ).isoformat(),
                    "price": float(px[i_intra + 1]),
                    "prev_price": float(px[i_intra]),
                    "gap_ms": float(gap_ns[i_intra] / 1e6),
                }

    stats["max_price_jump_ticks_overall"] = overall_max
    stats["max_price_jump_location_overall"] = overall_loc
    stats["max_price_jump_ticks_intrasession"] = intra_max
    stats["max_price_jump_location_intrasession"] = intra_loc
    # Legacy key for backward-compat with any tool that imports the dict.
    stats["max_price_jump_ticks"] = intra_max
    return stats


def _write_parquet(df: pd.DataFrame, dst: Path) -> None:
    """Write the canonical parquet with ts_event as the index."""
    out = df[["ts_event", "symbol", "price", "size", "side",
              "bid_px_00", "ask_px_00"]].copy()
    out = out.set_index("ts_event")
    print(f"[build] writing parquet: {dst.name}")
    t0 = time.time()
    out.to_parquet(dst, compression="snappy", index=True)
    print(f"[build] wrote {dst.stat().st_size/1024**2:.0f} MB in {time.time()-t0:.1f}s")


def build_clean_tick_cache(force_rebuild: bool = False) -> Path:
    """Build (or refresh) the canonical clean tick parquet.

    Args:
        force_rebuild: if True, always rebuild even if parquet exists and is
            newer than the source DBN.

    Returns:
        Path to the clean parquet on disk.
    """
    if not SRC_DBN.exists():
        raise FileNotFoundError(f"source DBN missing: {SRC_DBN}")

    # Decide whether a rebuild is needed.
    stale = False
    if not CLEAN_PARQUET.exists():
        stale = True
        reason = "parquet missing"
    elif CLEAN_PARQUET.stat().st_mtime < SRC_DBN.stat().st_mtime:
        stale = True
        reason = "parquet older than source DBN"
    else:
        reason = "fresh"

    if not force_rebuild and not stale:
        print(f"[build] clean parquet up to date ({reason}); skip")
        return CLEAN_PARQUET

    if force_rebuild and not stale:
        print("[build] force_rebuild=True; rebuilding even though parquet is fresh")
    else:
        print(f"[build] rebuilding ({reason})")

    raw = _load_raw_dbn(SRC_DBN)
    clean, drop_stats = _apply_hygiene_filters(raw)
    sanity = _compute_sanity_stats(clean)

    # Hard sanity gate.  We only fail on INTRA-SESSION jumps, not overall:
    # the Fri->Sun weekend gap on MNQ can be >$200 ($800+ ticks) and that
    # is real market behaviour, not a hygiene defect.
    sanity_ok = True
    sanity_failures: list[str] = []
    if sanity["max_price_jump_ticks_intrasession"] >= MAX_TICK_JUMP_ALLOWED:
        sanity_ok = False
        sanity_failures.append(
            f"max_price_jump_ticks_intrasession="
            f"{sanity['max_price_jump_ticks_intrasession']} >= "
            f"{MAX_TICK_JUMP_ALLOWED} (likely contract-mix contamination)"
        )
    if sanity["price_min"] < PRICE_LO:
        sanity_ok = False
        sanity_failures.append(
            f"price_min={sanity['price_min']} < {PRICE_LO} (spread leak?)"
        )
    if sanity["price_max"] > PRICE_HI:
        sanity_ok = False
        sanity_failures.append(
            f"price_max={sanity['price_max']} > {PRICE_HI}"
        )

    _write_parquet(clean, CLEAN_PARQUET)

    meta = {
        "source_file": str(SRC_DBN.relative_to(ROOT)).replace("\\", "/"),
        "source_file_mtime": datetime.fromtimestamp(
            SRC_DBN.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "source_file_size_bytes": int(SRC_DBN.stat().st_size),
        "output_file": str(CLEAN_PARQUET.relative_to(ROOT)).replace("\\", "/"),
        "output_size_bytes": int(CLEAN_PARQUET.stat().st_size),
        "row_count": sanity["row_count"],
        "date_range_start": sanity["date_range_start"],
        "date_range_end": sanity["date_range_end"],
        "price_min": sanity["price_min"],
        "price_max": sanity["price_max"],
        "symbols_included": sanity["symbols_included"],
        "max_price_jump_ticks_overall": sanity["max_price_jump_ticks_overall"],
        "max_price_jump_location_overall": sanity["max_price_jump_location_overall"],
        "max_price_jump_ticks_intrasession": sanity["max_price_jump_ticks_intrasession"],
        "max_price_jump_location_intrasession": sanity["max_price_jump_location_intrasession"],
        # Back-compat: legacy single 'max_price_jump_seen' name expected by spec.
        "max_price_jump_seen": sanity["max_price_jump_ticks_intrasession"],
        "rows_dropped_count": int(sum(drop_stats.values())),
        "rows_dropped_reasons": drop_stats,
        "sanity_ok": sanity_ok,
        "sanity_failures": sanity_failures,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "builder_version": "1.0.0",
    }

    with open(CLEAN_METADATA, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)
    print(f"[build] wrote metadata: {CLEAN_METADATA.name}")

    if not sanity_ok:
        print("[build] WARNING: sanity checks failed:")
        for s in sanity_failures:
            print(f"  - {s}")
    else:
        print("[build] all sanity checks passed")

    return CLEAN_PARQUET


# ----------------------------------------------------------------------
# Loader (the only API future tools should call)
# ----------------------------------------------------------------------

def load_clean_ticks(
    symbol_filter: Optional[str] = None,
    start: Optional[str | pd.Timestamp] = None,
    end: Optional[str | pd.Timestamp] = None,
) -> pd.DataFrame:
    """Load the canonical clean tick parquet.

    Args:
        symbol_filter: optional outright symbol to keep (e.g. 'MNQM6').
            If None, all dominant-per-day symbols pass through (typically a
            single outright per day, varying across rollovers).
        start: optional start timestamp (inclusive).  Anything pd.Timestamp
            accepts works; tz-naive strings are interpreted as UTC.
        end:   optional end   timestamp (inclusive).

    Returns:
        DataFrame indexed by ts_event (datetime64[ns, UTC]) with columns:
        symbol, price, size, side, bid_px_00, ask_px_00.

    Raises:
        FileNotFoundError if the canonical parquet has not been built yet.
        Run `python tools/tbbo_cache_builder.py --rebuild` first.
    """
    if not CLEAN_PARQUET.exists():
        raise FileNotFoundError(
            f"{CLEAN_PARQUET} not found - run "
            f"`python tools/tbbo_cache_builder.py --rebuild` to build it"
        )

    df = pd.read_parquet(CLEAN_PARQUET)
    if symbol_filter is not None:
        df = df[df["symbol"] == symbol_filter]
    if start is not None:
        s = pd.Timestamp(start)
        if s.tzinfo is None:
            s = s.tz_localize("UTC")
        df = df[df.index >= s]
    if end is not None:
        e = pd.Timestamp(end)
        if e.tzinfo is None:
            e = e.tz_localize("UTC")
        df = df[df.index <= e]
    return df


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _inspect() -> int:
    if not CLEAN_METADATA.exists():
        print("[inspect] no metadata file - has the cache been built?")
        return 1
    with open(CLEAN_METADATA, encoding="utf-8") as f:
        meta = json.load(f)
    print(json.dumps(meta, indent=2, default=str))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build/inspect the canonical MNQ TBBO clean tick cache."
    )
    ap.add_argument("--rebuild", action="store_true",
                    help="force rebuild even if the parquet is up to date")
    ap.add_argument("--inspect", action="store_true",
                    help="print the metadata JSON and exit (no build)")
    args = ap.parse_args(argv)

    if args.inspect:
        return _inspect()

    try:
        build_clean_tick_cache(force_rebuild=args.rebuild)
    except FileNotFoundError as e:
        print(f"[build] FATAL: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
