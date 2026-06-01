"""Phoenix Strategy Oracle -- CLI entry point.

Task 6 of the Phoenix Strategy Oracle build. A thin command-line wrapper
around ``agents.strategy_oracle.run()``. Holds no business logic; argparse
in, JSON out, shell exit code out.

Usage:
    python -m tools.run_oracle research [--no-save-baseline]
    python -m tools.run_oracle weekly
    python -m tools.run_oracle daily

Exit codes:
    0  on status == "complete"
    1  on any halt / error status
    2  on argparse failure (bogus mode, missing positional, etc.)

Allowed imports
---------------
- Standard library (argparse, sys, json, logging)
- agents.strategy_oracle

Forbidden imports (CI invariant)
--------------------------------
- bots/, core/, bridge/, data_feeds/
- Any trade-path module.

The CLI must emit ASCII-only output on stdout so it remains safe under
the Windows cp1252 console codepage. ``json.dumps`` does this by default
(``ensure_ascii=True``); this module does not override that.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

try:
    from pathlib import Path as _Path

    from dotenv import load_dotenv

    _PROJECT_ROOT = _Path(__file__).resolve().parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass

from agents import strategy_oracle

__no_trade_path_imports__ = True

logger = logging.getLogger(__name__)


# Modes that the orchestrator accepts. Mirror MODE_CONFIG keys so argparse
# rejects anything the orchestrator can't dispatch.
_MODES = ("research", "weekly", "daily")


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Split out for testability."""
    parser = argparse.ArgumentParser(
        prog="run_oracle",
        description=(
            "Phoenix Strategy Oracle CLI. Dispatches research, weekly, "
            "or daily oracle runs and prints the result dict as JSON."
        ),
    )
    parser.add_argument(
        "mode",
        choices=_MODES,
        help=(
            "Oracle run mode: 'research' (5y deep dive), 'weekly' "
            "(trailing 7 days vs baseline), or 'daily' (single-day "
            "anomaly surface; no proposals)."
        ),
    )
    # BooleanOptionalAction gives us --save-baseline / --no-save-baseline.
    # Default True per spec. Only meaningful for `research` mode -- the
    # orchestrator ignores it for weekly/daily, so no special-casing here.
    parser.add_argument(
        "--save-baseline",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Save the run's facts.json as the research baseline. "
            "Default ON. Only used by 'research' mode."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Python logging level for the CLI (default: INFO).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns shell exit code:
        0  if the orchestrator returned status == "complete"
        1  on any halt or error status

    Argparse failures (bogus mode, missing positional) raise SystemExit
    via argparse itself; the test suite catches those with pytest.raises.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("dispatching oracle run: mode=%s save_baseline=%s",
                args.mode, args.save_baseline)

    result = strategy_oracle.run(
        mode=args.mode,
        save_baseline=args.save_baseline,
    )

    # Emit the orchestrator result as a single line of ASCII-safe JSON.
    # json.dumps(ensure_ascii=True) is the default; default=str catches
    # any stray non-serializable values (e.g. Path objects) so the CLI
    # never crashes mid-print.
    line = json.dumps(result, default=str, ensure_ascii=True)
    print(line)

    status = result.get("status") if isinstance(result, dict) else None
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
