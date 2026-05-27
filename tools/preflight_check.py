"""
Phoenix — Preflight Check (2026-05-25)

Pre-bot-restart invariant check. Imports config + key modules and asserts
the invariants established in the 2026-05-25 session — does NOT start the
bot, hit the network, or touch NT8.

Operator runs this before every bot restart:

    python tools/preflight_check.py

Exit codes:
    0 — all checks green
    2 — at least one check failed; an actionable hint is printed per fail

Checks (numbered to match session notes):
  1. Live canary gate — LIVE_STRATEGY_ALLOWLIST shape, LIVE_TRADING False,
     core.live_canary_gate.validate_live_config importable.
  2. Production freeze — config.strategies.FREEZE_ACTIVE is True.
  3. walk_forward_gate field present per strategy (hard_block for the
     live canary, informational for the named sim-only strategies).
  4. dom_pullback re-add — STRATEGIES entry exists, file importable,
     listed in tools/strategy_change_log.py::_KNOWN_STRATEGIES.
  5. B2 fix — strategies.opening_session imports + R1 logic present.
  6. B2-3 NameError fixes — bots/_strategy_dispatch.py and
     bots/_ws_dispatcher.py have the corrected token sequences and
     do NOT have the pre-fix tokens.
  7. AI agents disabled in live canary mode (AGENT_*_ENABLED == False).
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─── Result type ─────────────────────────────────────────────────────
class CheckResult:
    """Single check outcome. ok=True → [OK], else [FAIL] with hint."""
    __slots__ = ("name", "ok", "hint")

    def __init__(self, name: str, ok: bool, hint: str = "") -> None:
        self.name = name
        self.ok = ok
        self.hint = hint

    def render(self) -> str:
        if self.ok:
            return f"[OK] {self.name}"
        return f"[FAIL] {self.name}: {self.hint}"


# ─── Helpers ─────────────────────────────────────────────────────────
def _read_text(path: Path) -> str:
    """Read a file as utf-8; return empty string on miss so the check
    can FAIL with an actionable hint rather than blow up with IOError."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _fresh_import(modname: str):
    """Import a module by name. Returns the cached module if already
    imported (so monkeypatched attributes in tests are preserved);
    otherwise does a fresh import. Raises on import failure — the
    caller wraps this in a try/except to convert the raise into a
    FAIL with the exception text.

    NB: we intentionally do NOT call importlib.reload here. Reload
    would wipe any test-time monkeypatches and also defeat the
    operator's expectation that running this tool inside a long-lived
    Python session (e.g. an IPython REPL) honors the same module
    state the bot would see.
    """
    if modname in sys.modules:
        return sys.modules[modname]
    return importlib.import_module(modname)


