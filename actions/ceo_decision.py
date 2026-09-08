"""The layer that makes the Brain participate in decisions rather than
just store things.

WHAT WAS MISSING
Every piece existed and none of them met. jarvis_brain.recall() could
retrieve Brain excerpts, operating_memory recorded every agent outcome,
business_intelligence.get_lessons_for() returned lessons — and the CEO
cycle read none of them when deciding what to do. Prioritisation was
priorities_engine.get_todays_priorities(), which sorts by a FIXED severity
tier: risk=4, approval=3, stale follow-up=2, recommendation=1. Revenue,
deadlines, dependencies, expected value and prior failures did not enter
into it at all, and a task that had failed four times ranked exactly where
it ranked the first time.

So JARVIS could remember, and could act, but nothing connected memory to
the choice. That is what this module is: the arrow between RETRIEVE and
DECIDE.

    OBSERVE -> RETRIEVE RELEVANT MEMORY -> UNDERSTAND -> REASON ->
    DECIDE -> ACT -> VERIFY -> RECORD -> LEARN -> UPDATE MEMORY

RETRIEVAL IS TARGETED, NOT A DUMP
context_for() takes ONE item and asks each store only about that item's
subject. The whole Brain in every request is both expensive and useless —
it buries the one relevant note under fifty irrelevant ones. Budgets are
per-item and capped.

NOTHING HERE EXECUTES ANYTHING
It scores, it classifies, and it explains. Dispatch stays in
agent_orchestrator and the approval gate stays exactly where it is: this
module can raise something to REQUIRE_APPROVAL but has no power to lower
anything below it.

Every score carries its reasoning. A priority order nobody can explain is
one nobody can correct.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.ceo_decision")

# ── What JARVIS may do about an item, in ascending order of consequence ──
OBSERVE = "OBSERVE"                    # look, record, say nothing
RECOMMEND = "RECOMMEND"                # surface it to Lee as a suggestion
PREPARE = "PREPARE"                    # do the reversible groundwork
EXECUTE = "EXECUTE"                    # act autonomously
REQUIRE_APPROVAL = "REQUIRE_APPROVAL"  # Lee decides before anything happens

DISPOSITIONS = (OBSERVE, RECOMMEND, PREPARE, EXECUTE, REQUIRE_APPROVAL)

# Per-item retrieval budgets. Small on purpose — see the module docstring.
BRAIN_CHARS_PER_ITEM = 600
MEMORY_ENTRIES_PER_ITEM = 5
LESSONS_PER_ITEM = 3

# A source that has failed this many times running is not worth retrying
# unattended; it needs a human to look at it.
ESCALATION_STREAK = 3

# Weights for the priority score. Deliberately a transparent linear
# combination in the same style as daily_deal_finders.rank_products() —
# not a learned model, because a ranking Lee cannot audit is one he cannot
# overrule.
_WEIGHTS = {
    "severity": 0.30,     # how bad is it if this is ignored
    "revenue": 0.25,      # money actually attached to it
    "urgency": 0.20,      # deadline pressure and time already waited
    "value": 0.10,        # expected value of doing it
    "user_priority": 0.10,  # Lee said this matters
    "readiness": 0.05,    # dependencies met, not already running
}

_MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
_DEADLINE_RE = re.compile(
    r"\b(today|tomorrow|overdue|past due|by \w+day|end of (?:day|week)|"
    r"within \d+ (?:hours?|days?)|expires?|deadline)\b", re.I)


# ══ RETRIEVE ═════════════════════════════════════════════════════════════

def _subject_of(item: dict[str, Any]) -> str:
    """The few words worth searching memory for. A whole title makes a
    poor query — it retrieves on incidental words."""
    text = " ".join(str(item.get(k) or "") for k in ("title", "source", "kind"))
    words = [w for w in re.findall(r"[A-Za-z][\w'-]{2,}", text)][:8]
    return " ".join(words)


def context_for(item: dict[str, Any]) -> dict[str, Any]:
    """Relevant memory for ONE item, from every store that has any.

    Each store is isolated: one that is unavailable or raises contributes
    nothing and never prevents the others from answering. An empty section
    means "nothing on file", which is information, not a failure."""
    subject = _subject_of(item)
    source = str(item.get("source") or item.get("kind") or "")
    context: dict[str, Any] = {
        "subject": subject, "brain": "", "outcomes": [], "lessons": [],
        "failure_streak": 0, "retrieved": [],
    }

    try:
        from actions import jarvis_brain
        if jarvis_brain.is_available() and subject:
            excerpt = jarvis_brain.recall(subject, max_chars=BRAIN_CHARS_PER_ITEM)
            if excerpt.strip():
                context["brain"] = excerpt
                context["retrieved"].append("brain")
    except Exception:
        logger.debug("brain recall failed for %r", subject, exc_info=True)

    try:
        from actions import operating_memory
        if source:
            outcomes = operating_memory.recall(
                source=source, limit=MEMORY_ENTRIES_PER_ITEM) or []
            if outcomes:
                context["outcomes"] = outcomes
                context["retrieved"].append("operating_memory")
            context["failure_streak"] = operating_memory.failure_streak(source)
    except Exception:
        logger.debug("operating memory recall failed for %r", source, exc_info=True)

    try:
        from actions import business_intelligence as biz
        business = str(item.get("business") or "buildpro")
        lessons = biz.get_lessons_for(business, limit=LESSONS_PER_ITEM) or []
        if lessons:
            context["lessons"] = lessons
            context["retrieved"].append("lessons")
    except Exception:
        logger.debug("lesson recall failed", exc_info=True)

    return context


# ══ REASON ═══════════════════════════════════════════════════════════════

def _revenue_signal(item: dict[str, Any]) -> tuple[float, str]:
    """Money actually attached to this item. Read from the record, never
    estimated — an invented number would rank real work below imaginary."""
    for key in ("revenue", "value", "amount", "estimated_value"):
        raw = item.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            return min(float(raw) / 25_000.0, 1.0), f"{key}={raw}"
    money = _MONEY_RE.search(str(item.get("title") or ""))
    if money:
        try:
            amount = float(money.group(1).replace(",", ""))
            return min(amount / 25_000.0, 1.0), f"${amount:,.0f} named in the item"
        except ValueError:
            pass
    return 0.0, "no revenue attached"


def _urgency_signal(item: dict[str, Any]) -> tuple[float, str]:
    reasons: list[str] = []
    score = 0.0
    title = str(item.get("title") or "")
    if _DEADLINE_RE.search(title):
        score += 0.6
        reasons.append("a deadline is named")
    waited = item.get("waited_hours")
    if isinstance(waited, (int, float)) and waited > 0:
        # Something blocked on Lee for two days is more urgent than
        # something blocked for two minutes.
        score += min(float(waited) / 48.0, 0.4)
        reasons.append(f"waiting {waited}h")
    return min(score, 1.0), "; ".join(reasons) or "no deadline pressure"


def _readiness_signal(item: dict[str, Any], context: dict[str, Any]) -> tuple[float, str]:
    """Whether this can actually move. Work already underway, or blocked on
    an unmet dependency, should not outrank work that can start now."""
    if item.get("in_progress") or item.get("task_id") and item.get("status") == "running":
        return 0.0, "already underway"
    blockers = item.get("depends_on") or []
    if blockers:
        return 0.2, f"{len(blockers)} unmet dependenc(ies)"
    return 1.0, "ready to start"


def score_item(item: dict[str, Any], context: Optional[dict[str, Any]] = None
               ) -> dict[str, Any]:
    """One item's priority, with the reasoning that produced it.

    A repeatedly-failing source is DEMOTED rather than promoted: retrying
    it ahead of everything else burns the cycle on something already known
    to be broken. It escalates instead — see decide()."""
    context = context if context is not None else context_for(item)

    severity = min(max(float(item.get("severity") or 1), 0) / 4.0, 1.0)
    revenue, revenue_why = _revenue_signal(item)
    urgency, urgency_why = _urgency_signal(item)
    readiness, readiness_why = _readiness_signal(item, context)
    user_priority = 1.0 if item.get("user_priority") else 0.0
    value = min(max(float(item.get("expected_value") or 0), 0.0), 1.0)

    score = (_WEIGHTS["severity"] * severity
             + _WEIGHTS["revenue"] * revenue
             + _WEIGHTS["urgency"] * urgency
             + _WEIGHTS["value"] * value
             + _WEIGHTS["user_priority"] * user_priority
             + _WEIGHTS["readiness"] * readiness)

    reasoning = [
        f"severity {item.get('severity', 1)}",
        f"revenue: {revenue_why}",
        f"urgency: {urgency_why}",
        f"readiness: {readiness_why}",
    ]
    if user_priority:
        reasoning.append("Lee marked this a priority")

    streak = int(context.get("failure_streak") or 0)
    if streak:
        # Past failure is a real input to the decision, and this is where
        # "use previous results to improve future decisions" becomes
        # concrete rather than aspirational.
        penalty = min(0.10 * streak, 0.30)
        score -= penalty
        reasoning.append(f"demoted {penalty:.2f}: {streak} consecutive prior failure(s)")

    if context.get("lessons"):
        reasoning.append(f"{len(context['lessons'])} prior lesson(s) on file")
    if context.get("brain"):
        reasoning.append("Brain has relevant context")

    return {
        **item,
        "priority_score": round(max(score, 0.0), 4),
        "reasoning": reasoning,
        "context_sources": context.get("retrieved", []),
        "failure_streak": streak,
    }


def prioritize(items: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    """Rerank real signals into a real order.

    priorities_engine still produces the items — this does not replace it,
    and building a second source of signals would be exactly the duplicate
    system the architecture rules forbid. What it replaces is the fixed
    severity tier that was the only thing ordering them."""
    scored = [score_item(item) for item in (items or [])]
    scored.sort(key=lambda i: (-i["priority_score"], -float(i.get("severity") or 0),
                               str(i.get("title") or "")))
    return scored[:max(int(limit), 0)]


# ══ DECIDE ═══════════════════════════════════════════════════════════════

def decide(item: dict[str, Any], context: Optional[dict[str, Any]] = None
           ) -> dict[str, Any]:
    """What JARVIS may do about this item, and why.

    This can only ever RAISE the bar. An item whose own record says it
    needs approval keeps needing approval; nothing here can lower one
    below REQUIRE_APPROVAL, because the approval gate is not this
    module's to move."""
    context = context if context is not None else context_for(item)
    streak = int(context.get("failure_streak") or 0)
    kind = str(item.get("kind") or "").lower()

    if item.get("requires_approval") or kind == "approval":
        return {"disposition": REQUIRE_APPROVAL,
                "why": "this item is blocked on Lee's decision by its own record",
                "escalate": False}

    if streak >= ESCALATION_STREAK:
        return {"disposition": REQUIRE_APPROVAL,
                "why": (f"{streak} consecutive failures — retrying unattended would "
                        f"repeat a known-broken action; a person needs to look at it"),
                "escalate": True}

    permission = str(item.get("permission_level") or "").lower()
    if permission == "execute":
        return {"disposition": REQUIRE_APPROVAL,
                "why": "EXECUTE-level work requires explicit approval before it runs",
                "escalate": False}
    if permission in ("observe", ""):
        if kind == "risk":
            return {"disposition": RECOMMEND,
                    "why": "a risk is worth surfacing, but acting on it is Lee's call",
                    "escalate": False}
        return {"disposition": OBSERVE,
                "why": "read-only work; safe to run unattended",
                "escalate": False}
    if permission == "suggest":
        return {"disposition": PREPARE,
                "why": "reversible groundwork may proceed; the result is a proposal",
                "escalate": False}

    return {"disposition": RECOMMEND,
            "why": "no permission level on the record — defaulting to a suggestion",
            "escalate": False}


