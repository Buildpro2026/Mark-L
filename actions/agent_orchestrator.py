"""Agent Orchestrator — architecture/hooks for the future specialized-agent
workforce (Task 2), extended with a real controlled lifecycle in Task 3.

Long-term model:
    Lee -> Jarvis (AI CEO) -> Agent Orchestrator -> Specialized Agents
         -> Results -> Jarvis -> Lee

Guardrails (do not weaken without explicit instruction):
  * Agents are defined in code (BUILTIN_AGENTS below) — nothing in this
    module lets an agent, or Jarvis, register a NEW agent type at runtime.
    That is the defence against uncontrolled self-replication.
  * Every agent has a PermissionLevel. OBSERVE/SUGGEST-level tasks may run
    immediately (they only read or propose). EXECUTE-level tasks are created
    PENDING_APPROVAL and will not run until approve_task() is called —
    i.e. Lee's authority — see AgentOrchestrator.assign_task/approve_task.
  * Agents cannot assign tasks to other agents or create agents themselves;
    only code calling into AgentOrchestrator (main.py's tool dispatch, i.e.
    Jarvis acting on Lee's behalf) can.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DATA_DIR / "jarvis2.db"
LOCK_PATH = DATA_DIR / "agent_scheduler.lock"

# A lock older than this is treated as abandoned and silently reclaimed,
# regardless of whether its PID is still running — the hard backstop that
# guarantees a stale lock can never PERMANENTLY block scheduling, even if
# PID-liveness checking itself is somehow wrong. Comfortably longer than
# main.py's 5-minute scheduler poll interval and refresh_scheduler_lock()
# cadence, so a genuinely live instance's lock never goes stale by accident.
_STALE_LOCK_SECONDS = 900


class PermissionLevel(str, Enum):
    OBSERVE = "observe"    # agent may only read/monitor — always auto-runs
    SUGGEST = "suggest"    # agent may propose actions for approval — auto-runs, result is a suggestion
    EXECUTE = "execute"    # agent performs real side effects — requires explicit approval before running


_PERMISSION_ORDER = {PermissionLevel.OBSERVE: 0, PermissionLevel.SUGGEST: 1, PermissionLevel.EXECUTE: 2}


def _parse_schedule_minutes(schedule: Optional[str]) -> Optional[float]:
    """'60m' -> 60.0, '2h' -> 120.0, '90' -> 90.0. None/unparseable -> None
    (no schedule — on-demand only)."""
    if not schedule:
        return None
    s = schedule.strip().lower()
    try:
        if s.endswith("m"):
            return float(s[:-1])
        if s.endswith("h"):
            return float(s[:-1]) * 60
        return float(s)
    except ValueError:
        return None


class AgentStatus(str, Enum):
    REGISTERED = "registered"   # known to the orchestrator, not started
    IDLE = "idle"                # started, waiting for work
    RUNNING = "running"          # actively working a task
    STOPPED = "stopped"
    ERROR = "error"


class TaskStatus(str, Enum):
    PENDING = "pending"                   # queued, will auto-run (OBSERVE/SUGGEST)
    PENDING_APPROVAL = "pending_approval"  # queued, needs Lee's approval (EXECUTE)
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"                 # agent stopped, or approval denied


@dataclass
class AgentDefinition:
    """A registered agent blueprint. New blueprints are added to
    BUILTIN_AGENTS in code, not created dynamically — see module guardrails.

    Fields cover identity (id/name/description="purpose"), permissions,
    schedule, status, owner, and business association, per the Phase 5 spec.
    Tasks/results/event history live in the orchestrator (list_tasks/
    get_results/list_events, all filterable by agent_id) rather than on the
    definition itself, so a single AgentDefinition stays a lightweight
    blueprint even as its task history grows.
    """
    id: str
    name: str
    description: str                       # purpose
    nucleus_id: str                        # which Nucleus this agent serves, e.g. "buildpro"
    business: str = "general"              # buildpro | careerrocket | ddf | general
    owner: str = "Lee"                     # accountable human
    permission_level: PermissionLevel = PermissionLevel.OBSERVE
    status: AgentStatus = AgentStatus.REGISTERED
    schedule: Optional[str] = None         # e.g. "60m" — see Phase 7's scheduler; None = on-demand only
    last_run_ts: Optional[float] = None
    last_error: Optional[str] = None       # error state — set by run_task on failure, cleared on success
    handler: Optional[Callable[["AgentTask"], dict]] = field(default=None, repr=False)

    def to_public_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "handler"}
        return d


@dataclass
class AgentTask:
    id: str
    agent_id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)
    result: Any = None
    error: Optional[str] = None

    def to_public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, TaskStatus) else self.status
        return d


@dataclass
class AgentEvent:
    agent_id: str
    kind: str              # "status_change" | "task_assigned" | "task_done" | "task_failed" | "log"
    message: str
    ts: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Persistence — restart recovery for agent status/task/event history ─────
# Before this, AgentOrchestrator was purely in-memory: every agent's status
# (including whether Lee had start_agent()'d it — the ONLY gate that makes
# scheduled automation auto-run) reset to REGISTERED on every JARVIS
# restart, silently stopping all scheduled agents with no indication to the
# user. Every mutator (start_agent/stop_agent/run_task/assign_task/
# approve_task/reject_task/_log_event) now writes through to data/jarvis2.db
# immediately; __init__ restores state from it. All writes are best-effort —
# a persistence failure degrades to in-memory-only behavior (today's
# pre-existing behavior) rather than blocking a real operation.

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            agent_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_run_ts REAL,
            last_error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL,
            result_json TEXT,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            kind TEXT,
            message TEXT,
            ts REAL NOT NULL
        )
    """)
    return conn


