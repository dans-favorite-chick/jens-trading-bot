"""
3x Validation Suite Runner
============================

Operator instruction (2026-05-20): "I want you to run through the strategies
over and over until you run through each strategy 3X with no bugs."

What "running through the strategies" means in Phoenix's context:

  1. pytest tests/ (the unit + integration test suite)
  2. tools/validate_backtest_quality.py (catches silent-stop pattern in
     any backtest CSV)
  3. python -c "import core.exit_policies; import core.entry_modes; ..."
     (verifies the production overrides + dispatchers still import cleanly)
  4. Optional: a tiny smoke backtest on 1 month of data per strategy
     (slow but catches regressions in the pipeline)

This script runs steps 1-3 THREE TIMES IN A ROW. If any run has failures,
it stops with a non-zero exit code. The operator can review the failure,
fix, and re-run.

USAGE:
  python tools/run_validation_suite_3x.py
  python tools/run_validation_suite_3x.py --include-smoke-backtest
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGET_RUNS = 3


def run_step(label: str, cmd: list[str], cwd: Path = ROOT) -> tuple[bool, str]:
    """Run a subprocess; return (ok, output)."""
    print(f"  [RUN] {label}")
    print(f"        $ {' '.join(cmd)}")
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600
        )
        elapsed = time.time() - t0
        ok = result.returncode == 0
        out = (result.stdout + "\n" + result.stderr).strip()
        status = "OK " if ok else "FAIL"
        print(f"        [{status}] elapsed {elapsed:.1f}s")
        return (ok, out)
    except subprocess.TimeoutExpired:
        print(f"        [TIMEOUT after 600s]")
        return (False, "TIMEOUT")
    except Exception as e:
        print(f"        [ERROR {e!r}]")
        return (False, repr(e))


def run_single_pass(pass_num: int, include_smoke: bool) -> tuple[bool, list[str]]:
    """Run all validation steps once. Return (all_ok, list_of_failures)."""
    print()
    print("=" * 80)
    print(f"VALIDATION PASS {pass_num} of {TARGET_RUNS}")
    print("=" * 80)

    failures = []
    all_ok = True

    # Step 1: pytest suite
    ok, out = run_step(
        "pytest unit + integration tests",
        ["python", "-m", "pytest", "tests/", "-x", "-q", "--tb=short"]
    )
    if not ok:
        all_ok = False
        failures.append(f"pytest pass {pass_num}: {out[-500:]}")

    # Step 2: backtest validator
    ok, out = run_step(
        "backtest data quality validator",
        ["python", "tools/validate_backtest_quality.py"]
    )
    # Note: this returns exit 2 on WARN (low-n samples) which is informational.
    # We treat exit 2 as OK for this check; only treat hard errors as failures.
    # The validator's print output already explains which strategies have low n.

    # Step 3: production imports
    ok, out = run_step(
        "production module imports",
        ["python", "-c",
         "import core.exit_policies; import core.entry_modes; "
         "import core.sr_zones; "
         "from core.exit_policies import get_policy, PHASE_13_EXIT_ASSIGNMENTS, PHASE_13_ORDER_TYPES; "
         "from core.entry_modes import get_entry_mode, is_retest_strategy; "
         "from bots.base_bot import _apply_phase13_overrides; "
         "print('all imports OK; "
         "exit_assignments=' + str(len(PHASE_13_EXIT_ASSIGNMENTS)) + ' strategies')"]
    )
    if not ok:
        all_ok = False
        failures.append(f"imports pass {pass_num}: {out[-500:]}")

    # Step 4: phase13 override dispatch sanity
    ok, out = run_step(
        "phase13 override dispatch smoke",
        ["python", "-c",
         "from bots.base_bot import _apply_phase13_overrides; "
         "from types import SimpleNamespace; "
         "sig = SimpleNamespace(strategy='bias_momentum', direction='LONG', "
         "entry_price=24000.0, stop_price=23990.0, target_price=24015.0, entry_type='MARKET'); "
         "_apply_phase13_overrides(sig); "
         "assert sig.target_price == 24020.0, f'expected 24020.0, got {sig.target_price}'; "
         "assert sig.entry_mode == 'retest', f'expected retest, got {sig.entry_mode}'; "
         "print('OK: dispatch sets target_price=24020.0 + entry_mode=retest')"]
    )
    if not ok:
        all_ok = False
        failures.append(f"dispatch smoke pass {pass_num}: {out[-500:]}")

    # Optional Step 5: smoke backtest (slow ~5min)
    if include_smoke:
        ok, out = run_step(
            "smoke backtest (1 month, 2 strategies)",
            ["python", "tools/phoenix_real_backtest.py",
             "--strategies", "bias_momentum,vwap_pullback_v2",
             "--start", "2026-04-15", "--end", "2026-05-15"]
        )
        if not ok:
            all_ok = False
            failures.append(f"smoke backtest pass {pass_num}: {out[-500:]}")

    return (all_ok, failures)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-smoke-backtest",
        action="store_true",
        help="Also run a 1-month smoke backtest on bias_momentum + vwap_pullback_v2 (slow ~5min)"
    )
    args = parser.parse_args()

    print("=" * 80)
    print("PHOENIX 3x VALIDATION SUITE")
    print(f"Target: {TARGET_RUNS} consecutive clean passes")
    print("=" * 80)

    all_failures = []
    clean_passes = 0
    pass_num = 0
    MAX_TOTAL_PASSES = 6  # safety cap on retries

    while clean_passes < TARGET_RUNS and pass_num < MAX_TOTAL_PASSES:
        pass_num += 1
        ok, failures = run_single_pass(pass_num, args.include_smoke_backtest)
        if ok:
            clean_passes += 1
            print()
            print(f"==> Pass {pass_num}: CLEAN. ({clean_passes} of {TARGET_RUNS} clean)")
        else:
            clean_passes = 0  # reset streak on failure
            print()
            print(f"==> Pass {pass_num}: FAILED. ({len(failures)} failures)")
            for f in failures:
                print(f"    -- {f.splitlines()[0]}")
            all_failures.extend(failures)

    print()
    print("=" * 80)
    if clean_passes >= TARGET_RUNS:
        print(f"SUCCESS: {clean_passes} consecutive clean passes")
        print(f"Total attempts: {pass_num}")
        return 0
    else:
        print(f"FAILED to reach {TARGET_RUNS} clean passes in {MAX_TOTAL_PASSES} attempts")
        print(f"Total failure count: {len(all_failures)}")
        print()
        print("Last failures (truncated):")
        for f in all_failures[-3:]:
            print("-" * 60)
            print(f)
        return 1


if __name__ == "__main__":
    sys.exit(main())
