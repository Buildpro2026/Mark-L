"""Browser UI backend surface for jarvis-headless-core.

Serves a same-origin, session-cookie-authenticated JSON API for the static
single-page app in ui_static/index.html. Exists because the deployed
headless service had no browser-usable frontend at all — only /health and
a bearer-token JSON API meant for programmatic callers.

Security model: the browser NEVER sees JARVIS_API_TOKEN. The user pastes
it once into the login form, POST /ui/login checks it server-side against
config.API_TOKEN (the same constant-time check as auth.require_auth) and,
on success, issues a random opaque session id as an httpOnly/Secure
cookie. Every other /ui/api/* route requires that cookie, not the token
itself — so the token never touches page source, browser storage, or JS
memory beyond the single login POST body.

Every /ui/api/* handler below is a thin pass-through to the same
functions tools_api.py / orchestrator_api.py / status_api.py already
expose over the bearer-token API — no business logic is duplicated or
reimplemented, so the UI and the programmatic API can never drift apart.
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.headless import config
from core.headless.auth import check_token
from core.headless import orchestrator_api
from core.headless import status_api
from core.headless import tools_api
from core.headless.orchestrator_api import CreateTaskRequest
from core.headless.tools_api import ExecuteToolRequest

STATIC_DIR = Path(__file__).parent / "ui_static"
INDEX_FILE = STATIC_DIR / "index.html"

COOKIE_NAME = "jarvis_ui_session"
SESSION_TTL_SECONDS = 12 * 3600

# In-memory session store — fine for a single-instance deployment (see
# render.yaml: Free plan, one instance). A restart clears everyone's
# session, same as it clears everything else on this ephemeral host.
_sessions: dict[str, float] = {}


def _new_session() -> str:
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = time.time() + SESSION_TTL_SECONDS
    return sid


def _session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    expiry = _sessions.get(sid)
    if expiry is None:
        return False
    if expiry < time.time():
        _sessions.pop(sid, None)
        return False
    return True


def require_ui_session(jarvis_ui_session: str | None = Cookie(default=None)) -> None:
    if not _session_valid(jarvis_ui_session):
        raise HTTPException(status_code=401, detail="Not logged in")


router = APIRouter(prefix="/ui")


@router.get("/session")
def get_session(jarvis_ui_session: str | None = Cookie(default=None)) -> dict:
    return {"authenticated": _session_valid(jarvis_ui_session)}


class LoginRequest(BaseModel):
    token: str


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if not config.API_TOKEN:
        raise HTTPException(status_code=503, detail="JARVIS_API_TOKEN is not configured on this server.")
    if not check_token(body.token):
        raise HTTPException(status_code=401, detail="Incorrect token")
    sid = _new_session()
    response.set_cookie(
        COOKIE_NAME, sid,
        max_age=SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="lax",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response, jarvis_ui_session: str | None = Cookie(default=None)):
    if jarvis_ui_session:
        _sessions.pop(jarvis_ui_session, None)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


# ── Authenticated proxy routes — every one below requires the session cookie ──
api = APIRouter(prefix="/ui/api", dependencies=[Depends(require_ui_session)])


@api.get("/tools")
def ui_list_tools():
    return tools_api.list_tools()


@api.post("/tools/execute")
async def ui_execute_tool(body: ExecuteToolRequest):
    return await tools_api.execute_tool(body)


@api.get("/status")
def ui_status():
    return status_api.status()


@api.get("/activity")
def ui_activity(limit: int = 50):
    return status_api.activity(limit=limit)


@api.get("/brief")
def ui_brief():
    return status_api.brief()


@api.get("/agents")
def ui_list_agents():
    return orchestrator_api.list_agents()


@api.post("/agents/{agent_id}/start")
def ui_start_agent(agent_id: str):
    return orchestrator_api.start_agent(agent_id)


@api.post("/agents/{agent_id}/stop")
def ui_stop_agent(agent_id: str):
    return orchestrator_api.stop_agent(agent_id)


@api.post("/tasks", status_code=201)
def ui_create_task(body: CreateTaskRequest):
    return orchestrator_api.create_task(body)


@api.get("/tasks/{task_id}")
def ui_get_task(task_id: str):
    return orchestrator_api.get_task(task_id)


@api.get("/tasks/{task_id}/result")
def ui_get_task_result(task_id: str):
    return orchestrator_api.get_task_result(task_id)


@api.post("/tasks/{task_id}/approve")
def ui_approve_task(task_id: str):
    return orchestrator_api.approve_task(task_id)


@api.post("/tasks/{task_id}/reject")
def ui_reject_task(task_id: str):
    return orchestrator_api.reject_task(task_id)


@api.post("/tasks/{task_id}/execute")
def ui_execute_task(task_id: str):
    return orchestrator_api.execute_task(task_id)


@api.get("/events")
def ui_list_events(agent_id: str | None = None, limit: int = 50):
    return orchestrator_api.list_events(agent_id=agent_id, limit=limit)


def serve_index() -> FileResponse:
    """Root-path handler — app.py wires GET / to this. Returning the SPA
    at the bare public URL is the whole point: before this, opening
    https://jarvis-headless-core.onrender.com/ in a browser 404'd because
    no route existed there at all."""
    return FileResponse(INDEX_FILE, media_type="text/html")