def _save_agent_state(agent: "AgentDefinition") -> None:
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO agent_state (agent_id, status, last_run_ts, last_error) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET status=excluded.status, "
                "last_run_ts=excluded.last_run_ts, last_error=excluded.last_error",
                (agent.id, agent.status.value, agent.last_run_ts, agent.last_error),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _load_agent_state() -> dict[str, dict[str, Any]]:
    try:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM agent_state").fetchall()
            return {r["agent_id"]: dict(r) for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}


def _save_task(task: "AgentTask") -> None:
    try:
        status = task.status.value if isinstance(task.status, TaskStatus) else task.status
        result_json = None
        if task.result is not None:
            try:
                result_json = json.dumps(task.result)
            except Exception:
                result_json = json.dumps(str(task.result))
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO agent_tasks (id, agent_id, description, status, created_ts, updated_ts, result_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_ts=excluded.updated_ts, "
                "result_json=excluded.result_json, error=excluded.error",
                (task.id, task.agent_id, task.description, status,
                 task.created_ts, task.updated_ts, result_json, task.error),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _load_tasks() -> dict[str, "AgentTask"]:
    try:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM agent_tasks ORDER BY created_ts").fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    tasks: dict[str, AgentTask] = {}
    for r in rows:
        try:
            result = json.loads(r["result_json"]) if r["result_json"] else None
        except Exception:
            result = None
        try:
            status = TaskStatus(r["status"])
        except ValueError:
            status = TaskStatus.FAILED
        tasks[r["id"]] = AgentTask(
            id=r["id"], agent_id=r["agent_id"], description=r["description"] or "",
            status=status, created_ts=r["created_ts"], updated_ts=r["updated_ts"],
            result=result, error=r["error"],
        )
    return tasks


def _save_event(event: "AgentEvent") -> None:
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO agent_events (agent_id, kind, message, ts) VALUES (?, ?, ?, ?)",
                (event.agent_id, event.kind, event.message, event.ts),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _load_events(limit: int = 500) -> list["AgentEvent"]:
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM agent_events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        AgentEvent(agent_id=r["agent_id"], kind=r["kind"], message=r["message"], ts=r["ts"])
        for r in reversed(rows)
    ]