# ─── Check 1: live canary gate ───────────────────────────────────────
def check_live_canary_gate() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        settings = _fresh_import("config.settings")
    except Exception as e:
        return [CheckResult(
            "live_canary_gate.settings_importable", False,
            f"config.settings failed to import: {e!r}",
        )]

    # 1a. LIVE_STRATEGY_ALLOWLIST exists, tuple, exactly ("bias_momentum",)
    allowlist = getattr(settings, "LIVE_STRATEGY_ALLOWLIST", None)
    if allowlist is None:
        results.append(CheckResult(
            "live_canary_gate.allowlist_exists", False,
            "config.settings.LIVE_STRATEGY_ALLOWLIST is missing. "
            "Add: LIVE_STRATEGY_ALLOWLIST: tuple[str, ...] = "
            "(\"bias_momentum\",)",
        ))
    elif not isinstance(allowlist, tuple):
        results.append(CheckResult(
            "live_canary_gate.allowlist_is_tuple", False,
            f"LIVE_STRATEGY_ALLOWLIST must be a tuple, got "
            f"{type(allowlist).__name__}: {allowlist!r}",
        ))
    elif allowlist != ("bias_momentum",):
        results.append(CheckResult(
            "live_canary_gate.allowlist_contents", False,
            f"LIVE_STRATEGY_ALLOWLIST should be (\"bias_momentum\",), "
            f"got {allowlist!r}. If the operator promoted a new live "
            "strategy, update this check.",
        ))
    else:
        results.append(CheckResult(
            "live_canary_gate.allowlist_contents", True,
        ))

    # 1b. LIVE_TRADING is False (canary)
    live_trading = getattr(settings, "LIVE_TRADING", None)
    if live_trading is None:
        results.append(CheckResult(
            "live_canary_gate.LIVE_TRADING_defined", False,
            "config.settings.LIVE_TRADING is missing — required for "
            "canary mode gating.",
        ))
    elif live_trading is not False:
        results.append(CheckResult(
            "live_canary_gate.LIVE_TRADING_is_false", False,
            f"LIVE_TRADING must be False during canary, got "
            f"{live_trading!r}. Flip back to False before restart, or "
            "explicitly authorize live trading via the canary playbook.",
        ))
    else:
        results.append(CheckResult(
            "live_canary_gate.LIVE_TRADING_is_false", True,
        ))

    # 1c. core.live_canary_gate.validate_live_config importable
    try:
        gate_mod = _fresh_import("core.live_canary_gate")
    except Exception as e:
        results.append(CheckResult(
            "live_canary_gate.module_importable", False,
            f"core.live_canary_gate failed to import: {e!r}. "
            "This module gates live promotions — bot won't start "
            "safely without it.",
        ))
        return results
    if not callable(getattr(gate_mod, "validate_live_config", None)):
        results.append(CheckResult(
            "live_canary_gate.validate_live_config_present", False,
            "core.live_canary_gate.validate_live_config is missing or "
            "not callable. Re-check the module — it must expose a "
            "callable validate_live_config().",
        ))
    else:
        results.append(CheckResult(
            "live_canary_gate.validate_live_config_present", True,
        ))
    return results


# ─── Check 2: production freeze ──────────────────────────────────────
def check_production_freeze() -> list[CheckResult]:
    try:
        strategies = _fresh_import("config.strategies")
    except Exception as e:
        return [CheckResult(
            "production_freeze.strategies_importable", False,
            f"config.strategies failed to import: {e!r}",
        )]
    if not hasattr(strategies, "FREEZE_ACTIVE"):
        return [CheckResult(
            "production_freeze.FREEZE_ACTIVE_exists", False,
            "config.strategies.FREEZE_ACTIVE is missing. Per "
            "docs/audits/SYNTHESIS_2026-05-24.md P0-5 the constant "
            "must exist and be True.",
        )]
    if strategies.FREEZE_ACTIVE is not True:
        return [CheckResult(
            "production_freeze.FREEZE_ACTIVE_is_true", False,
            f"FREEZE_ACTIVE must be True (got {strategies.FREEZE_ACTIVE!r}). "
            "If the operator lifted the freeze, name the authorizing "
            "reconciliation report in config/strategies.py and update "
            "this check.",
        )]
    return [CheckResult("production_freeze.FREEZE_ACTIVE_is_true", True)]


# ─── Check 3: walk_forward_gate field present ────────────────────────
_HARD_BLOCK_STRATEGIES = ("bias_momentum",)
_INFORMATIONAL_STRATEGIES = (
    "vwap_pullback_v2", "nq_lsr", "orb_fade", "orb_v2",
    "compression_breakout_v2", "compression_breakout_micro",
    "dom_pullback",
)


def check_walk_forward_gates() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        strategies_mod = _fresh_import("config.strategies")
    except Exception as e:
        return [CheckResult(
            "walk_forward_gate.strategies_importable", False,
            f"config.strategies failed to import: {e!r}",
        )]
    STRATEGIES = getattr(strategies_mod, "STRATEGIES", None)
    if not isinstance(STRATEGIES, dict):
        return [CheckResult(
            "walk_forward_gate.STRATEGIES_dict", False,
            "config.strategies.STRATEGIES must be a dict.",
        )]

    for name in _HARD_BLOCK_STRATEGIES:
        cfg = STRATEGIES.get(name)
        if cfg is None:
            results.append(CheckResult(
                f"walk_forward_gate.{name}", False,
                f"STRATEGIES[{name!r}] is missing.",
            ))
            continue
        gate = cfg.get("walk_forward_gate")
        if gate != "hard_block":
            results.append(CheckResult(
                f"walk_forward_gate.{name}", False,
                f"STRATEGIES[{name!r}]['walk_forward_gate'] must be "
                f"'hard_block', got {gate!r}.",
            ))
        else:
            results.append(CheckResult(
                f"walk_forward_gate.{name}", True,
            ))

    for name in _INFORMATIONAL_STRATEGIES:
        cfg = STRATEGIES.get(name)
        if cfg is None:
            results.append(CheckResult(
                f"walk_forward_gate.{name}", False,
                f"STRATEGIES[{name!r}] is missing.",
            ))
            continue
        gate = cfg.get("walk_forward_gate")
        if gate != "informational":
            results.append(CheckResult(
                f"walk_forward_gate.{name}", False,
                f"STRATEGIES[{name!r}]['walk_forward_gate'] must be "
                f"'informational', got {gate!r}.",
            ))
        else:
            results.append(CheckResult(
                f"walk_forward_gate.{name}", True,
            ))
    return results


