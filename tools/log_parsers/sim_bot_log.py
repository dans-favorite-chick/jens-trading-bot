"""
sim_bot_log.py — turn the line-oriented sim_bot_stdout.log into a list
of structured events. Parser is liberal: malformed lines are skipped,
not crashed-on.

Event shape:
    {
      "ts": datetime,              # parsed timestamp (or None)
      "level": "INFO" / "DEBUG" / ...,
      "module": str,               # e.g. "strategies.bias_momentum"
      "message": str,              # full message text
      "kind": str,                 # one of: "BLOCKED", "REJECTED", "NO_SIGNAL",
                                   #         "SKIP", "SIGNAL", "TRADE", "PRICE_SANITY",
                                   #         "FILTER", "STOP_SANITY_FAIL", "EVAL", "OTHER"
      "strategy": str | None,      # extracted strategy name when present
      "gate": str | None,          # extracted "gate:..." when present
      "raw_line": str,
    }

Used by every grader in tools/graders/*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import datetime, time
from pathlib import Path
from typing import Iterator


_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,\.]\d{3})\s+\[(?P<module>[^\]]+)\]\s+(?P<level>[A-Z]+)\s+(?P<message>.*)$")
_STRATEGY_RE = re.compile(r"\b(bias_momentum|spring_setup|vwap_pullback|dom_pullback|ib_breakout|orb|noise_area|compression_breakout|opening_session|vwap_band_pullback)\b")
_GATE_RE = re.compile(r"BLOCKED gate:(\w+)")


@dataclass
class LogEvent:
    ts: datetime | None
    level: str
    module: str
    message: str
    kind: str
    strategy: str | None
    gate: str | None
    raw_line: str

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.ts:
            d["ts"] = self.ts.isoformat()
        return d


def _classify(message: str) -> str:
    """Bucket the message into a coarse kind for grader filtering."""
    if "PRICE_SANITY" in message:
        return "PRICE_SANITY"
    if "STOP_SANITY_FAIL" in message:
        return "STOP_SANITY_FAIL"
    if message.startswith("[FILTER]") or " [FILTER] " in message:
        return "FILTER"
    if "BLOCKED gate:" in message:
        return "BLOCKED"
    if "REJECTED:" in message or " REJECTED:" in message:
        return "REJECTED"
    if "NO_SIGNAL " in message:
        return "NO_SIGNAL"
    if "SKIP warmup_incomplete" in message or "warmup_incomplete" in message:
        return "SKIP"
    if "SIGNAL " in message and "NO_SIGNAL" not in message:
        return "SIGNAL"
    if "[TRADE]" in message or "FILLED:" in message:
        return "TRADE"
    if message.startswith("[EVAL]") or " [EVAL] " in message:
        return "EVAL"
    return "OTHER"


def _parse_ts(raw: str) -> datetime | None:
    raw = raw.replace(",", ".")
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def parse_sim_bot_log(path: str | Path,
                     since: datetime | None = None,
                     until: datetime | None = None) -> Iterator[LogEvent]:
    """Yield LogEvent for every parseable line in `path`.

    Optionally filter by ts >= since and ts < until. Lines that don't
    match the timestamp regex are skipped silently — the grader doesn't
    need them.
    """
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = _TS_RE.match(line.rstrip("\n"))
            if not m:
                continue
            ts = _parse_ts(m.group("ts"))
            if since and ts and ts < since:
                continue
            if until and ts and ts >= until:
                continue
            msg = m.group("message")
            sm = _STRATEGY_RE.search(msg)
            gm = _GATE_RE.search(msg)
            yield LogEvent(
                ts=ts,
                level=m.group("level"),
                module=m.group("module"),
                message=msg,
                kind=_classify(msg),
                strategy=sm.group(1) if sm else None,
                gate=gm.group(1) if gm else None,
                raw_line=line.rstrip("\n"),
            )


def filter_events(events: list[LogEvent], strategy: str | None = None,
                  kind: str | None = None, gate: str | None = None,
                  ct_window: tuple[time, time] | None = None) -> list[LogEvent]:
    """Convenience filter so each grader can express its query in one line."""
    out = events
    if strategy:
        out = [e for e in out if e.strategy == strategy]
    if kind:
        out = [e for e in out if e.kind == kind]
    if gate:
        out = [e for e in out if e.gate == gate]
    if ct_window and ct_window[0] is not None:
        lo, hi = ct_window
        out = [e for e in out if e.ts and lo <= e.ts.time() <= hi]
    return out
