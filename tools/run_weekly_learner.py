"""
Phoenix Bot — Weekly Learner CLI (S8)

Thin wrapper: invokes `agents.historical_learner.run_weekly_learner` once.
Intended for Windows Task Scheduler / cron on Sunday night (or first tick
after midnight Sunday).

Usage:
    python tools/run_weekly_learner.py
    python tools/run_weekly_learner.py --days 21
    python tools/run_weekly_learner.py --date 2026-04-19
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

# Ensure project root on sys.path when invoked as a script.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.historical_learner import run_weekly_learner  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phoenix weekly historical learner")
    p.add_argument("--days", type=int, default=14,
                   help="Rolling window in days (default 14).")
    p.add_argument("--date", type=str, default=None,
                   help="Override 'today' as YYYY-MM-DD (default: actual today).")
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    try:
        from dotenv import load_dotenv  # optional
        load_dotenv()
    except Exception:
        pass

    today = date.fromisoformat(args.date) if args.date else None
    result = await run_weekly_learner(days=args.days, today=today)

    print(f"\nMarkdown:        {result.md_path}")
    print(f"Recommendations: {result.json_path}")
    print(f"Recs produced:   {len(result.recommendations)}")
    print(f"Trades analyzed: {result.aggregates.get('total_trades', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
