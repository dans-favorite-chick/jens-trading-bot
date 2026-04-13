"""
Phoenix Bot — Dashboard Server

Flask app serving the trading dashboard and REST API endpoints.
Polls bridge health and bot state; serves to browser on :5000.
"""

import json
import logging
import math
import os
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

# ─── Bot Process Manager ───────────────────────────────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_bot_processes: dict[str, subprocess.Popen] = {}
_bot_proc_lock = threading.Lock()


def _start_bot(name: str) -> dict:
    """Start a bot subprocess. name = 'prod' or 'lab'."""
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
            proc = subprocess.Popen(
                [sys.executable, script],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            _bot_processes[name] = proc
            logger.info(f"Started {name} bot (pid {proc.pid})")
            return {"ok": True, "pid": proc.pid}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def _stop_bot(name: str) -> dict:
    """Stop a running bot subprocess."""
    with _bot_proc_lock:
        proc = _bot_processes.get(name)
        if not proc or proc.poll() is not None:
            _bot_processes.pop(name, None)
            return {"ok": True, "message": f"{name} bot not running"}

        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            proc.kill()

        _bot_processes.pop(name, None)
        logger.info(f"Stopped {name} bot")
        return {"ok": True}


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
    "lab": {},
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
        return safe_jsonify({
            "prod": _state["prod"],
            "lab": _state["lab"],
            "bridge": _state["bridge_health"],
            "bot_processes": {
                "prod": _bot_status("prod"),
                "lab": _bot_status("lab"),
            },
            "connection_log": _state["connection_log"][-200:],
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
    with _state_lock:
        prod_trades = _state.get("prod", {}).get("trades", [])
        lab_trades = _state.get("lab", {}).get("trades", [])
    return jsonify({"prod": prod_trades, "lab": lab_trades})


@app.route("/api/strategies")
def api_strategies():
    with _state_lock:
        prod_strats = _state.get("prod", {}).get("strategies", [])
        lab_strats = _state.get("lab", {}).get("strategies", [])
    return jsonify({"prod": prod_strats, "lab": lab_strats})


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
    if name not in ("prod", "lab"):
        return jsonify({"ok": False, "error": "name must be 'prod' or 'lab'"}), 400
    result = _start_bot(name)
    return jsonify(result)


@app.route("/api/bot/stop", methods=["POST"])
def api_stop_bot():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if name not in ("prod", "lab"):
        return jsonify({"ok": False, "error": "name must be 'prod' or 'lab'"}), 400
    result = _stop_bot(name)
    return jsonify(result)


@app.route("/api/bot/status")
def api_bot_proc_status():
    return jsonify({
        "prod": _bot_status("prod"),
        "lab": _bot_status("lab"),
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


# ─── Main ───────────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    logger.info(f"Dashboard starting on http://127.0.0.1:{DASHBOARD_PORT}")
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()
