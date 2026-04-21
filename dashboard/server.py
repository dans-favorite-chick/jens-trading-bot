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


# ─── Main ───────────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    logger.info(f"Dashboard starting on http://127.0.0.1:{DASHBOARD_PORT}")
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()
