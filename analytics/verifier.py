"""Phase 3 verifier for the Phoenix Strategy Oracle.

Task 4 of the Phoenix Strategy Oracle build (spec sec 6, 9, 16).

DESIGN PHILOSOPHY
-----------------
Pure-Python defense against LLM hallucination. The verifier consumes the
LLM's narrative + ``findings`` and the deterministic ``facts.json`` panel
produced by ``analytics.compute_engine``. It does FOUR jobs:

1. **Numbers reconciliation.** Every number in the narrative must trace
   back to a leaf-value in ``facts.json`` (within a small relative
   tolerance). Numbers inside ``facts['findings']`` are NOT ground truth
   and are excluded from the search - they are LLM-written content.

2. **Look-ahead defense.** For every ``YYYY-MM`` date in the narrative,
   scan a +/-10-token window for event keywords (crash, FOMC, Fed pivot,
   etc.). A hit suggests the LLM is pattern-matching memorized history
   rather than reasoning from facts.

3. **Causal restraint.** Causal phrases ("because", "due to", "caused by")
   are conditionally violating: "because n < 30" is statistical reasoning
   and OK; "because the Fed pivoted" is a causal claim about exogenous
   events and is flagged. The check considers a +/-10-token window around
   each causal phrase for event keywords / dates / macro words.

4. **Finding classification.** A finding is TRANSCRIPTION if every
   numeric token in its rationale appears in ``facts.json``; otherwise it
   is INTERPRETATION. Per spec sec 9, the orchestrator applies the
   selective look-ahead confidence downgrade only to INTERPRETATION
   findings - pure transcription is unpenalized.

ALLOWED IMPORTS
---------------
- Standard library only (re, math, dataclasses, logging, typing).

FORBIDDEN IMPORTS (CI invariant)
--------------------------------
- bots/, core/, bridge/, data_feeds/  (verifier sits above the trade path)
- anthropic / claude / openai SDKs    (no LLM here; that defeats the point)
- analytics.compute_engine            (verifier consumes its output, does
                                      not import it - keeps the modules
                                      independent so they can be tested
                                      and swapped in isolation)

The forbidden-import invariant is enforced by ``test_verifier.py`` via a
text scan of this file.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

DEFAULT_EVENT_KEYWORDS = [
    "crash", "crashed", "crashing",
    "rally", "rallied", "rallying",
    "pivot", "pivoted",
    "spike", "spiked", "spiking",
    "collapse", "collapsed",
    "meltdown",
    "surge", "surged", "surging",
    "FOMC", "CPI", "NFP",
    "Fed pivot", "rate hike", "rate cut",
]

DEFAULT_CAUSAL_PHRASES = [
    "because", "due to", "caused by", "resulted in", "led to",
    "driven by", "as a result", "in response to", "triggered by",
]

# Statistical-reason whitelist. Causal phrases co-located with these tokens
# describe internal reasoning about sample size / methodology, NOT exogenous
# market causation. Matched as substrings, case-insensitive.
#
# Whitelist hygiene: every entry MUST be specific enough that it cannot
# false-negative a real causal claim. Bare tokens like "tolerance" or
# "n_trades" alone matched legitimate-sounding prose ("Fed tolerance for
# inflation", "n_trades declined in 2022-10") and were tightened to the
# parameter/comparator forms that actually indicate methodology talk.
_STATISTICAL_REASON_TERMS = [
    "n <", "n <=", "n =", "n=",
    "sample size", "sample-size",
    "insufficient sample",
    "degrees of freedom",
    "n_trades <", "n_trades =", "n_trades below", "n_trades exceeded",
    "min_trl",
    "tolerance pct", "tolerance_pct", "tolerance =",
    "below the floor", "above the floor",
    "p-value", "p value", "p < ", "p<0",
    "confidence interval",
    "standard error",
]

# Macro context terms that elevate a causal phrase to a violation even when
# no event-action keyword (crash / rally / etc.) is present. Per spec sec 10,
# "the Oracle is explicitly prohibited from causation; the verifier's
# causal-language detector enforces it."
_MACRO_CONTEXT_TERMS = [
    "fed", "fomc", "cpi", "nfp", "ecb", "boe",
    "the fed", "fed pivot",
]

DEFAULT_TOLERANCE = 0.005           # 0.5% relative tolerance
DEFAULT_WINDOW_TOKENS = 10          # tokens around a date for keyword scan

# YYYY-MM with plausible market history (year starts with 20 or 19).
DATE_PATTERN = r"\b(20\d{2})-(0[1-9]|1[0-2])\b"

# Number tokens we consider: optional sign, digits with optional thousands
# separators, optional decimal part, optional trailing percent sign. The
# scanner also accepts a leading $ which is stripped during parsing.
_NUMBER_PATTERN = re.compile(
    r"""
    (?P<sign>[+-])?                    # optional sign
    (?P<currency>\$)?                  # optional dollar sign
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)    # integer part with optional thousands
    (?:\.(?P<frac>\d+))?               # optional decimal part
    (?P<pct>%)?                        # optional percent
    """,
    re.VERBOSE,
)

_ORDINAL_SUFFIXES = ("st", "nd", "rd", "th")

__all__ = [
    "extract_numbers",
    "verify_numbers_in_facts",
    "check_lookahead_keywords",
    "check_causal_language",
    "classify_finding_type",
    "verify_report",
    "DEFAULT_EVENT_KEYWORDS",
    "DEFAULT_CAUSAL_PHRASES",
    "DEFAULT_TOLERANCE",
    "DEFAULT_WINDOW_TOKENS",
    "DATE_PATTERN",
]


# ---------------------------------------------------------------------------
# Number extraction
# ---------------------------------------------------------------------------

def _char_at(text: str, idx: int) -> str:
    if idx < 0 or idx >= len(text):
        return ""
    return text[idx]


_DATE_FULL_RE = re.compile(r"\b(19|20)\d{2}-\d{2}(?:-\d{2})?\b")


def _is_inside_date(text: str, span: tuple[int, int]) -> bool:
    """Return True iff the matched span is part of a YYYY-MM or
    YYYY-MM-DD date pattern. We scan the entire text because dates need
    a leading word-boundary anchor that a narrow snippet can miss.

    Performance: the text is the narrative (a few KB max) and we call
    this O(numbers) times. Total cost is negligible.
    """
    for m in _DATE_FULL_RE.finditer(text):
        if span[0] >= m.start() and span[1] <= m.end():
            return True
    return False


def _is_bare_year(raw: str, sign: str, currency: str, pct: str,
                  frac: str) -> bool:
    """A 4-digit token starting with 19 or 20 with no sign / currency /
    decimal / percent is a bare year and should not be extracted as a
    measurement number."""
    if sign or currency or pct or frac:
        return False
    if not re.fullmatch(r"(19|20)\d{2}", raw):
        return False
    return True


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """Extract numeric tokens from a narrative.

    Returns a list of ``(raw_token, parsed_value)`` pairs. Percentages are
    converted to proportion form (``52.1%`` -> ``0.521``). Currency
    symbols and thousands separators are stripped. Signed numbers preserve
    their sign.

    Tokens REJECTED:

    - Bare 4-digit years (e.g. ``"2024"``)
    - Ordinals (``"1st"``, ``"2nd"``, ``"3rd"``, ``"4th"``)
    - Numbers embedded inside identifier-like tokens (the ``4`` in
      ``"q4_2024"`` is not extracted)
    - Date components inside a ``YYYY-MM`` or ``YYYY-MM-DD`` pattern

    The function never raises. Malformed numeric tokens are simply
    skipped with a debug log.
    """
    out: list[tuple[str, float]] = []
    if not text:
        return out

    for m in _NUMBER_PATTERN.finditer(text):
        start, end = m.span()
        sign = m.group("sign") or ""
        currency = m.group("currency") or ""
        int_part = m.group("int") or ""
        frac = m.group("frac") or ""
        pct = m.group("pct") or ""

        # The numeric body (without sign/currency/percent) for the bare-year
        # check.
        body = int_part if not frac else f"{int_part}.{frac}"

        # Reject ordinals: trailing two letters from the ordinal set
        # immediately after the digits (no decimal / no percent).
        if not frac and not pct:
            tail = text[end:end + 2].lower()
            if tail in _ORDINAL_SUFFIXES:
                continue

        # Reject identifier-context: if the char immediately before the
        # full match is alphabetic or underscore (and not an = sign or
        # whitespace), the digits are embedded in a name like q4_2024.
        # Exception: a leading $ or = is allowed because those are
        # legitimate prefixes (handled by the regex itself / by =
        # appearing before the sign-stripped digits).
        prev_char = _char_at(text, start - 1)
        if prev_char and (prev_char.isalpha() or prev_char == "_"):
            continue

        # Reject identifier-context after the match: digits followed by
        # an underscore (e.g. "2024" in "2024_q1") or by a letter that
        # makes the token an identifier rather than a measurement (e.g.
        # "100" in "100x"). Ordinal suffixes were filtered above. We
        # allow a trailing 'e'/'E' to leave the door open for future
        # scientific-notation support without changing the contract.
        next_char = _char_at(text, end)
        if next_char == "_":
            continue
        if next_char and next_char.isalpha() and next_char.lower() != "e":
            continue

        # Reject if the match sits inside a YYYY-MM(-DD) date.
        if _is_inside_date(text, (start, end)):
            continue

        # Reject bare years.
        if _is_bare_year(body, sign, currency, pct, frac):
            continue

        # Parse value.
        try:
            digits = int_part.replace(",", "")
            if frac:
                value = float(f"{digits}.{frac}")
            else:
                value = float(digits)
            if sign == "-":
                value = -value
            if pct == "%":
                value = value / 100.0
        except ValueError:
            logger.debug("extract_numbers: could not parse %r", m.group(0))
            continue

        # Build raw token: include sign, currency, body, percent.
        raw = f"{sign}{currency}{int_part}"
        if frac:
            raw += f".{frac}"
        if pct:
            raw += "%"

        out.append((raw, value))

    return out


# ---------------------------------------------------------------------------
# Numbers-in-facts verification
# ---------------------------------------------------------------------------

def _walk_facts_numbers(facts: Any,
                        out: list[float],
                        skip_keys: tuple[str, ...] = ("findings",)) -> None:
    """Recursively collect every numeric leaf value from `facts`.

    Skips any sub-tree rooted at a key in `skip_keys` (default: findings).
    Booleans are excluded (Python treats bool as int but they aren't
    numeric measurements).

    2026-06-01 master fix Phase 7 — sign-symmetric matching. After
    collecting a leaf value x, ALSO collect -x. This makes citations
    like "HLZ t < -3.0 required, observed -7.77" match a gate_thresholds
    leaf of 3.0 (unsigned) AND a strategy metric of -7.77 (already
    signed). Run #6 of the Oracle had 6 REFUTED findings rejected by
    the verifier because the LLM cited "-3.0" gate threshold while
    facts.json carried the unsigned +3.0; this expands the candidate
    set so the magnitude is what's reconciled, not the convention.
    """
    if isinstance(facts, Mapping):
        for k, val in facts.items():
            if k in skip_keys:
                continue
            _walk_facts_numbers(val, out, skip_keys)
    elif isinstance(facts, (list, tuple)):
        for item in facts:
            _walk_facts_numbers(item, out, skip_keys)
    elif isinstance(facts, bool):
        # Skip booleans even though they pass isinstance(int).
        return
    elif isinstance(facts, (int, float)):
        if isinstance(facts, float) and not math.isfinite(facts):
            return
        x = float(facts)
        out.append(x)
        # Also emit the negation when non-zero so signed citations of
        # the same magnitude reconcile. -0.0 == 0.0 in float math, so
        # the guard skips redundant zero-doubling.
        if x != 0.0:
            out.append(-x)


def _number_matches(target: float, candidates: Iterable[float],
                    tolerance: float) -> bool:
    """A target number matches a candidate if their relative deviation is
    within `tolerance`. When the candidate is exactly zero, fall back to
    an absolute tolerance equal to `tolerance` itself."""
    for c in candidates:
        if c == 0.0:
            if abs(target) <= tolerance:
                return True
            continue
        if abs(target - c) / abs(c) <= tolerance:
            return True
    return False


def verify_numbers_in_facts(narrative: str,
                            facts: Mapping[str, Any],
                            tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """Reconcile every number in `narrative` against `facts`.

    Returns:
        {"ok": bool, "unmatched": list[tuple[str, float]]}

    A number matches if there exists a leaf-value in the facts tree
    (excluding the ``findings`` subtree) that is within +/-`tolerance`
    relative deviation. If the facts tree contains the value 0 exactly,
    an absolute deviation of `tolerance` is allowed (since relative
    deviation is undefined at 0).
    """
    candidates: list[float] = []
    _walk_facts_numbers(facts, candidates)
    extracted = extract_numbers(narrative)
    unmatched: list[tuple[str, float]] = []
    for raw, val in extracted:
        if not _number_matches(val, candidates, tolerance):
            unmatched.append((raw, val))
    return {"ok": len(unmatched) == 0, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Look-ahead keyword scan
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[tuple[str, int, int]]:
    """Tokenize on whitespace; return list of (token, start_idx, end_idx).

    The indices point into the original `text` so we can map a token back
    to a character span for excerpting.
    """
    tokens: list[tuple[str, int, int]] = []
    for m in re.finditer(r"\S+", text):
        tokens.append((m.group(0), m.start(), m.end()))
    return tokens


def _find_token_index(tokens: list[tuple[str, int, int]],
                      char_idx: int) -> int:
    """Return the index of the token that contains `char_idx`, or -1."""
    for i, (_tok, start, end) in enumerate(tokens):
        if start <= char_idx < end:
            return i
    return -1


def _excerpt(text: str, start_char: int, end_char: int,
             pad: int = 40) -> str:
    """Return a short excerpt of the narrative around a match."""
    lo = max(0, start_char - pad)
    hi = min(len(text), end_char + pad)
    snippet = text[lo:hi].replace("\n", " ")
    return snippet.strip()


def _window_text(tokens: list[tuple[str, int, int]],
                 center_idx: int, window: int) -> str:
    """Concatenate the +/- window tokens around `center_idx` into one
    lowercase string for keyword substring matching."""
    lo = max(0, center_idx - window)
    hi = min(len(tokens), center_idx + window + 1)
    return " ".join(tok for tok, _s, _e in tokens[lo:hi]).lower()


def check_lookahead_keywords(
    narrative: str,
    event_keywords: Iterable[str] = DEFAULT_EVENT_KEYWORDS,
    date_pattern: str = DATE_PATTERN,
    window_tokens: int = DEFAULT_WINDOW_TOKENS,
) -> dict:
    """Scan for event keywords near YYYY-MM dates.

    For each YYYY-MM date in `narrative`, look at the +/- `window_tokens`
    word-tokens around the date. If any keyword in `event_keywords`
    appears in that window (case-insensitive, substring match for
    multi-word keywords), record a violation.

    Returns ``{"ok": bool, "violations": list[dict]}`` where each
    violation has keys ``{date, keyword, span, excerpt}``.
    """
    tokens = _tokenize(narrative)
    kw_lower = [k.lower() for k in event_keywords]
    violations: list[dict] = []

    for m in re.finditer(date_pattern, narrative):
        date_text = m.group(0)
        tok_idx = _find_token_index(tokens, m.start())
        if tok_idx < 0:
            # Date sits between tokens (shouldn't happen with \S+ tokens
            # but guard anyway).
            continue
        window = _window_text(tokens, tok_idx, window_tokens)
        # Collect EVERY matching keyword for this date. Recording only the
        # first match drops diagnostic information about how concentrated
        # the lookahead artifact is (a paragraph mentioning crash + rally +
        # FOMC near the same date is a stronger tell than just one of them).
        for kw in kw_lower:
            # Substring search; word-boundary check for short single-word
            # keywords to avoid spurious matches like "rate" in "operate".
            if " " in kw:
                # Multi-word keyword: substring match is fine.
                hit = kw in window
            else:
                hit = bool(re.search(rf"\b{re.escape(kw)}\b", window))
            if hit:
                violations.append({
                    "date": date_text,
                    "keyword": kw,
                    "span": (m.start(), m.end()),
                    "excerpt": _excerpt(narrative, m.start(), m.end()),
                })

    return {"ok": len(violations) == 0, "violations": violations}


# ---------------------------------------------------------------------------
# Causal language detection
# ---------------------------------------------------------------------------

def _window_indicates_statistical_reason(window_text: str) -> bool:
    """Return True if the window contains language describing statistical /
    methodological reasoning (sample size, p-values, etc.). Such phrases
    legitimize a causal connector ("because n < 30")."""
    for term in _STATISTICAL_REASON_TERMS:
        if term in window_text:
            return True
    return False


def _window_event_trigger(window_text: str,
                          event_keywords: Iterable[str],
                          date_pattern: str) -> str | None:
    """Return the trigger token if the window contains an event keyword,
    a YYYY-MM date, or a macro context term. Otherwise None."""
    # Event keywords.
    for kw in event_keywords:
        kw_lower = kw.lower()
        if " " in kw_lower:
            if kw_lower in window_text:
                return kw
        else:
            if re.search(rf"\b{re.escape(kw_lower)}\b", window_text):
                return kw
    # Macro context terms.
    for term in _MACRO_CONTEXT_TERMS:
        if " " in term:
            if term in window_text:
                return term
        else:
            if re.search(rf"\b{re.escape(term)}\b", window_text):
                return term
    # Dates.
    m = re.search(date_pattern, window_text)
    if m:
        return m.group(0)
    return None


def check_causal_language(
    narrative: str,
    causal_phrases: Iterable[str] = DEFAULT_CAUSAL_PHRASES,
    event_keywords: Iterable[str] = DEFAULT_EVENT_KEYWORDS,
    window_tokens: int = DEFAULT_WINDOW_TOKENS,
) -> dict:
    """Detect causal phrases that point at exogenous market events.

    Causal phrases are CONDITIONALLY violating:

    - "because n < 30" -> statistical reasoning -> OK
    - "due to insufficient sample size" -> OK
    - "because the Fed pivoted" -> violation
    - "due to CPI release" -> violation

    For each causal phrase, we look at the +/- `window_tokens` and apply
    a two-step check:

    1. If the window contains a statistical-reason term, accept it as
       methodological reasoning (no violation).
    2. Otherwise, if the window contains any event keyword, YYYY-MM
       date, or macro context term (Fed/FOMC/CPI/NFP/ECB/BOE), flag it.

    Returns ``{"ok": bool, "violations": list[dict]}`` where each
    violation has keys ``{phrase, span, excerpt, trigger}``.
    """
    tokens = _tokenize(narrative)
    text_lower = narrative.lower()
    violations: list[dict] = []

    for phrase in causal_phrases:
        phrase_lower = phrase.lower()
        # Find every occurrence as a word-bounded substring.
        pattern = rf"\b{re.escape(phrase_lower)}\b"
        for m in re.finditer(pattern, text_lower):
            tok_idx = _find_token_index(tokens, m.start())
            if tok_idx < 0:
                continue
            window = _window_text(tokens, tok_idx, window_tokens)
            if _window_indicates_statistical_reason(window):
                continue
            trigger = _window_event_trigger(window, event_keywords,
                                            DATE_PATTERN)
            if trigger is not None:
                violations.append({
                    "phrase": phrase,
                    "span": (m.start(), m.end()),
                    "excerpt": _excerpt(narrative, m.start(), m.end()),
                    "trigger": trigger,
                })

    return {"ok": len(violations) == 0, "violations": violations}


# ---------------------------------------------------------------------------
# Finding classification
# ---------------------------------------------------------------------------

def classify_finding_type(finding: dict,
                          facts: Mapping[str, Any],
                          tolerance: float = DEFAULT_TOLERANCE,
                          num_check: dict | None = None) -> str:
    """Classify a finding as TRANSCRIPTION or INTERPRETATION.

    TRANSCRIPTION: every numeric token in ``finding['rationale']`` traces
    to a leaf-value in ``facts`` (within `tolerance`).

    INTERPRETATION: at least one numeric token does NOT trace, or the
    rationale is empty / missing.

    Empty rationale defaults to the conservative classification
    (INTERPRETATION) so an unannotated finding does not slip through the
    look-ahead downgrade.

    Vacuous-truth divergence
    ------------------------
    When the rationale contains zero numbers, we return INTERPRETATION (a
    conservative override of the spec's literal vacuous-truth reading).
    Rationale: a numeric-free claim is pure LLM inference with no
    quantitative anchor; treating it as TRANSCRIPTION would exempt it
    from the lookahead downgrade despite carrying maximum interpretive
    risk.

    Parameters
    ----------
    num_check:
        Optional pre-computed result of ``verify_numbers_in_facts`` on
        the rationale. ``verify_report`` already runs that check to make
        the reject/keep decision; passing it here avoids the duplicate
        walk through facts. If None, the check is run locally.
    """
    rationale = finding.get("rationale") if isinstance(finding, dict) else None
    if not rationale:
        return "INTERPRETATION"
    if num_check is None:
        num_check = verify_numbers_in_facts(rationale, facts, tolerance)
    if not num_check["ok"]:
        return "INTERPRETATION"
    # If the rationale contains zero numbers, it is interpretive prose by
    # default - no quantitative claim to ground in facts (see vacuous-truth
    # divergence note above).
    if not extract_numbers(rationale):
        return "INTERPRETATION"
    return "TRANSCRIPTION"


# ---------------------------------------------------------------------------
# Top-level verify_report
# ---------------------------------------------------------------------------

def _downgrade_tier(tier: str) -> str:
    """HIGH -> MEDIUM, MEDIUM -> LOW, LOW unchanged.

    Unknown tiers pass through. INSUFFICIENT is never published as a
    finding (the orchestrator drops those upstream), so a passthrough
    here is safe.
    """
    if tier == "HIGH":
        return "MEDIUM"
    if tier == "MEDIUM":
        return "LOW"
    return tier


def _finding_rejection_reason(finding: dict,
                              facts: Mapping[str, Any],
                              tolerance: float
                              ) -> tuple[str | None, dict | None]:
    """Determine whether a finding should be REJECTED outright.

    A finding is rejected when its rationale contains:
    - a number that does not trace to facts (hallucination), OR
    - a lookahead violation, OR
    - a hard causal violation.

    Returns ``(reason, num_check)``. ``reason`` is the rejection-reason
    string or None if the finding survives. ``num_check`` is the cached
    ``verify_numbers_in_facts`` result (or None if there was no
    rationale to check); callers may pass it back into
    ``classify_finding_type`` to avoid a second walk.
    """
    rationale = finding.get("rationale") if isinstance(finding, dict) else None
    if not rationale:
        return None, None
    # 1) Fabricated number in rationale.
    num_check = verify_numbers_in_facts(rationale, facts, tolerance)
    if not num_check["ok"]:
        return f"unmatched_numbers:{num_check['unmatched']}", num_check
    # 2) Lookahead violation in rationale.
    look = check_lookahead_keywords(rationale)
    if not look["ok"]:
        return f"lookahead:{look['violations'][0]}", num_check
    # 3) Causal violation in rationale.
    causal = check_causal_language(rationale)
    if not causal["ok"]:
        return f"causal:{causal['violations'][0]}", num_check
    return None, num_check


def verify_report(facts: Mapping[str, Any],
                  narrative_md: str,
                  findings: list[dict],
                  lookahead_active: bool) -> dict:
    """Top-level verification of the LLM's output against facts.

    Pipeline:
      1. verify_numbers_in_facts on the narrative
      2. check_lookahead_keywords on the narrative
      3. check_causal_language on the narrative
      4. For each finding: classify TRANSCRIPTION vs INTERPRETATION;
         decide whether to REJECT it (fabricated number / lookahead /
         causal violation in its rationale, OR a missing finding id).
      5. If `lookahead_active` is True, build the per-finding confidence
         downgrade list for surviving INTERPRETATION findings.

    Narrative-level vs finding-level asymmetry
    ------------------------------------------
    Steps 1-3 run on the FULL narrative; step 4's rejection logic runs
    only on each finding's ``rationale``. A narrative-level violation
    (e.g. the prose around a finding mentions "the market crashed in
    2022-10") sets ``result["ok"] = False`` but does NOT add anything to
    ``rejected_findings`` - the violating text is outside any specific
    finding's rationale. **The orchestrator MUST act on
    ``result['ok']`` independently of ``rejected_findings``.** A clean
    ``rejected_findings`` list does not imply the report is publishable.

    Structural rejection: missing finding id
    ----------------------------------------
    A finding without an ``id`` field is REJECTED outright with reason
    ``"structural: missing finding id"``. Silent-skipping such findings
    would let a fabricated rationale slip through unchecked: rejection
    here both surfaces the malformed input and runs the same content
    checks (which we still execute against a synthetic placeholder id).

    Returns:
        {
            "ok": bool,
            "numbers_check": dict,
            "lookahead_check": dict,
            "causal_check": dict,
            "downgrades": list[dict],
            "rejected_findings": list[str],
            "rejection_reasons": dict[str, str],
        }
    """
    numbers_check = verify_numbers_in_facts(narrative_md, facts)
    lookahead_check = check_lookahead_keywords(narrative_md)
    causal_check = check_causal_language(narrative_md)

    rejected: list[str] = []
    rejection_reasons: dict[str, str] = {}
    downgrades: list[dict] = []

    for idx, finding in enumerate(findings or []):
        fid = finding.get("id") if isinstance(finding, dict) else None
        missing_id = fid is None
        if missing_id:
            # Generate a synthetic id so the rejection is trackable. We
            # still run the content checks below so the orchestrator can
            # see WHY this finding is bad (structural + possibly
            # content). The structural reason wins if no content reason
            # fires.
            fid = f"missing_id_{idx}"
        reason, _num_check = _finding_rejection_reason(
            finding, facts, DEFAULT_TOLERANCE
        )
        if missing_id and reason is None:
            reason = "structural: missing finding id"
        elif missing_id and reason is not None:
            reason = f"structural: missing finding id; {reason}"
        if reason is not None:
            rejected.append(fid)
            rejection_reasons[fid] = reason
            continue
        # Survived rejection. If lookahead is active, downgrade
        # INTERPRETATION findings only. Reuse the cached num_check from
        # the rejection probe to avoid re-walking facts.
        if lookahead_active:
            ftype = classify_finding_type(
                finding, facts, num_check=_num_check
            )
            if ftype == "INTERPRETATION":
                old = finding.get("confidence", "")
                new = _downgrade_tier(old)
                if new != old:
                    downgrades.append({
                        "finding_id": fid,
                        "old": old,
                        "new": new,
                        "reason": "lookahead_active+INTERPRETATION",
                    })

    ok = (
        numbers_check["ok"]
        and lookahead_check["ok"]
        and causal_check["ok"]
        and not rejected
    )

    return {
        "ok": bool(ok),
        "numbers_check": numbers_check,
        "lookahead_check": lookahead_check,
        "causal_check": causal_check,
        "downgrades": downgrades,
        "rejected_findings": rejected,
        "rejection_reasons": rejection_reasons,
    }
