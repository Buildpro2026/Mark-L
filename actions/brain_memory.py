"""Typed operating knowledge — the layer that makes the Brain participate.

WHAT WAS MISSING
jarvis_brain.py retrieves Obsidian notes. operating_memory.py records
cycle runs, agent outcomes and integration states. business_intelligence
holds lessons. All three worked, and between them there was nowhere to put
"Turner is a data-centre GC in Phoenix, learned from a job posting on
2026-09-09, moderately confident" — a durable business FACT with a source
and a confidence, as distinct from an event that happened once.

So JARVIS could remember that a task failed and could read a note someone
wrote by hand, but could not accumulate what it learned by working. Every
research run started from zero.

This adds that layer on top of operating_memory rather than beside it: a
new entry type, not a new store. No second database, no migration.

WHAT A MEMORY MUST CARRY
    kind        FACT | PREFERENCE | DECISION | LESSON | OUTCOME |
                OBSERVATION | RECOMMENDATION
    subject     what it is about, so retrieval can be targeted
    confidence  how much to trust it
    source      where it came from — a URL, a system, or "lee"
    observed_at when it was learned

A fact JARVIS generated about itself is not the same as a fact it read off
a filing, and remember() will not let the two be stored identically:
anything without a source is capped at low confidence, so a hallucination
cannot become durable knowledge merely by being written down.

RETRIEVAL IS TARGETED
recall_for() takes a subject and returns what is relevant to it, newest
and most confident first, bounded. Dumping everything JARVIS has ever
learned into a prompt is how the one relevant memory gets buried.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("jarvis.brain_memory")

# ── Kinds of knowledge ───────────────────────────────────────────────────
FACT = "FACT"                    # something true about the world
PREFERENCE = "PREFERENCE"        # how Lee wants things done
DECISION = "DECISION"            # a choice made, and why
LESSON = "LESSON"                # what an outcome taught
OUTCOME = "OUTCOME"              # what happened when JARVIS acted
OBSERVATION = "OBSERVATION"      # noticed, not yet concluded
RECOMMENDATION = "RECOMMENDATION"  # a proposed course of action

KINDS = (FACT, PREFERENCE, DECISION, LESSON, OUTCOME, OBSERVATION, RECOMMENDATION)

# Stored as one operating_memory entry type so there is no second store.
ENTRY_TYPE = "knowledge"

# Anything with no external source is capped here. A model-generated claim
# is an OBSERVATION at best until something corroborates it — this is what
# stops a hallucination becoming permanent memory by being written down.
UNSOURCED_CONFIDENCE_CAP = 0.4
# Facts age. A price learned six months ago is history, not knowledge.
DEFAULT_HALF_LIFE_DAYS = {FACT: 90, OBSERVATION: 30, RECOMMENDATION: 14}

_WORD = re.compile(r"[A-Za-z][\w'-]{2,}")
_STOP = {"the", "and", "for", "with", "from", "that", "this", "what", "when",
         "who", "how", "are", "was", "were", "has", "have", "its", "our"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")
            if w.lower() not in _STOP}


def remember(kind: str, subject: str, content: str, *,
             confidence: float = 0.5, source: str = "",
             source_url: str = "", data: Optional[dict] = None,
             business: str = "general") -> dict[str, Any]:
    """Store one piece of durable operating knowledge.

    Refuses an unknown kind rather than storing it under a label nothing
    will ever query. Caps unsourced confidence — see the module docstring.
    Never raises: a memory write must not fail the work that produced it.
    """
    if kind not in KINDS:
        return {"ok": False, "detail": f"unknown knowledge kind: {kind!r}"}
    subject = (subject or "").strip()
    content = (content or "").strip()
    if not subject or not content:
        return {"ok": False, "detail": "a memory needs both a subject and content"}

    confidence = round(max(0.0, min(1.0, float(confidence))), 2)
    sourced = bool(source or source_url)
    if not sourced and confidence > UNSOURCED_CONFIDENCE_CAP:
        confidence = UNSOURCED_CONFIDENCE_CAP

    payload = {
        "kind": kind, "subject": subject, "content": content[:2000],
        "confidence": confidence, "source": source or ("jarvis" if not source_url else ""),
        "source_url": source_url, "sourced": sourced,
        "observed_at": _now(), "business": business,
        "terms": sorted(_terms(f"{subject} {content}"))[:24],
        **(data or {}),
    }

    try:
        from actions import operating_memory
        operating_memory.record(
            ENTRY_TYPE, source=f"{kind.lower()}:{business}",
            subject=subject[:200], summary=content[:400],
            data=payload, ok=True,
            # The same fact re-learned within the hour is one fact.
            dedup_seconds=3600.0)
        return {"ok": True, "kind": kind, "subject": subject,
                "confidence": confidence, "sourced": sourced}
    except Exception as exc:
        logger.debug("could not store knowledge", exc_info=True)
        return {"ok": False, "detail": str(exc)[:200]}


def _decayed(entry: dict[str, Any]) -> float:
    """Confidence reduced by age, for kinds that age. A lesson does not
    expire; a price does."""
    payload = entry.get("data") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    confidence = float(payload.get("confidence") or 0.0)
    half_life = DEFAULT_HALF_LIFE_DAYS.get(payload.get("kind"))
    if not half_life:
        return confidence
    observed = payload.get("observed_at")
    try:
        then = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - then).days
    except Exception:
        return confidence
    return round(confidence * (0.5 ** (age_days / half_life)), 3)


def _payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("data") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def recall_for(subject: str, kinds: Optional[Iterable[str]] = None,
               limit: int = 5, min_confidence: float = 0.15) -> list[dict[str, Any]]:
    """Knowledge relevant to one subject, best first.

    Scored on term overlap times age-decayed confidence, so a strong old
    fact and a weak new one are ranked against each other rather than by
    recency alone. Bounded: dumping everything is how the one relevant
    memory gets buried."""
    wanted = set(kinds) if kinds else None
    query_terms = _terms(subject)
    if not query_terms:
        return []

    try:
        from actions import operating_memory
        entries = operating_memory.recall(entry_type=ENTRY_TYPE, limit=400) or []
    except Exception:
        logger.debug("knowledge recall failed", exc_info=True)
        return []

    scored = []
    for entry in entries:
        payload = _payload(entry)
        if not payload or (wanted and payload.get("kind") not in wanted):
            continue
        terms = set(payload.get("terms") or [])
        overlap = len(query_terms & terms)
        if not overlap:
            continue
        confidence = _decayed(entry)
        if confidence < min_confidence:
            continue
        scored.append({
            "kind": payload.get("kind"), "subject": payload.get("subject"),
            "content": payload.get("content"), "confidence": confidence,
            "source": payload.get("source"), "source_url": payload.get("source_url"),
            "observed_at": payload.get("observed_at"),
            "relevance": round(overlap / max(len(query_terms), 1), 3),
            "score": round(overlap * confidence, 4),
        })
    scored.sort(key=lambda m: -m["score"])
    return scored[:max(int(limit), 0)]


def context_for(subject: str, limit: int = 5) -> str:
    """Relevant knowledge as a short block for a prompt or a report.

    Every line carries its confidence and source, so a downstream reader —
    model or human — can weigh it instead of treating all of it as fact."""
    memories = recall_for(subject, limit=limit)
    if not memories:
        return ""
    lines = [f"What JARVIS already knows about {subject}:"]
    for memory in memories:
        origin = memory.get("source_url") or memory.get("source") or "unsourced"
        lines.append(f"  - [{memory['kind']} {memory['confidence']:.0%} via {origin}] "
                     f"{memory['content'][:220]}")
    return "\n".join(lines)


def learn_from_research(outcome: dict[str, Any], subject: str = "") -> dict[str, Any]:
    """Distil a completed research run into knowledge.

    Only observed values become FACTs, and only with the source URL that
    carried them. Reported values become OBSERVATIONs. A run that read
    nothing teaches nothing — and says so, rather than storing an empty
    fact that would later read as "we looked and found nothing true"."""
    if not outcome.get("ok") or not outcome.get("results"):
        return {"ok": False, "stored": 0,
                "detail": "the research run read no source, so there is nothing to learn"}

    subject = subject or outcome.get("question") or ""
    stored = 0
    for result in outcome["results"]:
        source_url = result.get("source_url") or ""
        for name, entry in (result.get("fields") or {}).items():
            if entry.get("value") is None:
                continue
            evidence = entry.get("evidence")
            if evidence == "OBSERVED":
                kind, confidence = FACT, 0.75
            elif evidence in ("REPORTED", "CALCULATED"):
                kind, confidence = OBSERVATION, 0.55
            else:
                continue
            written = remember(
                kind, f"{subject} {name}".strip(),
                f"{name}: {entry['value']}",
                confidence=confidence, source_url=source_url,
                data={"field": name, "evidence": evidence})
            stored += 1 if written.get("ok") else 0
    return {"ok": True, "stored": stored, "subject": subject,
            "detail": f"{stored} finding(s) stored as knowledge"}


def recent(kind: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    """Recent knowledge, newest first — for the Command Center's Brain
    view and for anyone asking what JARVIS has learned lately."""
    try:
        from actions import operating_memory
        entries = operating_memory.recall(entry_type=ENTRY_TYPE, limit=limit * 3) or []
    except Exception:
        return []
    out = []
    for entry in entries:
        payload = _payload(entry)
        if not payload or (kind and payload.get("kind") != kind):
            continue
        out.append({"kind": payload.get("kind"), "subject": payload.get("subject"),
                    "content": payload.get("content"),
                    "confidence": payload.get("confidence"),
                    "source": payload.get("source_url") or payload.get("source"),
                    "observed_at": payload.get("observed_at")})
    return out[:limit]
