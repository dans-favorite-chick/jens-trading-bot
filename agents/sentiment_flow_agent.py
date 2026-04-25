"""
Phoenix Bot - SentimentFlowAgent (Section 4, observation OR active mode)

Council voter that scores the latest news summary with FinBERT and emits a
vote. Mode is controlled by env vars:

    SENTIMENT_FLOW_ACTIVE   - "true"/"1"/"yes" enables a real vote with
                              weight = SENTIMENT_FLOW_WEIGHT. Anything else
                              (incl. unset) keeps the legacy observation
                              behavior: vote weight is hard-zero.
    SENTIMENT_FLOW_WEIGHT   - float, default 0.10. The vote magnitude when
                              active.

In BOTH modes, every scoring result is appended to
``logs/sentiment_observations.jsonl`` (or RAG, if provided) so we never lose
data while the strategy is being validated.

Public surface:
    DEFAULT_WEIGHT = 0.0
    NAME = "Sentiment Flow"
    SentimentFlowAgent(...).vote(market, intel) -> (Vote, info_dict)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.council_gate import Vote

logger = logging.getLogger("SentimentFlowAgent")

# Lazy import - core.news.finnhub_ws is local code, but we keep it
# soft so this module can be imported on machines without aiohttp/ws.
try:
    from core.news.finnhub_ws import FinnhubNewsItem as _NewsEvent  # type: ignore
    _HAS_NEWS_DC = True
except Exception:  # pragma: no cover
    _NewsEvent = None  # type: ignore
    _HAS_NEWS_DC = False

# Lazy import - core.sentiment_finbert is fine to import (degraded mode safe).
try:
    from core.sentiment_finbert import FinBERTSentiment
    _HAS_FINBERT = True
except Exception:  # pragma: no cover
    FinBERTSentiment = None  # type: ignore
    _HAS_FINBERT = False

# Optional persistence target.
try:
    from core.trade_rag import TradeRAG  # type: ignore
    _HAS_RAG = True
except Exception:  # pragma: no cover
    TradeRAG = None  # type: ignore
    _HAS_RAG = False


DEFAULT_WEIGHT: float = 0.0
NAME: str = "Sentiment Flow"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_FALLBACK_LOG = _PROJECT_ROOT / "logs" / "sentiment_observations.jsonl"

_TRUTHY = {"true", "1", "yes", "y", "on"}


def _env_active(env: Optional[dict] = None) -> bool:
    """Parse SENTIMENT_FLOW_ACTIVE truthy-flag (case-insensitive)."""
    src = env if env is not None else os.environ
    raw = str(src.get("SENTIMENT_FLOW_ACTIVE", "")).strip().lower()
    return raw in _TRUTHY


def _env_weight(env: Optional[dict] = None, default: float = 0.10) -> float:
    """Parse SENTIMENT_FLOW_WEIGHT as float; fall back to ``default``."""
    src = env if env is not None else os.environ
    raw = str(src.get("SENTIMENT_FLOW_WEIGHT", "")).strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


class SentimentFlowAgent:
    """Observation-mode FinBERT voter for the Council.

    Always returns a weight=0 vote (bias=NEUTRAL, confidence=0). The
    interesting payload is the ``info_dict`` returned alongside ``Vote``
    which carries the FinBERT score for the latest ``intel.news.summary``
    text. Persisted to ChromaDB (best-effort) or to a JSONL fallback.

    Parameters
    ----------
    sentiment : FinBERTSentiment, optional
        Pre-built sentiment engine. If None, one is constructed lazily
        with default model paths.
    rag : object, optional
        Persistence target. Must have ``add_observation(metadata: dict)``
        OR ``add_near_miss(metadata: dict)``. If neither is available we
        fall back to JSONL.
    fallback_log_path : str | Path, optional
        Where to write the JSONL fallback. Defaults to
        ``logs/sentiment_observations.jsonl`` under the project root.
    weight : float, optional
        Stored on the instance for telemetry. The vote magnitude is ALWAYS
        zero regardless. A WARN is logged if a non-zero weight is supplied
        - this is a safety rail because the strategy isn't validated yet.
    """

    NAME = NAME
    DEFAULT_WEIGHT = DEFAULT_WEIGHT

    def __init__(
        self,
        sentiment: Optional["FinBERTSentiment"] = None,
        rag: Optional[object] = None,
        fallback_log_path: Optional[Path] = None,
        weight: float = DEFAULT_WEIGHT,
        active: Optional[bool] = None,
    ) -> None:
        self.sentiment = sentiment
        self.rag = rag
        self.fallback_log_path = Path(fallback_log_path) if fallback_log_path else _DEFAULT_FALLBACK_LOG

        # Section 3.5: live news plumbing. The orchestrator owns the
        # FinnhubWebSocketClient and calls wire_news_source() on us.
        self._latest_news = None  # FinnhubNewsItem | None
        self._latest_news_ts: Optional[float] = None
        self._news_source = None  # client reference, opaque
        self._news_count: int = 0

        # Active flag: explicit ctor arg wins over env. Env default is false.
        self.active: bool = bool(active) if active is not None else _env_active()

        # Resolve effective weight. Priority:
        #   1) caller passed a non-default weight to __init__ -> use it
        #   2) else read SENTIMENT_FLOW_WEIGHT env (default 0.10)
        if float(weight) != float(DEFAULT_WEIGHT):
            self.weight = float(weight)
        else:
            self.weight = _env_weight()

        # Legacy guard: warn if a non-zero weight was passed AND we are
        # still in observation (inactive) mode. The vote magnitude in that
        # case remains zero.
        if not self.active and self.weight != 0.0:
            logger.warning(
                "[SentimentFlowAgent] non-zero weight=%.3f supplied but "
                "vote magnitude is hard-coded to 0 in observation mode.",
                self.weight,
            )

        logger.info(
            "[SentimentFlowAgent] init active=%s weight=%.3f log=%s",
            self.active, self.weight, str(self.fallback_log_path),
        )

    # ------------------------------------------------------------------
    # Live news source plumbing (Section 3.5)
    # ------------------------------------------------------------------
    def wire_news_source(self, client: object) -> None:
        """Wire a FinnhubWebSocketClient (or anything with ``on_news``).

        The orchestrator owns the client lifecycle (start/stop). All we
        do here is register our ``_handle_news`` as the callback so each
        delivered ``NewsEvent`` updates our internal freshness pointer.
        Idempotent: re-wiring overwrites the previous registration.
        """
        self._news_source = client
        register = getattr(client, "on_news", None)
        if callable(register):
            try:
                register(self._handle_news)
                logger.info("[SentimentFlowAgent] wired news source via on_news()")
                return
            except Exception as e:
                logger.warning(
                    "[SentimentFlowAgent] on_news() registration failed: %s", e,
                )
        # Fall through: client may expose a settable attribute instead.
        try:
            setattr(client, "_callback", self._handle_news)
            logger.info("[SentimentFlowAgent] wired news source via _callback attr")
        except Exception as e:  # pragma: no cover
            logger.warning(
                "[SentimentFlowAgent] failed to wire news source: %s", e,
            )

    def _handle_news(self, event: object) -> None:
        """Callback invoked once per delivered NewsEvent.

        Stores the event and updates the freshness timestamp. Does NOT
        score yet - scoring happens lazily inside ``vote()`` so we use
        the most recent headline available at decision time. We DO
        record an arrival breadcrumb to the observation log so we can
        later see headline density vs. trade outcomes even in obs mode.
        """
        self._latest_news = event
        self._latest_news_ts = time.time()
        self._news_count += 1

        # Best-effort breadcrumb. Compact: id + headline + ts only.
        try:
            breadcrumb: dict = {
                "kind": "news_arrival",
                "name": self.NAME,
                "active": self.active,
                "news_id": getattr(event, "id", "") or "",
                "headline": (getattr(event, "headline", "") or "")[:240],
                "source": getattr(event, "source", "") or "",
                "datetime_iso": getattr(event, "datetime_iso", "") or "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "n_seen": self._news_count,
            }
            self.fallback_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.fallback_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(breadcrumb, default=str) + "\n")
        except Exception as e:  # pragma: no cover
            logger.warning("[SentimentFlowAgent] news breadcrumb write failed: %s", e)

    # ------------------------------------------------------------------
    # Sentiment helper
    # ------------------------------------------------------------------
    def _ensure_sentiment(self) -> Optional["FinBERTSentiment"]:
        if self.sentiment is not None:
            return self.sentiment
        if not _HAS_FINBERT:
            return None
        try:
            self.sentiment = FinBERTSentiment()  # type: ignore[misc]
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[SentimentFlowAgent] FinBERT init failed: %s", e)
            self.sentiment = None
        return self.sentiment

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def vote(self, market: dict, intel: dict) -> tuple[Vote, dict]:
        """Return the (Vote, info) pair.

        Behavior depends on ``self.active``:

        - INACTIVE (default): ``Vote`` is weight=0 (bias="NEUTRAL",
          confidence=0). FinBERT score is still computed and persisted.
        - ACTIVE: ``Vote`` carries the configured weight via ``info`` and
          maps the FinBERT distribution to (BULLISH/BEARISH/NEUTRAL,
          confidence in 0-100).

        ``info`` always contains the raw FinBERT score plus context so a
        downstream consumer can persist or post-process. Persistence (RAG
        or JSONL fallback) happens in BOTH modes.
        """
        start = time.perf_counter()

        text = self._extract_news_text(intel)
        score = self._score_text(text)
        label, p_neg, p_neu, p_pos = score

        # Map FinBERT distribution to (bias, confidence). Threshold 0.6 on
        # the dominant probability gives a directional vote; otherwise
        # NEUTRAL with confidence = p_neu.
        if p_pos > 0.6 and p_pos > p_neg:
            bias_str = "BULLISH"
            confidence_pct = round(float(p_pos) * 100.0)
        elif p_neg > 0.6 and p_neg > p_pos:
            bias_str = "BEARISH"
            confidence_pct = round(float(p_neg) * 100.0)
        else:
            bias_str = "NEUTRAL"
            confidence_pct = round(float(p_neu) * 100.0)

        if self.active:
            effective_weight = float(self.weight)
            vote_bias = bias_str
            vote_conf = float(confidence_pct)
            reasoning = (
                f"FinBERT label={label} p_pos={p_pos:.2f} p_neg={p_neg:.2f} "
                f"weight={effective_weight:.3f}"
            )
        else:
            effective_weight = 0.0
            vote_bias = "NEUTRAL"
            vote_conf = 0.0
            reasoning = "weight=0 observation"

        info: dict = {
            "name": self.NAME,
            "active": self.active,
            "weight": effective_weight,
            "configured_weight": self.weight,
            "text": text,
            "label": label,
            "p_neg": p_neg,
            "p_neu": p_neu,
            "p_pos": p_pos,
            "net_score": float(p_pos) - float(p_neg),
            "n_headlines": 1 if text else 0,
            "price": market.get("price") if isinstance(market, dict) else None,
            "bias": vote_bias,
            "confidence": vote_conf,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Section 3.5: thread live-news metadata so even observation-mode
        # logs capture which headline drove the score.
        latest = self._latest_news
        if latest is not None:
            info["news_event"] = {
                "id": getattr(latest, "id", "") or "",
                "headline": (getattr(latest, "headline", "") or "")[:240],
                "source": getattr(latest, "source", "") or "",
                "category": getattr(latest, "category", "") or "",
                "datetime_iso": getattr(latest, "datetime_iso", "") or "",
                "symbols_related": list(getattr(latest, "symbols_related", []) or []),
            }
            info["news_age_s"] = (
                round(time.time() - self._latest_news_ts, 3)
                if self._latest_news_ts is not None else None
            )

        info["persisted"] = self._persist(info)
        info["latency_ms"] = round((time.perf_counter() - start) * 1000.0, 3)

        vote = Vote(
            voter=self.NAME,
            bias=vote_bias,
            confidence=vote_conf,
            reasoning=reasoning,
            latency_ms=info["latency_ms"],
        )
        return vote, info

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _extract_news_text(self, intel: dict) -> str:
        """Pull the latest news summary from intel safely.

        Priority order:
          1) ``intel['news']['summary'|'headline'|'text']`` (legacy path)
          2) ``intel['summary'|'headline']`` (top-level fallback)
          3) ``self._latest_news.summary`` then ``.headline`` (live feed)
        """
        if isinstance(intel, dict):
            news = intel.get("news")
            if isinstance(news, dict):
                for key in ("summary", "headline", "text"):
                    v = news.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            for key in ("summary", "headline"):
                v = intel.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        # Live feed fallback (Section 3.5).
        latest = self._latest_news
        if latest is not None:
            for key in ("summary", "headline"):
                v = getattr(latest, key, "")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    def _score_text(self, text: str) -> tuple[str, float, float, float]:
        if not text:
            return ("neutral", 0.5, 0.5, 0.0)
        engine = self._ensure_sentiment()
        if engine is None:
            return ("neutral", 0.5, 0.5, 0.0)
        try:
            return engine.score(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[SentimentFlowAgent] score() failed: %s", e)
            return ("neutral", 0.5, 0.5, 0.0)

    def _persist(self, info: dict) -> bool:
        """Best-effort persistence. Never raises."""
        # Try the RAG target first if it exposes a sensible method.
        rag = self.rag
        if rag is not None:
            for method_name in ("add_observation", "add_near_miss", "add"):
                fn = getattr(rag, method_name, None)
                if callable(fn):
                    try:
                        fn(info)
                        return True
                    except Exception as e:
                        logger.warning(
                            "[SentimentFlowAgent] rag.%s failed: %s",
                            method_name, e,
                        )
                        break
        # JSONL fallback.
        try:
            self.fallback_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.fallback_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(info, default=str) + "\n")
            return True
        except Exception as e:  # pragma: no cover
            logger.warning("[SentimentFlowAgent] jsonl write failed: %s", e)
            return False


__all__ = ["SentimentFlowAgent", "DEFAULT_WEIGHT", "NAME"]
