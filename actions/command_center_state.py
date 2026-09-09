"""One structured view of what JARVIS is actually doing.

The Command Center currently asks several endpoints separately and
assembles its own picture, which means the picture it shows can disagree
with the system it is showing. This is the single read-only assembly:
priorities, approvals, matches, research, health, learning and recent
activity, gathered from the systems that own each.

TWO RULES

  * Every section is isolated. One unavailable integration contributes an
    honest state and nothing else — it never empties the other sections
    and never fails the whole view. A section that could not be read says
    so, so "no approvals" and "the approvals store is down" cannot look
    identical on screen.

  * Nothing is computed here. Every number comes from the system that
    owns it. A dashboard that derives its own totals is a second source
    of truth, and the second one is always the one that is wrong.

Read-only. It reaches no external service that writes, and it returns no
credential — see _redact().
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger("jarvis.command_center_state")

OK = "OK"
UNAVAILABLE = "UNAVAILABLE"

# Never let a value whose key looks like a credential reach the UI, even
# if some upstream dict starts carrying one.
_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey",
                 "credential", "authorization", "cookie")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(h in str(k).lower() for h in _SECRET_HINTS)
                    else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _section(name: str, fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"state": OK, "data": _redact(fn())}
    except Exception as exc:
        logger.debug("command centre section %s failed", name, exc_info=True)
        return {"state": UNAVAILABLE, "data": None, "detail": str(exc)[:200]}


def _priorities() -> list[dict[str, Any]]:
    from actions import priorities_engine, ceo_decision
    raw = priorities_engine.get_todays_priorities(limit=10) or []
    planned = ceo_decision.plan(raw, limit=10)
    return [{
        "title": p.get("title"), "kind": p.get("kind"),
        "severity": p.get("severity"),
        "score": p.get("priority_score"),
        "disposition": (p.get("decision") or {}).get("disposition"),
        "why": (p.get("decision") or {}).get("why"),
        "reasoning": p.get("reasoning"),
        "source": p.get("source"),
    } for p in planned]


def _approvals() -> list[dict[str, Any]]:
    from actions.agent_orchestrator import orchestrator
    return [{
        "id": t.id, "agent": t.agent_id, "description": t.description,
        "status": t.status.value, "updated_ts": t.updated_ts,
    } for t in orchestrator.list_tasks() if t.status.value == "pending_approval"]


def _matches() -> dict[str, Any]:
    from actions import buildpro_daily
    result = buildpro_daily.run_daily_matching(top_n=5)
    return {
        "jobs_evaluated": result.get("jobs_evaluated"),
        "candidates_evaluated": result.get("candidates_evaluated"),
        "strong_matches": result.get("strong_matches"),
        "exceptional_matches": result.get("exceptional_matches"),
        "top": [{
            "candidate": m.get("candidate_name"), "candidate_id": m.get("candidate_id"),
            "job": m.get("job_title"), "job_id": m.get("job_id"),
            "score": m.get("score"),
            "action": buildpro_daily.recommended_action(m),
        } for m in (result.get("matches") or [])],
    }


def _research() -> list[dict[str, Any]]:
    from actions import web_research
    out = []
    for entry in web_research.history(limit=10):
        payload = entry.get("data") or {}
        out.append({
            "question": entry.get("subject"),
            "summary": entry.get("summary"),
            "sources_read": payload.get("sources_read") if isinstance(payload, dict) else None,
            "confidence": payload.get("confidence") if isinstance(payload, dict) else None,
            "state": payload.get("state") if isinstance(payload, dict) else None,
            "ok": entry.get("ok"),
        })
    return out


def _learning() -> list[dict[str, Any]]:
    from actions import brain_memory
    return brain_memory.recent(limit=10)


def _health() -> dict[str, Any]:
    from actions import integration_health
    return integration_health.check_all()


def _activity() -> list[dict[str, Any]]:
    from actions import operating_memory
    return [{
        "type": e.get("entry_type"), "source": e.get("source"),
        "subject": e.get("subject"), "summary": e.get("summary"),
        "ok": e.get("ok"), "ts": e.get("ts"),
    } for e in (operating_memory.recall(limit=20) or [])]


def _failures() -> list[dict[str, Any]]:
    from actions import notifications
    return notifications.recent_failures(limit=10)


SECTIONS: dict[str, Callable[[], Any]] = {
    "priorities": _priorities,
    "approvals": _approvals,
    "buildpro_matches": _matches,
    "research": _research,
    "brain_learning": _learning,
    "health": _health,
    "activity": _activity,
    "failures": _failures,
}


def snapshot(sections: list[str] | None = None) -> dict[str, Any]:
    """Everything the Command Center needs, in one read.

    `sections` narrows the work — a view that only shows approvals should
    not run the whole matching pass to draw itself."""
    started = time.time()
    wanted = [s for s in (sections or list(SECTIONS)) if s in SECTIONS]
    out = {name: _section(name, SECTIONS[name]) for name in wanted}
    degraded = [n for n, v in out.items() if v["state"] != OK]
    return {
        "ok": True,
        "generated_at": time.time(),
        "duration_ms": int((time.time() - started) * 1000),
        "sections": out,
        # Named explicitly so the UI can show "approvals unavailable"
        # rather than an empty list that reads as "no approvals".
        "degraded_sections": degraded,
        "fully_available": not degraded,
    }