def _buildpro_email_monitor_handler(task: "AgentTask") -> dict:
    """J3: wired to the real Gmail integration (actions/buildpro_email_monitor.py)
    now that Gmail is a real, OAuth-authorized capability (J2) — this used
    to unconditionally report "not configured" even after that stopped
    being true. Read-only by default (list + classify + log to business
    intelligence); never drafts or sends from a scheduled/unattended run
    — see buildpro_email_monitor.scan_inbox()'s draft_replies flag, left
    False here on purpose (SUGGEST-level agents auto-run, and an
    unattended run is exactly the case that should stay conservative).
    Honestly reports NOT_AUTHORIZED/ERROR rather than fabricating a scan
    if Gmail isn't currently authorized."""
    from actions import google_auth
    if not google_auth.get_credential_status().get("authorized"):
        return {
            "summary": "Gmail isn't authorized yet — nothing to scan. Run the one-time Google sign-in to enable this agent.",
            "configured": False,
        }
    from actions.buildpro_email_monitor import scan_inbox
    result = scan_inbox(query="is:unread", max_results=15, draft_replies=False)
    if not result["ok"]:
        return {"summary": f"Gmail scan failed ({result.get('state')}): {result.get('detail')}", "configured": True, "error": result.get("detail")}
    return {"summary": result["summary"], "configured": True, "relevant": result["relevant"]}


# ── Agent handlers built on real, already-existing infrastructure ────────
# Each one uses a genuinely working backend where one exists (web_search,
# daily_deal_finders, twilio_integration, strategic_objective, business_
# intelligence, opportunity_engine, system_monitor, buffer_integration) and
# honestly reports "no data source connected yet" where none does — never
# fabricated results.

def _research_handler(business: str, category: str = "research"):
    def _handler(task: "AgentTask") -> dict:
        from actions.web_search import _research as web_research
        from actions.business_intelligence import add_entry
        query = task.description
        findings = web_research(query)
        entry_id = add_entry(category, business, title=query[:120], content=findings)
        return {"summary": findings[:400], "logged_entry_id": entry_id}
    return _handler


def _opportunity_scout_handler(task: "AgentTask") -> dict:
    from actions.web_search import _search as web_search_fn
    from actions.opportunity_engine import add_opportunity
    query = task.description
    findings = web_search_fn(f"monetization opportunity: {query}")
    result = add_opportunity(
        "general", "quick_cash", title=query[:120], description=findings[:500],
    )
    return {
        "summary": f"Logged as proposed opportunity #{result['id']} (score {result['score']}/100) — needs Lee's review.",
        "opportunity_id": result["id"],
        "findings": findings[:400],
    }


def _daily_deal_finder_agent_handler(task: "AgentTask") -> dict:
    from actions.daily_deal_finders import get_top_products
    products = get_top_products(limit=5)
    return {"summary": f"{len(products)} top product(s) currently tracked.", "top_products": products}


def _communications_agent_handler(task: "AgentTask") -> dict:
    from actions.twilio_integration import get_status, get_missed_calls
    status = get_status()
    missed = get_missed_calls(limit=5)
    return {"summary": f"Twilio: {status['state']}. {len(missed)} missed call(s).", "status": status, "missed_calls": missed}


def _revenue_tracking_agent_handler(task: "AgentTask") -> dict:
    from actions.strategic_objective import get_objective_status
    from actions.business_intelligence import summary as bi_summary
    obj = get_objective_status()
    bi = bi_summary()
    return {
        "summary": (
            f"${obj['cumulative_revenue_usd']:,.0f} of ${obj['target_amount_usd']:,.0f} "
            f"({obj['progress_pct']}%) toward the objective."
        ),
        "objective": obj,
        "business_intelligence": bi,
    }


def _system_monitor_agent_handler(task: "AgentTask") -> dict:
    from actions.system_monitor import get_system_status
    status = get_system_status()
    return {"summary": "System status collected.", "status": status}


def _social_content_agent_handler(task: "AgentTask") -> dict:
    from actions.buffer_integration import verify_buffer
    status = verify_buffer()
    return {
        "summary": f"Buffer: {status['status']}." + ("" if status["configured"] else " Not configured — no posts can be scheduled yet."),
        "status": status,
    }


def _executive_analyst_handler(task: "AgentTask") -> dict:
    """Synthesizes objective progress + top-ranked opportunities + recent
    business-intelligence activity into one executive brief — reusing the
    other real data sources rather than reasoning independently (that
    reasoning is Gemini's job in the live conversation; this just assembles
    the inputs)."""
    from actions.strategic_objective import get_objective_status
    from actions.opportunity_engine import rank_opportunities
    from actions.business_intelligence import summary as bi_summary
    obj = get_objective_status()
    top_quick = rank_opportunities(opp_type="quick_cash", limit=3)
    top_long = rank_opportunities(opp_type="long_term", limit=3)
    bi = bi_summary()
    return {
        "summary": (
            f"Objective: ${obj['cumulative_revenue_usd']:,.0f}/${obj['target_amount_usd']:,.0f} "
            f"({obj['progress_pct']}%). Top quick-cash: "
            + (top_quick[0]["title"] if top_quick else "none logged") +
            ". Top long-term: " + (top_long[0]["title"] if top_long else "none logged") + "."
        ),
        "objective": obj,
        "top_quick_cash": top_quick,
        "top_long_term": top_long,
        "business_intelligence": bi,
    }


