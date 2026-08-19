"""Browser UI backend surface for jarvis-headless-core.

Serves a same-origin, session-cookie-authenticated JSON API for the static
single-page app in ui_static/index.html. Exists because the deployed
headless service had no browser-usable frontend at all — only /health and
a bearer-token JSON API meant for programmatic callers.

Security model: the browser NEVER sees JARVIS_API_TOKEN. The user pastes
it once into the login form, POST /ui/login checks it server-side against
config.API_TOKEN (the same constant-time check as auth.require_auth) and,
on success, issues a session cookie — not the token itself, and not an
id into any server-side store (see "Session persistence" below) — so the
token never touches page source, browser storage, or JS memory beyond
the single login POST body.

Session persistence: the cookie is a signed, stateless token
(base64(expiry) + "." + base64(HMAC-SHA256(expiry))), verified by
recomputing the HMAC with a key derived from config.API_TOKEN — never
stored anywhere server-side. This is deliberate: Render's Free plan has
no persistent disk (render.yaml's own J4 decision), so anything kept in
a dict or a file is wiped on every restart/redeploy/sleep-wake cycle —
which is exactly what broke the previous in-memory `_sessions` dict,
forcing re-login after every spin-down. A signature check needs no
storage to validate, only the (already-persistent, Render-env-var-backed)
API token, so it survives restarts the same way the token itself does.
Logout can't fully revoke a stateless token without server-side state,
so it does the best available thing: clears the browser's cookie AND
adds the token to a small in-memory revocation set for the remaining
life of THIS process (closes the "already-logged-out tab keeps working"
gap for as long as the process stays up; a restart clears the
revocation set exactly like it clears everything else here, but by then
the cookie is already gone from the browser that logged out, so this
only matters for a captured/replayed cookie value — an inherent limit
of stateless tokens on disk-less infrastructure, not a new one).

Every /ui/api/* handler below is a thin pass-through to the same
functions tools_api.py / orchestrator_api.py / status_api.py already
expose over the bearer-token API — no business logic is duplicated or
reimplemented, so the UI and the programmatic API can never drift apart.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from pathlib import Path
from typing import Awaitable, Callable

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

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "core" / "prompt.txt"
CHAT_MODEL = "gemini-flash-latest"   # same model main.py's own text-only Gemini calls use (see _save_session_summary)
_MAX_TOOL_CALL_ROUNDS = 4            # caps a runaway tool-call chain, not normal conversation length

STATIC_DIR = Path(__file__).parent / "ui_static"
INDEX_FILE = STATIC_DIR / "index.html"

COOKIE_NAME = "jarvis_ui_session"
SESSION_TTL_SECONDS = 12 * 3600

# Best-effort, process-lifetime-only revocation set for logout (see
# "Session persistence" above) — {token: expiry}. Not a session store:
# validity is decided by the HMAC signature, this only ever *removes*
# trust from an otherwise-valid token early. Fine for it to reset on
# restart; that's not a regression, the cookie that would need it is
# gone from the browser by then too.
_revoked: dict[str, float] = {}


def _prune_revoked() -> None:
    now = time.time()
    for tok in [t for t, exp in _revoked.items() if exp < now]:
        _revoked.pop(tok, None)


def _sign_key() -> bytes:
    # Derived, not the raw token itself — key separation, same pattern as
    # dashboard/server.py's _derive_key (SHA-256 of secret + purpose tag).
    return hashlib.sha256((config.API_TOKEN or "").encode("utf-8") + b"jarvis-ui-session-v1").digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _new_session() -> str:
    """Returns a signed, self-verifying session token — nothing is stored
    server-side for this call, on purpose (see module docstring)."""
    exp_bytes = str(int(time.time()) + SESSION_TTL_SECONDS).encode("ascii")
    sig = hmac.new(_sign_key(), exp_bytes, hashlib.sha256).digest()
    return f"{_b64(exp_bytes)}.{_b64(sig)}"


def _session_valid(token: str | None) -> bool:
    if not token or not config.API_TOKEN or "." not in token:
        return False
    exp_part, sig_part = token.split(".", 1)
    try:
        exp_bytes = _b64d(exp_part)
        sig = _b64d(sig_part)
        expiry = int(exp_bytes.decode("ascii"))
    except Exception:
        return False
    expected_sig = hmac.new(_sign_key(), exp_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    if expiry < time.time():
        return False
    _prune_revoked()
    return token not in _revoked


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
        # Best-effort revocation for the rest of this process's uptime —
        # see module docstring's "Session persistence" section for why a
        # stateless token can't be revoked more durably than that.
        _revoked[jarvis_ui_session] = time.time() + SESSION_TTL_SECONDS
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


class SettingsUpdateRequest(BaseModel):
    updates: dict


@api.get("/priorities")
def ui_priorities(limit: int = 8):
    return status_api.priorities(limit=limit)


@api.get("/active-agents")
def ui_active_agents():
    return status_api.active_agents()


@api.get("/opportunities")
def ui_opportunities(limit: int = 8):
    return status_api.opportunities(limit=limit)


@api.get("/calendar-snapshot")
def ui_calendar_snapshot(max_results: int = 10):
    return status_api.calendar_snapshot(max_results=max_results)


@api.get("/buildpro-overview")
def ui_buildpro_overview():
    return status_api.buildpro_overview()


@api.get("/ddf-overview")
def ui_ddf_overview():
    return status_api.ddf_overview()


@api.get("/intelligence")
def ui_intelligence():
    return status_api.intelligence()


@api.get("/settings")
def ui_get_settings():
    from core.headless import personalization
    return personalization.get_all_settings()


@api.post("/settings")
def ui_update_settings(body: SettingsUpdateRequest):
    from core.headless import personalization
    try:
        return personalization.update_settings(body.updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []   # [{"role": "user"|"model", "text": "..."}] — kept client-side, not persisted


def _chat_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, a concise and direct assistant. "
            "Always use the provided tools to complete tasks — never simulate or guess results."
        )


def _chat_tool_declarations() -> list[dict]:
    from core.headless.tool_registry import TOOL_DECLARATIONS, SESSION_ONLY_TOOLS
    # SESSION_ONLY_TOOLS (screen_process, close_camera, shutdown_jarvis,
    # navigate_command_center) need a live desktop/voice session — excluded
    # from the declared set entirely so Gemini never tries to call one here,
    # rather than declaring them and only failing when invoked.
    return [t for t in TOOL_DECLARATIONS if t["name"] not in SESSION_ONLY_TOOLS]


def _status_label_for_tool(name: str, args: dict) -> str:
    """Human-readable, present-tense status line for a tool about to run —
    what a real UI shows instead of leaving the user staring at nothing
    while a tool call is in flight. Deliberately only describes what's
    ACTUALLY about to happen (the tool being called), never a fabricated
    or generic 'working on it' — see the Phase 3 UX requirement this
    exists for."""
    labels = {
        "web_search": "Researching...",
        "gmail": "Checking email...",
        "calendar": "Checking your calendar...",
        "hubspot": "Checking HubSpot...",
        "airtable": "Checking Airtable...",
        "social_post": "Checking Buffer...",
        "communications": "Checking Twilio...",
        "browser_control": "Working in the browser...",
        "buildpro_matching": "Checking BuildPro records...",
        "daily_deal_finders": "Checking Daily Deal Finders...",
        "agent_orchestrator": "Checking on agents...",
        "business_intelligence": "Checking business intelligence...",
        "opportunity_engine": "Checking opportunities...",
        "strategic_objective": "Checking the revenue objective...",
        "ceo_decision": "Reviewing the decision log...",
        "cloud_status": "Checking the cloud instance...",
        "file_processor": "Processing the file...",
        "code_helper": "Working on the code...",
        "dev_agent": "Building the project...",
        "system_status": "Checking system status...",
        "save_memory": "Saving that...",
    }
    return labels.get(name, "Waiting on external service...")


async def run_chat_turn(
    message: str, history: list[dict],
    on_status: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[dict]]:
    """One conversational turn through Gemini + the shared ToolExecutor —
    the browser equivalent of what Gemini Live's function-calling already
    does for the desktop voice loop (main.py), using the SAME tool
    declarations and the SAME ToolExecutor, just over a plain (non-Live)
    Gemini call since neither a browser chat tab nor a headless cloud
    process can hold a Live audio session the way main.py's Gemini Live
    client does. Nothing about tool dispatch or the underlying agents/
    business logic is reimplemented here. Shared by /ui/api/chat (the
    generic web UI) and core.headless.dashboard_bridge (the original
    JARVIS phone/3D command-center UI's typed-command relay) so both
    surfaces run the exact same conversational path.

    Raises HTTPException on a missing GEMINI_API_KEY, an empty message, or
    a Gemini request failure — callers that don't want that (e.g. the
    dashboard bridge, which just wants a reply string) should catch it."""
    if not config.GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured on this server.")
    if not message.strip():
        raise HTTPException(status_code=400, detail="Empty message.")

    from google.genai import types as gtypes
    from core.headless.context import ToolContext
    from core.headless.tool_executor import ToolExecutor, UnknownToolError
    from core.headless.gemini_client import get_client

    client = get_client(config.GEMINI_API_KEY)
    tools = [gtypes.Tool(function_declarations=_chat_tool_declarations())]
    gen_config = gtypes.GenerateContentConfig(system_instruction=_chat_system_prompt(), tools=tools)

    contents: list = []
    for turn in history[-20:]:
        role = "model" if turn.get("role") == "model" else "user"
        text = str(turn.get("text") or "")
        if text:
            contents.append(gtypes.Content(role=role, parts=[gtypes.Part(text=text)]))
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part(text=message)]))

    executor = ToolExecutor(ToolContext())
    tool_calls_made: list[dict] = []
    loop = asyncio.get_event_loop()

    for _ in range(_MAX_TOOL_CALL_ROUNDS):
        try:
            resp = await loop.run_in_executor(
                None, lambda: client.models.generate_content(model=CHAT_MODEL, contents=contents, config=gen_config)
            )
        except Exception as e:
            # Gemini's own API returns a clean, user-safe error message
            # (e.g. "high demand" 503s) — safe to surface directly, this
            # is never a secret-bearing string.
            raise HTTPException(status_code=502, detail=f"Gemini request failed: {e}")

        if not resp.function_calls:
            return resp.text or "", tool_calls_made

        contents.append(resp.candidates[0].content)
        for fc in resp.function_calls:
            args = dict(fc.args or {})
            if on_status is not None:
                try:
                    await on_status(_status_label_for_tool(fc.name, args))
                except Exception:
                    pass   # a broken status sink must never break the actual turn
            try:
                result = await executor.execute(fc.name, args)
            except UnknownToolError as e:
                result = f"Error: {e}"
            except Exception as e:
                result = f"Error: {fc.name} failed — {e}"
            tool_calls_made.append({"name": fc.name, "args": args, "result": result})
            contents.append(gtypes.Content(
                role="user",
                parts=[gtypes.Part(function_response=gtypes.FunctionResponse(name=fc.name, response={"result": result}))],
            ))

        if on_status is not None and resp.function_calls:
            try:
                await on_status("Analyzing results...")
            except Exception:
                pass

    return (
        "I ran several tool calls but didn't reach a final answer — try rephrasing or breaking the request into smaller steps.",
        tool_calls_made,
    )


@api.post("/chat")
async def chat(body: ChatRequest):
    reply, tool_calls = await run_chat_turn(body.message, body.history)
    return {"reply": reply, "tool_calls": tool_calls}


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


@router.get("")
@router.get("/")
def serve_index() -> FileResponse:
    """Serves the SPA shell at GET /ui (and /ui/ — browsers routinely hit
    both depending on how the link was typed/clicked).

    Phase 3 fix: this function existed, and its own docstring claimed
    "app.py wires GET / to this," but nothing actually decorated it as a
    route — /ui returned a bare 404. That's the literal reason this page
    was "a fallback surface nothing currently links to": the route to
    reach it didn't exist. GET / itself intentionally stays owned by
    dashboard/server.py's phone/3D command-center UI (see app.py's mount
    comment) — this page lives at /ui specifically, not at the bare
    public URL."""
    return FileResponse(INDEX_FILE, media_type="text/html")
