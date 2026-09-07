"""The connective tissue between the integrations and the task system.

Every integration this touches already worked in isolation, and none of
them are reimplemented here. What was missing is the step AFTER "JARVIS can
read your calendar": nothing turned a real finding into a real task that a
real agent then executed. Findings became log lines and business-intelligence
rows that nobody acted on.

Each function here does the same four things and nothing more:

    read a real source  ->  decide what is actually actionable
                        ->  claim it once (autonomous_ledger)
                        ->  create a task the orchestrator will run

Deliberate constraints:

  * Nothing here sends, publishes, or writes to an external system. Every
    outbound side effect stays behind the existing approval gate — these
    functions PREPARE work, they do not commit it. An EXECUTE-level agent
    still sits at PENDING_APPROVAL until a human approves it.
  * Nothing here invents data. If a source is unconfigured or its auth is
    broken, that is reported as NOT_CONFIGURED / AUTH_ERROR and the
    function returns no findings — never a plausible-looking placeholder.
  * One source failing never stops the others: gather_and_dispatch()
    isolates each.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from actions import autonomous_ledger as ledger

logger = logging.getLogger("jarvis.business_pipeline")

# Outcome vocabulary, shared by every source below so the CEO report can
# treat them uniformly instead of each integration inventing its own words.
SUCCESS = "SUCCESS"
NOT_CONFIGURED = "NOT_CONFIGURED"
AUTH_ERROR = "AUTH_ERROR"
RATE_LIMITED = "RATE_LIMITED"
TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
FAILED = "FAILED"


def classify_failure(exc: BaseException) -> str:
    """Maps a raised exception onto the shared vocabulary. Errs toward
    FAILED: mislabelling a real bug as TRANSIENT_FAILURE would hide it
    behind a retry that never succeeds."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(w in text for w in ("invalid_scope", "insufficient", "unauthorized", "401",
                               "invalid_grant", "not yet authorized", "credentials")):
        return AUTH_ERROR
    if any(w in text for w in ("rate limit", "ratelimit", "429", "quota", "too many requests")):
        return RATE_LIMITED
    if any(w in text for w in ("timeout", "timed out", "connection", "503", "502", "504",
                               "temporarily unavailable")):
        return TRANSIENT_FAILURE
    if "not configured" in text:
        return NOT_CONFIGURED
    return FAILED