def _no_data_source_handler(reason: str):
    def _handler(task: "AgentTask") -> dict:
        return {"summary": reason, "configured": False}
    return _handler


def _buildpro_candidate_agent_handler(task: "AgentTask") -> dict:
    """Queries the real BuildPro candidate database (actions/buildpro_data.py)
    — never invents candidates. task.description is treated as an optional
    free-text hint: if it looks like a skill/location/role filter it's used
    as a substring match via find_candidates(); scheduled/generic
    descriptions (e.g. "Scheduled background check") fall back to a full
    overview instead of matching nothing."""
    from actions.buildpro_data import list_candidates, find_candidates, list_candidates_needing_followup

    query = (task.description or "").strip()
    generic_descriptions = {"", "scheduled background check"}
    if query.lower() in generic_descriptions:
        candidates = list_candidates(limit=50)
        filtered = False
    else:
        candidates = find_candidates(skills=query, limit=50)
        if not candidates:
            candidates = find_candidates(location=query, limit=50)
        if not candidates:
            candidates = find_candidates(title=query, limit=50)
        filtered = True

    followups = list_candidates_needing_followup()
    return {
        "summary": (
            f"{len(candidates)} candidate(s) {'matching ' + repr(query) if filtered else 'on file'}; "
            f"{len(followups)} need follow-up."
        ),
        "candidates": candidates,
        "needs_followup": followups,
        "filtered_by": query if filtered else None,
    }


def _buildpro_prospecting_agent_handler(task: "AgentTask") -> dict:
    """Identifies potential BuildPro clients from HubSpot company data
    (actions/hubspot_integration.py) that aren't already tracked locally,
    and records them as buildpro_clients with status='prospect' (a local
    write only — no outreach, no HubSpot writes; see module docstring on
    why OBSERVE-level agents already do local writes elsewhere, e.g.
    business_research_agent's add_entry calls). "Hiring activity" isn't a
    signal HubSpot's standard company properties expose, so this honestly
    reports that gap instead of fabricating it; industry is used as the
    one real, available signal for "potential construction client."
    """
    from actions import hubspot_integration as hubspot
    from actions import buildpro_data as bd

    if not hubspot.is_configured():
        return {
            "summary": "HubSpot isn't configured — no prospect data source available.",
            "configured": False, "prospects": [],
        }

    result = hubspot.get_companies(limit=100)
    if not result["ok"]:
        return {
            "summary": f"Could not reach HubSpot: {result.get('detail')}",
            "configured": True, "prospects": [], "error": result.get("detail"),
        }

    _CONSTRUCTION_HINTS = ("construction", "building", "contractor", "engineering", "architecture")
    new_prospects = []
    for company in result["results"]:
        company_id = company.get("id")
        props = company.get("properties") or {}
        if bd.get_client_by_hubspot_id(company_id):
            continue  # already tracked locally — not a new prospect
        name = (props.get("name") or props.get("domain") or "").strip()
        if not name:
            continue
        industry = (props.get("industry") or "").strip()
        likely_construction = any(hint in industry.lower() for hint in _CONSTRUCTION_HINTS)
        client_id = bd.add_client(
            name=name, industry=industry, phone=(props.get("phone") or "").strip(),
            source="hubspot", hubspot_company_id=company_id, status="prospect",
        )
        new_prospects.append({
            "client_id": client_id, "name": name, "industry": industry or None,
            "likely_construction_related": likely_construction,
        })

    followups = bd.list_clients_needing_followup()
    return {
        "summary": (
            f"{len(new_prospects)} new prospect(s) identified from HubSpot companies; "
            f"{len(followups)} existing prospect/client(s) need follow-up. "
            "Note: HubSpot's standard company data doesn't include hiring-activity signals — "
            "'industry' is the only real signal available for flagging construction-related prospects."
        ),
        "configured": True,
        "prospects": new_prospects,
        "needs_followup": followups,
    }


