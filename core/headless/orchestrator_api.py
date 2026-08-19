"""Agent Orchestrator HTTP surface — create/inspect/approve/execute/results/
events, all behind require_auth. Wraps actions/agent_orchestrator.py's
existing AgentOrchestrator singleton; the state machine, permission levels,
and approval gate all live there unchanged (see that module's own
docstring/guardrails) — this file only translates HTTP <-> its real
methods. It does not weaken or bypass approve_task/run_task's existing
PermissionError guard for PENDING_APPROVAL tasks.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from actions.agent_orchestrator import orchestrator
from core.headless.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


class CreateTaskRequest(BaseModel):
    agent_id: str
    description: str = "Manual task"


@router.get("/agents")
def list_agents():
    return {"agents": [a.to_public_dict() for a in orchestrator.list_agents()]}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    agent = orchestrator.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    return agent.to_public_dict()


@router.post("/agents/{agent_id}/start")
def start_agent(agent_id: str):
    try:
        agent = orchestrator.start_agent(agent_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return agent.to_public_dict()


@router.post("/agents/{agent_id}/stop")
def stop_agent(agent_id: str):
    try:
        agent = orchestrator.stop_agent(agent_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return agent.to_public_dict()


@router.post("/tasks", status_code=201)
def create_task(body: CreateTaskRequest):
    """Assigns a task to an agent. OBSERVE/SUGGEST agents run it immediately
    (orchestrator.assign_task's existing behavior); EXECUTE agents come
    back PENDING_APPROVAL and will not run until POST /tasks/{id}/approve —
    same gate the voice tool uses, not a separate/weaker one."""
    try:
        task = orchestrator.assign_task(body.agent_id, body.description)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return task.to_public_dict()


@router.get("/tasks")
def list_tasks(agent_id: str | None = None, limit: int = 200):
    """Every task the orchestrator knows about, newest first — the org
    chart's "what are they working on" view needs the real task
    descriptions, not just the coarse running/idle status get_agent_status
    already exposes."""
    tasks = orchestrator.list_tasks(agent_id=agent_id)
    tasks.sort(key=lambda t: t.updated_ts, reverse=True)
    return {"tasks": [t.to_public_dict() for t in tasks[:limit]]}


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    task = orchestrator.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown task: {task_id}")
    return task.to_public_dict()


@router.get("/tasks/{task_id}/result")
def get_task_result(task_id: str):
    task = orchestrator.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown task: {task_id}")
    return {"status": task.status.value, "result": task.result, "error": task.error}


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: str):
    try:
        task = orchestrator.approve_task(task_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return task.to_public_dict()


@router.post("/tasks/{task_id}/reject")
def reject_task(task_id: str):
    try:
        task = orchestrator.reject_task(task_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return task.to_public_dict()


@router.post("/tasks/{task_id}/execute")
def execute_task(task_id: str):
    """Manually (re-)runs a task that's already PENDING (e.g. one created
    out of band). Still refuses a PENDING_APPROVAL task — run_task() itself
    raises PermissionError for that, translated to 403 here, not swallowed."""
    try:
        task = orchestrator.run_task(task_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return task.to_public_dict()


@router.get("/events")
def list_events(agent_id: str | None = None, limit: int = 50):
    return {"events": [e.to_public_dict() for e in orchestrator.list_events(agent_id, limit)]}


@router.get("/summary")
def summary():
    return orchestrator.summary()