def _integration_failure(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Every integration here reports failure the same way — ok=False with a
    state and detail — and none of them fabricate data on failure. Checking
    for an "error" key instead (as this module first did) silently read an
    unauthorized Gmail as a successful empty inbox, which is precisely the
    fake success this system must never produce. Returns a failure dict, or
    None when the call genuinely succeeded."""
    if result.get("ok", True):
        return None
    detail = str(result.get("detail") or result.get("state") or "integration unavailable")
    state = result.get("state")
    if state in ("NOT_AUTHORIZED", "AUTH_ERROR", "INVALID_SCOPE"):
        mapped = AUTH_ERROR
    elif state == "NOT_CONFIGURED":
        mapped = NOT_CONFIGURED
    elif state == "RATE_LIMITED":
        mapped = RATE_LIMITED
    else:
        mapped = classify_failure(RuntimeError(f"{state}: {detail}"))
    return {"state": mapped, "detail": detail, "findings": []}


def _finding(kind: str, subject_id: str, title: str, agent_id: str,
             description: str, detail: Optional[dict] = None) -> dict[str, Any]:
    return {
        "kind": kind, "subject_id": subject_id, "title": title,
        "agent_id": agent_id, "description": description, "detail": detail or {},
    }


# ── ITEM 6: CALENDAR ─────────────────────────────────────────────────────

def calendar_findings(max_events: int = 10) -> dict[str, Any]:
    """Upcoming events that genuinely warrant preparation.

    Not every meeting needs a task. A real external meeting with other
    attendees does; a solo block, a reminder, or an all-day marker does
    not, and creating a preparation task for each would bury the ones that
    matter. Events are identified by Google's own event id."""
    from actions import calendar_integration

    try:
        result = calendar_integration.list_upcoming_events(max_results=max_events)
    except Exception as exc:
        return {"state": classify_failure(exc), "detail": str(exc), "findings": []}

    failure = _integration_failure(result)
    if failure:
        return failure

    events = result.get("events") or []
    findings = []
    for ev in events:
        event_id = ev.get("id") or ledger.subject_key(ev.get("summary"), ev.get("start"))
        attendees = ev.get("attendees") or []
        # "Worth preparing for" = someone else is in the room.
        if len(attendees) < 1:
            continue
        summary = ev.get("summary") or "(untitled event)"
        findings.append(_finding(
            "calendar_prep", str(event_id),
            f"Prepare for: {summary}",
            "jarvis_executive_analyst",
            f"Upcoming meeting '{summary}' at {ev.get('start')} with "
            f"{len(attendees)} attendee(s). Gather relevant context and open items.",
            {"event_id": event_id, "start": ev.get("start"), "attendee_count": len(attendees)},
        ))
    return {"state": SUCCESS, "findings": findings, "scanned": len(events)}


# ── ITEM 5: GMAIL ────────────────────────────────────────────────────────

def gmail_findings(max_messages: int = 20) -> dict[str, Any]:
    """Inbox messages that need a human-visible action.

    Classification itself is not duplicated here — actions/email_classification.py
    owns the 7-category system and email_intelligence_router already logs
    every category. What was missing is that nothing turned an actionable
    message into a TASK. Messages are identified by Gmail's own message id,
    so a message stays claimed across restarts and re-scans.

    IRRELEVANT and PERSONAL are never turned into work, matching the hard
    rule the router already enforces."""
    from actions import gmail_integration
    from actions import email_classification as ec

    try:
        listing = gmail_integration.list_messages(query="in:inbox", max_results=max_messages)
    except Exception as exc:
        return {"state": classify_failure(exc), "detail": str(exc), "findings": []}

    failure = _integration_failure(listing)
    if failure:
        return failure

    # Which categories become work, and who handles them. BUILDPRO is
    # deliberately absent: buildpro_candidate_intake/_client_intake already
    # own that mail end to end, and a second creator of BuildPro records is
    # exactly the duplicate this system must not have.
    routing = {
        ec.CATEGORY_DDF: "daily_deal_finder_agent",
        ec.CATEGORY_CAREERROCKET: "careerrocket_research_agent",
        ec.CATEGORY_JARVIS: "system_monitor_agent",
        ec.CATEGORY_REVIEW: "jarvis_executive_analyst",
    }

    findings, scanned = [], 0
    for stub in (listing.get("messages") or []):
        msg_id = stub.get("id")
        if not msg_id:
            continue
        try:
            message = gmail_integration.get_message(msg_id)
        except Exception:
            logger.debug("could not fetch message %s", msg_id, exc_info=True)
            continue
        if not message.get("ok", True):
            continue
        scanned += 1
        try:
            verdict = ec.classify_email(message)
        except Exception:
            logger.debug("classification failed for %s", msg_id, exc_info=True)
            continue
        agent_id = routing.get(verdict.get("category"))
        if not agent_id:
            continue
        subject = (message.get("subject") or "(no subject)")[:120]
        findings.append(_finding(
            "gmail_actionable", str(msg_id),
            f"Email needs attention: {subject}",
            agent_id,
            f"Inbox message classified {verdict.get('category')} "
            f"(confidence {verdict.get('confidence')}): {subject}",
            {"message_id": msg_id, "category": verdict.get("category"),
             "confidence": verdict.get("confidence")},
        ))
    return {"state": SUCCESS, "findings": findings, "scanned": scanned}


# ── ITEM 8: HUBSPOT ──────────────────────────────────────────────────────

def hubspot_findings(limit: int = 25) -> dict[str, Any]:
    """Companies in the CRM that look like real recruiting opportunities.

    Uses HubSpot's own record ids as identity, so the same company is never
    worked twice. Nothing is written back to HubSpot here — this only reads
    and proposes."""
    from actions import hubspot_integration as hs

    if not hs.is_configured():
        return {"state": NOT_CONFIGURED, "detail": "HUBSPOT_TOKEN is not set", "findings": []}

    try:
        result = hs.get_companies(limit=limit)
    except Exception as exc:
        return {"state": classify_failure(exc), "detail": str(exc), "findings": []}

    failure = _integration_failure(result)
    if failure:
        return failure

    findings = []
    for company in (result.get("results") or result.get("items") or []):
        cid = company.get("id")
        props = company.get("properties") or {}
        name = props.get("name")
        if not cid or not name:
            continue
        findings.append(_finding(
            "hubspot_opportunity", str(cid),
            f"CRM opportunity: {name}",
            "buildpro_prospecting_agent",
            f"HubSpot company '{name}' has not yet been assessed as a "
            f"recruiting client. Review fit and prepare an approach.",
            {"company_id": cid, "name": name, "domain": props.get("domain")},
        ))
    return {"state": SUCCESS, "findings": findings, "scanned": len(findings)}


# ── ITEMS 10 + 11: DAILY DEAL FINDERS -> SOCIAL ──────────────────────────

def ddf_content_findings(limit: int = 3) -> dict[str, Any]:
    """High-ticket products that deserve social content.

    This is the DDF -> Social handoff that did not exist: DDF could rank and
    select products, and Buffer could publish, but nothing connected the
    two, so a good product never became a post. Selection reuses the real
    ranking (select_daily_high_ticket_picks); the product's own id is the
    identity, so one product produces content exactly once."""
    from actions import daily_deal_finders as ddf

    try:
        picks = ddf.select_daily_high_ticket_picks(limit=limit)
    except Exception as exc:
        return {"state": classify_failure(exc), "detail": str(exc), "findings": []}

    findings = []
    for product in picks or []:
        pid = product.get("id") or product.get("product_id")
        name = product.get("name") or product.get("title")
        if not pid or not name:
            continue
        findings.append(_finding(
            "ddf_content", str(pid),
            f"Create content for: {name}",
            "social_content_agent",
            f"High-ticket DDF pick '{name}' selected for promotion. "
            f"Prepare social content for the supported channels. [product:{pid}]",
            {"product_id": pid, "name": name, "price": product.get("price")},
        ))
    return {"state": SUCCESS, "findings": findings, "scanned": len(picks or [])}


# ── ITEM 9: BUILDPRO RECRUITING ──────────────────────────────────────────

def buildpro_findings(min_score: float = 70.0, limit: int = 10) -> dict[str, Any]:
    """Strong candidate/job matches that are ready for outreach.

    The matching engine is untouched — this reads its output and turns a
    strong match into work. Identity is the (candidate, job) pair, so the
    same pairing never produces a second outreach task no matter how many
    times the matcher re-runs."""
    from actions import buildpro_intelligence

    try:
        data = buildpro_intelligence.generate_morning_report_data()
    except Exception as exc:
        return {"state": classify_failure(exc), "detail": str(exc), "findings": []}

    findings = []
    matches = (data or {}).get("top_matches") or []
    for m in matches[:limit]:
        score = m.get("score") or m.get("match_score") or 0
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        cand = m.get("candidate_name") or m.get("candidate") or m.get("candidate_id")
        job = m.get("job_title") or m.get("job") or m.get("job_id")
        if not cand or not job:
            continue
        findings.append(_finding(
            "buildpro_match", ledger.subject_key(cand, job),
            f"Strong match: {cand} -> {job}",
            "buildpro_candidate_agent",
            f"Candidate '{cand}' matches '{job}' at {score:.0f}. "
            f"Prepare outreach for review.",
            {"candidate": cand, "job": job, "score": score},
        ))
    return {"state": SUCCESS, "findings": findings, "scanned": len(matches)}


# ── ITEM 12: RESEARCH ────────────────────────────────────────────────────

def research_findings(limit: int = 5) -> dict[str, Any]:
    """Ranked opportunities from the existing opportunity engine, turned
    into work rather than left as rows nobody reads."""
    from actions import opportunity_engine

    try:
        opps = opportunity_engine.rank_opportunities(limit=limit)
    except Exception as exc:
        return {"state": classify_failure(exc), "detail": str(exc), "findings": []}

    findings = []
    for opp in opps or []:
        oid = opp.get("id") or ledger.subject_key(opp.get("title"), opp.get("type"))
        title = opp.get("title")
        if not title:
            continue
        findings.append(_finding(
            "research_opportunity", str(oid),
            f"Research: {title}",
            "business_research_agent",
            f"Ranked opportunity '{title}' needs supporting research.",
            {"opportunity_id": oid, "title": title, "score": opp.get("score")},
        ))
    return {"state": SUCCESS, "findings": findings, "scanned": len(opps or [])}


# ── DISPATCH ─────────────────────────────────────────────────────────────

SOURCES: dict[str, Callable[[], dict[str, Any]]] = {
    "calendar": calendar_findings,
    "gmail": gmail_findings,
    "hubspot": hubspot_findings,
    "ddf_content": ddf_content_findings,
    "buildpro": buildpro_findings,
    "research": research_findings,
}


def dispatch_findings(findings: list[dict[str, Any]], orchestrator=None) -> list[dict[str, Any]]:
    """Claims each finding once and creates its task.

    The claim happens BEFORE the task is created, and is released if
    creation fails — so a crash between the two cannot permanently swallow
    a subject. assign_task applies the existing permission model unchanged:
    an EXECUTE-level agent's task lands PENDING_APPROVAL, not RUNNING."""
    if orchestrator is None:
        from actions.agent_orchestrator import orchestrator as _o
        orchestrator = _o

    dispatched = []
    for f in findings:
        if not ledger.claim(f["kind"], f["subject_id"], detail={"title": f["title"]}):
            continue   # already handled on an earlier sweep
        try:
            task = orchestrator.assign_task(f["agent_id"], f["description"])
        except Exception as exc:
            ledger.release(f["kind"], f["subject_id"])
            logger.warning("could not dispatch %s/%s: %s", f["kind"], f["subject_id"], exc)
            continue
        dispatched.append({
            "kind": f["kind"], "subject_id": f["subject_id"], "title": f["title"],
            "agent_id": f["agent_id"], "task_id": task.id, "task_status": task.status.value,
        })
    return dispatched


def gather_and_dispatch(sources: Optional[dict] = None, orchestrator=None) -> dict[str, Any]:
    """One full pass across every business source.

    Each source is isolated: Gmail's auth being broken must not stop HubSpot
    or DDF from producing work, which is the whole point of running a
    portfolio of agents rather than one pipeline."""
    sources = sources if sources is not None else SOURCES
    report: dict[str, Any] = {"sources": {}, "dispatched": [], "states": {}}

    for name, fn in sources.items():
        try:
            result = fn()
        except Exception as exc:
            logger.exception("business source %r raised", name)
            result = {"state": classify_failure(exc), "detail": str(exc), "findings": []}
        state = result.get("state", FAILED)
        report["states"][name] = state
        report["sources"][name] = {
            "state": state,
            "detail": result.get("detail"),
            "found": len(result.get("findings") or []),
            "scanned": result.get("scanned", 0),
        }
        if state == SUCCESS and result.get("findings"):
            try:
                created = dispatch_findings(result["findings"], orchestrator=orchestrator)
            except Exception:
                logger.exception("dispatch failed for source %r", name)
                created = []
            report["sources"][name]["dispatched"] = len(created)
            report["dispatched"].extend(created)

    report["total_dispatched"] = len(report["dispatched"])
    report["healthy_sources"] = sum(1 for s in report["states"].values() if s == SUCCESS)
    return report