# Fixed, code-level set of agent blueprints — the ONLY way new agents enter
# the system. See module guardrails above. Every handler either uses real,
# already-built infrastructure (web_search, daily_deal_finders, twilio,
# strategic_objective, business_intelligence, opportunity_engine,
# system_monitor, buffer_integration) or honestly reports that no data
# source is connected yet — never a fabricated result.
BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    "buildpro_email_monitor": AgentDefinition(
        id="buildpro_email_monitor", name="BuildPro Email Monitor",
        description="Watches the BuildPro inbox for new candidate/client messages and drafts triage suggestions.",
        nucleus_id="buildpro", business="buildpro",
        permission_level=PermissionLevel.SUGGEST, schedule="60m",
        handler=_buildpro_email_monitor_handler,
    ),
    "business_research_agent": AgentDefinition(
        id="business_research_agent", name="Business Research Agent",
        description="Researches a given topic via web search and files findings to business intelligence.",
        nucleus_id="system", business="general",
        permission_level=PermissionLevel.OBSERVE, schedule=None,
        handler=_research_handler("general", "research"),
    ),
    "opportunity_scout": AgentDefinition(
        id="opportunity_scout", name="Opportunity Scout",
        description="Searches for monetization opportunities matching a brief and logs them as proposed opportunities for review.",
        nucleus_id="ddf", business="general",
        permission_level=PermissionLevel.SUGGEST, schedule=None,
        handler=_opportunity_scout_handler,
    ),
    "buildpro_prospecting_agent": AgentDefinition(
        id="buildpro_prospecting_agent", name="BuildPro Prospecting Agent",
        description="Identifies new client/company prospects for BuildPro recruiting from HubSpot company data.",
        nucleus_id="buildpro", business="buildpro",
        permission_level=PermissionLevel.OBSERVE, schedule=None,
        handler=_buildpro_prospecting_agent_handler,
    ),
    "buildpro_candidate_agent": AgentDefinition(
        id="buildpro_candidate_agent", name="BuildPro Candidate Agent",
        description="Queries and tracks candidates in the BuildPro pipeline by skill, location, role, and status.",
        nucleus_id="buildpro", business="buildpro",
        permission_level=PermissionLevel.OBSERVE, schedule=None,
        handler=_buildpro_candidate_agent_handler,
    ),
    "careerrocket_research_agent": AgentDefinition(
        id="careerrocket_research_agent", name="CareerRocket Research Agent",
        description="Researches CareerRocket Pro market/product topics via web search and files findings.",
        nucleus_id="careerrocket", business="careerrocket",
        permission_level=PermissionLevel.OBSERVE, schedule=None,
        handler=_research_handler("careerrocket", "research"),
    ),
    "daily_deal_finder_agent": AgentDefinition(
        id="daily_deal_finder_agent", name="Daily Deal Finder Agent",
        description="Reports the current top-tracked deal products.",
        nucleus_id="ddf", business="ddf",
        permission_level=PermissionLevel.OBSERVE, schedule="120m",
        handler=_daily_deal_finder_agent_handler,
    ),
    "social_content_agent": AgentDefinition(
        id="social_content_agent", name="Social Content Agent",
        description="Checks Buffer connectivity for scheduled social content — does not post without EXECUTE approval.",
        nucleus_id="ddf", business="general",
        permission_level=PermissionLevel.OBSERVE, schedule=None,
        handler=_social_content_agent_handler,
    ),
    "market_intelligence_agent": AgentDefinition(
        id="market_intelligence_agent", name="Market Intelligence Agent",
        description="Scans the web for competitor/market signals and files them to business intelligence.",
        nucleus_id="system", business="general",
        permission_level=PermissionLevel.OBSERVE, schedule=None,
        handler=_research_handler("general", "market_observations"),
    ),
    "communications_agent": AgentDefinition(
        id="communications_agent", name="Communications Agent",
        description="Reports Twilio connectivity and missed-call status.",
        nucleus_id="communications", business="general",
        permission_level=PermissionLevel.OBSERVE, schedule="30m",
        handler=_communications_agent_handler,
    ),
    "revenue_tracking_agent": AgentDefinition(
        id="revenue_tracking_agent", name="Revenue Tracking Agent",
        description="Reports progress toward the $1,000,000 strategic objective and business-intelligence revenue totals.",
        nucleus_id="system", business="general",
        permission_level=PermissionLevel.OBSERVE, schedule="1440m",
        handler=_revenue_tracking_agent_handler,
    ),
    "system_monitor_agent": AgentDefinition(
        id="system_monitor_agent", name="System Monitor",
        description="Reports live CPU/RAM/GPU/uptime system status.",
        nucleus_id="system", business="general",
        permission_level=PermissionLevel.OBSERVE, schedule="60m",
        handler=_system_monitor_agent_handler,
    ),
    "jarvis_executive_analyst": AgentDefinition(
        id="jarvis_executive_analyst", name="Jarvis Executive Analyst",
        description="Synthesizes objective progress, top-ranked opportunities, and business intelligence into one executive brief.",
        nucleus_id="system", business="general",
        permission_level=PermissionLevel.OBSERVE, schedule="1440m",
        handler=_executive_analyst_handler,
    ),
}