# ─── Check 4: dom_pullback re-add ────────────────────────────────────
def check_dom_pullback_readd() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        strategies_mod = _fresh_import("config.strategies")
    except Exception as e:
        return [CheckResult(
            "dom_pullback.strategies_importable", False,
            f"config.strategies failed to import: {e!r}",
        )]
    STRATEGIES = getattr(strategies_mod, "STRATEGIES", {})
    cfg = STRATEGIES.get("dom_pullback")
    if cfg is None:
        results.append(CheckResult(
            "dom_pullback.entry_present", False,
            "STRATEGIES['dom_pullback'] is missing. Re-add per "
            "2026-05-25 session — sim-only, enabled=True, validated=False.",
        ))
        # Don't short-circuit — still check the file + change-log so
        # the operator sees the full repair list.
    else:
        results.append(CheckResult("dom_pullback.entry_present", True))
        if cfg.get("enabled") is not True:
            results.append(CheckResult(
                "dom_pullback.enabled_true", False,
                f"STRATEGIES['dom_pullback']['enabled'] must be True, "
                f"got {cfg.get('enabled')!r}.",
            ))
        else:
            results.append(CheckResult("dom_pullback.enabled_true", True))
        if cfg.get("validated") is not False:
            results.append(CheckResult(
                "dom_pullback.validated_false", False,
                f"STRATEGIES['dom_pullback']['validated'] must be False "
                f"(sim-only by policy), got {cfg.get('validated')!r}.",
            ))
        else:
            results.append(CheckResult("dom_pullback.validated_false", True))

    # File exists + importable
    dom_file = ROOT / "strategies" / "dom_pullback.py"
    if not dom_file.is_file():
        results.append(CheckResult(
            "dom_pullback.file_exists", False,
            f"strategies/dom_pullback.py does not exist at {dom_file}. "
            "Restore from git history (deleted 2026-05-21, restored "
            "2026-05-25 per session notes).",
        ))
    else:
        results.append(CheckResult("dom_pullback.file_exists", True))
        try:
            mod = _fresh_import("strategies.dom_pullback")
        except Exception as e:
            results.append(CheckResult(
                "dom_pullback.importable", False,
                f"strategies.dom_pullback failed to import: {e!r}",
            ))
        else:
            # Class name is DOMPullback (see strategies/dom_pullback.py).
            if not hasattr(mod, "DOMPullback"):
                results.append(CheckResult(
                    "dom_pullback.class_exposed", False,
                    "strategies.dom_pullback must expose class "
                    "DOMPullback (BaseStrategy subclass).",
                ))
            else:
                results.append(CheckResult(
                    "dom_pullback.class_exposed", True,
                ))

    # _KNOWN_STRATEGIES membership
    try:
        cl_mod = _fresh_import("tools.strategy_change_log")
    except Exception as e:
        results.append(CheckResult(
            "dom_pullback.in_change_log", False,
            f"tools.strategy_change_log failed to import: {e!r}",
        ))
    else:
        known = getattr(cl_mod, "_KNOWN_STRATEGIES", ())
        if "dom_pullback" not in known:
            results.append(CheckResult(
                "dom_pullback.in_change_log", False,
                "tools/strategy_change_log.py::_KNOWN_STRATEGIES is "
                "missing 'dom_pullback'. Add it back so the change-log "
                "parity test passes.",
            ))
        else:
            results.append(CheckResult(
                "dom_pullback.in_change_log", True,
            ))
    return results


