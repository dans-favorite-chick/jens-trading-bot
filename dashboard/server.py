"""
Phoenix Bot — Dashboard Server

Flask app serving the trading dashboard and REST API endpoints.
Polls bridge health and bot state; serves to browser on :5000.
"""

import datetime
import json
import logging
import math
import os
import re
import signal
import subprocess
import time
import threading

from flask import Flask, render_template, jsonify, request

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import DASHBOARD_PORT, HEALTH_HTTP_PORT

app = Flask(__name__)
logger = logging.getLogger("Dashboard")


# ─── NaN-safe JSON ────────────────────────────────────────────────
# Bot state can contain NaN floats (e.g. RSI before enough bars).
# Python's json.dumps outputs "NaN" which is invalid JSON —
# browsers reject it. Replace NaN/Inf with null before serializing.
def _sanitize_nans(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_nans(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_nans(v) for v in obj]
    return obj


def safe_jsonify(data):
    """jsonify that converts NaN/Inf to null for valid JSON."""
    return app.response_class(
        json.dumps(_sanitize_nans(data), default=str),
        mimetype="application/json",
    )


# ─── B82: CME-session-scoped durable trade history ─────────────────
# The in-memory _state["sim"]["trades"] is populated by the bot's
# state-push and is capped + volatile (reset on dashboard restart,
# truncated to recent-N). The browser-facing dashboard must show ALL
# trades from the current CME globex session, durably across dashboard
# and bot restarts. Pull from logs/trade_memory.json directly — that's
# the durable source of truth, already written on every trade close
# and hydrated at bot boot by P0.1 (load_history=True).
#
# Globex session semantics: opens 17:00 CT daily. The "current session"
# for display is the window [most-recent 17:00 CT, now]. That naturally
# spans the 16:00-17:00 daily-flatten dead zone (trades from the
# afternoon remain visible) and flips over when the next session
# opens at 17:00 CT the following day.

def _session_start_ct_epoch(now_ct=None) -> float:
    """Unix epoch seconds for the start of the current CME globex session.

    Returns the timestamp of the most recent 17:00 America/Chicago.
    If now is 16:00 CT Thursday, session_start is 17:00 CT Wednesday.
    If now is 17:01 CT Thursday, session_start is 17:00 CT Thursday.

    `now_ct` is an optional CT-aware datetime for testability. Production
    callers pass nothing; tests freeze "now" to verify boundary edges.
    """
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        ct = ZoneInfo("America/Chicago")
    except Exception:
        # Fallback: UTC-5 (not DST-aware but better than crashing).
        from datetime import timezone
        ct = timezone(timedelta(hours=-5))

    if now_ct is None:
        now_ct = datetime.now(ct)
    if now_ct.hour >= 17:
        session_start = now_ct.replace(
            hour=17, minute=0, second=0, microsecond=0,
        )
    else:
        session_start = (now_ct - timedelta(days=1)).replace(
            hour=17, minute=0, second=0, microsecond=0,
        )
    return session_start.timestamp()


def _load_session_trades_by_bot() -> dict[str, list[dict]]:
    """Read logs/trade_memory.json and bucket current-session trades by
    bot_id. Returns {"prod": [...], "sim": [...], "lab": [...]}.

    Each bucket is sorted newest-first by exit_time. Trades without a
    recognized bot_id land under "unknown" so nothing is silently dropped.

    Graceful failure: missing / corrupt / non-list file → empty buckets.
    """
    tm_path = os.path.join(PROJECT_ROOT, "logs", "trade_memory.json")
    try:
        with open(tm_path, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as e:
        logger.warning(f"[SESSION_TRADES] trade_memory.json read failed: {e!r}")
        return {}
    if not isinstance(rows, list):
        logger.warning(
            f"[SESSION_TRADES] trade_memory.json wrong shape "
            f"(got {type(rows).__name__}) — treating as empty"
        )
        return {}

    session_start = _session_start_ct_epoch()
    buckets: dict[str, list[dict]] = {}
    for t in rows:
        exit_ts = t.get("exit_time")
        if exit_ts is None:
            continue
        try:
            ts = float(exit_ts)
        except (TypeError, ValueError):
            continue
        if ts < session_start:
            continue
        bot_id = t.get("bot_id") or "unknown"
        buckets.setdefault(bot_id, []).append(t)

    # Preserve append-order (oldest-first). The dashboard template
    # (dashboard.html renderTrades) calls `.reverse()` so it expects
    # oldest-first from the server; sorting newest-first here would
    # double-reverse and put oldest at the top of the UI table.
    # Still, file contents may not be perfectly chronological — enforce
    # oldest-first explicitly so the template contract holds.
    for b in buckets.values():
        b.sort(key=lambda t: float(t.get("exit_time") or 0))
    return buckets

# ─── Bot Process Manager ───────────────────────────────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_bot_processes: dict[str, subprocess.Popen] = {}
_bot_proc_lock = threading.Lock()


def _start_bot(name: str) -> dict:
    """Start a bot subprocess. name = 'prod', 'lab', or 'sim'."""
    # Check if already running externally (connected to bridge)
    if _bot_status(name) == "running":
        return {"ok": False, "error": f"{name} bot already running (started externally)"}

    with _bot_proc_lock:
        # Check if already running as subprocess
        proc = _bot_processes.get(name)
        if proc and proc.poll() is None:
            return {"ok": False, "error": f"{name} bot already running (pid {proc.pid})"}

        script = os.path.join(PROJECT_ROOT, "bots", f"{name}_bot.py")
        if not os.path.exists(script):
            return {"ok": False, "error": f"Script not found: {script}"}

        try:
            # Write bot output to a log file instead of a pipe.
            # CRITICAL: stdout=subprocess.PIPE without a reader causes a
            # deadlock when the OS pipe buffer (~64KB) fills up — the bot
            # blocks on the next print/log call, freezing the entire process.
            log_dir = os.path.join(PROJECT_ROOT, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, f"{name}_bot_stdout.log")
            log_file = open(log_path, "a", buffering=1)  # Line-buffered
            proc = subprocess.Popen(
                [sys.executable, "-u", script],  # -u = unbuffered stdout
                cwd=PROJECT_ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            _bot_processes[name] = proc
            logger.info(f"Started {name} bot (pid {proc.pid}), log → {log_path}")
            return {"ok": True, "pid": proc.pid}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def _stop_bot(name: str) -> dict:
    """Stop a running bot — whether started by dashboard or externally.

    First kills a tracked subprocess (if any). Then scans for ANY python
    process whose command line matches `{name}_bot.py` (handles externally
    started bots from launch_all.bat or PowerShell) and kills those too.
    """
    killed_pids: list[int] = []

    # Path 1: dashboard-spawned subprocess
    with _bot_proc_lock:
        proc = _bot_processes.pop(name, None)
        if proc and proc.poll() is None:
            try:
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                try: proc.kill()
                except Exception: pass
            killed_pids.append(proc.pid)

    # Path 2: externally-started bots (PowerShell, launch_all.bat, etc.)
    # Use psutil if available for clean cross-platform; fall back to taskkill.
    target_script = f"{name}_bot.py"
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if p.info["name"] and "python" in p.info["name"].lower():
                    cmd = " ".join(p.info["cmdline"] or [])
                    if target_script in cmd and p.info["pid"] not in killed_pids:
                        p.terminate()
                        try:
                            p.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            p.kill()
                        killed_pids.append(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        # Fallback: taskkill via command line (Windows)
        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["wmic", "process", "where",
                     f"name='python.exe' and commandline like '%{target_script}%'",
                     "get", "processid", "/format:value"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("ProcessId="):
                        try:
                            pid = int(line.split("=", 1)[1])
                            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                           capture_output=True, timeout=5)
                            killed_pids.append(pid)
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"fallback taskkill for {name} failed: {e}")

    if killed_pids:
        logger.info(f"Stopped {name} bot (PIDs: {killed_pids})")
        return {"ok": True, "pids": killed_pids}
    return {"ok": True, "message": f"{name} bot was not running"}


def _bot_status(name: str) -> str:
    """Return 'running' or 'stopped'.
    Checks both dashboard-spawned subprocesses AND externally-started bots
    (detected by recent state pushes or bridge connection).
    NOTE: Do NOT acquire _state_lock here — callers (api_status) may already hold it.
    """
    # Check dashboard-spawned subprocess first
    with _bot_proc_lock:
        proc = _bot_processes.get(name)
        if proc and proc.poll() is None:
            return "running"

    # Check if bot is connected to bridge (most reliable)
    bridge = _state.get("bridge_health", {})
    bots_connected = bridge.get("bots_connected", [])
    if name in bots_connected:
        return "running"

    # Check if bot is pushing state recently (externally started)
    # State must be fresh (< 15s old) — bots push every 2s
    bot_state = _state.get(name, {})
    if bot_state and bot_state.get("status"):
        last_push = bot_state.get("_received_ts", 0)
        if time.time() - last_push < 15:
            return "running"

    return "stopped"

# ─── Shared State ───────────────────────────────────────────────────
# Bots push state here via POST /api/bot-state
# Dashboard reads via GET /api/status

_state = {
    "prod": {},
    "sim": {},
    "bridge_health": {},
    "connection_log": [],
    "last_update": 0,
}
_state_lock = threading.Lock()


# ─── Bridge Health Poller ───────────────────────────────────────────
def _poll_bridge_health():
    """Background thread that polls bridge :8767/health every 2s."""
    import urllib.request
    url = f"http://127.0.0.1:{HEALTH_HTTP_PORT}/health"
    while True:
        try:
            req = urllib.request.urlopen(url, timeout=5)
            data = json.loads(req.read().decode())
            with _state_lock:
                _state["bridge_health"] = data
                # Merge bridge connection events into our log
                bridge_events = data.get("connection_events", [])
                for evt in bridge_events:
                    if evt not in _state["connection_log"][-50:]:
                        _state["connection_log"].append(evt)
                # Trim to 200
                _state["connection_log"] = _state["connection_log"][-200:]
        except Exception as e:
            logger.debug(f"Bridge health poll failed: {e}")
            with _state_lock:
                _state["bridge_health"] = {
                    "nt8_status": "disconnected",
                    "nt8_connected": False,
                    "bots_connected": [],
                    "bots_count": 0,
                    "error": f"Bridge unreachable: {e}",
                }
        time.sleep(2)


# Start bridge health poller
_poller = threading.Thread(target=_poll_bridge_health, daemon=True)
_poller.start()


# ─── Pages ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("dashboard.html")


# ─── API: Read Endpoints ────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    with _state_lock:
        prod_bucket = dict(_state.get("prod") or {})
        sim_bucket = dict(_state.get("sim") or {})
        bridge = _state["bridge_health"]
        conn_log = _state["connection_log"][-200:]

    # B82: overlay session-scoped trades from trade_memory.json. This
    # survives both dashboard restart (in-memory _state resets) and bot
    # restart (bot's push-buffer resets) because the file is updated on
    # every trade close. The browser-visible trade table is durable until
    # the next CME session open at 17:00 CT.
    session_trades = _load_session_trades_by_bot()
    prod_bucket["trades"] = session_trades.get("prod", [])
    sim_bucket["trades"] = session_trades.get("sim", [])

    return safe_jsonify({
        "prod": prod_bucket,
        "sim": sim_bucket,
        "bridge": bridge,
        "bot_processes": {
            "prod": _bot_status("prod"),
            "sim": _bot_status("sim"),
        },
        "connection_log": conn_log,
        "session_start_ts": _session_start_ct_epoch(),
        "ts": time.time(),
    })


@app.route("/api/council")
def api_council():
    """Get latest council vote result from active bot."""
    with _state_lock:
        prod_council = _state.get("prod", {}).get("council")
        lab_council = _state.get("lab", {}).get("council")
    return jsonify({"prod": prod_council, "lab": lab_council})


@app.route("/api/strategy-performance")
def api_strategy_performance():
    """Get per-strategy performance metrics for AI learning."""
    with _state_lock:
        prod_perf = _state.get("prod", {}).get("strategy_performance")
        lab_perf = _state.get("lab", {}).get("strategy_performance")
    return jsonify({"prod": prod_perf, "lab": lab_perf})


@app.route("/api/debug")
def api_debug():
    """Raw diagnostic: shows exactly what's in _state for bridge health."""
    with _state_lock:
        bh = _state.get("bridge_health", {})
    # Also try a direct fetch from bridge health endpoint
    import urllib.request
    direct = {}
    try:
        url = f"http://127.0.0.1:{HEALTH_HTTP_PORT}/health"
        req = urllib.request.urlopen(url, timeout=3)
        direct = json.loads(req.read().decode())
    except Exception as e:
        direct = {"error": str(e)}
    return jsonify({
        "cached_bridge_health": bh,
        "direct_bridge_fetch": direct,
        "cached_bots_connected": bh.get("bots_connected", "MISSING"),
        "direct_bots_connected": direct.get("bots_connected", "MISSING"),
    })


@app.route("/api/debrief")
def api_debrief():
    """Get latest debrief file content."""
    from datetime import date
    debrief_dir = os.path.join(PROJECT_ROOT, "logs")
    today = date.today().isoformat()
    debrief_path = os.path.join(debrief_dir, f"ai_debrief_{today}.txt")
    if os.path.exists(debrief_path):
        with open(debrief_path, "r", encoding="utf-8") as f:
            return jsonify({"date": today, "content": f.read()})
    return jsonify({"date": today, "content": None})


@app.route("/api/system-health")
def api_system_health():
    with _state_lock:
        bridge = _state["bridge_health"]
    return jsonify(bridge)


@app.route("/api/connection-log")
def api_connection_log():
    with _state_lock:
        return jsonify(_state["connection_log"][-200:])


@app.route("/api/trades")
def api_trades():
    """B82: all current-session trades, bucketed by bot_id, sourced from
    the durable trade_memory.json so the response survives dashboard and
    bot restarts. Session = most-recent 17:00 CT → now.

    Response shape is the legacy {prod, lab} plus a new sim bucket.
    lab is preserved for back-compat with any consumers still reading it
    (lab was decommissioned 2026-04-21 but the key stays for old UIs).
    """
    session_trades = _load_session_trades_by_bot()
    return safe_jsonify({
        "prod": session_trades.get("prod", []),
        "sim": session_trades.get("sim", []),
        "lab": session_trades.get("lab", []),
        "session_start_ts": _session_start_ct_epoch(),
    })


@app.route("/api/strategy-risk")
def api_strategy_risk():
    """Per-strategy risk registry snapshot from the sim bot (balance, daily P&L, halt state)."""
    with _state_lock:
        sim_state = _state.get("sim", {}) or {}
        sr = sim_state.get("strategy_risk") if isinstance(sim_state, dict) else None
        sim_running = _bot_status("sim") == "running"
    return safe_jsonify({
        "sim_running": sim_running,
        "strategy_risk": sr or {},
    })


@app.route("/api/today-pnl")
def api_today_pnl():
    """B79 + B82: compute current CME-session P&L from trade_memory.json
    — the durable source of truth that survives bot and dashboard restarts.

    B82 change: "today" now means "current globex session" (most-recent
    17:00 CT → now), not "calendar day CT". This keeps the dashboard P&L
    coherent across the 16:00-17:00 daily-flatten dead zone and matches
    the session boundary used by /api/status and /api/trades.

    Returns per-bot and per-strategy P&L.
    """
    tm_path = os.path.join(PROJECT_ROOT, "logs", "trade_memory.json")
    try:
        with open(tm_path, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as e:
        return safe_jsonify({
            "error": f"trade_memory read: {e}",
            "per_bot": {}, "per_strategy": {}, "trade_count": 0,
        })
    if not isinstance(rows, list):
        return safe_jsonify({
            "error": "trade_memory.json shape != list",
            "per_bot": {}, "per_strategy": {}, "trade_count": 0,
        })

    session_start = _session_start_ct_epoch()
    per_bot: dict = {}
    per_strategy: dict = {}
    session_rows = []
    for t in rows:
        exit_ts = t.get("exit_time") or t.get("ts_exit")
        if exit_ts is None:
            continue
        try:
            ts = float(exit_ts) if isinstance(exit_ts, (int, float)) \
                else _iso_to_epoch(str(exit_ts))
        except Exception:
            continue
        if ts is None or ts < session_start:
            continue
        session_rows.append(t)
        bot = t.get("bot_id") or "unknown"
        strat = t.get("strategy") or "unknown"
        pnl = float(t.get("pnl_dollars") or 0.0)
        won = 1 if pnl > 0 else 0
        lost = 1 if pnl < 0 else 0
        # Per-bot
        b = per_bot.setdefault(bot, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
        b["pnl"] += pnl
        b["trades"] += 1
        b["wins"] += won
        b["losses"] += lost
        # Per-strategy (key on strategy + sub_strategy if present)
        sub = t.get("sub_strategy")
        key = f"{strat}.{sub}" if sub else strat
        s = per_strategy.setdefault(key, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "bot": bot})
        s["pnl"] += pnl
        s["trades"] += 1
        s["wins"] += won
        s["losses"] += lost
    # Round for display
    for b in per_bot.values():
        b["pnl"] = round(b["pnl"], 2)
        b["win_rate"] = round(100 * b["wins"] / b["trades"], 1) if b["trades"] else 0.0
    for s in per_strategy.values():
        s["pnl"] = round(s["pnl"], 2)
        s["win_rate"] = round(100 * s["wins"] / s["trades"], 1) if s["trades"] else 0.0

    return safe_jsonify({
        "session_start_ts": session_start,
        "per_bot": per_bot,
        "per_strategy": per_strategy,
        "trade_count": len(session_rows),
        "ts": time.time(),
    })


def _iso_to_epoch(s: str) -> float | None:
    """Best-effort ISO-8601 → epoch seconds. Returns None on parse failure."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


@app.route("/api/working-orders")
def api_working_orders():
    """B74: unified view of every active position with its stop/target
    across prod_bot AND sim_bot. Frontend can't install NT8 Chart
    Trader, so we show NT8's working orders here.

    Each row: account, strategy, direction, entry, stop, target,
    contracts, hold_time_s, unrealized_pnl, bot.
    """
    rows = []
    with _state_lock:
        for bot in ("prod", "sim"):
            s = _state.get(bot, {}) or {}
            pos = s.get("position") or {}
            active = pos.get("all_positions") or []
            # Legacy single-position fallback if all_positions not present
            if not active and pos.get("status") == "IN_TRADE":
                active = [{
                    "trade_id": "legacy",
                    "strategy": pos.get("strategy"),
                    "direction": pos.get("direction"),
                    "entry_price": pos.get("entry_price"),
                    "stop_price": pos.get("stop_price"),
                    "target_price": pos.get("target_price"),
                    "contracts": pos.get("contracts"),
                    "account": pos.get("account") or "Sim101",
                    "hold_time_s": pos.get("hold_time_s"),
                    "unrealized_pnl": pos.get("unrealized_pnl"),
                }]
            for p in active:
                p = dict(p)  # don't mutate state
                p["bot"] = bot
                rows.append(p)
    return safe_jsonify({
        "working_orders": rows,
        "count": len(rows),
        "ts": time.time(),
    })


@app.route("/api/strategies")
def api_strategies():
    with _state_lock:
        prod_strats = _state.get("prod", {}).get("strategies", [])
        lab_strats = _state.get("lab", {}).get("strategies", [])
    return jsonify({"prod": prod_strats, "lab": lab_strats})


# ─── NEW Sunday endpoints: composite structural bias + all new signal modules ───

@app.route("/api/structural-bias")
def api_structural_bias():
    """Composite bias from all Sunday modules. Dual-write with old tf_bias."""
    with _state_lock:
        prod_state = _state.get("prod", {})
    return jsonify({
        "structural_bias": prod_state.get("structural_bias", {}),
        "old_tf_bias": prod_state.get("tf_bias", {}),
        "note": "structural_bias runs alongside old tf_bias — strategies use old until WFO-validated"
    })


@app.route("/api/footprint")
def api_footprint():
    """Footprint bar + active pattern signals."""
    with _state_lock:
        prod_state = _state.get("prod", {})
    return jsonify({
        "current_bar": prod_state.get("footprint_current", {}),
        "last_completed": prod_state.get("footprint_last_completed", {}),
        "active_signals": prod_state.get("footprint_signals", []),
    })


@app.route("/api/gamma-context")
def api_gamma_context():
    """MenthorQ state + gamma flip detector + pinning + OpEx."""
    with _state_lock:
        prod_state = _state.get("prod", {})
    return jsonify({
        "menthorq": prod_state.get("menthorq", {}),
        "gamma_flip": prod_state.get("gamma_flip_state", {}),
        "pinning": prod_state.get("pinning_state", {}),
        "opex": prod_state.get("opex_status", {}),
        "vix_term_structure": prod_state.get("vix_term_structure", {}),
        "es_confirmation": prod_state.get("es_confirmation", {}),
    })


@app.route("/api/risk-mgmt")
def api_risk_mgmt():
    """Decay monitor + TCA + circuit breakers + sizing."""
    with _state_lock:
        prod_state = _state.get("prod", {})
    return jsonify({
        "decay_monitor": prod_state.get("decay_monitor_summary", {}),
        "tca_weekly": prod_state.get("tca_weekly_report", {}),
        "circuit_breakers": prod_state.get("circuit_breakers_state", {}),
        "simple_sizing_config": prod_state.get("sizing_config", {}),
    })


@app.route("/api/chart-patterns-v1")
def api_chart_patterns_v1():
    """Detected + context-weighted patterns (bull/bear flag, H&S)."""
    with _state_lock:
        prod_state = _state.get("prod", {})
    return jsonify({
        "enriched_patterns": prod_state.get("chart_patterns_v1", []),
        "best_signal": prod_state.get("chart_pattern_best", None),
    })


@app.route("/api/all-signals")
def api_all_signals():
    """Catch-all: every new signal module's state in one shot."""
    with _state_lock:
        p = _state.get("prod", {})
    return jsonify({
        "structural_bias": p.get("structural_bias", {}),
        "old_tf_bias": p.get("tf_bias", {}),
        "swing_state": p.get("swing_state", {}),
        "volume_profile": p.get("volume_profile", {}),
        "climax_state": p.get("climax_state", {}),
        "sweep_state": p.get("sweep_state", {}),
        "footprint_signals": p.get("footprint_signals", []),
        "chart_patterns_v1": p.get("chart_patterns_v1", []),
        "menthorq": p.get("menthorq", {}),
        "gamma_flip": p.get("gamma_flip_state", {}),
        "vix": p.get("vix_term_structure", {}),
        "pinning": p.get("pinning_state", {}),
        "opex": p.get("opex_status", {}),
        "es_confirmation": p.get("es_confirmation", {}),
        "decay_monitor": p.get("decay_monitor_summary", {}),
        "tca_weekly": p.get("tca_weekly_report", {}),
        "circuit_breakers": p.get("circuit_breakers_state", {}),
    })


# ─── API: Write Endpoints ───────────────────────────────────────────
@app.route("/api/bot-state", methods=["POST"])
def api_bot_state():
    """Bots push their full state here."""
    data = request.get_json(silent=True) or {}
    bot_name = data.get("bot_name", "unknown")
    data["_received_ts"] = time.time()
    # Sanitize NaN/Inf on intake so _state never contains invalid floats
    data = _sanitize_nans(data)
    with _state_lock:
        _state[bot_name] = data
        _state["last_update"] = time.time()
    return jsonify({"ok": True})


@app.route("/api/runtime-controls/profile", methods=["POST"])
def api_set_profile():
    """Set aggression profile (Safe/Balanced/Aggressive)."""
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "balanced")
    with _state_lock:
        for _bn in ("prod", "lab"):
            _state.setdefault(f"_commands_{_bn}", []).append({
                "type": "set_profile",
                "profile": profile,
                "ts": time.time(),
            })
    logger.info(f"Profile set: {profile}")
    return jsonify({"ok": True, "profile": profile})


@app.route("/api/runtime-controls/strategy", methods=["POST"])
def api_toggle_strategy():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    enabled = data.get("enabled", True)
    with _state_lock:
        for _bn in ("prod", "lab"):
            _state.setdefault(f"_commands_{_bn}", []).append({
                "type": "toggle_strategy",
                "name": name,
                "enabled": enabled,
                "ts": time.time(),
            })
    return jsonify({"ok": True})


@app.route("/api/runtime-controls/params", methods=["POST"])
def api_update_params():
    data = request.get_json(silent=True) or {}
    with _state_lock:
        for _bn in ("prod", "lab"):
            _state.setdefault(f"_commands_{_bn}", []).append({
                "type": "update_params",
                "params": data,
                "ts": time.time(),
            })
    return jsonify({"ok": True})


@app.route("/api/runtime-controls/save", methods=["POST"])
def api_save_config():
    """Save current runtime params to config/strategies.py (persistent)."""
    # TODO: implement safe file write
    return jsonify({"ok": True, "message": "Save not yet implemented"})


@app.route("/api/test-trade", methods=["POST"])
def api_test_trade():
    data = request.get_json(silent=True) or {}
    with _state_lock:
        for _bn in ("prod", "lab"):
            _state.setdefault(f"_commands_{_bn}", []).append({
                "type": "test_trade",
                "action": data.get("action", "ENTER_LONG"),
                "ts": time.time(),
            })
    return jsonify({"ok": True})


@app.route("/api/commands")
def api_get_commands():
    """Bots poll this to get pending commands. Per-bot queues prevent race conditions."""
    bot_name = request.args.get("bot", "")
    with _state_lock:
        if bot_name:
            # Per-bot queue: each bot gets its own copy
            key = f"_commands_{bot_name}"
            cmds = _state.pop(key, [])
        else:
            # Legacy fallback: drain shared queue
            cmds = _state.pop("_commands", [])
    return jsonify(cmds)


# ─── API: Bot Process Control ──────────────────────────────────────
@app.route("/api/bot/start", methods=["POST"])
def api_start_bot():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if name not in ("prod", "sim"):
        return jsonify({"ok": False, "error": "name must be 'prod' or 'sim' (lab retired 2026-04-21)"}), 400
    result = _start_bot(name)
    return jsonify(result)


@app.route("/api/bot/stop", methods=["POST"])
def api_stop_bot():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if name not in ("prod", "sim"):
        return jsonify({"ok": False, "error": "name must be 'prod' or 'sim' (lab retired 2026-04-21)"}), 400
    result = _stop_bot(name)
    return jsonify(result)


@app.route("/api/bot/status")
def api_bot_proc_status():
    return jsonify({
        "prod": _bot_status("prod"),
        "sim": _bot_status("sim"),
    })


# ─── API: Watchdog Status ─────────────────────────────────────────
@app.route("/api/watchdog")
def api_watchdog():
    """Proxy to watchdog API on :5001 for dashboard display."""
    try:
        import urllib.request
        req = urllib.request.urlopen("http://127.0.0.1:5001/status", timeout=2)
        data = json.loads(req.read().decode())
        return jsonify(data)
    except Exception:
        return jsonify({"error": "Watchdog not running", "bots": {}})


@app.route("/api/watchdog/forensics")
def api_watchdog_forensics():
    """Read disconnect forensics from the shared JSONL log."""
    forensics_path = os.path.join(PROJECT_ROOT, "logs", "disconnect_forensics.jsonl")
    events = []
    if os.path.exists(forensics_path):
        try:
            with open(forensics_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        events.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass
    # Return last 100 events, newest first
    return jsonify(events[-100:][::-1])


@app.route("/api/mq-status")
def api_mq_status():
    """
    Menthor Q data flow diagnostic.
    Shows what's in menthorq_daily.json, whether it's stale (yesterday's date),
    and which fields are still PLACEHOLDER/zero — so you can spot what needs
    to be filled in before session start.
    """
    from datetime import date as _date
    try:
        from core.menthorq_feed import get_snapshot, DATA_FILE
        snap = get_snapshot()
        today = str(_date.today())

        # Read raw JSON to detect placeholder fields
        raw = {}
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            pass

        # Build a list of fields that look unfilled
        warnings = []
        if snap.date != today:
            warnings.append(
                f"DATE STALE: file date={snap.date!r}, today={today!r}. "
                "Update menthorq_daily.json before session."
            )
        if snap.gex_regime in ("UNKNOWN", ""):
            warnings.append("GEX regime not set (UNKNOWN) — pretrade filter cannot use it.")
        if snap.net_gex_bn == 0.0:
            warnings.append("net_gex_bn = 0.0 — looks like a placeholder. Fill from MQ dashboard.")
        if snap.hvl == 0.0:
            warnings.append("HVL = 0.0 — THE most important MQ number. Fill before trading.")
        if not snap.allow_longs and not snap.allow_shorts:
            warnings.append("Both allow_longs and allow_shorts are False — bot will not trade.")

        return jsonify({
            "ok": len(warnings) == 0,
            "warnings": warnings,
            "snapshot": {
                "date":           snap.date,
                "today":          today,
                "date_is_current": snap.date == today,
                "gex_regime":     snap.gex_regime,
                "net_gex_bn":     snap.net_gex_bn,
                "hvl":            snap.hvl,
                "dex":            snap.dex,
                "direction_bias": snap.direction_bias,
                "allow_longs":    snap.allow_longs,
                "allow_shorts":   snap.allow_shorts,
                "stop_multiplier": snap.stop_multiplier,
                "strategy_type":  snap.strategy_type,
                "vanna":          snap.vanna,
                "charm":          snap.charm,
                "cta_positioning": snap.cta_positioning,
                "call_resistance_all": snap.call_resistance_all,
                "put_support_all":     snap.put_support_all,
                "call_resistance_0dte": snap.call_resistance_0dte,
                "put_support_0dte":     snap.put_support_0dte,
                "gex_level_1":    snap.gex_level_1,
                "gex_level_2":    snap.gex_level_2,
                "gex_level_3":    snap.gex_level_3,
                "notes":          snap.notes,
            },
            "data_file": DATA_FILE,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "warnings": [str(e)]}), 500


# ─── API: MenthorQ Quick Entry ────────────────────────────────────
@app.route("/api/mq-update", methods=["POST"])
def api_mq_update():
    """
    Save MenthorQ morning setup fields to menthorq_daily.json.
    Merges into existing file so other fields (prices from NT8) are preserved.
    Invalidates the menthorq_feed cache so the next poll sees fresh values.
    """
    from datetime import date as _date
    data = request.get_json(silent=True) or {}

    # Locate the data file next to project root
    data_file = os.path.join(PROJECT_ROOT, "data", "menthorq_daily.json")

    # Read existing content (preserve NT8-populated price fields)
    existing = {}
    if os.path.exists(data_file):
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    today = str(_date.today())

    # --- Merge supplied fields into the nested structure ---
    existing["date"] = today
    existing["_last_updated"] = today

    # gex section
    gex = existing.setdefault("gex", {})
    if "gex_regime" in data:
        gex["regime"] = str(data["gex_regime"]).upper()
    if "net_gex_bn" in data:
        try:
            gex["net_gex_bn"] = float(data["net_gex_bn"])
        except (TypeError, ValueError):
            pass

    # HVL — stored at top level (prices section populated by NT8, but user can override)
    if "hvl" in data:
        try:
            existing["hvl"] = float(data["hvl"])
        except (TypeError, ValueError):
            pass

    # regime_summary section
    summary = existing.setdefault("regime_summary", {})
    if "direction_bias" in data:
        summary["direction_bias"] = str(data["direction_bias"]).upper()
    if "stop_multiplier" in data:
        try:
            summary["stop_multiplier"] = float(data["stop_multiplier"])
        except (TypeError, ValueError):
            pass
    if "notes" in data:
        summary["notes"] = str(data["notes"])

    # Derive allow_longs / allow_shorts from direction_bias
    bias = summary.get("direction_bias", "NEUTRAL")
    summary["allow_longs"] = bias in ("NEUTRAL", "LONG")
    summary["allow_shorts"] = bias in ("NEUTRAL", "SHORT")

    # Write back
    try:
        os.makedirs(os.path.dirname(data_file), exist_ok=True)
        with open(data_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Write failed: {e}"}), 500

    # Invalidate menthorq_feed in-process cache
    try:
        import core.menthorq_feed as mf
        mf._cached_snap = None
        mf._cached_date = ""
    except Exception:
        pass  # Module may not be loaded yet — safe to ignore

    # Build snapshot summary to return
    snapshot = {
        "date":            existing.get("date"),
        "gex_regime":      existing.get("gex", {}).get("regime"),
        "net_gex_bn":      existing.get("gex", {}).get("net_gex_bn"),
        "hvl":             existing.get("hvl"),
        "direction_bias":  existing.get("regime_summary", {}).get("direction_bias"),
        "stop_multiplier": existing.get("regime_summary", {}).get("stop_multiplier"),
        "allow_longs":     existing.get("regime_summary", {}).get("allow_longs"),
        "allow_shorts":    existing.get("regime_summary", {}).get("allow_shorts"),
        "notes":           existing.get("regime_summary", {}).get("notes"),
    }
    return jsonify({"ok": True, "snapshot": snapshot})


# ─── API: Phoenix Routines (added 2026-04-25 §3.6) ──────────────────
#
# Surfaces the latest morning_ritual / post_session_debrief / weekly_evolution
# artifacts so the dashboard's Routines tab can render verdict + summary +
# link to the full markdown/HTML/PDF reports.

_ROUTINES = ("morning_ritual", "post_session_debrief", "weekly_evolution")
_OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "out")


@app.route("/api/routines/list")
def api_routines_list():
    """Latest report per routine, plus the digest queue contents."""
    import datetime as _dt
    out = {"routines": {}, "digest_queue": []}
    for r in _ROUTINES:
        rdir = os.path.join(_OUT_DIR, r)
        if not os.path.isdir(rdir):
            out["routines"][r] = {"available": False}
            continue
        md_files = sorted(
            (f for f in os.listdir(rdir) if f.endswith(".md")),
            reverse=True,
        )[:7]
        latest = None
        if md_files:
            mp = os.path.join(rdir, md_files[0])
            try:
                with open(mp, encoding="utf-8") as f:
                    head = "".join(f.readlines()[:60])
                # Extract verdict from the markdown header
                m = re.search(r"\*\*Verdict:\*\*\s*(\w+)", head)
                verdict = m.group(1) if m else "?"
                latest = {
                    "filename": md_files[0],
                    "session_date": md_files[0].replace(".md", ""),
                    "verdict": verdict,
                    "preview": head,
                    "modified_iso": _dt.datetime.fromtimestamp(
                        os.path.getmtime(mp)
                    ).isoformat(timespec="seconds"),
                }
            except Exception as e:
                latest = {"error": repr(e)}
        out["routines"][r] = {
            "available": True,
            "latest": latest,
            "history": md_files,
        }
    # Digest queue (peek without draining)
    queue_path = os.path.join(_OUT_DIR, "digest_queue.jsonl")
    if os.path.exists(queue_path):
        try:
            with open(queue_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out["digest_queue"].append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            pass
    return safe_jsonify(out)


@app.route("/api/routines/<routine>/<date>")
def api_routines_one(routine: str, date: str):
    if routine not in _ROUTINES:
        return safe_jsonify({"ok": False, "error": "unknown routine"}), 404
    rdir = os.path.join(_OUT_DIR, routine)
    md_path = os.path.join(rdir, f"{date}.md")
    html_path = os.path.join(rdir, f"{date}.html")
    if not os.path.exists(md_path):
        return safe_jsonify({"ok": False, "error": "not_found"}), 404
    try:
        markdown = open(md_path, encoding="utf-8").read()
        html = open(html_path, encoding="utf-8").read() if os.path.exists(html_path) else None
        return safe_jsonify({"ok": True, "routine": routine, "date": date,
                             "markdown": markdown, "html": html})
    except Exception as e:
        return safe_jsonify({"ok": False, "error": repr(e)}), 500


# ─── API: live PriceSanity + Advisor snapshot (added 2026-04-25) ────
#
# Surfaces the in-process price_sanity guard state + advisor guidance so
# the dashboard's Trading view can show a live "what mode is the guard in"
# pill + the current MarketAdvisor RR-tier recommendation. Both modules
# are imported lazily because the dashboard process is separate from the
# bot process — we read whatever last-published snapshot is available via
# the bot's /api/status push (already in _state) AND we can query our own
# in-process imports (which match the bot's, modulo separate processes).


@app.route("/api/sanity-snapshot")
def api_sanity_snapshot():
    """Live PriceSanity + AdvisorGuidance snapshot for the dashboard.

    Note: dashboard runs in its own process. The price_sanity module is
    a singleton WITHIN a process, so we get the dashboard's view of it,
    not the sim_bot's. The bot's view is the authoritative one — we
    surface the dashboard-process view here as a sanity check / live
    reference. For the BOT's view, the bot pushes its own price_sanity
    snapshot through /api/bot-state which we mirror in _state.
    """
    out = {"ok": True}
    try:
        from core import price_sanity
        out["price_sanity"] = price_sanity.snapshot()
    except Exception as e:
        out["price_sanity_error"] = repr(e)
    try:
        from core import fmp_sanity
        out["fmp_reference"] = fmp_sanity.get_reference_mnq_price()
    except Exception as e:
        out["fmp_error"] = repr(e)
    # Latest market dict from any bot push
    try:
        with _state_lock:
            for bot_name in ("sim", "prod"):
                bot = _state.get(bot_name) or {}
                m = bot.get("market") or {}
                if m and "advisor_guidance" in m:
                    out[f"{bot_name}_advisor"] = m["advisor_guidance"]
                    break
    except Exception as e:
        out["advisor_error"] = repr(e)
    return safe_jsonify(out)


# ─── API: Phase B+ Grades + Logs (added 2026-04-25) ─────────────────
#
# Three feature surfaces:
#   /api/grades/live              — running metrics for today's session
#   /api/grades/list, /<date>     — historical 16:00-CT grade reports
#   /api/learner-status           — proof the AI Historical Learner has
#                                    actually consumed grade + trade data
#   /api/logs/why-no-trade        — live "why didn't we trade" feed,
#                                    grouped by strategy and rejection reason
#   /api/logs/tail, /files        — raw log tail + file inventory


_GRADES_DIR = os.path.join(os.path.dirname(__file__), "..", "out", "grades")
_LEARNER_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "ai_learner")
_LOGS_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
_PRIMARY_LOGS = {
    "sim_bot": "sim_bot_stdout.log",
    "prod_bot": "prod_bot_stdout.log",
    "bridge": "bridge_stdout.log",
    "watchdog": "watchdog.log",
    "watcher": None,   # resolved at runtime — date-dependent name
}


def _resolve_log_path(key: str) -> str | None:
    if key == "watcher":
        # watcher_<YYYY-MM-DD>.log
        from datetime import date as _d
        cand = os.path.join(_LOGS_DIR, f"watcher_{_d.today().isoformat()}.log")
        return cand if os.path.exists(cand) else None
    if key in _PRIMARY_LOGS and _PRIMARY_LOGS[key]:
        cand = os.path.join(_LOGS_DIR, _PRIMARY_LOGS[key])
        return cand if os.path.exists(cand) else None
    return None


@app.route("/api/grades/list")
def api_grades_list():
    """Recent grade reports — newest first. Each entry: {date, score, results}."""
    out = []
    if os.path.isdir(_GRADES_DIR):
        for fname in sorted(os.listdir(_GRADES_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(_GRADES_DIR, fname), encoding="utf-8") as f:
                    data = json.load(f)
                results = data.get("results", [])
                pass_count = sum(1 for r in results if r.get("overall_pass"))
                out.append({
                    "date": data.get("session_date") or fname.replace(".json", ""),
                    "score": f"{pass_count}/{len(results)}",
                    "summary": [
                        {"id": r.get("prediction_id"), "label": r.get("label"),
                         "pass": r.get("overall_pass"), "qual_obs": r.get("qual_observation")}
                        for r in results
                    ],
                })
            except Exception as e:
                logger.debug(f"[grades] skipping {fname}: {e!r}")
    return safe_jsonify({"grades": out[:30]})


@app.route("/api/grades/<date>")
def api_grades_one(date: str):
    """Full grade detail for one date."""
    fname = f"{date}.json"
    p = os.path.join(_GRADES_DIR, fname)
    if not os.path.exists(p):
        return safe_jsonify({"ok": False, "error": "not_found", "date": date}), 404
    try:
        with open(p, encoding="utf-8") as f:
            return safe_jsonify({"ok": True, **json.load(f)})
    except Exception as e:
        return safe_jsonify({"ok": False, "error": repr(e)}), 500


@app.route("/api/grades/live")
def api_grades_live():
    """Running grade for TODAY's session, computed on-the-fly from
    logs/sim_bot_stdout.log. Returns the same shape as a stored grade
    but uses session-so-far data, so the dashboard can show a scrolling
    in-progress view rather than waiting until 16:00 CT."""
    from datetime import date as _d, datetime as _dt
    log_path = os.path.join(_LOGS_DIR, "sim_bot_stdout.log")
    if not os.path.exists(log_path):
        return safe_jsonify({"ok": False, "error": "log_not_found"})
    # Late import — keeps dashboard importable even if graders package
    # ever moves under a feature flag.
    try:
        from tools.log_parsers.sim_bot_log import parse_sim_bot_log
        from tools.graders.orb_or_too_wide import OrbOrTooWideGrader
        from tools.graders.bias_vwap_gate import BiasVwapGateGrader
        from tools.graders.noise_cadence_spam import NoiseCadenceSpamGrader
        from tools.graders.ib_warmup import IbWarmupGrader
        from tools.graders.compression_squeeze import CompressionSqueezeGrader
        from tools.graders.spring_silence import SpringSilenceGrader
    except Exception as e:
        return safe_jsonify({"ok": False, "error": f"graders_import: {e!r}"})
    today = _d.today()
    since = _dt.combine(today, _dt.min.time())
    until = _dt.combine(today, _dt.max.time())
    events = list(parse_sim_bot_log(log_path, since=since, until=until))
    # Load baseline (for P5)
    baseline = {}
    bp = os.path.join(os.path.dirname(__file__), "..", "out", "baselines", "squeeze_baseline.json")
    if os.path.exists(bp):
        try:
            with open(bp, encoding="utf-8") as f:
                baseline = json.load(f)
        except Exception:
            baseline = {}
    graders = [OrbOrTooWideGrader(), BiasVwapGateGrader(), NoiseCadenceSpamGrader(),
               IbWarmupGrader(), CompressionSqueezeGrader(), SpringSilenceGrader()]
    results = []
    for g in graders:
        r = g._safe_grade(events, baseline)
        results.append(r.to_dict())
    return safe_jsonify({
        "ok": True,
        "session_date": today.isoformat(),
        "n_events_today": len(events),
        "results": results,
    })


@app.route("/api/learner-status")
def api_learner_status():
    """Proof the AI Historical Learner has consumed grade + trade data.
    Surfaces:
      - latest weekly_*.md report (date + size + first lines)
      - pending_recommendations.json (count + sample)
      - learner_scheduled.log tail (last cron run)
    """
    out = {
        "ok": True,
        "weekly_reports": [],
        "pending_recommendations": [],
        "last_scheduled_run": None,
        "evidence": {},
    }
    if not os.path.isdir(_LEARNER_DIR):
        out["ok"] = False
        out["error"] = "learner_dir_missing"
        return safe_jsonify(out)
    # Weekly reports
    weekly = sorted([f for f in os.listdir(_LEARNER_DIR) if f.startswith("weekly_") and f.endswith(".md")],
                    reverse=True)[:7]
    for fname in weekly:
        p = os.path.join(_LEARNER_DIR, fname)
        try:
            st = os.stat(p)
            with open(p, encoding="utf-8", errors="ignore") as f:
                first_lines = [next(f, "") for _ in range(8)]
            out["weekly_reports"].append({
                "filename": fname,
                "size_bytes": st.st_size,
                "modified_iso": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "preview": "".join(first_lines).strip()[:600],
            })
        except Exception as e:
            logger.debug(f"[learner] weekly skip {fname}: {e!r}")
    # Pending recommendations
    rec_path = os.path.join(_LEARNER_DIR, "pending_recommendations.json")
    if os.path.exists(rec_path):
        try:
            with open(rec_path, encoding="utf-8") as f:
                recs = json.load(f)
            if isinstance(recs, list):
                out["pending_recommendations"] = recs[:20]
                out["evidence"]["recommendations_count"] = len(recs)
            elif isinstance(recs, dict):
                # Tolerate {"recommendations": [...], "metadata": {...}} shape
                lst = recs.get("recommendations") or recs.get("items") or []
                out["pending_recommendations"] = lst[:20]
                out["evidence"]["recommendations_count"] = len(lst)
                out["evidence"]["last_run_iso"] = recs.get("generated_at")
        except Exception as e:
            out["evidence"]["recommendations_error"] = repr(e)
    # Scheduled run log
    sched = os.path.join(_LOGS_DIR, "learner_scheduled.log")
    if os.path.exists(sched):
        try:
            st = os.stat(sched)
            with open(sched, encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-20:]
            out["last_scheduled_run"] = {
                "modified_iso": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "tail": "".join(tail).strip()[:1500],
            }
        except Exception as e:
            out["last_scheduled_run"] = {"error": repr(e)}
    # Has the learner consumed today's data? Heuristic: latest weekly's
    # date is within the last 48h.
    if out["weekly_reports"]:
        try:
            latest_iso = out["weekly_reports"][0]["modified_iso"]
            age_h = (datetime.datetime.now() - datetime.datetime.fromisoformat(latest_iso)).total_seconds() / 3600
            out["evidence"]["latest_weekly_age_hours"] = round(age_h, 1)
            out["evidence"]["learning_active"] = age_h < 48.0
        except Exception:
            pass
    return safe_jsonify(out)


def _classify_log_kind(message: str) -> str:
    """Mirror tools.log_parsers.sim_bot_log._classify but inline so the
    dashboard doesn't depend on that package at import time."""
    if "PRICE_SANITY" in message: return "PRICE_SANITY"
    if "STOP_SANITY_FAIL" in message: return "STOP_SANITY_FAIL"
    if "[FILTER]" in message: return "FILTER"
    if "BLOCKED gate:" in message: return "BLOCKED"
    if "REJECTED:" in message: return "REJECTED"
    if "NO_SIGNAL" in message: return "NO_SIGNAL"
    if "warmup_incomplete" in message: return "SKIP"
    if "SIGNAL " in message: return "SIGNAL"
    if "[TRADE]" in message: return "TRADE"
    return "OTHER"


@app.route("/api/logs/why-no-trade")
def api_logs_why_no_trade():
    """Live 'why didn't we trade' feed. Parses the last N lines of the
    sim_bot log and groups rejections by strategy + reason. Updates as
    the bot runs.

    Query params:
      ?bot=sim|prod   (default: sim)
      ?lines=2000     (default: tail this many lines)
    """
    bot = request.args.get("bot", "sim")
    n = int(request.args.get("lines", 2000))
    log_key = "sim_bot" if bot == "sim" else "prod_bot"
    path = _resolve_log_path(log_key)
    if not path:
        return safe_jsonify({"ok": False, "error": f"log not found for {bot}"}), 404
    try:
        from tools.log_parsers.sim_bot_log import parse_sim_bot_log
        # Read just the tail — efficient on large logs
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, max(50_000, n * 200))
            f.seek(max(0, size - chunk), os.SEEK_SET)
            data = f.read().decode("utf-8", errors="ignore")
        # Stash to a temp file just so we can reuse the parser
        # — alternative: parse line by line here. Simple split is fine.
        import re as _re
        events = []
        for line in data.splitlines()[-n:]:
            from tools.log_parsers.sim_bot_log import _TS_RE, _STRATEGY_RE, _GATE_RE, _parse_ts
            m = _TS_RE.match(line)
            if not m:
                continue
            msg = m.group("message")
            strat = _STRATEGY_RE.search(msg)
            gate = _GATE_RE.search(msg)
            events.append({
                "ts_iso": (_parse_ts(m.group("ts")) or datetime.datetime.now()).isoformat(timespec="seconds"),
                "module": m.group("module"),
                "level": m.group("level"),
                "message": msg,
                "strategy": strat.group(1) if strat else None,
                "gate": gate.group(1) if gate else None,
                "kind": _classify_log_kind(msg),
            })
    except Exception as e:
        return safe_jsonify({"ok": False, "error": repr(e)}), 500

    # Aggregate by strategy
    by_strategy: dict[str, dict] = {}
    blocking_kinds = {"REJECTED", "BLOCKED", "NO_SIGNAL", "SKIP"}
    for ev in events:
        s = ev.get("strategy") or "_unknown"
        bucket = by_strategy.setdefault(s, {
            "total_evals": 0,
            "n_signal": 0,
            "n_trade": 0,
            "n_rejection": 0,
            "reasons": {},
            "last_rejections": [],
        })
        bucket["total_evals"] += 1
        kind = ev["kind"]
        if kind == "SIGNAL":
            bucket["n_signal"] += 1
        elif kind == "TRADE":
            bucket["n_trade"] += 1
        elif kind in blocking_kinds:
            bucket["n_rejection"] += 1
            # Reason key: gate name if BLOCKED, else first 40 chars of message
            if kind == "BLOCKED":
                reason = f"gate:{ev.get('gate') or 'unknown'}"
            elif kind == "REJECTED":
                # extract "REJECTED: <FOO>" prefix
                m = _re.search(r"REJECTED:\s*([A-Z_]+)", ev["message"])
                reason = m.group(1) if m else "REJECTED"
            elif kind == "SKIP":
                reason = "warmup_incomplete"
            else:  # NO_SIGNAL
                m = _re.search(r"NO_SIGNAL\s+(\w+)", ev["message"])
                reason = m.group(1) if m else "NO_SIGNAL"
            bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + 1
            # Keep last 5 rejection events per strategy
            if len(bucket["last_rejections"]) >= 5:
                bucket["last_rejections"].pop(0)
            bucket["last_rejections"].append({
                "ts": ev["ts_iso"], "kind": kind, "reason": reason,
                "message": ev["message"][:240],
            })
    # Sort reasons within each strategy
    for s, b in by_strategy.items():
        b["top_reasons"] = sorted(
            ({"reason": k, "count": v} for k, v in b["reasons"].items()),
            key=lambda x: -x["count"]
        )[:8]
        del b["reasons"]
    return safe_jsonify({
        "ok": True,
        "bot": bot,
        "n_events_scanned": len(events),
        "by_strategy": by_strategy,
    })


@app.route("/api/logs/files")
def api_logs_files():
    """Inventory of available log files."""
    out = []
    for key, fname in _PRIMARY_LOGS.items():
        if fname is None:
            path = _resolve_log_path(key)
        else:
            path = os.path.join(_LOGS_DIR, fname) if os.path.exists(os.path.join(_LOGS_DIR, fname)) else None
        if not path:
            out.append({"key": key, "available": False})
            continue
        try:
            st = os.stat(path)
            out.append({
                "key": key,
                "available": True,
                "filename": os.path.basename(path),
                "size_mb": round(st.st_size / 1_000_000, 2),
                "modified_iso": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        except Exception as e:
            out.append({"key": key, "available": False, "error": repr(e)})
    return safe_jsonify({"files": out})


@app.route("/api/logs/tail")
def api_logs_tail():
    """Raw last-N lines from a chosen log file. Defaults to sim_bot, 200 lines.

    ?key=<sim_bot|prod_bot|bridge|watchdog|watcher>
    ?lines=200
    """
    key = request.args.get("key", "sim_bot")
    n = int(request.args.get("lines", 200))
    path = _resolve_log_path(key)
    if not path:
        return safe_jsonify({"ok": False, "error": f"log {key!r} not found"}), 404
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, max(20_000, n * 200))
            f.seek(max(0, size - chunk), os.SEEK_SET)
            data = f.read().decode("utf-8", errors="ignore")
        lines = data.splitlines()[-n:]
        return safe_jsonify({
            "ok": True, "key": key, "filename": os.path.basename(path),
            "lines": lines,
        })
    except Exception as e:
        return safe_jsonify({"ok": False, "error": repr(e)}), 500


# ─── Main ───────────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    logger.info(f"Dashboard starting on http://127.0.0.1:{DASHBOARD_PORT}")
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()