# ── Single-instance scheduler lock ──────────────────────────────────────
# Guards against two JARVIS processes (e.g. launched twice by accident)
# both independently deciding the same due agent should run and running
# it twice. File-based rather than in-process, since the risk is across
# OS processes, not threads within one. See main.py::_run_agent_scheduler
# for how this is acquired/refreshed/released around the actual poll loop.

def _pid_is_running(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        # Can't check — fail open (treat as not running) rather than let an
        # unverifiable liveness check permanently block the scheduler.
        return False


def acquire_scheduler_lock() -> bool:
    """True if this process now holds the lock (either it was free, or the
    existing lock was stale and got reclaimed). False if another live
    process genuinely holds it. Never raises — a filesystem error while
    reading/writing the lock fails OPEN (returns True) so a local disk
    hiccup can't permanently stop scheduled agents from running."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if LOCK_PATH.exists():
        try:
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            age = now - float(data.get("acquired_ts", 0))
            pid = data.get("pid")
            if age < _STALE_LOCK_SECONDS and pid and pid != os.getpid() and _pid_is_running(int(pid)):
                return False
        except Exception:
            pass   # unreadable/corrupt lock file — treat as stale, reclaim below
    try:
        LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "acquired_ts": now}), encoding="utf-8")
        return True
    except Exception:
        return True


def refresh_scheduler_lock() -> None:
    """Call periodically while holding the lock so its timestamp never
    ages past _STALE_LOCK_SECONDS while this process is genuinely still
    alive and scheduling — best-effort, never raises."""
    try:
        LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "acquired_ts": time.time()}), encoding="utf-8")
    except Exception:
        pass


def release_scheduler_lock() -> None:
    """Only removes the lock if this process is the one that holds it —
    never raises."""
    try:
        if LOCK_PATH.exists():
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                LOCK_PATH.unlink()
    except Exception:
        pass


class AgentOrchestrator:
    """Controlled registry + lifecycle for specialized agents.

    Covers: registration (code-defined only — see module guardrails), status,
    start/stop, task queueing + permission-gated execution, agent-generated
    events, and results storage.

    Permission policy (OBSERVE -> SUGGEST -> EXECUTE), enforced in
    assign_task/run_task/approve_task:
      * OBSERVE / SUGGEST agents: assign_task runs the task immediately.
        Neither level performs real side effects by definition, so no
        approval gate is needed to stay safe.
      * EXECUTE agents: assign_task leaves the task PENDING_APPROVAL: it will
        NOT run until approve_task() is called. That call is the seam where
        Lee's authority lives — main.py's tool dispatch never calls
        approve_task on its own initiative from a bare "do X" request; see
        the agent_orchestrator tool's description for the exact policy.
    """

    def __init__(self, agents: Optional[dict[str, AgentDefinition]] = None):
        # copy.copy each AgentDefinition (not just the dict) — otherwise every
        # AgentOrchestrator instance built from BUILTIN_AGENTS would share the
        # same mutable objects, so start_agent()/run_task() on one instance
        # would silently corrupt status on every other instance too.
        source = agents if agents is not None else BUILTIN_AGENTS
        self._agents: dict[str, AgentDefinition] = {aid: copy.copy(a) for aid, a in source.items()}

        # Restore persisted status/last_run_ts/last_error so a restart
        # doesn't silently stop scheduled automation — see module comment
        # above _connect(). Only applies to agents that already exist in
        # this instance's agent set; a stale row for a since-removed
        # agent_id is simply ignored.
        persisted = _load_agent_state()
        for agent_id, agent in self._agents.items():
            state = persisted.get(agent_id)
            if not state:
                continue
            try:
                agent.status = AgentStatus(state["status"])
            except ValueError:
                pass
            agent.last_run_ts = state["last_run_ts"]
            agent.last_error = state["last_error"]

        self._tasks: dict[str, AgentTask] = _load_tasks()
        self._events: list[AgentEvent] = _load_events()

    # ── registration / status (hooks) ───────────────────────────────────
    def list_agents(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def get_agent(self, agent_id: str) -> Optional[AgentDefinition]:
        return self._agents.get(agent_id)

    def get_agent_status(self, agent_id: str) -> Optional[AgentStatus]:
        agent = self._agents.get(agent_id)
        return agent.status if agent else None

    # ── start/stop (hooks) ──────────────────────────────────────────────
    def start_agent(self, agent_id: str) -> AgentDefinition:
        agent = self._require_agent(agent_id)
        agent.status = AgentStatus.IDLE
        _save_agent_state(agent)
        self._log_event(agent_id, "status_change", f"{agent.name} started (idle).")
        return agent

    def stop_agent(self, agent_id: str) -> AgentDefinition:
        agent = self._require_agent(agent_id)
        agent.status = AgentStatus.STOPPED
        _save_agent_state(agent)
        self._log_event(agent_id, "status_change", f"{agent.name} stopped.")
        return agent

    # ── task assignment + execution ─────────────────────────────────────
    def assign_task(self, agent_id: str, description: str) -> AgentTask:
        """Queue a task for `agent_id`. OBSERVE/SUGGEST tasks run immediately
        (see class docstring); EXECUTE tasks stay PENDING_APPROVAL until
        approve_task() is called."""
        agent = self._require_agent(agent_id)
        status = (
            TaskStatus.PENDING_APPROVAL
            if agent.permission_level == PermissionLevel.EXECUTE
            else TaskStatus.PENDING
        )
        task = AgentTask(id=str(uuid.uuid4()), agent_id=agent_id, description=description, status=status)
        self._tasks[task.id] = task
        _save_task(task)
        self._log_event(agent_id, "task_assigned", f"Task assigned to {agent.name}: {description}")

        if status == TaskStatus.PENDING:
            self.run_task(task.id)
        return task

    def approve_task(self, task_id: str) -> AgentTask:
        """Lee's approval gate for EXECUTE-level tasks. Only this call may
        move a PENDING_APPROVAL task forward — see module/class guardrails."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.PENDING_APPROVAL:
            return task
        task.status = TaskStatus.PENDING
        task.updated_ts = time.time()
        _save_task(task)
        self._log_event(task.agent_id, "log", f"Task {task_id} approved — running.")
        return self.run_task(task_id)

    def reject_task(self, task_id: str) -> AgentTask:
        task = self._require_task(task_id)
        task.status = TaskStatus.REJECTED
        task.updated_ts = time.time()
        _save_task(task)
        self._log_event(task.agent_id, "log", f"Task {task_id} rejected.")
        return task

    def run_task(self, task_id: str) -> AgentTask:
        """Executes a PENDING task by calling its agent's handler. Refuses to
        run a PENDING_APPROVAL task directly — only approve_task() may do that."""
        task = self._require_task(task_id)
        agent = self._require_agent(task.agent_id)
        if task.status == TaskStatus.PENDING_APPROVAL:
            raise PermissionError(
                f"Task {task_id} requires approval (agent '{agent.name}' is EXECUTE-level)."
            )

        agent.status = AgentStatus.RUNNING
        task.status = TaskStatus.RUNNING
        task.updated_ts = time.time()
        try:
            result = agent.handler(task) if agent.handler else {"summary": "No handler configured."}
            task.result = result
            task.status = TaskStatus.DONE
            agent.last_error = None
            self._log_event(agent.id, "task_done", f"{agent.name} completed task {task_id}.")
        except Exception as exc:
            task.error = str(exc)
            task.status = TaskStatus.FAILED
            agent.last_error = str(exc)
            self._log_event(agent.id, "task_failed", f"{agent.name} failed task {task_id}: {exc}")
        finally:
            task.updated_ts = time.time()
            agent.last_run_ts = task.updated_ts
            agent.status = AgentStatus.IDLE
            _save_task(task)
            _save_agent_state(agent)
        return task

    # ── results / events (hooks, real) ──────────────────────────────────
    def get_task(self, task_id: str) -> Optional[AgentTask]:
        return self._tasks.get(task_id)

    def list_tasks(self, agent_id: Optional[str] = None) -> list[AgentTask]:
        tasks = list(self._tasks.values())
        return [t for t in tasks if t.agent_id == agent_id] if agent_id else tasks

    def get_results(self, agent_id: str) -> list[dict[str, Any]]:
        return [t.result for t in self._tasks.values() if t.agent_id == agent_id and t.status == TaskStatus.DONE and t.result is not None]

    def list_events(self, agent_id: Optional[str] = None, limit: int = 50) -> list[AgentEvent]:
        events = [e for e in self._events if agent_id is None or e.agent_id == agent_id]
        return events[-limit:]

    def report_event(self, agent_id: str, message: str, kind: str = "log") -> AgentEvent:
        """Public hook for an agent (or its handler) to report something
        happened, independent of the task lifecycle's automatic events."""
        self._require_agent(agent_id)
        return self._log_event(agent_id, kind, message)

    def summary(self) -> dict[str, Any]:
        """Compact status snapshot — consumed by dashboard/server.py's system
        module data so the 3D command center can surface agent status without
        the frontend needing to know orchestrator internals."""
        return {
            "agents": [a.to_public_dict() for a in self.list_agents()],
            "task_count": len(self._tasks),
            "pending_approval_count": sum(1 for t in self._tasks.values() if t.status == TaskStatus.PENDING_APPROVAL),
            "recent_events": [e.to_public_dict() for e in self.list_events(limit=10)],
        }

    # ── background autonomy (Phase 7) ───────────────────────────────────
    # Safety gates, both required for an agent to ever auto-run:
    #   1. status must be IDLE — i.e. Lee/Jarvis explicitly called
    #      start_agent() at some point. A freshly-registered or stopped
    #      agent NEVER auto-runs just because it has a `schedule`.
    #   2. permission_level must not be EXECUTE — scheduled background
    #      runs never bypass Lee's explicit approve_task() gate, regardless
    #      of schedule. Only OBSERVE/SUGGEST agents (no real side effects
    #      by definition) can run unattended.
    def get_due_agents(self, now: Optional[float] = None) -> list[AgentDefinition]:
        now = now if now is not None else time.time()
        due = []
        for agent in self._agents.values():
            if agent.status != AgentStatus.IDLE:
                continue
            if agent.permission_level == PermissionLevel.EXECUTE:
                continue
            minutes = _parse_schedule_minutes(agent.schedule)
            if minutes is None:
                continue
            if agent.last_run_ts is None or (now - agent.last_run_ts) >= minutes * 60:
                due.append(agent)
        return due

    def run_due_agents(self, task_description: str = "Scheduled background check") -> list[AgentTask]:
        """Assigns (and, since these are all OBSERVE/SUGGEST, immediately
        runs) a default task for every currently-due agent. Call
        periodically from a background loop — see
        main.py::_run_agent_scheduler."""
        return [self.assign_task(agent.id, task_description) for agent in self.get_due_agents()]

    # ── internal ─────────────────────────────────────────────────────────
    def _require_agent(self, agent_id: str) -> AgentDefinition:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"Unknown agent: {agent_id}")
        return agent

    def _require_task(self, task_id: str) -> AgentTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        return task

    def _log_event(self, agent_id: str, kind: str, message: str) -> AgentEvent:
        event = AgentEvent(agent_id=agent_id, kind=kind, message=message)
        self._events.append(event)
        _save_event(event)
        return event


# Module-level singleton — mirrors the pattern used by voice_manager/
# nucleus_hierarchy (shared state accessed via plain functions/one instance).
orchestrator = AgentOrchestrator()