# ─── Check 5: B2 fix (opening_session R1 logic) ──────────────────────
def check_b2_opening_session() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        _fresh_import("strategies.opening_session")
    except Exception as e:
        results.append(CheckResult(
            "b2.opening_session_importable", False,
            f"strategies.opening_session failed to import: {e!r}",
        ))
        return results
    results.append(CheckResult("b2.opening_session_importable", True))

    src = _read_text(ROOT / "strategies" / "opening_session.py")
    if not src:
        results.append(CheckResult(
            "b2.source_readable", False,
            "Could not read strategies/opening_session.py source.",
        ))
        return results

    if '"target_method": target_source' not in src:
        results.append(CheckResult(
            "b2.target_method_source", False,
            'strategies/opening_session.py missing the marker '
            '\'"target_method": target_source\' — the B2 fix '
            "wires target_source into the signal meta. Verify the "
            "2026-05-25 fix landed.",
        ))
    else:
        results.append(CheckResult("b2.target_method_source", True))

    if "2.0 * pivot_pp - pd_low" not in src:
        results.append(CheckResult(
            "b2.R1_formula_present", False,
            "strategies/opening_session.py missing R1 pivot formula "
            "'2.0 * pivot_pp - pd_low'. The B2 fix restored the R1 "
            "target logic — re-check the file.",
        ))
    else:
        results.append(CheckResult("b2.R1_formula_present", True))
    return results


# ─── Check 6: B2-3 NameError fixes (dispatch + ws_dispatcher) ────────
def check_b2_3_nameerror_fixes() -> list[CheckResult]:
    results: list[CheckResult] = []

    sd_src = _read_text(ROOT / "bots" / "_strategy_dispatch.py")
    if not sd_src:
        results.append(CheckResult(
            "b2_3.strategy_dispatch_readable", False,
            "Could not read bots/_strategy_dispatch.py.",
        ))
    else:
        if "cr_assess(market, None," not in sd_src:
            results.append(CheckResult(
                "b2_3.strategy_dispatch.fixed_call", False,
                "bots/_strategy_dispatch.py is missing the fixed call "
                "'cr_assess(market, None,'. The B2-3 NameError fix "
                "replaced the undefined _mq_snap with None.",
            ))
        else:
            results.append(CheckResult(
                "b2_3.strategy_dispatch.fixed_call", True,
            ))
        if "cr_assess(market, _mq_snap," in sd_src:
            results.append(CheckResult(
                "b2_3.strategy_dispatch.old_call_absent", False,
                "bots/_strategy_dispatch.py still contains the pre-fix "
                "call 'cr_assess(market, _mq_snap,' — _mq_snap is "
                "undefined and will raise NameError at dispatch time.",
            ))
        else:
            results.append(CheckResult(
                "b2_3.strategy_dispatch.old_call_absent", True,
            ))

    ws_src = _read_text(ROOT / "bots" / "_ws_dispatcher.py")
    if not ws_src:
        results.append(CheckResult(
            "b2_3.ws_dispatcher_readable", False,
            "Could not read bots/_ws_dispatcher.py.",
        ))
    else:
        if "bot.aggregator.snapshot(), pos.direction," not in ws_src:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.snapshot_call", False,
                "bots/_ws_dispatcher.py missing fixed token "
                "'bot.aggregator.snapshot(), pos.direction,'.",
            ))
        else:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.snapshot_call", True,
            ))
        if "_market = bot.aggregator.snapshot()" not in ws_src:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.market_assign", False,
                "bots/_ws_dispatcher.py missing fixed assignment "
                "'_market = bot.aggregator.snapshot()'.",
            ))
        else:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.market_assign", True,
            ))
        # Negative checks — pre-fix names must not appear.
        # The fixed code uses `bot.aggregator.snapshot(), pos.direction,`
        # and `_market.get(_atr_key`; the broken code used bare
        # `market, pos.direction` and `market.get(_atr_key`. Use a
        # word-boundary regex so `_market` isn't mistaken for `market`.
        import re as _re
        bad_market_token = _re.search(
            r"(?<![A-Za-z0-9_])market, pos\.direction", ws_src,
        )
        if bad_market_token:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.old_market_token_absent", False,
                "bots/_ws_dispatcher.py still references bare "
                "'market, pos.direction' — should be "
                "'bot.aggregator.snapshot(), pos.direction,'.",
            ))
        else:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.old_market_token_absent", True,
            ))
        # Negative check: bare `market.get(_atr_key` (pre-fix), NOT
        # `_market.get(_atr_key` (post-fix). Use a regex-ish anchor by
        # scanning for the token preceded by a non-identifier char.
        import re as _re
        bad_atr = _re.search(
            r"(?<![A-Za-z0-9_])market\.get\(_atr_key", ws_src,
        )
        if bad_atr:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.old_atr_token_absent", False,
                "bots/_ws_dispatcher.py still references bare "
                "'market.get(_atr_key' — should be "
                "'_market.get(_atr_key'.",
            ))
        else:
            results.append(CheckResult(
                "b2_3.ws_dispatcher.old_atr_token_absent", True,
            ))
    return results