def plan(items: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    """Prioritised items, each carrying its disposition and its reasoning.
    One pass, one retrieval per item — the context fetched for scoring is
    reused for the decision rather than fetched twice."""
    out: list[dict[str, Any]] = []
    for item in (items or []):
        context = context_for(item)
        scored = score_item(item, context)
        scored["decision"] = decide(item, context)
        out.append(scored)
    out.sort(key=lambda i: (-i["priority_score"], str(i.get("title") or "")))
    return out[:max(int(limit), 0)]


# ══ LEARN ════════════════════════════════════════════════════════════════

def record_outcome(item: dict[str, Any], ok: bool, detail: str = "",
                   verified: Optional[bool] = None) -> dict[str, Any]:
    """Write what happened back where the next decision will read it.

    Two stores on purpose, because they answer different questions.
    operating_memory answers "has this source been failing?" and drives
    failure_streak, which is what demotes and escalates. business_
    intelligence answers "what did we learn?" and is what a human reads.
    Neither raises: a lesson that cannot be filed must not fail the action
    that produced it."""
    written: list[str] = []
    source = str(item.get("source") or item.get("kind") or "ceo_decision")
    title = str(item.get("title") or source)[:200]

    try:
        from actions import operating_memory
        operating_memory.record(
            operating_memory.AGENT_OUTCOME, source=source, subject=title,
            summary=(detail or ("completed" if ok else "failed"))[:400],
            data={"priority_score": item.get("priority_score"),
                  "disposition": (item.get("decision") or {}).get("disposition"),
                  "verified": verified},
            ok=ok)
        written.append("operating_memory")
    except Exception:
        logger.debug("could not record an outcome to operating memory", exc_info=True)

    # Only real conclusions become lessons. Filing every routine success
    # as a "lesson" is how a lessons store becomes unreadable.
    if not ok or verified is False:
        try:
            from actions import business_intelligence as biz
            biz.add_entry(
                category="lessons_learned",
                business=str(item.get("business") or "buildpro"),
                title=f"Did not complete: {title}"[:120],
                content=(f"{detail or 'no detail recorded'}\n"
                         f"Priority score at the time: {item.get('priority_score')}\n"
                         f"Reasoning: {'; '.join(item.get('reasoning') or [])}"),
                data={"source": source, "verified": verified},
            )
            written.append("lessons_learned")
        except Exception:
            logger.debug("could not record a lesson", exc_info=True)

    return {"ok": True, "recorded_to": written, "source": source,
            "outcome": "ok" if ok else "failed", "ts": time.time()}
