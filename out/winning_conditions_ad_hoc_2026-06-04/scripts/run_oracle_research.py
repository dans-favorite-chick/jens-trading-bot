"""
Phase 1 — Invoke the Phoenix Strategy Oracle in research mode.

This is the canonical re-run of the 2026-06-04 Winning Conditions sprint
through the standard agent (vs the prior ad-hoc /c/tmp/winning_conditions/
scripts). Output lands at logs/oracle/research/{today}_*.

Usage:
    python C:/tmp/winning_conditions/run_oracle_research.py
"""
from __future__ import annotations

import sys
import json
import time
from pathlib import Path

# 1. Load env (ANTHROPIC_API_KEY) from repo .env
REPO = Path(r"C:\Trading Project\phoenix_bot")
sys.path.insert(0, str(REPO))

# dotenv first
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env", override=True)
except ImportError:
    print("WARNING: python-dotenv not available; ANTHROPIC_API_KEY must be set externally")

import os
if not os.getenv("ANTHROPIC_API_KEY"):
    print("FATAL: ANTHROPIC_API_KEY still not in environment after dotenv load")
    sys.exit(1)
print(f"ANTHROPIC_API_KEY loaded: len={len(os.environ['ANTHROPIC_API_KEY'])}")

# 2. Invoke Oracle
from agents.strategy_oracle import run, MODE_CONFIG, LOGS_ORACLE_ROOT, WAREHOUSE_PATH, _today_str

print()
print("=" * 72)
print("Phoenix Strategy Oracle -- research mode invocation")
print("=" * 72)
print(f"Date (today_str): {_today_str()}")
print(f"Warehouse: {WAREHOUSE_PATH}")
print(f"Logs root: {LOGS_ORACLE_ROOT}")
print(f"Mode config: {json.dumps(MODE_CONFIG['research'], indent=2)}")
print()
print("Starting run() ... (token budget 600K, this may take a few minutes)")
print()

t0 = time.time()
result = run(mode="research", save_baseline=True)
elapsed = time.time() - t0

print()
print("=" * 72)
print(f"Run complete in {elapsed:.1f}s")
print("=" * 72)
print(json.dumps(
    {k: v for k, v in result.items() if k != "regime"},
    indent=2, default=str,
))
print()
print("Regime gate verdict:")
regime = result.get("regime", {}) or {}
print(json.dumps(regime, indent=2, default=str))

# Save invocation summary to scratch
summary_path = Path(r"C:\tmp\winning_conditions") / "oracle_run_summary.json"
summary = {
    "invocation_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "elapsed_seconds": round(elapsed, 1),
    "result": result,
}
summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print(f"\nInvocation summary written to {summary_path}")