# ─── Check 7: AI agents disabled ─────────────────────────────────────
def check_ai_agents_disabled() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        settings = _fresh_import("config.settings")
    except Exception as e:
        return [CheckResult(
            "ai_agents.settings_importable", False,
            f"config.settings failed to import: {e!r}",
        )]
    # Find every AI_*_ENABLED and AGENT_*_ENABLED flag and assert False.
    flags = [
        name for name in dir(settings)
        if (name.startswith("AI_") or name.startswith("AGENT_"))
        and name.endswith("_ENABLED")
    ]
    if not flags:
        # No flags at all — informational PASS. The 2026-05-24 P0-4 kill
        # only required the flags that EXIST to be False; absence is
        # also acceptable (means no AI in the entry path at all).
        return [CheckResult("ai_agents.no_enabled_flags", True)]
    for f in flags:
        val = getattr(settings, f)
        if val is not False:
            results.append(CheckResult(
                f"ai_agents.{f}", False,
                f"{f} must be False in live canary mode, got {val!r}. "
                "Per P0-4 (synthesis F-03) all AI agents are killed "
                "until an A/B harness publishes uplift data with 95% "
                "CI lower bound > 0.",
            ))
        else:
            results.append(CheckResult(f"ai_agents.{f}", True))
    return results


# ─── Driver ──────────────────────────────────────────────────────────
_ALL_CHECKS = (
    ("1. Live canary gate",          check_live_canary_gate),
    ("2. Production freeze",         check_production_freeze),
    ("3. walk_forward_gate fields",  check_walk_forward_gates),
    ("4. dom_pullback re-add",       check_dom_pullback_readd),
    ("5. B2 opening_session fix",    check_b2_opening_session),
    ("6. B2-3 NameError fixes",      check_b2_3_nameerror_fixes),
    ("7. AI agents disabled",        check_ai_agents_disabled),
)


def run_all() -> tuple[list[CheckResult], int]:
    """Run every check. Returns (results, exit_code).

    Exit 0 if every result passed, 2 otherwise.
    """
    all_results: list[CheckResult] = []
    for label, fn in _ALL_CHECKS:
        print(f"\n--- {label} ---")
        try:
            results = fn()
        except Exception as e:
            # A check function itself raising is a FAIL, not a crash.
            results = [CheckResult(
                f"{label}.uncaught_exception", False,
                f"check function raised {type(e).__name__}: {e}",
            )]
        for r in results:
            print(f"  {r.render()}")
        all_results.extend(results)
    fails = [r for r in all_results if not r.ok]
    print("\n" + "=" * 64)
    if not fails:
        print(f"PREFLIGHT OK — {len(all_results)} checks green.")
        return all_results, 0
    print(f"PREFLIGHT FAIL — {len(fails)}/{len(all_results)} checks failed:")
    for r in fails:
        print(f"  {r.render()}")
    print("Address each [FAIL] above before restarting the bot.")
    return all_results, 2


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Phoenix preflight check (no side effects).",
    )
    ap.parse_args()
    _, code = run_all()
    return code


if __name__ == "__main__":
    sys.exit(main())
