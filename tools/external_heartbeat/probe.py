"""External dead-man's switch probe for Phoenix (F-22 mitigation).

Pings :8767/health on a fixed interval. After N consecutive failures fires
DOWN via Telegram + Twilio SMS. After M consecutive successes while down,
fires RESOLVED via Telegram only. stdlib only — Python 3.10+.
"""
from __future__ import annotations

import logging, os, signal, sys, time
import urllib.error, urllib.parse, urllib.request
from base64 import b64encode
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("phoenix_probe")
_STOP = False


def _handle_stop(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True
    log.info("signal %s — shutting down", signum)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def probe_health(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Return (ok, detail). ok=True only on HTTP 200."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                return True, f"HTTP 200 ({len(resp.read())} bytes)"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"NetError {type(e).__name__}: {e}"


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


def send_twilio_sms(sid: str, token: str, from_: str, to: str, body: str) -> bool:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    payload = urllib.parse.urlencode({"From": from_, "To": to, "Body": body}).encode()
    auth = b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        log.error("Twilio send failed: %s", e)
        return False


def fire_alert(kind: str, text: str, *, include_sms: bool) -> None:
    log.warning("ALERT[%s] %s", kind, text)
    tg_t, tg_c = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tg_t and tg_c:
        send_telegram(tg_t, tg_c, text)
    if include_sms:
        sid = os.environ.get("TWILIO_ACCOUNT_SID")
        tok = os.environ.get("TWILIO_AUTH_TOKEN")
        frm, to = os.environ.get("TWILIO_FROM"), os.environ.get("TWILIO_TO")
        if sid and tok and frm and to:
            send_twilio_sms(sid, tok, frm, to, text)


def step(state: dict, ok: bool, detail: str, fail_threshold: int, recovery_threshold: int) -> None:
    """Mutate state and fire alerts at threshold transitions."""
    now = time.time()
    if ok:
        state["consecutive_fail"] = 0
        state["consecutive_ok"] += 1
        if state["down"] and state["consecutive_ok"] >= recovery_threshold:
            fire_alert("RESOLVED",
                       f"[PHOENIX RESOLVED] up at {_utc_now_iso()} "
                       f"(was down since {state.get('down_since_iso', '?')}). {detail}",
                       include_sms=False)
            state.update(down=False, down_since=None, down_since_iso=None)
    else:
        state["consecutive_ok"] = 0
        state["consecutive_fail"] += 1
        if not state["down"] and state["consecutive_fail"] >= fail_threshold:
            state.update(down=True, down_since=now, down_since_iso=_utc_now_iso())
        if state["down"]:
            since = state.get("down_since") or now
            fire_alert("DOWN",
                       f"[PHOENIX DOWN] {_utc_now_iso()} — unreachable for "
                       f"{_fmt_duration(now - since)} ({state['consecutive_fail']} misses). "
                       f"Last: {detail}",
                       include_sms=True)


def main() -> int:
    for sig in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, sig), _handle_stop)
        except (AttributeError, ValueError, OSError):
            pass
    url = os.environ.get("PHOENIX_HEALTH_URL", "http://127.0.0.1:8767/health")
    interval = float(os.environ.get("PHOENIX_PROBE_INTERVAL_S", "60"))
    fail_thr = int(os.environ.get("PHOENIX_PROBE_FAIL_THRESHOLD", "3"))
    recov_thr = int(os.environ.get("PHOENIX_PROBE_RECOVERY_THRESHOLD", "2"))
    log.info("probe start: url=%s interval=%ss fail=%d recov=%d",
             url, interval, fail_thr, recov_thr)
    state = {"consecutive_fail": 0, "consecutive_ok": 0,
             "down": False, "down_since": None, "down_since_iso": None}
    while not _STOP:
        ok, detail = probe_health(url)
        log.info("probe ok=%s detail=%s fails=%d oks=%d down=%s",
                 ok, detail, state["consecutive_fail"], state["consecutive_ok"], state["down"])
        step(state, ok, detail, fail_thr, recov_thr)
        for _ in range(int(interval * 10)):
            if _STOP:
                break
            time.sleep(0.1)
    log.info("exited cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
